"""The shipped skill pack must not tell users to run things that don't exist.

`skills/memory-protocol/` is copied verbatim into every IDE a user wires up,
and its templates are meant to be pasted into real config files. Until v13 they
referenced `python3 -m claude_total_memory.cli serve --mode stdio` — a module
that has never existed since the rebrand — and the pre-rebrand
`~/claude-memory-server/` install path. Both produce a broken setup, silently,
for anyone following the manual instructions.

Nothing catches that at runtime, so it is checked here.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills" / "memory-protocol"

DOC_SUFFIXES = {".md", ".mdc", ".toml", ".json", ".txt"}
DOCS = sorted(p for p in SKILLS.rglob("*") if p.suffix in DOC_SUFFIXES)


def test_the_skill_pack_is_actually_there():
    assert (SKILLS / "SKILL.md").is_file()
    assert len(DOCS) >= 8, "skill pack looks truncated"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(SKILLS)))
def test_no_reference_to_the_nonexistent_cli_module(doc: Path):
    """`claude_total_memory.cli` is not importable and never was."""
    assert "claude_total_memory.cli" not in doc.read_text(encoding="utf-8"), (
        "use the `total-agent-memory` console script instead"
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(SKILLS)))
def test_no_reference_to_the_pre_rebrand_install_path(doc: Path):
    """The documented checkout is ~/total-agent-memory."""
    text = doc.read_text(encoding="utf-8")
    assert "claude-memory-server" not in text
    assert "claude_memory_server" not in text


def test_the_entry_point_the_templates_name_is_declared():
    """`total-agent-memory` must exist as a console script."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    assert "total-agent-memory" in scripts
    assert "lookup-memory" in scripts


def test_claude_code_template_is_valid_and_points_at_the_entry_point():
    cfg = json.loads((SKILLS / "templates" / "claude-code-settings.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["memory"]["command"] == "total-agent-memory"
    for event, blocks in cfg["hooks"].items():
        for block in blocks:
            for hook in block["hooks"]:
                assert hook["type"] == "command", event
                assert hook["command"].startswith("$HOME/.claude/hooks/"), event


def test_codex_template_is_valid_toml_and_points_at_the_entry_point():
    """Codex reads `[mcp_servers.<name>]`; a `[mcp.servers.<name>]` table is ignored without an error."""
    cfg = tomllib.loads((SKILLS / "templates" / "codex-config.toml").read_text(encoding="utf-8"))
    assert cfg["mcp_servers"]["memory"]["command"] == "total-agent-memory"
    assert cfg["mcp_servers"]["memory"]["args"] == []
    # The installer registers no Codex hooks, so the template must not show a hook table either.
    assert set(cfg) == {"mcp_servers"}


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(SKILLS)))
def test_no_doc_shows_the_codex_table_codex_ignores(doc: Path):
    assert "[mcp.servers." not in doc.read_text(encoding="utf-8")


def test_codex_setup_claims_only_what_the_installer_does():
    """`install.sh --ide codex` writes the MCP table and the skills: no hooks, no AGENTS.md."""
    guide = (SKILLS / "references" / "ide-setup.md").read_text(encoding="utf-8")
    codex = guide[guide.index("## 2. Codex CLI"):guide.index("## 3. Cursor")]
    assert "[hooks]" not in codex and "hook entries" not in codex
    assert "It does not write hooks, `~/.codex/AGENTS.md`" in codex
    block = (SKILLS / "templates" / "codex-AGENTS-block.md").read_text(encoding="utf-8")
    assert "does this automatically" not in block
    matrix = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    (row,) = [line for line in matrix.splitlines() if line.startswith("| Codex CLI |") and "--ide codex" in line]
    assert row.split("|")[3].strip().startswith("❌")


def test_templates_named_in_the_setup_guide_exist():
    guide = (SKILLS / "references" / "ide-setup.md").read_text(encoding="utf-8")
    named = set(re.findall(r"templates/([\w.-]+\.\w+)", guide))
    assert named, "the guide no longer names any template"
    for name in sorted(named):
        assert (SKILLS / "templates" / name).is_file(), f"templates/{name} is named in ide-setup.md"


def test_lookup_helper_is_referenced_by_its_installed_name():
    """Sub-agents are told to call `lookup-memory`, not a path into a checkout."""
    text = (SKILLS / "references" / "subagent-protocol.md").read_text(encoding="utf-8")
    assert "lookup-memory" in text
