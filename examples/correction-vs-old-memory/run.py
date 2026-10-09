"""A newer correction versus an older stored memory.

Saves "port 5432" for the staging database, then saves the correction
"port 5433" with supersede=true, and shows what recall returns afterwards.
The same scenario is replayed on the temporal knowledge graph with dated
facts and a point-in-time query.

Runs against a throwaway database in a temporary directory. No LLM, no API
keys. Exit code is 0 when every check passes and 1 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"

PROJECT = "demo"
OLD_FACT = "The staging database is on port 5432"
NEW_FACT = "The staging database is on port 5433"
QUESTION = "which port is staging on"

KG_SUBJECT = "staging_database"
KG_PREDICATE = "listens_on_port"
OLD_PORT = "5432"
NEW_PORT = "5433"
OLD_FACT_DATE = "2026-01-10T00:00:00Z"
CORRECTION_DATE = "2026-03-01T00:00:00Z"
BEFORE_CORRECTION = "2026-02-01T00:00:00Z"
AFTER_CORRECTION = "2026-04-01T00:00:00Z"

# The server derives most knobs from MEMORY_MODE. These overrides keep the
# run local and deterministic: no LLM, no reranker download, no background
# enrichment thread racing with the database close at the end.
RUNTIME_ENV = {
    "MEMORY_MODE": "fast",
    "MEMORY_LLM_ENABLED": "false",
    "MEMORY_CROSS_RERANK": "off",
    "MEMORY_ASYNC_ENRICHMENT": "false",
    "MEMORY_QUALITY_GATE_ENABLED": "false",
    "MEMORY_CONTRADICTION_DETECT_ENABLED": "false",
    "MCP_TRANSPORT": "stdio",
}


@dataclass(frozen=True)
class Check:
    """One verified expectation with what the server actually returned."""

    name: str
    expected: str
    actual: str
    passed: bool


def configure_environment(memory_dir: Path) -> None:
    """Point the server at a throwaway memory directory before it is imported."""
    os.environ["TAM_MEMORY_DIR"] = str(memory_dir)
    os.environ["MEMORY_ACTIVECONTEXT_VAULT"] = str(memory_dir / "vault")
    for key, value in RUNTIME_ENV.items():
        os.environ.setdefault(key, value)


def open_server(memory_dir: Path) -> ModuleType:
    """Import the server module and bind its store and recall to memory_dir."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    import server

    server.MEMORY_DIR = memory_dir
    server.store = server.Store()
    server.recall = server.Recall(server.store)
    server.SID = "correction-example"
    server.BRANCH = ""
    server._v5_modules = {}
    return server


def call(server: ModuleType, tool: str, **args: Any) -> dict[str, Any]:
    """Invoke one MCP tool in-process and decode its JSON reply."""
    return json.loads(asyncio.run(server._do(tool, args)))


def recall_contents(server: ModuleType, query: str) -> list[str]:
    """Return the content of every recall hit, best first."""
    out = call(server, "memory_recall", query=query, project=PROJECT, limit=5)
    return [hit["content"] for group in out["results"].values() for hit in group]


def run_memory_scenario(server: ModuleType) -> list[Check]:
    """memory_save with supersede=true, then memory_get and memory_recall."""
    old = call(server, "memory_save", content=OLD_FACT, type="fact", project=PROJECT)
    new = call(server, "memory_save", content=NEW_FACT, type="fact", project=PROJECT, supersede=True)
    old_id, new_id = old["id"], new["id"]

    superseded = new.get("superseded", [])
    rows = call(server, "memory_get", ids=[old_id, new_id])["results"]
    status = {row["id"]: (row["status"], row["superseded_by"]) for row in rows}
    hits = recall_contents(server, QUESTION)

    return [
        Check(
            name="save: the older fact is stored",
            expected="saved=True",
            actual=f"saved={old['saved']} id={old_id}",
            passed=old["saved"] is True,
        ),
        Check(
            name="save: the correction retires the older record",
            expected=f"superseded=[{old_id}]",
            actual=f"superseded={superseded}",
            passed=superseded == [old_id],
        ),
        Check(
            name="get: the older record is marked superseded by the correction",
            expected=f"status=superseded superseded_by={new_id}",
            actual=f"status={status[old_id][0]} superseded_by={status[old_id][1]}",
            passed=status[old_id] == ("superseded", new_id),
        ),
        Check(
            name="get: the correction stays active",
            expected="status=active superseded_by=None",
            actual=f"status={status[new_id][0]} superseded_by={status[new_id][1]}",
            passed=status[new_id] == ("active", None),
        ),
        Check(
            name="recall: the first hit is the correction",
            expected=repr(NEW_FACT),
            actual=repr(hits[0]) if hits else "no hits",
            passed=bool(hits) and hits[0] == NEW_FACT,
        ),
        Check(
            name="recall: the older value is not returned",
            expected=f"{OLD_FACT!r} absent",
            actual=f"hits={hits}",
            passed=OLD_FACT not in hits,
        ),
    ]


def kg_ports(server: ModuleType, timestamp: str | None) -> list[str]:
    """Objects of the staging port fact valid at timestamp (None means now)."""
    args: dict[str, Any] = {"subject": KG_SUBJECT, "predicate": KG_PREDICATE, "project": PROJECT}
    if timestamp is not None:
        args["timestamp"] = timestamp
    return [row["object"] for row in call(server, "kg_at", **args)["assertions"]]


def run_kg_scenario(server: ModuleType) -> list[Check]:
    """kg_add_fact with dated facts, then kg_at before and after the correction."""
    old_id = call(server, "kg_add_fact", subject=KG_SUBJECT, predicate=KG_PREDICATE, object=OLD_PORT,
                  project=PROJECT, valid_from=OLD_FACT_DATE)["assertion_id"]
    new_id = call(server, "kg_add_fact", subject=KG_SUBJECT, predicate=KG_PREDICATE, object=NEW_PORT,
                  project=PROJECT, valid_from=CORRECTION_DATE)["assertion_id"]

    before = kg_ports(server, BEFORE_CORRECTION)
    after = kg_ports(server, AFTER_CORRECTION)
    now = kg_ports(server, None)
    timeline = call(server, "kg_timeline", subject=KG_SUBJECT, project=PROJECT)["timeline"]
    old_row = next(row for row in timeline if row["id"] == old_id)

    return [
        Check(
            name="kg_at: before the correction date the old port is valid",
            expected=f"[{OLD_PORT!r}]",
            actual=repr(before),
            passed=before == [OLD_PORT],
        ),
        Check(
            name="kg_at: after the correction date the new port is valid",
            expected=f"[{NEW_PORT!r}]",
            actual=repr(after),
            passed=after == [NEW_PORT],
        ),
        Check(
            name="kg_at: now only the new port is valid",
            expected=f"[{NEW_PORT!r}]",
            actual=repr(now),
            passed=now == [NEW_PORT],
        ),
        Check(
            name="kg_timeline: the old assertion is closed on the correction date",
            expected=f"valid_to={CORRECTION_DATE} superseded_by={new_id}",
            actual=f"valid_to={old_row['valid_to']} superseded_by={old_row['superseded_by']}",
            passed=old_row["valid_to"] == CORRECTION_DATE and old_row["superseded_by"] == new_id,
        ),
    ]


def run_scenario(server: ModuleType) -> list[Check]:
    """Both scenarios against one server."""
    return run_memory_scenario(server) + run_kg_scenario(server)


def report(checks: list[Check]) -> int:
    """Print one line per check and return the process exit code."""
    for check in checks:
        verdict = "PASS" if check.passed else "FAIL"
        print(f"{verdict}  {check.name}")
        print(f"      expected: {check.expected}")
        print(f"      actual:   {check.actual}")
    failed = sum(1 for check in checks if not check.passed)
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tam-correction-example-") as tmp:
        memory_dir = Path(tmp)
        configure_environment(memory_dir)
        server = open_server(memory_dir)
        try:
            checks = run_scenario(server)
        finally:
            server.store.db.close()
    return report(checks)


if __name__ == "__main__":
    sys.exit(main())
