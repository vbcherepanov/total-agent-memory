"""Tests for P0.2: unified install.sh with --ide <IDE> flag.

All cases run install.sh in INSTALL_TEST_MODE=1 with a sandbox HOME so we
don't hit the real filesystem, pip, launchctl, or systemctl.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
INSTALL_SH = ROOT / "install.sh"
INSTALL_CODEX_SH = ROOT / "install-codex.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="install.sh is the POSIX installer; Windows uses install.ps1 (tests/test_windows_installer_runtime.py)",
)


def _add_fake_systemctl(env: dict[str, str], home: Path) -> None:
    fake_bin = home / ".test-bin"
    fake_bin.mkdir(exist_ok=True)
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then\n'
        "  exit 1\n"
        "fi\n"
        "exit 1\n", encoding="utf-8"
    )
    systemctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"


def _run_install(home: Path, *args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["INSTALL_TEST_MODE"] = "1"
    # Pin memory dir inside sandbox; keep inherited PATH so python3 (3.11+) is found.
    env["CLAUDE_MEMORY_DIR"] = str(home / ".claude-memory")
    env["TAM_MEMORY_DIR"] = env["CLAUDE_MEMORY_DIR"]
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    _add_fake_systemctl(env, home)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        # Generous — this runs the real installer; a loaded machine must not
        # turn into a red test. Nothing here measures speed.
        timeout=900,
    )


@pytest.fixture
def sandbox_home(tmp_path: Path) -> Path:
    home = tmp_path / "sandbox-home"
    home.mkdir()
    yield home
    # pytest cleans tmp_path automatically
    shutil.rmtree(home, ignore_errors=True)


# ---------- claude-code (default, no flag) ----------

def test_no_flag_defaults_to_claude_code(sandbox_home: Path):
    result = _run_install(sandbox_home)
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"

    # Claude Code reads MCP servers from ~/.claude.json; hooks live in ~/.claude/settings.json.
    registered = json.loads((sandbox_home / ".claude.json").read_text(encoding="utf-8"))
    assert registered["mcpServers"]["memory"]["args"][0].endswith("server.py")
    settings = sandbox_home / ".claude" / "settings.json"
    assert settings.exists(), "claude-code settings.json must be created by default"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "mcpServers" not in data
    # Hooks registered only for claude-code
    assert "hooks" in data
    assert "SessionStart" in data["hooks"]
    assert "PostToolUse" in data["hooks"]


def test_explicit_ide_claude_code(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "claude-code")
    assert result.returncode == 0, result.stderr

    data = json.loads((sandbox_home / ".claude.json").read_text(encoding="utf-8"))
    assert "memory" in data["mcpServers"]


# ---------- cursor ----------

def test_ide_cursor_writes_cursor_mcp_json(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "cursor")
    assert result.returncode == 0, result.stderr

    cfg = sandbox_home / ".cursor" / "mcp.json"
    assert cfg.exists(), "cursor mcp.json must be created"

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "memory" in data["mcpServers"]
    entry = data["mcpServers"]["memory"]
    assert "command" in entry
    assert entry["args"][0].endswith("server.py")
    assert entry["env"]["TAM_MEMORY_DIR"]
    # EMBEDDING_MODEL named the sentence-transformers fallback model, which the
    # base install stopped shipping in 13.0.2. Writing it into an IDE config
    # pins a model the server cannot load; the fastembed default applies.
    assert "EMBEDDING_MODEL" not in entry["env"]


def test_ide_cursor_equals_form(sandbox_home: Path):
    # --ide=cursor should work same as --ide cursor
    result = _run_install(sandbox_home, "--ide=cursor")
    assert result.returncode == 0, result.stderr
    cfg = sandbox_home / ".cursor" / "mcp.json"
    assert cfg.exists()


# ---------- gemini-cli ----------

def test_ide_gemini_cli_writes_gemini_settings(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "gemini-cli")
    assert result.returncode == 0, result.stderr

    cfg = sandbox_home / ".gemini" / "settings.json"
    assert cfg.exists()

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "memory" in data["mcpServers"]


# ---------- opencode ----------

def test_ide_opencode_writes_opencode_config(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "opencode")
    assert result.returncode == 0, result.stderr

    # OpenCode reads $XDG_CONFIG_HOME/opencode/opencode.json, with `mcp` entries of type local.
    cfg = sandbox_home / ".config" / "opencode" / "opencode.json"
    assert cfg.exists()
    entry = json.loads(cfg.read_text(encoding="utf-8"))["mcp"]["memory"]
    assert entry["type"] == "local" and entry["enabled"] is True
    assert entry["command"][1].endswith("server.py")
    assert not (sandbox_home / ".opencode" / "config.json").exists()


# ---------- codex ----------

def test_ide_codex_writes_codex_toml_with_env_overrides(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "codex")
    assert result.returncode == 0, result.stderr

    cfg = sandbox_home / ".codex" / "config.toml"
    assert cfg.exists()

    content = cfg.read_text(encoding="utf-8")
    assert "[mcp_servers.memory]" in content
    assert "[mcp_servers.memory.env]" in content
    # PR #5 env overrides must be present
    assert f'TAM_MEMORY_DIR = "{sandbox_home / ".claude-memory"}"' in content
    assert f'CLAUDE_MEMORY_DIR = "{sandbox_home / ".claude-memory"}"' in content
    assert 'MEMORY_MODE = "fast"' in content
    assert 'MEMORY_TRIPLE_TIMEOUT_SEC = "120"' in content
    assert 'MEMORY_ENRICH_TIMEOUT_SEC = "90"' in content
    assert 'MEMORY_REPR_TIMEOUT_SEC = "120"' in content
    assert 'MEMORY_TRIPLE_MAX_PREDICT = "512"' in content
    # Fence markers for idempotent replace
    assert "# --- total-agent-memory MCP Server ---" in content
    assert "# --- End total-agent-memory ---" in content


def test_ide_codex_install_claims_no_dashboard_service_hooks_or_agents_file(sandbox_home: Path):
    """The dashboard service and the hooks belong to the Claude Code install."""
    result = _run_install(sandbox_home, "--ide", "codex")
    assert result.returncode == 0, result.stderr

    assert "Web dashboard (not started by this install" in result.stdout
    assert "Dashboard management" not in result.stdout
    assert "launchctl" not in result.stdout
    codex_home = sandbox_home / ".codex"
    assert "hooks" not in (codex_home / "config.toml").read_text(encoding="utf-8")
    assert not (codex_home / "hooks").exists()
    assert not (codex_home / "AGENTS.md").exists()


def test_dashboard_service_names_in_the_banner_are_the_shipped_ones():
    """The banner prints stop/start commands; they must name the service the installer registers."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    label = re.search(r'^DASHBOARD_LAUNCHD_LABEL="([^"]+)"$', script, re.MULTILINE).group(1)
    unit = re.search(r'^DASHBOARD_SYSTEMD_UNIT="([^"]+)"$', script, re.MULTILINE).group(1)
    plist = ROOT / "launchagents" / f"{label}.plist"
    assert f"<string>{label}</string>" in plist.read_text(encoding="utf-8")
    assert (ROOT / "systemd" / unit).is_file()
    # The helper prints the names of its own, separately installed service.
    assert "print-management" not in script


def test_install_codex_shim_still_works(sandbox_home: Path):
    # install-codex.sh remains as backward-compat shim
    env = os.environ.copy()
    env["HOME"] = str(sandbox_home)
    env["INSTALL_TEST_MODE"] = "1"
    env["CLAUDE_MEMORY_DIR"] = str(sandbox_home / ".claude-memory")
    env["TAM_MEMORY_DIR"] = env["CLAUDE_MEMORY_DIR"]
    env["XDG_CONFIG_HOME"] = str(sandbox_home / ".config")
    _add_fake_systemctl(env, sandbox_home)

    result = subprocess.run(
        ["bash", str(INSTALL_CODEX_SH)],
        env=env,
        capture_output=True,
        text=True,
        # Generous — this runs the real installer; a loaded machine must not
        # turn into a red test. Nothing here measures speed.
        timeout=900,
    )
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"

    cfg = sandbox_home / ".codex" / "config.toml"
    assert cfg.exists(), "shim must still produce codex config.toml"
    assert 'MEMORY_TRIPLE_TIMEOUT_SEC = "120"' in cfg.read_text(encoding="utf-8")


# ---------- unknown / error paths ----------

def test_unknown_ide_exits_nonzero_with_hint(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "emacs-doctor")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "unsupported" in combined.lower() or "unknown" in combined.lower()
    # Hint must list supported values
    assert "cursor" in combined or "claude-code" in combined


def test_unknown_flag_exits_nonzero(sandbox_home: Path):
    result = _run_install(sandbox_home, "--bogus")
    assert result.returncode != 0


# ---------- idempotency ----------

def test_cursor_install_is_idempotent(sandbox_home: Path):
    # Pre-existing cursor config with unrelated keys must be preserved
    cursor_dir = sandbox_home / ".cursor"
    cursor_dir.mkdir()
    cfg = cursor_dir / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"github": {"command": "github-mcp"}},
        "unrelated_key": {"foo": "bar"},
    }), encoding="utf-8")

    result = _run_install(sandbox_home, "--ide", "cursor")
    assert result.returncode == 0, result.stderr

    data = json.loads(cfg.read_text(encoding="utf-8"))
    # memory added
    assert "memory" in data["mcpServers"]
    # existing github server untouched
    assert "github" in data["mcpServers"]
    assert data["mcpServers"]["github"]["command"] == "github-mcp"
    # unrelated keys preserved
    assert data["unrelated_key"] == {"foo": "bar"}


def test_codex_install_is_idempotent(sandbox_home: Path):
    codex_dir = sandbox_home / ".codex"
    codex_dir.mkdir()
    cfg = codex_dir / "config.toml"
    cfg.write_text('[other_section]\nfoo = "bar"\n', encoding="utf-8")

    # First run
    result1 = _run_install(sandbox_home, "--ide", "codex")
    assert result1.returncode == 0, result1.stderr
    content1 = cfg.read_text(encoding="utf-8")
    assert "[other_section]" in content1
    assert "[mcp_servers.memory]" in content1

    # Second run - must not duplicate the memory block
    result2 = _run_install(sandbox_home, "--ide", "codex")
    assert result2.returncode == 0, result2.stderr
    content2 = cfg.read_text(encoding="utf-8")
    assert content2.count("[mcp_servers.memory]") == 1
    assert content2.count("# --- total-agent-memory MCP Server ---") == 1
    # Other section still there
    assert "[other_section]" in content2
