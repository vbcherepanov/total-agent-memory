"""The Claude Code plugin must stay installable.

Covers the two ways this breaks silently: a manifest that drifts out of sync
with the release (wrong version, dangling file references), and a bootstrap
that cannot find a runnable server.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
MARKETPLACE = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))


# ── Manifest ────────────────────────────────────────────────────────


def test_plugin_manifest_has_the_required_fields():
    assert PLUGIN["name"] == "total-agent-memory"
    assert PLUGIN["description"].strip()
    # `repository` must be a string — Claude Code rejects the npm-style object.
    assert isinstance(PLUGIN["repository"], str)


def test_plugin_version_tracks_the_package_version():
    """A stale plugin version means users never get the update."""
    from version import VERSION

    assert PLUGIN["version"] == VERSION, (
        f"plugin.json says {PLUGIN['version']}, src/version.py says {VERSION}"
    )


def test_marketplace_lists_this_plugin():
    names = {p["name"] for p in MARKETPLACE["plugins"]}
    assert PLUGIN["name"] in names
    assert MARKETPLACE["owner"]["name"]


def test_marketplace_ships_the_plugin_from_this_repo():
    """The plugin lives at the marketplace root, so its source is the root."""
    (entry,) = [p for p in MARKETPLACE["plugins"] if p["name"] == PLUGIN["name"]]
    assert entry["source"] == "./"


def test_plugin_has_no_top_level_bin_dir():
    """claude.ai and Cowork refuse a plugin with a top-level bin/ directory."""
    assert not (ROOT / "bin").exists()


# ── Wiring ──────────────────────────────────────────────────────────


PROJECT_PREFIX = "${CLAUDE_PLUGIN_ROOT:-.}/"


def _bootstrap_arg(server: dict, prefix: str) -> Path:
    assert server["command"] == "python3"
    (arg,) = server["args"]
    assert arg.startswith(prefix), arg
    return ROOT / arg.removeprefix(prefix)


def test_plugin_server_points_at_an_existing_bootstrap():
    """The installed plugin starts the server from its own directory.

    Claude Code replaces the exact token ``${CLAUDE_PLUGIN_ROOT}``. A shell-style default
    (``${CLAUDE_PLUGIN_ROOT:-.}``) expands to ``.``, the user's project directory, where the script
    does not exist, and the server never connects.
    """
    target = _bootstrap_arg(PLUGIN["mcpServers"]["memory"], "${CLAUDE_PLUGIN_ROOT}/")
    assert target.is_file(), f"{target} referenced by plugin.json does not exist"


def test_project_mcp_config_points_at_an_existing_bootstrap():
    """.mcp.json serves the checkout opened as a project: outside a plugin the variable is unset,
    so its default "." makes the path relative to the repository root."""
    cfg = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    target = _bootstrap_arg(cfg["mcpServers"]["memory"], PROJECT_PREFIX)
    assert target.is_file(), f"{target} referenced by .mcp.json does not exist"


def test_plugin_and_project_servers_differ_only_in_the_path():
    cfg = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    project, plugin = cfg["mcpServers"]["memory"], PLUGIN["mcpServers"]["memory"]
    assert {**project, "args": None} == {**plugin, "args": None}
    assert _bootstrap_arg(project, PROJECT_PREFIX) == _bootstrap_arg(plugin, "${CLAUDE_PLUGIN_ROOT}/")


def test_every_hook_command_exists_and_is_executable():
    cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    seen = 0
    for event, matchers in cfg["hooks"].items():
        for matcher in matchers:
            for hook in matcher["hooks"]:
                command = hook["command"]
                # Quoted so an install path with spaces stays one word.
                assert command.startswith('"${CLAUDE_PLUGIN_ROOT}/'), event
                assert command.endswith('"'), event
                path = ROOT / command.strip('"').replace("${CLAUDE_PLUGIN_ROOT}/", "")
                assert path.is_file(), f"{event}: missing {path}"
                if sys.platform != "win32":  # NTFS has no POSIX executable bit
                    assert os.stat(path).st_mode & stat.S_IXUSR, f"{event}: {path} not +x"
                seen += 1
    assert seen >= 7, "expected the full capture hook set"


def test_hook_events_are_real_claude_code_events():
    cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    known = {
        "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
        "PostToolUse", "PostToolUseFailure", "Stop", "SubagentStop",
        "PreCompact", "PostCompact", "Notification", "MessageDisplay",
    }
    unknown = set(cfg["hooks"]) - known
    assert not unknown, f"unknown hook events: {sorted(unknown)}"


def test_the_skill_ships_with_the_plugin():
    skill = ROOT / "skills" / "memory-protocol" / "SKILL.md"
    assert skill.is_file()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md needs YAML frontmatter"
    assert "description:" in text.split("---")[1]


# ── Bootstrap resolution ────────────────────────────────────────────


@pytest.fixture
def bootstrap():
    import tam_plugin_bootstrap

    return tam_plugin_bootstrap


def test_bootstrap_prefers_an_existing_install(bootstrap, tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    bindir.mkdir(parents=True)
    entry = bindir / ("total-agent-memory.exe" if os.name == "nt" else "total-agent-memory")
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("TAM_MEMORY_DIR", str(tmp_path))

    assert bootstrap._from_existing_venv() == [str(entry)]


def test_bootstrap_reports_no_install_when_there_is_none(bootstrap, tmp_path, monkeypatch):
    monkeypatch.setenv("TAM_MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(bootstrap, "PLUGIN_ROOT", tmp_path)

    assert bootstrap._from_existing_venv() is None


def test_bootstrap_honours_the_legacy_memory_dir(bootstrap, tmp_path, monkeypatch):
    monkeypatch.delenv("TAM_MEMORY_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_MEMORY_DIR", str(tmp_path / "legacy"))

    assert bootstrap._memory_home() == tmp_path / "legacy"


def test_bootstrap_falls_back_to_a_runner(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: (
        "/usr/local/bin/uvx" if name == "uvx" else None
    ))
    assert bootstrap._from_runner() == ["/usr/local/bin/uvx", "total-agent-memory"]

    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: (
        "/usr/local/bin/npx" if name == "npx" else None
    ))
    assert bootstrap._from_runner() == ["/usr/local/bin/npx", "-y", "total-agent-memory"]

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    assert bootstrap._from_runner() is None
