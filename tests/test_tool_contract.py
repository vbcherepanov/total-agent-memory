"""The MCP tool contract is frozen: names, input schemas and behaviour hints.

Claude Code's plugin (``.claude-plugin``), the capture hooks and the
``memory-protocol`` skill call these tools by name with these arguments.
A release that renames a tool, drops a parameter, changes a default or
flips an annotation breaks every installed copy silently, so the whole
catalogue is pinned in ``tests/fixtures/tool_contract.json``.

Adding a tool or an optional parameter with a default is allowed: the
fixture is then regenerated on purpose with

    TAM_UPDATE_TOOL_CONTRACT=1 python -m pytest tests/test_tool_contract.py

and the diff of the fixture is reviewed like any other API change.
Descriptions are not pinned; they are prose for the model, not a contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import server

FIXTURE = ROOT / "tests" / "fixtures" / "tool_contract.json"
UPDATE_FLAG = "TAM_UPDATE_TOOL_CONTRACT"


def _schema(tool) -> dict:
    """mcp SDK 1.x spells it inputSchema, 2.x input_schema; both serialize to the same JSON."""
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = tool.inputSchema
    return schema


def _annotations(tool) -> dict[str, bool]:
    ann = tool.annotations
    return {
        "readOnlyHint": bool(ann.read_only_hint),
        "destructiveHint": bool(ann.destructive_hint),
        "idempotentHint": bool(ann.idempotent_hint),
        "openWorldHint": bool(ann.open_world_hint),
    }


def current_contract() -> dict[str, dict]:
    tools = asyncio.run(server.list_tools())
    return {
        tool.name: {"inputSchema": _schema(tool), "annotations": _annotations(tool)}
        for tool in sorted(tools, key=lambda t: t.name)
    }


def _dump(contract: dict[str, dict]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.fixture(scope="module")
def contract() -> dict[str, dict]:
    live = current_contract()
    if os.environ.get(UPDATE_FLAG) == "1":
        FIXTURE.write_text(_dump(live), encoding="utf-8")
    return live


@pytest.fixture(scope="module")
def pinned() -> dict[str, dict]:
    assert FIXTURE.exists(), f"missing {FIXTURE}; generate it with {UPDATE_FLAG}=1"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_no_pinned_tool_was_removed_or_renamed(contract, pinned):
    missing = sorted(set(pinned) - set(contract))
    assert not missing, f"tools removed or renamed (plugin/hooks call them): {missing}"


def test_new_tools_are_pinned_on_purpose(contract, pinned):
    added = sorted(set(contract) - set(pinned))
    assert not added, f"new tools must be added to the contract fixture with {UPDATE_FLAG}=1: {added}"


@pytest.mark.parametrize("name", sorted(json.loads(FIXTURE.read_text(encoding="utf-8"))) if FIXTURE.exists() else [])
def test_pinned_tool_keeps_its_schema_and_hints(name, contract, pinned):
    live = contract.get(name)
    assert live is not None, name
    live_schema, pinned_schema = live["inputSchema"], pinned[name]["inputSchema"]
    live_props = live_schema.get("properties", {})
    pinned_props = pinned_schema.get("properties", {})

    dropped = sorted(set(pinned_props) - set(live_props))
    assert not dropped, f"{name}: parameters removed: {dropped}"

    for param, pinned_spec in pinned_props.items():
        assert live_props[param] == pinned_spec, (
            f"{name}.{param}: schema changed\n pinned: {pinned_spec}\n live:   {live_props[param]}"
        )

    pinned_required = set(pinned_schema.get("required", []))
    live_required = set(live_schema.get("required", []))
    assert live_required <= pinned_required, (
        f"{name}: parameters became required, old callers would fail: {sorted(live_required - pinned_required)}"
    )
    assert pinned_required <= live_required, (
        f"{name}: required parameters relaxed, pin the new contract on purpose: {sorted(pinned_required - live_required)}"
    )

    added = sorted(set(live_props) - set(pinned_props))
    for param in added:
        spec = live_props[param]
        assert "default" in spec or param not in live_required, (
            f"{name}.{param}: new parameter needs a default (or stay optional) and the fixture regenerated"
        )
    assert not added, f"{name}: new optional parameters must be pinned with {UPDATE_FLAG}=1: {added}"

    assert live["annotations"] == pinned[name]["annotations"], f"{name}: behaviour hints changed"


def test_fixture_is_canonical(contract, pinned):
    """The fixture on disk is byte-identical to a fresh dump of itself."""
    assert FIXTURE.read_text(encoding="utf-8") == _dump(pinned)
