"""Tests for Linux systemd --user setup in install.sh.

All cases run install.sh with INSTALL_TEST_MODE=1, FAKE_UNAME=Linux, and
sandbox HOME / XDG_CONFIG_HOME so we don't touch the real systemd bus or
user filesystem.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
INSTALL_SH = ROOT / "install.sh"
SYSTEMD_DIR = ROOT / "systemd"


# ---------- helpers ----------


def _run_install(
    home: Path,
    *args: str,
    fake_uname: str = "Linux",
    extra_env: dict | None = None,
    fake_systemctl: Path | None = None,
):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["INSTALL_TEST_MODE"] = "1"
    env["FAKE_UNAME"] = fake_uname
    env["TAM_MEMORY_DIR"] = str(home / ".tam")
    env.pop("CLAUDE_MEMORY_DIR", None)
    env["XDG_CONFIG_HOME"] = str(home / ".config")

    if fake_systemctl is not None:
        # Prepend fake bin dir so our stub shadows any real systemctl.
        env["PATH"] = f"{fake_systemctl.parent}:{env.get('PATH','')}"

    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _write_systemctl_stub(bin_dir: Path, log_path: Path, show_env_exit: int = 0) -> Path:
    """Write a fake systemctl that logs invocations.

    show_env_exit=0 → `systemctl --user show-environment` succeeds → bus is
    considered "available" (install.sh will try to enable units).
    show_env_exit=1 → bus is "not available" → graceful skip path.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log_path}"\n'
        '# detect the availability probe\n'
        'if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then\n'
        f'  exit {show_env_exit}\n'
        'fi\n'
        'exit 0\n'
    )
    stub.chmod(0o755)
    return stub


@pytest.fixture
def sandbox_home(tmp_path: Path) -> Path:
    home = tmp_path / "sandbox-home"
    home.mkdir()
    yield home
    shutil.rmtree(home, ignore_errors=True)


# ---------- unit-file templates exist (precondition) ----------


def test_all_systemd_templates_present():
    expected = [
        "claude-memory-reflection.service",
        "claude-memory-reflection.path",
        "claude-memory-dashboard.service",
        "claude-memory-orphan-backfill.service",
        "claude-memory-orphan-backfill.timer",
        "claude-memory-check-updates.service",
        "claude-memory-check-updates.timer",
    ]
    for name in expected:
        assert (SYSTEMD_DIR / name).exists(), f"missing template: {name}"


def test_templates_use_placeholders():
    for path in SYSTEMD_DIR.glob("*.service"):
        text = path.read_text()
        # Service units must have ExecStart. Reflection and orphan-backfill
        # etc. reference the venv; dashboard references dashboard.py.
        assert "ExecStart=" in text, f"{path.name}: no ExecStart"
        assert "@INSTALL_DIR@" in text or "@MEMORY_DIR@" in text, (
            f"{path.name}: no placeholders — won't substitute at install time"
        )


# ---------- linux branch detection ----------


def test_linux_branch_creates_systemd_user_files(sandbox_home: Path, tmp_path: Path):
    # systemctl stub where bus is "available" so enable runs too
    systemctl_log = tmp_path / "systemctl.log"
    fake_bin = tmp_path / "bin"
    stub = _write_systemctl_stub(fake_bin, systemctl_log, show_env_exit=0)

    result = _run_install(sandbox_home, "--ide", "claude-code", fake_systemctl=stub)
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"

    target = sandbox_home / ".config" / "systemd" / "user"
    assert target.is_dir(), "systemd --user target dir must be created"

    expected_files = {
        "claude-memory-reflection.service",
        "claude-memory-reflection.path",
        "claude-memory-dashboard.service",
        "claude-memory-orphan-backfill.service",
        "claude-memory-orphan-backfill.timer",
        "claude-memory-check-updates.service",
        "claude-memory-check-updates.timer",
    }
    actual = {p.name for p in target.glob("claude-memory-*")}
    missing = expected_files - actual
    assert not missing, f"missing units in {target}: {missing}"


def test_linux_templates_are_substituted(sandbox_home: Path, tmp_path: Path):
    stub = _write_systemctl_stub(tmp_path / "bin", tmp_path / "systemctl.log", show_env_exit=0)
    result = _run_install(sandbox_home, "--ide", "claude-code", fake_systemctl=stub)
    assert result.returncode == 0, result.stderr

    target = sandbox_home / ".config" / "systemd" / "user"
    dashboard = (target / "claude-memory-dashboard.service").read_text()

    assert "@INSTALL_DIR@" not in dashboard, "placeholder leaked into installed unit"
    assert "@MEMORY_DIR@" not in dashboard
    assert str(ROOT) in dashboard, "INSTALL_DIR must be substituted with repo path"
    assert str(sandbox_home / ".tam") in dashboard, "MEMORY_DIR must be substituted to ~/.tam"


def test_linux_enables_units_when_bus_available(sandbox_home: Path, tmp_path: Path):
    systemctl_log = tmp_path / "systemctl.log"
    stub = _write_systemctl_stub(tmp_path / "bin", systemctl_log, show_env_exit=0)

    result = _run_install(sandbox_home, "--ide", "claude-code", fake_systemctl=stub)
    assert result.returncode == 0, result.stderr

    calls = systemctl_log.read_text() if systemctl_log.exists() else ""
    assert "--user daemon-reload" in calls, "daemon-reload must be called"
    assert "--user enable --now claude-memory-reflection.path" in calls
    assert "--user enable --now claude-memory-dashboard.service" in calls
    assert "--user enable --now claude-memory-orphan-backfill.timer" in calls
    assert "--user enable --now claude-memory-check-updates.timer" in calls


# ---------- WSL2 detection + graceful fallback ----------


def test_graceful_fallback_when_systemd_bus_unavailable(sandbox_home: Path, tmp_path: Path):
    # show-environment exits 1 → bus unavailable → install.sh should WARN
    # but still drop the files and not crash.
    systemctl_log = tmp_path / "systemctl.log"
    stub = _write_systemctl_stub(tmp_path / "bin", systemctl_log, show_env_exit=1)

    result = _run_install(sandbox_home, "--ide", "claude-code", fake_systemctl=stub)
    assert result.returncode == 0, f"install.sh must not crash: {result.stderr}"

    target = sandbox_home / ".config" / "systemd" / "user"
    assert (target / "claude-memory-reflection.service").exists(), (
        "units must be staged even when activation is skipped"
    )

    combined = result.stdout + result.stderr
    assert "WARN" in combined, "user must see a warning about systemd being unavailable"


def test_no_systemctl_available_still_stages_files(sandbox_home: Path, tmp_path: Path):
    # Stub systemctl that ALWAYS returns non-zero for show-environment AND
    # otherwise acts as if present but non-functional (simulates minimal
    # container/CI where systemctl exists but the user bus is down).
    systemctl_log = tmp_path / "systemctl.log"
    stub = _write_systemctl_stub(tmp_path / "bin", systemctl_log, show_env_exit=1)

    result = _run_install(sandbox_home, "--ide", "claude-code", fake_systemctl=stub)

    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
    target = sandbox_home / ".config" / "systemd" / "user"
    # Files must be staged even when activation can't happen.
    assert (target / "claude-memory-dashboard.service").exists()
    assert (target / "claude-memory-reflection.path").exists()

    # And install.sh must NOT have called `enable --now` when the bus is down.
    calls = systemctl_log.read_text() if systemctl_log.exists() else ""
    assert "enable --now" not in calls, (
        "install.sh must skip systemctl enable when bus is unavailable"
    )


# ---------- hooks copy parity on Linux ----------


def test_linux_hooks_and_mcp_config_created_same_as_macos(sandbox_home: Path, tmp_path: Path):
    stub = _write_systemctl_stub(tmp_path / "bin", tmp_path / "systemctl.log", show_env_exit=0)
    result = _run_install(sandbox_home, "--ide", "claude-code", fake_systemctl=stub)
    assert result.returncode == 0, result.stderr

    settings = sandbox_home / ".claude" / "settings.json"
    assert settings.exists(), "claude-code settings.json must be created on Linux too"

    import json
    data = json.loads(settings.read_text())
    # MCP server registered
    assert "memory" in data["mcpServers"]
    # Hooks registered — same set as macOS
    assert "hooks" in data
    assert "SessionStart" in data["hooks"]
    assert "SessionEnd" in data["hooks"]
    assert "Stop" in data["hooks"]
    assert "PostToolUse" in data["hooks"]


# ---------- uninstall path ----------


def test_uninstall_on_linux_removes_systemd_files(sandbox_home: Path, tmp_path: Path):
    # First install → then uninstall → files must be gone.
    systemctl_log = tmp_path / "systemctl.log"
    stub = _write_systemctl_stub(tmp_path / "bin", systemctl_log, show_env_exit=0)

    install_res = _run_install(sandbox_home, "--ide", "claude-code", fake_systemctl=stub)
    assert install_res.returncode == 0, install_res.stderr

    target = sandbox_home / ".config" / "systemd" / "user"
    assert (target / "claude-memory-dashboard.service").exists()

    uninstall_res = _run_install(sandbox_home, "--uninstall", fake_systemctl=stub)
    assert uninstall_res.returncode == 0, uninstall_res.stderr

    remaining = list(target.glob("claude-memory-*"))
    assert not remaining, f"expected no claude-memory-* units, found: {remaining}"

    calls = systemctl_log.read_text()
    assert "--user disable --now claude-memory-reflection.path" in calls
    assert "--user disable --now claude-memory-dashboard.service" in calls
    assert "--user disable --now claude-memory-orphan-backfill.timer" in calls
    assert "--user disable --now claude-memory-check-updates.timer" in calls


def test_uninstall_on_darwin_does_not_touch_systemd(sandbox_home: Path, tmp_path: Path):
    # Sanity: on macOS the Linux branch must NOT run.
    # We don't care if real launchctl exists — just that install.sh exits 0.
    uninstall_res = _run_install(sandbox_home, "--uninstall", fake_uname="Darwin")
    assert uninstall_res.returncode == 0, uninstall_res.stderr
    target = sandbox_home / ".config" / "systemd" / "user"
    # target dir should not have claude-memory files (it may not even exist)
    if target.exists():
        assert not list(target.glob("claude-memory-*"))


# ---------- fake_uname shields Linux-specific tests on macOS hosts ----------


def test_fake_uname_linux_activates_linux_branch_on_darwin_host(sandbox_home: Path, tmp_path: Path):
    # Even when the real host is Darwin, FAKE_UNAME=Linux must route install.sh
    # through the Linux branch (this is the whole point of the override).
    stub = _write_systemctl_stub(tmp_path / "bin", tmp_path / "systemctl.log", show_env_exit=0)
    result = _run_install(
        sandbox_home,
        "--ide", "claude-code",
        fake_uname="Linux",
        fake_systemctl=stub,
    )
    assert result.returncode == 0, result.stderr
    # Linux-specific artefact
    assert (sandbox_home / ".config" / "systemd" / "user" / "claude-memory-reflection.path").exists()
    # Darwin-specific artefact must NOT be created
    assert not (sandbox_home / "Library" / "LaunchAgents").exists() or \
           not list((sandbox_home / "Library" / "LaunchAgents").glob("com.claude.memory*.plist")), (
        "LaunchAgents must not be installed when FAKE_UNAME=Linux"
    )
