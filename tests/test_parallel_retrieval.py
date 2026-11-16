"""Tests for v9.0 A1 parallel retrieval path in multi_repr_search.

The flag ``V9_PARALLEL_RETRIEVAL`` gates parallel per-representation
processing. These tests cover:
  1) flag OFF -> identical v8 behavior
  2) flag ON  -> same result set as v8
  3) flag ON  + one tier raises -> survives, others return
  4) flag ON  + multiple tiers raise -> no crash
  5) flag ON  parallel faster than sequential on mocked 50ms tiers
  6) flag ON  empty corpus -> empty result
  7) top_n propagates in parallel mode
  8) works when called from inside a running asyncio loop
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from memory_core.embedding_identity import EmbedIdentity


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def mrs_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    root = Path(__file__).parent.parent
    conn.executescript((root / "migrations" / "001_v5_schema.sql").read_text(encoding="utf-8"))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, project TEXT DEFAULT 'general',
            status TEXT DEFAULT 'active', created_at TEXT
        );
        """
    )
    conn.executescript((root / "migrations" / "002_multi_representation.sql").read_text(encoding="utf-8"))
    yield conn
    conn.close()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("V9_PARALLEL_RETRIEVAL", "1")
    yield


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setenv("V9_PARALLEL_RETRIEVAL", "0")
    yield


# ─────────────────────────── helpers ────────────────────────────


def _det_emb(text: str, dim: int = 8) -> list[float]:
    h = hashlib.md5(text.encode()).digest()
    return [b / 255.0 for b in h[:dim]]


def _seed(db, text: str, project: str = "demo") -> int:
    return db.execute(
        "INSERT INTO knowledge (content, project, status, created_at) "
        "VALUES (?, ?, 'active', '2026-04-14T00:00:00Z')",
        (text, project),
    ).lastrowid


def _add_repr(db, kid: int, repr_name: str, text: str, dim: int = 8):
    from multi_repr_store import MultiReprStore
    MultiReprStore(db).upsert(kid, repr_name, text, _det_emb(text, dim=dim), "fake")


def _seed_rich(db) -> tuple[int, int, int]:
    """Populate three knowledge rows with representations on all 4 types."""
    k1 = _seed(db, "k1 doc")
    k2 = _seed(db, "k2 doc")
    k3 = _seed(db, "k3 doc")
    for kid, tag in [(k1, "alpha"), (k2, "beta"), (k3, "gamma")]:
        _add_repr(db, kid, "summary", f"{tag} summary phrase")
        _add_repr(db, kid, "keywords", f"{tag} keywords list")
        _add_repr(db, kid, "questions", f"{tag} related questions")
        _add_repr(db, kid, "compressed", f"{tag} compressed form")
    return k1, k2, k3


# ─────────────────────────── tests ──────────────────────────────


def test_flag_off_matches_v8_behavior(flag_off, mrs_db):
    """Flag OFF path must be byte-identical to v8."""
    from multi_repr_search import search

    k1, k2, k3 = _seed_rich(mrs_db)
    q_emb = _det_emb("alpha summary phrase")

    result = search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared"))
    assert result, "expected at least one hit"
    # k1 has the matching 'alpha' representation on all 4 tiers -> top rank.
    assert result[0][0] == k1


def test_flag_on_returns_same_result_set_as_flag_off(mrs_db):
    """Flag ON and OFF must agree on the final set of (id, score) pairs."""
    from multi_repr_search import search

    k1, k2, k3 = _seed_rich(mrs_db)
    q_emb = _det_emb("alpha summary phrase")

    with patch.dict(os.environ, {"V9_PARALLEL_RETRIEVAL": "0"}):
        seq = search(mrs_db, q_emb, top_n=10, query_identity=EmbedIdentity("fake", "declared"))
    with patch.dict(os.environ, {"V9_PARALLEL_RETRIEVAL": "1"}):
        par = search(mrs_db, q_emb, top_n=10, query_identity=EmbedIdentity("fake", "declared"))

    # RRF fuse is deterministic given identical per-repr ranked lists, so the
    # ids AND fused scores should match exactly.
    assert [x[0] for x in seq] == [x[0] for x in par]
    for (ids, sseq), (idp, spar) in zip(seq, par):
        assert ids == idp
        assert abs(sseq - spar) < 1e-9


def test_flag_on_single_tier_failure_survives(flag_on, mrs_db):
    """If one representation's scorer raises, others still produce results."""
    from multi_repr_search import search
    import multi_repr_search as mrs_mod

    _seed_rich(mrs_db)
    q_emb = _det_emb("alpha summary phrase")

    real_score = mrs_mod._score_rows
    call_counter = {"n": 0}

    def flaky(rows, query_embedding, top_n):
        # Blow up on exactly one of the calls — mimics a scoring crash on one tier.
        call_counter["n"] += 1
        if call_counter["n"] == 2:
            raise RuntimeError("simulated scorer crash")
        return real_score(rows, query_embedding, top_n)

    with patch.object(mrs_mod, "_score_rows", side_effect=flaky):
        result = search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared"))

    # Three healthy tiers out of four — result must not be empty.
    assert result, "result should not be empty when only one tier fails"


def test_flag_on_multiple_tier_failures_no_crash(flag_on, mrs_db):
    """Two tiers raising must not crash the search — remaining tiers serve."""
    from multi_repr_search import search
    import multi_repr_search as mrs_mod

    _seed_rich(mrs_db)
    q_emb = _det_emb("alpha summary phrase")

    real_score = mrs_mod._score_rows
    call_counter = {"n": 0}

    def flakier(rows, query_embedding, top_n):
        call_counter["n"] += 1
        if call_counter["n"] in (1, 3):
            raise RuntimeError(f"scorer crash on call {call_counter['n']}")
        return real_score(rows, query_embedding, top_n)

    with patch.object(mrs_mod, "_score_rows", side_effect=flakier):
        result = search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared"))

    # Two healthy tiers still alive -> must yield something.
    assert result, "remaining tiers should still return results"


def test_flag_on_parallel_is_faster_than_sequential(mrs_db):
    """Synthetic: with the flag on the tier scorers overlap, with it off they run one at a time.

    Measured as peak concurrency, not wall time: a loaded CI runner stretches
    milliseconds, while overlapping scorers stay overlapping.
    """
    from multi_repr_search import search
    import multi_repr_search as mrs_mod

    _seed_rich(mrs_db)
    q_emb = _det_emb("alpha summary phrase")

    TIER_SLEEP = 0.05  # long enough for concurrent scorers to overlap
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def slow_score(rows, query_embedding, top_n):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(TIER_SLEEP)
        finally:
            with lock:
                state["active"] -= 1
        # Return a minimal non-empty ranked list so fusion runs.
        return [(1, 0.9)]

    def peak_concurrency(flag: str) -> int:
        state.update(active=0, peak=0)
        with patch.dict(os.environ, {"V9_PARALLEL_RETRIEVAL": flag}):
            with patch.object(mrs_mod, "_score_rows", side_effect=slow_score):
                assert search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared"))
        return state["peak"]

    assert peak_concurrency("0") == 1, "flag off must score one representation at a time"
    assert peak_concurrency("1") >= 2, "flag on must score representations concurrently"


def test_flag_on_empty_corpus_returns_empty(flag_on, mrs_db):
    """Empty representations table → [] without crashing."""
    from multi_repr_search import search

    q_emb = _det_emb("anything")
    assert search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared")) == []


def test_flag_on_respects_top_n(flag_on, mrs_db):
    """top_n must propagate through the parallel path into RRF fusion."""
    from multi_repr_search import search

    # Seed 5 docs so top_n actually caps.
    ids = []
    for tag in ("alpha", "beta", "gamma", "delta", "epsilon"):
        k = _seed(mrs_db, f"doc {tag}")
        _add_repr(mrs_db, k, "summary", f"{tag} phrase")
        _add_repr(mrs_db, k, "keywords", f"{tag} kw")
        ids.append(k)

    q_emb = _det_emb("alpha phrase")
    result = search(mrs_db, q_emb, top_n=2, query_identity=EmbedIdentity("fake", "declared"))
    assert len(result) <= 2


def test_flag_on_works_from_running_event_loop(flag_on, mrs_db):
    """Must not crash when called from code that already has a running loop."""
    from multi_repr_search import search

    _seed_rich(mrs_db)
    q_emb = _det_emb("alpha summary phrase")

    async def caller():
        # Call the sync `search()` from inside a running loop. The internal
        # _run_coroutine helper must detect the running loop and bounce to a
        # worker-thread loop instead of raising RuntimeError.
        return search(mrs_db, q_emb, top_n=5, query_identity=EmbedIdentity("fake", "declared"))

    result = asyncio.run(caller())
    assert result, "search() must work from inside a running event loop"
