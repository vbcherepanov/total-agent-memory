from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "dashboard-service.sh"


def _free_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    except PermissionError as exc:  # pragma: no cover - sandbox-dependent
        raise RuntimeError("socket creation not permitted in this environment") from exc


def _static_port(seed: int) -> int:
    return 38000 + seed


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_helper(
    *args: str,
    env: dict[str, str],
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(SCRIPT), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=True,
    )


def _base_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "CTM_UNAME": "Linux",
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_systemctl_stub(bin_dir: Path, log_path: Path, show_env_exit: int = 0) -> None:
    _write_executable(
        bin_dir / "systemctl",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log_path}"
if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then
  exit {show_env_exit}
fi
exit 0
""",
    )


def _write_launchctl_stub(bin_dir: Path, log_path: Path) -> None:
    _write_executable(
        bin_dir / "launchctl",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log_path}"
exit 0
""",
    )


def _write_noop_python_wrapper(wrapper_path: Path, log_path: Path, probe_exit: int = 1) -> None:
    _write_executable(
        wrapper_path,
        f"""#!/usr/bin/env bash
if [ "${{1:-}}" = "-" ]; then
  exit {probe_exit}
fi
printf '%s\\n' "$@" >> "{log_path}"
exit 0
""",
    )


def _write_exec_python_wrapper(wrapper_path: Path, pid_path: Path) -> None:
    _write_executable(
        wrapper_path,
        f"""#!/usr/bin/env bash
if [ "${{1:-}}" = "-" ]; then
  exec "{sys.executable}" "$@"
fi
echo $$ > "{pid_path}"
exec "{sys.executable}" "$@"
""",
    )


def _poll_status(port: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    url = f"http://127.0.0.1:{port}/status"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # pragma: no cover - diagnostic only
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"dashboard never became ready on port {port}: {last_error}")


def test_fallback_install_is_idempotent_and_writes_profile_hook(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    wrapper_log = tmp_path / "wrapper.log"
    wrapper = bin_dir / "python-wrapper"
    _write_systemctl_stub(bin_dir, tmp_path / "systemctl.log", show_env_exit=1)
    _write_noop_python_wrapper(wrapper, wrapper_log, probe_exit=1)

    env = _base_env(tmp_path)
    env["DASHBOARD_PORT"] = str(_static_port(11))

    _run_helper("install", str(wrapper), str(ROOT), str(memory_dir), env=env)
    _run_helper("install", str(wrapper), str(ROOT), str(memory_dir), env=env)

    profile = _read(tmp_path / "home" / ".profile")
    autostart = memory_dir / "dashboard-autostart.sh"

    assert profile.count("# Claude Total Memory dashboard auto-start") == 1
    assert str(autostart) in profile
    assert autostart.exists()
    assert os.access(autostart, os.X_OK)
    autostart_text = _read(autostart)
    assert f'DASHBOARD_PORT="{env["DASHBOARD_PORT"]}"' in autostart_text
    assert f'CLAUDE_MEMORY_DIR="{memory_dir}"' in autostart_text


def test_fallback_install_starts_dashboard_and_serves_status(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    pid_path = tmp_path / "dashboard.pid"
    wrapper = bin_dir / "python-wrapper"
    _write_systemctl_stub(bin_dir, tmp_path / "systemctl.log", show_env_exit=1)
    _write_exec_python_wrapper(wrapper, pid_path)

    env = _base_env(tmp_path)
    try:
        port = _free_port()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    env["DASHBOARD_PORT"] = str(port)

    try:
        _run_helper("install", str(wrapper), str(ROOT), str(memory_dir), env=env)
        status = _poll_status(port)
        assert status["status"] == "running"
        assert status["port"] == port

        log_path = memory_dir / "logs" / "dashboard.log"
        deadline = time.time() + 5
        while time.time() < deadline and not log_path.exists():
            time.sleep(0.1)
        assert log_path.exists()
    finally:
        if pid_path.exists():
            os.kill(int(pid_path.read_text(encoding="utf-8").strip()), signal.SIGTERM)


def test_fallback_install_does_not_start_second_dashboard_when_port_in_use(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    wrapper_log = tmp_path / "wrapper.log"
    wrapper = bin_dir / "python-wrapper"
    _write_systemctl_stub(bin_dir, tmp_path / "systemctl.log", show_env_exit=1)
    _write_noop_python_wrapper(wrapper, wrapper_log, probe_exit=0)

    env = _base_env(tmp_path)
    port = _static_port(12)
    env["DASHBOARD_PORT"] = str(port)

    _run_helper("install", str(wrapper), str(ROOT), str(memory_dir), env=env)

    assert not wrapper_log.exists()


def test_systemd_install_writes_unit_and_management_output(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    _write_systemctl_stub(bin_dir, systemctl_log, show_env_exit=0)

    env = _base_env(tmp_path)
    port = _static_port(13)
    env["DASHBOARD_PORT"] = str(port)

    result = _run_helper("install", sys.executable, str(ROOT), str(memory_dir), env=env)
    unit_path = tmp_path / "home" / ".config" / "systemd" / "user" / "claude-total-memory-dashboard.service"
    unit_text = _read(unit_path)

    assert "Dashboard service installed with systemd --user" in result.stdout
    assert f"Environment=DASHBOARD_PORT={port}" in unit_text
    assert f"Environment=CLAUDE_MEMORY_DIR={memory_dir}" in unit_text
    assert f"ExecStart={sys.executable} {ROOT / 'src' / 'dashboard.py'}" in unit_text

    calls = _read(systemctl_log)
    assert "--user show-environment" in calls
    assert "--user daemon-reload" in calls
    assert "--user enable --now claude-total-memory-dashboard.service" in calls

    mgmt = _run_helper("print-management", env=env).stdout
    assert "systemctl --user status claude-total-memory-dashboard" in mgmt


def test_darwin_install_writes_plist_and_management_output(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    _write_launchctl_stub(bin_dir, launchctl_log)

    env = _base_env(tmp_path)
    env["CTM_UNAME"] = "Darwin"
    port = _static_port(14)
    env["DASHBOARD_PORT"] = str(port)

    result = _run_helper("install", sys.executable, str(ROOT), str(memory_dir), env=env)
    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / "com.claude-total-memory.dashboard.plist"
    plist_text = _read(plist_path)

    assert "Dashboard service installed (auto-starts on login)" in result.stdout
    assert f"<string>{port}</string>" in plist_text
    assert f"<string>{sys.executable}</string>" in plist_text
    assert f"<string>{ROOT / 'src' / 'dashboard.py'}</string>" in plist_text

    calls = _read(launchctl_log)
    assert "bootout" in calls
    assert "bootstrap" in calls

    mgmt = _run_helper("print-management", env=env).stdout
    assert "launchctl bootstrap" in mgmt
