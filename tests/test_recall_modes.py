"""Tests for memory_recall mode='index' and mode='timeline'.

Covers the standalone transforms in ``recall_modes.py`` plus an
integration check that the dispatcher wires them in without breaking
backward compatibility (``mode`` default = ``search``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ── unit: index_response ─────────────────────────────────────


def _make_item(i: int, content: str, score: float = 0.5, **extra):
    base = {
        "id": i,
        "content": content,
        "score": score,
        "type": extra.pop("type", "fact"),
        "project": extra.pop("project", "demo"),
        "created_at": extra.pop("created_at", f"2026-04-{i:02d}T00:00:00Z"),
        "session_id": extra.pop("session_id", "s1"),
        "context": extra.pop("context", "ctx"),
        "tags": extra.pop("tags", []),
    }
    base.update(extra)
    return base


def test_mode_index_returns_only_metadata():
    from recall_modes import index_response

    raw = {
        "query": "auth",
        "results": {
            "fact": [_make_item(1, "jwt refresh tokens ok", score=0.8, context="WHY")],
        },
    }
    out = index_response(raw)
    assert out["mode"] == "index"
    assert out["total"] == 1
    [entry] = out["results"]
    assert set(entry.keys()) == {"id", "title", "score", "type", "project", "created_at"}
    # Explicitly verify no content/context leakage.
    assert "content" not in entry and "context" not in entry
    assert "session_id" not in entry and "tags" not in entry


@pytest.mark.parametrize("compact", [False, True])
def test_modes_preserve_type_from_group_without_mutating_input(compact):
    from recall_modes import index_response, timeline_response

    field = "title" if compact else "content"
    original = {"id": 1, field: "Use ClickHouse for analytics", "score": 1.0}
    raw = {"results": {"decision": [original]}}
    indexed = index_response(raw)
    timeline = timeline_response(raw, None, neighbors=0)
    assert indexed["results"][0]["type"] == "decision"
    assert timeline["results"][0]["type"] == "decision"
    assert "type" not in original


def test_mode_index_title_truncated_to_80():
    from recall_modes import index_response

    long_body = "x" * 200
    raw = {"results": {"fact": [_make_item(1, long_body)]}}
    [entry] = index_response(raw)["results"]
    # 80 chars + "..." suffix
    assert entry["title"].endswith("...")
    assert len(entry["title"]) == 83


def test_mode_index_title_uses_first_line():
    from recall_modes import index_response

    raw = {"results": {"fact": [_make_item(1, "first line\nsecond line\nthird")]}}
    [entry] = index_response(raw)["results"]
    assert entry["title"] == "first line"


def test_mode_index_respects_limit_via_caller():
    """``index_response`` does not re-truncate — it trusts the underlying
    ``Recall.search`` limit. We verify that 3 items in → 3 items out, sorted
    by score desc across type groups.
    """
    from recall_modes import index_response

    raw = {
        "results": {
            "fact": [_make_item(1, "a", score=0.9), _make_item(2, "b", score=0.3)],
            "solution": [_make_item(3, "c", score=0.6)],
        },
    }
    out = index_response(raw)
    assert out["total"] == 3
    scores = [e["score"] for e in out["results"]]
    assert scores == sorted(scores, reverse=True)


def test_mode_index_skips_cognitive_expansion(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    """Dispatcher must not attach cognitive/expansion blocks for index mode."""
    # Use a real Store so recall.search works, but intercept cognitive.
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)

    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)

    s = server.Store(database=store_backend)
    server.store = s
    server.recall = server.Recall(s)
    server.SID = "sess-index-1"
    server.BRANCH = ""

    s.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
        (server.SID, "2026-04-14T00:00:00Z", "demo", "open"),
    )
    s.db.commit()

    s.save_knowledge(
        sid=server.SID,
        content="Use Argon2id for password hashing in auth layer.",
        ktype="solution",
        project="demo",
        tags=["auth", "security"],
    )

    # Spy on cognitive enrichment — must not be called for index mode.
    cognitive_called = {"yes": False}

    def fake_get_v5(name, db):
        if name == "cognitive":
            cognitive_called["yes"] = True
        raise RuntimeError("blocked for test")

    monkeypatch.setattr(server, "_get_v5", fake_get_v5)

    out_raw = asyncio.run(server._do("memory_recall", {
        "query": "password hashing", "project": "demo",
        "mode": "index", "limit": 5,
    }))
    out = json.loads(out_raw)
    assert out["mode"] == "index"
    # Cognitive should be absent (skipped) and must not have been called.
    assert "cognitive" not in out
    assert cognitive_called["yes"] is False
    # Each entry must be minimal.
    if out["results"]:
        entry = out["results"][0]
        assert "content" not in entry
        assert "context" not in entry

    try:
        s.db.close()
    except Exception:
        pass


# ── unit: timeline_response ──────────────────────────────────


class _FakeStore:
    """Minimal stand-in for Store with just a ``db`` exposing fetchall rows."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

        class _DB:
            def __init__(self, parent):
                self._parent = parent

            def execute(self, sql: str, params: tuple):
                return _FakeCursor(self._parent, sql, params)

        self.db = _DB(self)


class _FakeRow(dict):
    """dict subclass that mimics sqlite3.Row's ``keys()`` + ``__getitem__``."""

    def keys(self):  # noqa: D401
        return super().keys()


class _FakeCursor:
    def __init__(self, store: _FakeStore, sql: str, params: tuple):
        self._store = store
        self._sql = sql
        self._params = params

    def fetchall(self):
        rows = [_FakeRow(r) for r in self._store._rows]
        sql = self._sql
        # Very light SQL interpretation — good enough for the neighbour logic.
        before = "created_at < ?" in sql
        after = "created_at > ?" in sql
        session_filtered = "session_id=?" in sql

        if session_filtered:
            sid = self._params[0]
            pivot_ca = self._params[1]
            limit = self._params[-1]
            filt = [r for r in rows if r.get("session_id") == sid and r.get("status") == "active"]
        else:
            pivot_ca = self._params[0]
            limit = self._params[-1]
            filt = [r for r in rows if r.get("status") == "active"]

        if before:
            filt = [r for r in filt if (r.get("created_at") or "") < pivot_ca]
            filt.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        elif after:
            filt = [r for r in filt if (r.get("created_at") or "") > pivot_ca]
            filt.sort(key=lambda r: r.get("created_at") or "")

        return filt[:limit]


def test_mode_timeline_adds_neighbors():
    from recall_modes import timeline_response

    # Session s1 has 5 records by created_at. Anchor hit = id=3.
    all_rows = [
        {"id": 1, "session_id": "s1", "type": "fact", "content": "c1",
         "project": "demo", "created_at": "2026-04-10T10:00:00Z", "status": "active"},
        {"id": 2, "session_id": "s1", "type": "fact", "content": "c2",
         "project": "demo", "created_at": "2026-04-10T10:05:00Z", "status": "active"},
        {"id": 3, "session_id": "s1", "type": "solution", "content": "hit",
         "project": "demo", "created_at": "2026-04-10T10:10:00Z", "status": "active"},
        {"id": 4, "session_id": "s1", "type": "fact", "content": "c4",
         "project": "demo", "created_at": "2026-04-10T10:15:00Z", "status": "active"},
        {"id": 5, "session_id": "s1", "type": "fact", "content": "c5",
         "project": "demo", "created_at": "2026-04-10T10:20:00Z", "status": "active"},
    ]
    fake = _FakeStore(all_rows)

    raw = {
        "results": {
            "solution": [_make_item(3, "hit", score=0.9,
                                    session_id="s1",
                                    created_at="2026-04-10T10:10:00Z")],
        },
    }
    out = timeline_response(raw, fake, neighbors=2, limit=5)
    assert out["mode"] == "timeline"
    ids = [e["id"] for e in out["results"]]
    # Anchor plus 2 before + 2 after — ordered chronologically.
    assert ids == [1, 2, 3, 4, 5]
    roles = [e.get("role") for e in out["results"]]
    assert roles.count("hit") == 1
    assert roles.count("neighbor") == 4


def test_mode_timeline_no_duplicates():
    from recall_modes import timeline_response

    all_rows = [
        {"id": i, "session_id": "s1", "type": "fact", "content": f"c{i}",
         "project": "demo", "created_at": f"2026-04-10T10:{i:02d}:00Z",
         "status": "active"}
        for i in range(1, 6)
    ]
    fake = _FakeStore(all_rows)

    # Two hits — id=2 and id=4 — with overlapping neighbours.
    raw = {
        "results": {
            "fact": [
                _make_item(2, "c2", score=0.8, session_id="s1",
                           created_at="2026-04-10T10:02:00Z"),
                _make_item(4, "c4", score=0.7, session_id="s1",
                           created_at="2026-04-10T10:04:00Z"),
            ],
        },
    }
    out = timeline_response(raw, fake, neighbors=2, limit=5)
    ids = [e["id"] for e in out["results"]]
    # No duplicates despite overlapping windows.
    assert len(ids) == len(set(ids))
    # And both anchors must still be marked role=hit.
    hits = [e for e in out["results"] if e.get("role") == "hit"]
    assert {h["id"] for h in hits} == {2, 4}


def test_mode_timeline_falls_back_when_no_session_neighbors():
    """When ``session_id`` is empty, neighbour lookup falls back to global
    chronology so the hit still gets context."""
    from recall_modes import timeline_response

    all_rows = [
        {"id": 10, "session_id": "other", "type": "fact", "content": "earlier",
         "project": "demo", "created_at": "2026-04-10T09:00:00Z", "status": "active"},
        {"id": 20, "session_id": "other", "type": "fact", "content": "later",
         "project": "demo", "created_at": "2026-04-10T11:00:00Z", "status": "active"},
    ]
    fake = _FakeStore(all_rows)

    raw = {
        "results": {
            "fact": [_make_item(99, "anchor", score=0.9,
                                session_id="",  # no session info
                                created_at="2026-04-10T10:00:00Z")],
        },
    }
    out = timeline_response(raw, fake, neighbors=1, limit=5)
    ids = [e["id"] for e in out["results"]]
    # Fallback must fetch one before and one after the anchor.
    assert 10 in ids
    assert 20 in ids
    assert 99 in ids


# ── integration: backward compat ─────────────────────────────


def test_mode_search_backward_compat(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    """Default call (no ``mode=``) must return the legacy ``results`` dict
    shape grouped by type — existing callers break otherwise.
    """
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)

    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    s = server.Store(database=store_backend)
    server.store = s
    server.recall = server.Recall(s)
    server.SID = "sess-compat-1"
    server.BRANCH = ""
    s.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
        (server.SID, "2026-04-14T00:00:00Z", "demo", "open"),
    )
    s.db.commit()
    s.save_knowledge(
        sid=server.SID,
        content="Use Postgres row-level security for multi-tenant SaaS.",
        ktype="solution", project="demo", tags=["db"],
    )

    # Avoid cognitive engine side effects.
    monkeypatch.setattr(server, "_get_v5", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError()))

    raw = asyncio.run(server._do("memory_recall", {
        "query": "row-level security", "project": "demo",
    }))
    out = json.loads(raw)
    assert "results" in out
    # Legacy shape: results is a dict grouped by type, not a flat list.
    assert isinstance(out["results"], dict)
    # ``mode`` key must not have leaked into the legacy response.
    assert out.get("mode") != "index"
    assert out.get("mode") != "timeline"

    try:
        s.db.close()
    except Exception:
        pass
