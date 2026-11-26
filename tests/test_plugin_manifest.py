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


def test_mcp_config_points_at_an_existing_bootstrap():
    cfg = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = cfg["mcpServers"]["memory"]
    assert server["command"] == "python3"
    (arg,) = server["args"]
    # The root variable defaults to "." so the file also works when launched from the repo root.
    prefix = "${CLAUDE_PLUGIN_ROOT:-.}/"
    assert arg.startswith(prefix)
    target = ROOT / arg.removeprefix(prefix)
    assert target.is_file(), f"{target} referenced by .mcp.json does not exist"


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


def _resolvers(bootstrap, monkeypatch) -> list[str]:
    """The order main() tries the resolvers in, with every one of them finding nothing."""
    tried: list[str] = []
    for name in ("_from_existing_venv", "_from_path", "_from_plugin_checkout",
                 "_bootstrap_venv", "_from_runner"):
        monkeypatch.setattr(bootstrap, name, lambda name=name: tried.append(name))
    assert bootstrap.main() == 1
    return tried


def test_bootstrap_builds_the_venv_before_it_uses_a_runner(bootstrap, monkeypatch):
    """The hooks take their interpreter from that venv; uvx and npx leave them without one."""
    tried = _resolvers(bootstrap, monkeypatch)
    assert tried.index("_bootstrap_venv") < tried.index("_from_runner")


def _fake_build(bootstrap, venv: Path, monkeypatch, *, fail=None, entry=True) -> list[list[str]]:
    """Replace subprocess.run with a build that writes the venv's files; returns the commands."""
    ran: list[list[str]] = []

    def run(command, **kwargs):
        assert kwargs.get("capture_output"), "stdout belongs to the MCP client"
        ran.append(command)
        python = bootstrap._exe(venv, "python")
        if len(ran) == 1:  # venv creation
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            return
        if fail is not None:  # the install
            raise fail
        if entry:
            bootstrap._exe(venv, "total-agent-memory").write_text("", encoding="utf-8")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    return ran


@pytest.fixture
def memory_dir(bootstrap, tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("TAM_MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    return tmp_path


def test_bootstrap_venv_is_where_the_hooks_look(bootstrap, memory_dir, monkeypatch):
    venv = memory_dir / ".venv"
    ran = _fake_build(bootstrap, venv, monkeypatch)

    assert bootstrap._bootstrap_venv() == [str(bootstrap._exe(venv, "total-agent-memory"))]
    python = str(bootstrap._exe(venv, "python"))
    assert ran == [
        [sys.executable, "-m", "venv", str(venv)],
        [python, "-m", "pip", "install", "--quiet", "--upgrade", "total-agent-memory"],
    ]
    assert not bootstrap._build_marker(venv).exists()
    # Both hook libraries probe <memory dir>/.venv.
    assert '"${TAM_MEMORY_DIR}"' in (ROOT / "hooks/lib/common.sh").read_text(encoding="utf-8")
    assert "$MemoryDir" in (ROOT / "hooks/lib/common.ps1").read_text(encoding="utf-8")


def test_bootstrap_venv_uses_uv_when_it_is_installed(bootstrap, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/opt/uv" if name == "uv" else None)
    venv = tmp_path / ".venv"

    create, install = bootstrap._venv_commands(venv)
    assert create == ["/opt/uv", "venv", "--quiet", "--python", ">=3.11", str(venv)]
    assert install[:3] == ["/opt/uv", "pip", "install"] and install[-1] == "total-agent-memory"
    assert str(bootstrap._exe(venv, "python")) in install


def test_bootstrap_removes_a_venv_it_could_not_finish(bootstrap, memory_dir, monkeypatch):
    """Left in place, it would be taken for an existing install on the next start."""
    venv = memory_dir / ".venv"
    failure = bootstrap.subprocess.CalledProcessError(1, ["pip"], stderr=b"no network")
    _fake_build(bootstrap, venv, monkeypatch, fail=failure)

    assert bootstrap._bootstrap_venv() is None
    assert not venv.exists() and not bootstrap._build_marker(venv).exists()


def test_bootstrap_removes_a_venv_whose_install_left_no_entry_point(bootstrap, memory_dir, monkeypatch):
    venv = memory_dir / ".venv"
    _fake_build(bootstrap, venv, monkeypatch, entry=False)

    assert bootstrap._bootstrap_venv() is None
    assert not venv.exists()


def test_bootstrap_removes_the_venv_when_the_start_is_interrupted(bootstrap, memory_dir, monkeypatch):
    """Claude Code interrupts a server that takes longer than MCP_TIMEOUT to start."""
    venv = memory_dir / ".venv"
    _fake_build(bootstrap, venv, monkeypatch, fail=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        bootstrap._bootstrap_venv()
    assert not venv.exists() and not bootstrap._build_marker(venv).exists()


def _half_built(bootstrap, venv: Path) -> None:
    """What a killed build leaves: an interpreter, no package, and the marker."""
    python = bootstrap._exe(venv, "python")
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    bootstrap._build_marker(venv).touch()


def test_bootstrap_does_not_start_from_a_venv_a_killed_build_left(bootstrap, memory_dir, monkeypatch):
    venv = memory_dir / ".venv"
    monkeypatch.setattr(bootstrap, "_candidate_venvs", lambda: [venv])
    _half_built(bootstrap, venv)
    assert (bootstrap.PLUGIN_ROOT / "src" / "server.py").is_file()

    assert bootstrap._from_existing_venv() is None


def test_bootstrap_leaves_a_build_in_progress_alone(bootstrap, memory_dir, monkeypatch):
    venv = memory_dir / ".venv"
    _half_built(bootstrap, venv)
    ran = _fake_build(bootstrap, venv, monkeypatch)

    assert bootstrap._bootstrap_venv() is None
    assert ran == [] and venv.exists()


def test_bootstrap_rebuilds_after_a_killed_build(bootstrap, memory_dir, monkeypatch):
    venv = memory_dir / ".venv"
    _half_built(bootstrap, venv)
    old = bootstrap.time.time() - bootstrap.BUILD_GRACE_SECONDS - 1
    os.utime(bootstrap._build_marker(venv), (old, old))
    _fake_build(bootstrap, venv, monkeypatch)

    assert bootstrap._bootstrap_venv() == [str(bootstrap._exe(venv, "total-agent-memory"))]
