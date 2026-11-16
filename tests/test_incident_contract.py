"""Issue #42: the save/update tools gained only optional incident fields (and the ``incident`` type value) since v14.10.

``tool_contract_v14_10_save_tools.json`` is the v14.10 schema of the three tools, frozen as released
(the main fixture is regenerated on every contract change, so it cannot prove this on its own).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_tool_contract import contract  # noqa: F401 -- module fixture

BASELINE = Path(__file__).parent / "fixtures" / "tool_contract_v14_10_save_tools.json"
INCIDENT_FIELDS = frozenset({"detection", "affected", "resolution", "incident_status"})
INCIDENT_TYPE = "incident"
TOOLS = ("memory_save", "memory_save_fast", "memory_update")


@pytest.mark.parametrize("name", TOOLS)
def test_tool_changed_only_by_added_optional_incident_fields(name, contract):  # noqa: F811
    old = json.loads(BASELINE.read_text(encoding="utf-8"))[name]
    live = contract[name]
    old_schema, live_schema = old["inputSchema"], live["inputSchema"]
    old_props, live_props = old_schema["properties"], live_schema["properties"]

    assert set(old_props) <= set(live_props), f"{name}: parameters removed"
    for param, spec in old_props.items():
        if param == "type" and "enum" in spec:
            # The incident record type is the one widening: every v14.10 value stays accepted.
            assert live_props[param]["enum"] == [*spec["enum"], INCIDENT_TYPE]
            assert {k: v for k, v in live_props[param].items() if k != "enum"} == {k: v for k, v in spec.items() if k != "enum"}
            continue
        assert live_props[param] == spec, f"{name}.{param}: v14.10 parameter changed"
    assert set(live_props) - set(old_props) == INCIDENT_FIELDS
    assert set(live_schema.get("required", [])) == set(old_schema.get("required", []))
    for key in set(old_schema) - {"properties"}:
        assert live_schema.get(key) == old_schema[key], f"{name}: schema key {key} changed"
    assert live["annotations"] == old["annotations"]
