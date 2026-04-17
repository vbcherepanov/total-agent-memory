"""Tests for src/temporal_kg.py — v7.0 Phase A."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from temporal_kg import TemporalKG


@pytest.fixture
def tkg_db():
    """SQLite DB with just the temporal KG schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migration = Path(__file__).parent.parent / "migrations" / "008_temporal_kg.sql"
    conn.executescript(migration.read_text())
    yield conn
    conn.close()


@pytest.fixture
def tkg(tkg_db):
    return TemporalKG(tkg_db)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────
# add_fact basics
# ──────────────────────────────────────────────

def test_add_fact_returns_id(tkg):
    fid = tkg.add_fact("auth_service", "uses", "JWT")
    assert isinstance(fid, str) and len(fid) == 32


def test_add_fact_stores_all_fields(tkg):
    fid = tkg.add_fact(
        "auth_service", "uses", "JWT",
        subject_name="Auth Service", object_name="JSON Web Token",
        confidence=0.9, context="Confirmed in src/auth.py",
        source="user", project="claude-total-memory",
    )
    rows = tkg.get_current(subject="auth_service")
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == fid
    assert r["predicate"] == "uses"
    assert r["object"] == "JWT"
    assert r["subject_name"] == "Auth Service"
    assert r["object_name"] == "JSON Web Token"
    assert r["confidence"] == 0.9
    assert r["context"] == "Confirmed in src/auth.py"
    assert r["source"] == "user"
    assert r["project"] == "claude-total-memory"
    assert r["valid_to"] is None


def test_add_fact_rejects_empty_fields(tkg):
    with pytest.raises(ValueError):
        tkg.add_fact("", "uses", "JWT")
    with pytest.raises(ValueError):
        tkg.add_fact("s", "", "o")
    with pytest.raises(ValueError):
        tkg.add_fact("s", "p", "")


def test_add_fact_rejects_invalid_confidence(tkg):
    with pytest.raises(ValueError):
        tkg.add_fact("s", "p", "o", confidence=1.5)
    with pytest.raises(ValueError):
        tkg.add_fact("s", "p", "o", confidence=-0.1)


def test_add_fact_idempotent_for_same_spo(tkg):
    fid1 = tkg.add_fact("a", "uses", "b")
    fid2 = tkg.add_fact("a", "uses", "b")
    assert fid1 == fid2
    rows = tkg.get_current(subject="a")
    assert len(rows) == 1


# ──────────────────────────────────────────────
# Invalidation (supersede)
# ──────────────────────────────────────────────

def test_add_fact_supersedes_previous_object(tkg):
    # Initially auth uses JWT
    old_id = tkg.add_fact("auth_service", "uses", "JWT")
    # Then migrates to session_tokens
    new_id = tkg.add_fact("auth_service", "uses", "session_tokens")

    current = tkg.get_current(subject="auth_service")
    assert len(current) == 1
    assert current[0]["object"] == "session_tokens"
    assert current[0]["id"] == new_id

    # Old assertion closed and linked to new
    all_history = tkg.timeline("auth_service")
    assert len(all_history) == 2
    old = [r for r in all_history if r["id"] == old_id][0]
    assert old["valid_to"] is not None
    assert old["superseded_by"] == new_id
    assert old["invalidation_reason"] == "replaced_by_newer_assertion"


def test_add_fact_does_not_supersede_when_disabled(tkg):
    tkg.add_fact("a", "likes", "x")
    tkg.add_fact("a", "likes", "y", invalidate_previous=False)
    current = tkg.get_current(subject="a", predicate="likes")
    # Both remain open
    assert len(current) == 2


def test_invalidate_fact_closes_assertion(tkg):
    tkg.add_fact("a", "uses", "b")
    closed = tkg.invalidate_fact("a", "uses", "b", reason="deprecated")
    assert closed == 1
    assert tkg.get_current(subject="a") == []

    history = tkg.timeline("a")
    assert len(history) == 1
    assert history[0]["valid_to"] is not None
    assert history[0]["invalidation_reason"] == "deprecated"


def test_invalidate_fact_on_missing_returns_zero(tkg):
    assert tkg.invalidate_fact("ghost", "p", "o") == 0


def test_invalidate_assertion_by_id(tkg):
    fid = tkg.add_fact("a", "uses", "b")
    assert tkg.invalidate_assertion(fid, reason="test") is True
    # Second call is a no-op
    assert tkg.invalidate_assertion(fid, reason="test") is False


# ──────────────────────────────────────────────
# Point-in-time queries
# ──────────────────────────────────────────────

def test_query_at_returns_facts_valid_at_timestamp(tkg):
    t0 = _iso(datetime(2026, 1, 1, tzinfo=timezone.utc))
    t1 = _iso(datetime(2026, 2, 1, tzinfo=timezone.utc))
    t2 = _iso(datetime(2026, 3, 1, tzinfo=timezone.utc))
    t3 = _iso(datetime(2026, 4, 1, tzinfo=timezone.utc))

    # At t1: auth uses JWT
    tkg.add_fact("auth", "uses", "JWT", valid_from=t1)
    # At t2: auth migrates to session_tokens
    tkg.add_fact("auth", "uses", "session_tokens", valid_from=t2)

    # t0: before any fact — empty
    assert tkg.query_at(t0, subject="auth") == []

    # Midway t1.5 (between t1=Feb 1 and t2=Mar 1): JWT
    t1_5 = _iso(datetime(2026, 2, 15, tzinfo=timezone.utc))
    rows = tkg.query_at(t1_5, subject="auth")
    assert len(rows) == 1
    assert rows[0]["object"] == "JWT"

    # At t3 (after migration): session_tokens
    rows = tkg.query_at(t3, subject="auth")
    assert len(rows) == 1
    assert rows[0]["object"] == "session_tokens"


def test_query_at_none_returns_current(tkg):
    tkg.add_fact("a", "uses", "b")
    tkg.add_fact("a", "uses", "c")  # supersedes
    rows = tkg.query_at(None, subject="a")
    assert len(rows) == 1
    assert rows[0]["object"] == "c"


def test_get_current_filters_by_project(tkg):
    tkg.add_fact("a", "uses", "b", project="p1")
    tkg.add_fact("a", "uses", "c", project="p2")
    rows_p1 = tkg.get_current(subject="a", project="p1")
    rows_p2 = tkg.get_current(subject="a", project="p2")
    assert len(rows_p1) == 1 and rows_p1[0]["object"] == "b"
    assert len(rows_p2) == 1 and rows_p2[0]["object"] == "c"


# ──────────────────────────────────────────────
# Timeline
# ──────────────────────────────────────────────

def test_timeline_returns_full_history_ordered(tkg):
    t1 = _iso(datetime(2026, 1, 1, tzinfo=timezone.utc))
    t2 = _iso(datetime(2026, 2, 1, tzinfo=timezone.utc))
    t3 = _iso(datetime(2026, 3, 1, tzinfo=timezone.utc))

    tkg.add_fact("auth", "uses", "basic_auth", valid_from=t1)
    tkg.add_fact("auth", "uses", "JWT", valid_from=t2)
    tkg.add_fact("auth", "uses", "session_tokens", valid_from=t3)

    history = tkg.timeline("auth")
    assert len(history) == 3
    assert [r["object"] for r in history] == ["basic_auth", "JWT", "session_tokens"]
    # First two are closed, last is open
    assert history[0]["valid_to"] is not None
    assert history[1]["valid_to"] is not None
    assert history[2]["valid_to"] is None


def test_timeline_filter_by_predicate(tkg):
    tkg.add_fact("x", "uses", "a")
    tkg.add_fact("x", "owns", "b")
    rows = tkg.timeline("x", predicate="uses")
    assert len(rows) == 1
    assert rows[0]["object"] == "a"


# ──────────────────────────────────────────────
# Diff
# ──────────────────────────────────────────────

def test_diff_detects_added_removed_changed(tkg):
    t1 = _iso(datetime(2026, 1, 1, tzinfo=timezone.utc))
    t2 = _iso(datetime(2026, 2, 1, tzinfo=timezone.utc))
    t3 = _iso(datetime(2026, 3, 1, tzinfo=timezone.utc))

    # At t1: {(a,uses,x), (b,owns,y)}
    tkg.add_fact("a", "uses", "x", valid_from=t1)
    tkg.add_fact("b", "owns", "y", valid_from=t1)
    # At t2: a migrates x→z, b retracted, c added
    tkg.add_fact("a", "uses", "z", valid_from=t2)
    tkg.invalidate_fact("b", "owns", "y", at=t2)
    tkg.add_fact("c", "uses", "w", valid_from=t2)

    d = tkg.diff(t1, t3)
    # c was added
    added_keys = {(r["subject"], r["predicate"], r["object"]) for r in d["added"]}
    assert ("c", "uses", "w") in added_keys
    # b removed
    removed_keys = {(r["subject"], r["predicate"], r["object"]) for r in d["removed"]}
    assert ("b", "owns", "y") in removed_keys
    # a changed x→z
    changes = [(c["subject"], c["predicate"], c["from_object"], c["to_object"])
               for c in d["changed"]]
    assert ("a", "uses", "x", "z") in changes


# ──────────────────────────────────────────────
# Stats
# ──────────────────────────────────────────────

def test_stats_counts(tkg):
    tkg.add_fact("a", "uses", "b")
    tkg.add_fact("a", "uses", "c")  # supersedes b
    tkg.add_fact("x", "owns", "y")
    s = tkg.stats()
    assert s["total_assertions"] == 3
    assert s["currently_valid"] == 2  # a->c and x->y
    assert s["closed"] == 1
    assert s["distinct_subjects"] == 2


def test_stats_filter_by_project(tkg):
    tkg.add_fact("a", "p", "b", project="p1")
    tkg.add_fact("x", "p", "y", project="p2")
    assert tkg.stats(project="p1")["total_assertions"] == 1
    assert tkg.stats(project="p2")["total_assertions"] == 1


# ──────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────

def test_supersede_only_within_same_project(tkg):
    tkg.add_fact("a", "uses", "b", project="p1")
    # Different project → should NOT supersede
    tkg.add_fact("a", "uses", "c", project="p2")
    assert len(tkg.get_current(subject="a", project="p1")) == 1
    assert len(tkg.get_current(subject="a", project="p2")) == 1


def test_readding_invalidated_fact_reopens_as_new_assertion(tkg):
    t1 = _iso(datetime(2026, 1, 1, tzinfo=timezone.utc))
    t2 = _iso(datetime(2026, 2, 1, tzinfo=timezone.utc))
    t3 = _iso(datetime(2026, 3, 1, tzinfo=timezone.utc))

    tkg.add_fact("a", "uses", "b", valid_from=t1)
    tkg.invalidate_fact("a", "uses", "b", at=t2)
    new_id = tkg.add_fact("a", "uses", "b", valid_from=t3)

    # Two distinct assertions in history
    history = tkg.timeline("a")
    assert len(history) == 2
    # Current is the reopened one
    current = tkg.get_current(subject="a")
    assert len(current) == 1
    assert current[0]["id"] == new_id
