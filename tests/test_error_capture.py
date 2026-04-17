"""Tests for src/error_capture.py — v7.0 Phase D."""

import sqlite3

import pytest

from error_capture import ErrorCapture


@pytest.fixture
def ec_db():
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
        CREATE TABLE rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, content TEXT, context TEXT, category TEXT,
            scope TEXT DEFAULT 'global', priority INTEGER DEFAULT 5,
            source_insight_id INTEGER, project TEXT DEFAULT 'general',
            tags TEXT DEFAULT '[]', status TEXT DEFAULT 'active',
            fire_count INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0, success_rate REAL DEFAULT 0.0,
            last_fired TEXT, created_at TEXT, updated_at TEXT
        );
    """)
    yield conn
    conn.close()


@pytest.fixture
def ec(ec_db):
    return ErrorCapture(ec_db, consolidate_threshold=3)


def _sample(**overrides):
    base = {
        "file": "src/auth.py",
        "error": "sqlite3.OperationalError: database is locked",
        "root_cause": "DDL during active transaction",
        "fix": "commit before ALTER TABLE",
        "pattern": "sqlite-locked-during-ddl",
    }
    base.update(overrides)
    return base


# ──────────────────────────────────────────────
# Capture
# ──────────────────────────────────────────────

def test_learn_error_creates_row(ec, ec_db):
    result = ec.learn_error(**_sample())
    assert result["error_id"] > 0
    assert result["pattern"] == "sqlite-locked-during-ddl"

    row = ec_db.execute("SELECT * FROM errors").fetchone()
    assert row["description"].startswith("sqlite3.OperationalError")
    assert "commit before ALTER" in row["fix"]
    assert "file:src/auth.py" in row["tags"]
    assert "pattern:sqlite-locked-during-ddl" in row["tags"]


def test_learn_error_rejects_missing_fields(ec):
    for missing in ["file", "error", "root_cause", "fix", "pattern"]:
        s = _sample()
        s[missing] = ""
        with pytest.raises(ValueError):
            ec.learn_error(**s)


def test_learn_error_stores_severity_and_category(ec, ec_db):
    ec.learn_error(**_sample(), severity="high", category="security")
    row = ec_db.execute("SELECT severity, category FROM errors").fetchone()
    assert row["severity"] == "high"
    assert row["category"] == "security"


# ──────────────────────────────────────────────
# Auto-consolidation
# ──────────────────────────────────────────────

def test_below_threshold_no_rule_created(ec, ec_db):
    for _ in range(2):
        r = ec.learn_error(**_sample())
        assert r["consolidated"] is False
    rules = ec_db.execute("SELECT * FROM rules").fetchall()
    assert len(rules) == 0


def test_threshold_reached_creates_rule(ec, ec_db):
    results = []
    for _ in range(3):
        results.append(ec.learn_error(**_sample()))
    # First two: not consolidated; third: consolidated
    assert results[0]["consolidated"] is False
    assert results[1]["consolidated"] is False
    assert results[2]["consolidated"] is True
    assert results[2]["rule_id"] is not None

    rules = ec_db.execute("SELECT * FROM rules WHERE status = 'active'").fetchall()
    assert len(rules) == 1
    rule = rules[0]
    assert "sqlite-locked-during-ddl" in rule["content"]
    assert "[pattern:sqlite-locked-during-ddl]" in rule["context"]


def test_consolidation_is_idempotent(ec, ec_db):
    for _ in range(5):
        ec.learn_error(**_sample())
    # Only one rule created
    rules = ec_db.execute("SELECT * FROM rules WHERE status = 'active'").fetchall()
    assert len(rules) == 1


def test_consolidation_links_source_errors_via_insight_id(ec, ec_db):
    for _ in range(3):
        ec.learn_error(**_sample())
    # All 3 should have insight_id pointing to rule
    rows = ec_db.execute("SELECT insight_id FROM errors").fetchall()
    ids = [r["insight_id"] for r in rows]
    assert len(set(ids)) == 1  # all same rule
    assert ids[0] is not None


def test_different_patterns_consolidate_independently(ec, ec_db):
    for _ in range(3):
        ec.learn_error(**_sample(pattern="pattern-A"))
    for _ in range(2):
        ec.learn_error(**_sample(pattern="pattern-B"))
    rules = ec_db.execute("SELECT * FROM rules").fetchall()
    assert len(rules) == 1  # only A reached threshold


def test_project_scoped_consolidation(ec, ec_db):
    for _ in range(3):
        ec.learn_error(**_sample(pattern="p1", project="projA"))
    for _ in range(2):
        ec.learn_error(**_sample(pattern="p1", project="projB"))
    rules = ec_db.execute(
        "SELECT * FROM rules WHERE project = 'projA' AND status = 'active'"
    ).fetchall()
    assert len(rules) == 1
    rules_b = ec_db.execute(
        "SELECT * FROM rules WHERE project = 'projB' AND status = 'active'"
    ).fetchall()
    assert len(rules_b) == 0


# ──────────────────────────────────────────────
# Queries
# ──────────────────────────────────────────────

def test_pattern_frequency_sorted_descending(ec):
    for _ in range(4):
        ec.learn_error(**_sample(pattern="freq-a"))
    for _ in range(2):
        ec.learn_error(**_sample(pattern="freq-b"))
    ec.learn_error(**_sample(pattern="freq-c"))

    freq = ec.pattern_frequency()
    patterns = [f["pattern"] for f in freq]
    assert patterns[0] == "freq-a"
    assert patterns[1] == "freq-b"
    assert patterns[2] == "freq-c"
    assert freq[0]["count"] == 4


def test_rules_for_pattern(ec):
    for _ in range(3):
        ec.learn_error(**_sample(pattern="looked-up"))
    rules = ec.rules_for_pattern("looked-up")
    assert len(rules) == 1
    assert rules[0]["priority"] == 7


def test_resolve_marks_error_resolved(ec, ec_db):
    r = ec.learn_error(**_sample())
    assert ec.resolve(r["error_id"], note="fixed") is True
    row = ec_db.execute("SELECT status, resolved_at FROM errors").fetchone()
    assert row["status"] == "resolved"
    assert row["resolved_at"] is not None
    # Second call no-op
    assert ec.resolve(r["error_id"]) is False


def test_custom_threshold(ec_db):
    low = ErrorCapture(ec_db, consolidate_threshold=2)
    for _ in range(2):
        r = low.learn_error(**_sample(pattern="quick"))
    assert r["consolidated"] is True
