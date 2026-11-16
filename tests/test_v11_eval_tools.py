"""v11.0 Phase 8 — tests for the eval-harness MCP tools.

Six new tools land in this phase:
  * memory_eval_locomo
  * memory_eval_recall
  * memory_eval_temporal
  * memory_eval_entity_consistency
  * memory_eval_contradictions
  * memory_eval_long_context

The contract:
  - Every tool returns structured JSON (no markdown).
  - In `mode="fast"` they MUST NOT touch the LLM or the network. The
    counters `llm_calls_during_eval` and `network_calls_during_eval` come
    back as 0.
  - When a sub-feature is genuinely missing, the tool returns
    {"status": "not_implemented", "reason": "..."} instead of erroring.

Tests reuse the `fast_store` pattern from `test_v11_new_tools.py` and call
`server._do(name, args)` directly. The module-level globals (`store`,
`recall`, `SID`) are wired through monkeypatch.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


SRC = str(Path(__file__).parent.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ──────────────────────────────────────────────
# Fast store fixture (FastEmbed-only, no LLM).
# ──────────────────────────────────────────────


@pytest.fixture
def fast_store(monkeypatch, tmp_path):
    """A Store running in v11 fast mode with FastEmbed available."""
    for var in (
        "MEMORY_QUALITY_GATE_ENABLED",
        "MEMORY_CONTRADICTION_DETECT_ENABLED",
        "MEMORY_ENTITY_DEDUP_ENABLED",
        "MEMORY_COREF_ENABLED",
        "USE_ADVANCED_RAG",
        "MEMORY_QUERY_REWRITE",
        "MEMORY_ASYNC_ENRICHMENT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEMORY_MODE", "fast")
    monkeypatch.setenv("MEMORY_USE_LLM_IN_HOT_PATH", "false")
    monkeypatch.setenv("MEMORY_ALLOW_OLLAMA_IN_HOT_PATH", "false")
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "false")
    monkeypatch.setenv("MEMORY_ENRICHMENT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_LLM_ENABLED", "false")
    # Silence the async enrichment worker — it races the fixture teardown
    # in the v11 fast-mode default (MEMORY_ASYNC_ENRICHMENT=true) and can
    # crash the interpreter on db.close(). Tests don't need it.
    monkeypatch.setenv("MEMORY_ASYNC_ENRICHMENT", "false")

    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)
    import config
    import server

    if hasattr(config, "_cache_clear"):
        config._cache_clear()
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    s = server.Store()
    s.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) "
        "VALUES ('s1','2026-04-27T00:00:00Z','demo','open')"
    )
    s.db.commit()
    yield s
    # Belt + braces: stop the async enrichment thread first, only then
    # close the db. Otherwise a still-ticking worker can segfault Python.
    try:
        worker = getattr(s, "_enrich_worker", None)
        if worker is not None and hasattr(worker, "stop"):
            worker.stop()
            if hasattr(worker, "join"):
                worker.join(timeout=2.0)
    except Exception:
        pass
    try:
        s.db.close()
    except Exception:
        pass


@pytest.fixture
def wired_server(fast_store, monkeypatch):
    """Wire the module-level globals that `_do()` reads."""
    import server
    monkeypatch.setattr(server, "store", fast_store)
    monkeypatch.setattr(server, "recall", server.Recall(fast_store))
    monkeypatch.setattr(server, "SID", "s1")
    monkeypatch.setattr(server, "BRANCH", "", raising=False)
    return server


def _call(server, name: str, args: dict) -> dict:
    raw = asyncio.run(server._do(name, args))
    return json.loads(raw)


# ──────────────────────────────────────────────
# (A) memory_eval_locomo — structured payload
# ──────────────────────────────────────────────


REQUIRED_KEYS = {
    "scenarios_total",
    "scenarios_passed",
    "recall_at_5",
    "recall_at_10",
    "latency_ms",
    "mode",
    "llm_calls_during_eval",
    "network_calls_during_eval",
}


def test_memory_eval_locomo_returns_structured_payload(wired_server, tmp_path):
    """memory_eval_locomo must return every required key with the right type
    even when the scenario suite is small."""
    from tests.test_locomo_dataset import sample
    scen = tmp_path / "tiny.json"
    scen.write_text(json.dumps([sample()]), encoding="utf-8")

    payload = _call(wired_server, "memory_eval_locomo", {
        "limit": 2,
        "scenarios_path": str(scen),
        "top_k": 5,
        "mode": "fast",
    })

    missing = REQUIRED_KEYS - set(payload.keys())
    assert not missing, f"missing keys: {missing}; got {payload}"

    assert isinstance(payload["scenarios_total"], int)
    assert isinstance(payload["scenarios_passed"], int)
    assert isinstance(payload["recall_at_5"], float)
    assert isinstance(payload["recall_at_10"], float)
    assert isinstance(payload["latency_ms"], float)
    assert payload["mode"] == "fast"
    assert payload["scenarios_total"] >= 1


def test_memory_eval_recall_builtin_fixture(wired_server):
    """memory_eval_recall with no dataset_path uses the built-in fixture and
    returns the same structured shape."""
    payload = _call(wired_server, "memory_eval_recall", {
        "top_k": 5, "mode": "fast",
    })
    assert REQUIRED_KEYS - set(payload.keys()) == set(), payload
    assert payload["mode"] == "fast"
    assert payload["scenarios_total"] >= 1
    assert payload.get("dataset") == "builtin"


# ──────────────────────────────────────────────
# (B) Fast mode must produce zero LLM / network calls
# ──────────────────────────────────────────────


def test_eval_runs_in_fast_mode_with_zero_llm(wired_server):
    """The fast-mode contract: no LLM, no network. Every eval tool that
    runs in fast mode must echo zeros."""
    # Seed something so the eval has data.
    wired_server.store.save_knowledge(
        sid="s1", content="postgres autovacuum tuning fact for eval",
        ktype="fact", project="memory_eval_test",
    )

    for tool, extra in [
        ("memory_eval_recall", {}),
        ("memory_eval_long_context", {"n_records": 20, "top_k": 5}),
        ("memory_eval_entity_consistency", {}),
    ]:
        payload = _call(wired_server, tool, {"mode": "fast", **extra})
        # not_implemented payloads are exempt by design.
        if payload.get("status") == "not_implemented":
            continue
        assert payload.get("mode") == "fast", (tool, payload)
        assert payload.get("llm_calls_during_eval") == 0, (tool, payload)
        assert payload.get("network_calls_during_eval") == 0, (tool, payload)


# ──────────────────────────────────────────────
# (C) Temporal — graceful not_implemented fallback
# ──────────────────────────────────────────────


def test_eval_temporal_returns_not_implemented_when_temporal_missing(
    wired_server, monkeypatch,
):
    """When `temporal_kg` cannot be imported, the tool must return
    {"status": "not_implemented", ...} rather than crashing."""
    import server as _srv

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _hide_temporal(name, *args, **kw):
        if name == "temporal_kg":
            raise ImportError("temporal_kg hidden for this test")
        return real_import(name, *args, **kw)

    # Drop any cached module so the import re-runs.
    sys.modules.pop("temporal_kg", None)
    monkeypatch.setitem(sys.modules, "temporal_kg", None)
    monkeypatch.setattr("builtins.__import__", _hide_temporal)

    payload = _call(_srv, "memory_eval_temporal", {"mode": "fast"})
    assert payload.get("status") == "not_implemented", payload
    assert "temporal" in payload.get("reason", "").lower()


# ──────────────────────────────────────────────
# (D) Contradictions — explicit not_implemented in fast mode
# ──────────────────────────────────────────────


def test_eval_contradictions_refuses_fast_mode(wired_server):
    """`memory_eval_contradictions` requires LLM. Fast mode must refuse
    cleanly via the not_implemented contract — never crash."""
    payload = _call(wired_server, "memory_eval_contradictions", {"mode": "fast"})
    assert payload.get("status") == "not_implemented", payload
    assert "LLM" in payload.get("reason", "")


# ──────────────────────────────────────────────
# (E) Entity consistency — deterministic stability
# ──────────────────────────────────────────────


def test_eval_entity_consistency_is_stable(wired_server):
    """Three identical inputs canonicalise to the same name three times in
    a row — that's the contract for `entity_dedup`."""
    payload = _call(wired_server, "memory_eval_entity_consistency",
                    {"mode": "fast"})
    if payload.get("status") == "not_implemented":
        pytest.skip(f"entity_dedup unavailable: {payload.get('reason')}")
    assert payload["scenarios_total"] == 3
    assert payload["scenarios_passed"] == 3, payload
    assert payload["llm_calls_during_eval"] == 0
    assert payload["network_calls_during_eval"] == 0


def test_locomo_without_dataset_reports_error(wired_server):
    result = _call(wired_server, 'memory_eval_locomo', {'limit': 1})
    assert result['status'] == 'error' and 'dataset_path' in result['reason']


def test_default_benchmark_does_not_require_owner_records(wired_server):
    before = wired_server.store.db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0]
    result = _call(wired_server, 'benchmark', {})
    assert result['total'] == result['passed'] == 5, result
    assert wired_server.store.db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0] == before
