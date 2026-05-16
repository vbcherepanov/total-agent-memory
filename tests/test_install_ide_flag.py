"""Tests for P0.2: unified install.sh with --ide <IDE> flag.

All cases run install.sh in INSTALL_TEST_MODE=1 with a sandbox HOME so we
don't hit the real filesystem, pip, or launchctl.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
INSTALL_SH = ROOT / "install.sh"
INSTALL_CODEX_SH = ROOT / "install-codex.sh"


def _run_install(home: Path, *args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["INSTALL_TEST_MODE"] = "1"
    # Pin memory dir inside sandbox; keep inherited PATH so python3 (3.10+) is found.
    env["CLAUDE_MEMORY_DIR"] = str(home / ".claude-memory")
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
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

    settings = sandbox_home / ".claude" / "settings.json"
    assert settings.exists(), "claude-code settings.json must be created by default"

    data = json.loads(settings.read_text())
    assert "mcpServers" in data
    assert "memory" in data["mcpServers"]
    assert data["mcpServers"]["memory"]["args"][0].endswith("server.py")
    # Hooks registered only for claude-code
    assert "hooks" in data
    assert "SessionStart" in data["hooks"]
    assert "PostToolUse" in data["hooks"]


def test_explicit_ide_claude_code(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "claude-code")
    assert result.returncode == 0, result.stderr

    settings = sandbox_home / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    assert "memory" in data["mcpServers"]


# ---------- cursor ----------

def test_ide_cursor_writes_cursor_mcp_json(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "cursor")
    assert result.returncode == 0, result.stderr

    cfg = sandbox_home / ".cursor" / "mcp.json"
    assert cfg.exists(), "cursor mcp.json must be created"

    data = json.loads(cfg.read_text())
    assert "mcpServers" in data
    assert "memory" in data["mcpServers"]
    entry = data["mcpServers"]["memory"]
    assert "command" in entry
    assert entry["args"][0].endswith("server.py")
    assert entry["env"]["EMBEDDING_MODEL"] == "all-MiniLM-L6-v2"


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

    data = json.loads(cfg.read_text())
    assert "mcpServers" in data
    assert "memory" in data["mcpServers"]


# ---------- opencode ----------

def test_ide_opencode_writes_opencode_config(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "opencode")
    assert result.returncode == 0, result.stderr

    cfg = sandbox_home / ".opencode" / "config.json"
    assert cfg.exists()

    data = json.loads(cfg.read_text())
    # OpenCode uses `mcp` (not `mcpServers`) as parent key
    assert "mcp" in data
    assert "memory" in data["mcp"]


# ---------- codex ----------

def test_ide_codex_writes_codex_toml_with_env_overrides(sandbox_home: Path):
    result = _run_install(sandbox_home, "--ide", "codex")
    assert result.returncode == 0, result.stderr

    cfg = sandbox_home / ".codex" / "config.toml"
    assert cfg.exists()

    content = cfg.read_text()
    assert "[mcp_servers.memory]" in content
    assert "[mcp_servers.memory.env]" in content
    # PR #5 env overrides must be present
    assert 'MEMORY_TRIPLE_TIMEOUT_SEC = "120"' in content
    assert 'MEMORY_ENRICH_TIMEOUT_SEC = "90"' in content
    assert 'MEMORY_REPR_TIMEOUT_SEC = "120"' in content
    assert 'MEMORY_TRIPLE_MAX_PREDICT = "512"' in content
    # Fence markers for idempotent replace
    assert "# --- total-agent-memory MCP Server ---" in content
    assert "# --- End total-agent-memory ---" in content


def test_install_codex_shim_still_works(sandbox_home: Path):
    # install-codex.sh remains as backward-compat shim
    env = os.environ.copy()
    env["HOME"] = str(sandbox_home)
    env["INSTALL_TEST_MODE"] = "1"
    env["CLAUDE_MEMORY_DIR"] = str(sandbox_home / ".claude-memory")

    result = subprocess.run(
        ["bash", str(INSTALL_CODEX_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"

    cfg = sandbox_home / ".codex" / "config.toml"
    assert cfg.exists(), "shim must still produce codex config.toml"
    assert 'MEMORY_TRIPLE_TIMEOUT_SEC = "120"' in cfg.read_text()


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
    }))

    result = _run_install(sandbox_home, "--ide", "cursor")
    assert result.returncode == 0, result.stderr

    data = json.loads(cfg.read_text())
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
    cfg.write_text('[other_section]\nfoo = "bar"\n')

    # First run
    result1 = _run_install(sandbox_home, "--ide", "codex")
    assert result1.returncode == 0, result1.stderr
    content1 = cfg.read_text()
    assert "[other_section]" in content1
    assert "[mcp_servers.memory]" in content1

    # Second run — must not duplicate the memory block
    result2 = _run_install(sandbox_home, "--ide", "codex")
    assert result2.returncode == 0, result2.stderr
    content2 = cfg.read_text()
    assert content2.count("[mcp_servers.memory]") == 1
    assert content2.count("# --- total-agent-memory MCP Server ---") == 1
    # Other section still there
    assert "[other_section]" in content2
