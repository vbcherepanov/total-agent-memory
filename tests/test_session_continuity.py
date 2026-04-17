"""Tests for src/session_continuity.py — v7.0 Phase G."""

import sqlite3
from pathlib import Path

import pytest

from session_continuity import SessionContinuity


@pytest.fixture
def sc_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migration = Path(__file__).parent.parent / "migrations" / "010_session_continuity.sql"
    conn.executescript(migration.read_text())
    yield conn
    conn.close()


@pytest.fixture
def sc(sc_db):
    return SessionContinuity(sc_db)


# ──────────────────────────────────────────────
# session_end
# ──────────────────────────────────────────────

def test_session_end_stores_summary(sc):
    r = sc.session_end(
        "sess_1", "Worked on temporal KG",
        highlights=["Phase A done"], pitfalls=["sqlite lock"],
        next_steps=["wire up MCP"], open_questions=["How to migrate?"],
        project="claude-total-memory",
    )
    assert r["id"]
    assert r["next_steps_count"] == 1


def test_session_end_validates_input(sc):
    with pytest.raises(ValueError):
        sc.session_end("", "summary")
    with pytest.raises(ValueError):
        sc.session_end("sid", "")


# ──────────────────────────────────────────────
# session_init
# ──────────────────────────────────────────────

def test_session_init_returns_none_when_empty(sc):
    assert sc.session_init(project="x") is None


def test_session_init_returns_most_recent_unconsumed(sc):
    sc.session_end("s1", "older", project="p")
    sc.session_end("s2", "newest", project="p",
                   next_steps=["continue"])
    result = sc.session_init(project="p")
    assert result["session_id"] == "s2"
    assert result["next_steps"] == ["continue"]


def test_session_init_marks_consumed_by_default(sc):
    sc.session_end("s1", "summary", project="p")
    first = sc.session_init(project="p")
    assert first is not None
    # Second call → no unconsumed remaining
    assert sc.session_init(project="p") is None


def test_session_init_can_skip_consumption(sc):
    sc.session_end("s1", "summary", project="p")
    sc.session_init(project="p", mark_consumed=False)
    # Still available
    result2 = sc.session_init(project="p")
    assert result2 is not None


def test_session_init_filters_by_project(sc):
    sc.session_end("s1", "for p1", project="p1")
    sc.session_end("s2", "for p2", project="p2")
    r1 = sc.session_init(project="p1")
    r2 = sc.session_init(project="p2")
    assert r1["summary"] == "for p1"
    assert r2["summary"] == "for p2"


def test_session_init_can_exclude_pitfalls(sc):
    sc.session_end("s1", "sum", pitfalls=["watch out"], project="p")
    r = sc.session_init(project="p", include_pitfalls=False)
    assert r["pitfalls"] == []


def test_session_init_parses_json_fields(sc):
    sc.session_end(
        "s1", "sum",
        highlights=["h1", "h2"],
        pitfalls=["p1"],
        next_steps=["n1", "n2", "n3"],
        open_questions=["q1"],
        project="p",
    )
    r = sc.session_init(project="p")
    assert r["highlights"] == ["h1", "h2"]
    assert r["pitfalls"] == ["p1"]
    assert r["next_steps"] == ["n1", "n2", "n3"]
    assert r["open_questions"] == ["q1"]


# ──────────────────────────────────────────────
# Listing / stats
# ──────────────────────────────────────────────

def test_list_summaries(sc):
    for i in range(3):
        sc.session_end(f"s{i}", f"summary {i}", project="p")
    rows = sc.list_summaries(project="p")
    assert len(rows) == 3
    assert rows[0]["summary"] == "summary 2"  # newest first


def test_stats(sc):
    sc.session_end("s1", "a", project="p")
    sc.session_end("s2", "b", project="p")
    sc.session_init(project="p")  # consume most recent
    s = sc.stats(project="p")
    assert s["total_summaries"] == 2
    assert s["pending"] == 1
    assert s["consumed"] == 1


def test_mark_unconsumed_replays_summary(sc):
    r = sc.session_end("s1", "sum", project="p")
    sc.session_init(project="p")
    # Nothing left
    assert sc.session_init(project="p") is None
    # Reopen
    assert sc.mark_unconsumed(r["id"]) is True
    assert sc.session_init(project="p") is not None
