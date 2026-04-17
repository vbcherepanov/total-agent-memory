"""Tests for src/file_context.py — v7.0 Phase C."""

import json
import sqlite3

import pytest

from file_context import FileContextGuard


@pytest.fixture
def fcdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'medium',
            description TEXT NOT NULL,
            context TEXT DEFAULT '',
            fix TEXT DEFAULT '',
            project TEXT DEFAULT 'general',
            tags TEXT DEFAULT '[]',
            status TEXT DEFAULT 'open',
            resolved_at TEXT,
            insight_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, type TEXT, content TEXT, context TEXT DEFAULT '',
            project TEXT DEFAULT 'general', tags TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active', confidence REAL DEFAULT 1.0,
            created_at TEXT, updated_at TEXT, recall_count INTEGER DEFAULT 0
        );
        CREATE TABLE rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, context TEXT, category TEXT,
            priority INTEGER DEFAULT 5, success_rate REAL DEFAULT 0.0,
            status TEXT DEFAULT 'active'
        );
    """)
    yield conn
    conn.close()


@pytest.fixture
def guard(fcdb):
    return FileContextGuard(fcdb)


def _add_error(db, *, desc, tags=None, project="general", severity="medium",
               status="open", context="", fix=""):
    db.execute(
        """INSERT INTO errors (session_id, category, severity, description,
           context, fix, project, tags, status, created_at)
           VALUES ('s', 'bug', ?, ?, ?, ?, ?, ?, ?, '2026-04-14T10:00:00Z')""",
        (severity, desc, context, fix, project,
         json.dumps(tags or []), status),
    )
    db.commit()


def _add_knowledge(db, *, content, tags=None, type="solution",
                    project="general", context=""):
    db.execute(
        """INSERT INTO knowledge (session_id, type, content, context, project,
           tags, status, confidence, created_at, recall_count)
           VALUES ('s', ?, ?, ?, ?, ?, 'active', 0.9,
                   '2026-04-14T10:00:00Z', 0)""",
        (type, content, context, project, json.dumps(tags or [])),
    )
    db.commit()


def _add_rule(db, *, content, context=""):
    db.execute(
        "INSERT INTO rules (content, context) VALUES (?, ?)",
        (content, context),
    )
    db.commit()


# ──────────────────────────────────────────────
# Empty path / no data
# ──────────────────────────────────────────────

def test_empty_path_returns_empty_warnings(guard):
    result = guard.get_file_warnings("")
    assert result["warnings"] == []
    assert result["risk_score"] == 0.0


def test_no_prior_context_returns_clean(guard):
    result = guard.get_file_warnings("src/fresh.py")
    assert result["warnings"] == []
    assert result["risk_score"] == 0.0
    assert "Proceed normally" in result["summary"]


# ──────────────────────────────────────────────
# Tag-based matching (strongest)
# ──────────────────────────────────────────────

def test_matches_error_by_exact_tag(guard, fcdb):
    _add_error(fcdb, desc="null pointer in auth",
               tags=["src/auth.py", "bug"], severity="high")
    result = guard.get_file_warnings("src/auth.py")
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["source"] == "error"
    assert result["warnings"][0]["severity"] == "high"
    assert result["risk_score"] > 0


def test_matches_knowledge_by_tag(guard, fcdb):
    _add_knowledge(fcdb, content="don't use global state here",
                   tags=["src/config.py"], type="lesson")
    result = guard.get_file_warnings("src/config.py")
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["source"] == "knowledge"
    assert result["warnings"][0]["severity"] == "high"  # lesson → high


def test_matches_tag_with_file_prefix(guard, fcdb):
    _add_error(fcdb, desc="fixed", tags=["file:src/auth.py"])
    result = guard.get_file_warnings("src/auth.py")
    assert len(result["warnings"]) == 1


# ──────────────────────────────────────────────
# Text-based matching
# ──────────────────────────────────────────────

def test_matches_path_in_description(guard, fcdb):
    _add_error(fcdb, desc="crash in src/auth.py at line 42")
    result = guard.get_file_warnings("src/auth.py")
    assert len(result["warnings"]) == 1


def test_matches_path_in_context(guard, fcdb):
    _add_error(fcdb, desc="race condition",
               context="affects src/queue/worker.py")
    result = guard.get_file_warnings("src/queue/worker.py")
    assert len(result["warnings"]) == 1


def test_matches_basename_only_when_distinctive(guard, fcdb):
    _add_error(fcdb, desc="segfault happens in my_distinct_module.py")
    # basename > 3 chars, distinctive
    result = guard.get_file_warnings("foo/my_distinct_module.py")
    assert len(result["warnings"]) == 1


def test_short_basename_does_not_false_positive(guard, fcdb):
    _add_error(fcdb, desc="generic error not related to any file path")
    result = guard.get_file_warnings("a.py")
    # Very short basename → no match on free text
    assert len(result["warnings"]) == 0


# ──────────────────────────────────────────────
# Risk score
# ──────────────────────────────────────────────

def test_risk_score_scales_with_count_and_severity(guard, fcdb):
    _add_error(fcdb, desc="minor issue", tags=["x.py"], severity="low",
               status="resolved")
    r1 = guard.get_file_warnings("x.py")["risk_score"]

    _add_error(fcdb, desc="critical bug", tags=["x.py"], severity="critical",
               status="open")
    r2 = guard.get_file_warnings("x.py")["risk_score"]

    assert r2 > r1


def test_risk_score_bounded_0_1(guard, fcdb):
    for i in range(20):
        _add_error(fcdb, desc=f"issue {i}", tags=["hot.py"],
                   severity="critical", status="open")
    risk = guard.get_file_warnings("hot.py")["risk_score"]
    assert 0.0 <= risk <= 1.0


# ──────────────────────────────────────────────
# Project filtering
# ──────────────────────────────────────────────

def test_project_filter_excludes_other_projects(guard, fcdb):
    _add_error(fcdb, desc="in p1", tags=["shared.py"], project="p1")
    _add_error(fcdb, desc="in p2", tags=["shared.py"], project="p2")

    result = guard.get_file_warnings("shared.py", project="p1")
    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["content"] == "in p1"


# ──────────────────────────────────────────────
# Rules scanning
# ──────────────────────────────────────────────

def test_related_rules_surface_when_path_mentioned(guard, fcdb):
    _add_rule(fcdb, content="never edit src/legacy.py directly — use migration")
    result = guard.get_file_warnings("src/legacy.py")
    assert len(result["related_rules"]) == 1


def test_rules_ignored_when_unrelated(guard, fcdb):
    _add_rule(fcdb, content="use snake_case for variables")
    result = guard.get_file_warnings("src/legacy.py")
    assert result["related_rules"] == []


# ──────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────

def test_summary_counts_errors_and_knowledge(guard, fcdb):
    _add_error(fcdb, desc="bug", tags=["m.py"], status="open")
    _add_error(fcdb, desc="another", tags=["m.py"], status="resolved")
    _add_knowledge(fcdb, content="convention", tags=["m.py"], type="convention")

    result = guard.get_file_warnings("m.py")
    assert "2 past error" in result["summary"]
    assert "1 unresolved" in result["summary"]
    assert "1 related lesson" in result["summary"]


# ──────────────────────────────────────────────
# Graceful degradation
# ──────────────────────────────────────────────

def test_no_errors_table_does_not_crash():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY, session_id TEXT, type TEXT, content TEXT,
            context TEXT DEFAULT '', project TEXT DEFAULT 'general',
            tags TEXT DEFAULT '[]', status TEXT DEFAULT 'active',
            confidence REAL DEFAULT 1.0, created_at TEXT,
            recall_count INTEGER DEFAULT 0
        );
    """)
    g = FileContextGuard(conn)
    # Should not raise even with no errors/rules tables
    result = g.get_file_warnings("any.py")
    assert result["warnings"] == []
    conn.close()
