"""Scope names of automatically created rules and the migration that repairs stored ones."""

import sqlite3
from pathlib import Path

import pytest

from rule_scopes import GENERAL_PROJECT, automatic_rule_scope, normalize_rule_project

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "053_auto_rule_scopes.sql"


@pytest.mark.parametrize("project", [None, "", " ", "\t\n"])
def test_blank_project_normalizes_to_general(project):
    assert normalize_rule_project(project) == GENERAL_PROJECT


@pytest.mark.parametrize("project, expected", [(" alpha ", "alpha"), ("alpha", "alpha"), ("general", "general")])
def test_project_is_trimmed(project, expected):
    assert normalize_rule_project(project) == expected


@pytest.mark.parametrize("project", [1, b"alpha", ["alpha"]])
def test_non_text_project_is_rejected(project):
    with pytest.raises(ValueError):
        normalize_rule_project(project)


@pytest.mark.parametrize(
    "project, scope",
    [(None, "global"), ("", "global"), ("  ", "global"), ("general", "global"),
     ("alpha", "project:alpha"), (" alpha ", "project:alpha")],
)
def test_automatic_scope(project, scope):
    assert automatic_rule_scope(project) == scope


@pytest.fixture
def rules_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE rules (id INTEGER PRIMARY KEY, session_id TEXT, project TEXT, scope TEXT)")
    yield conn
    conn.close()


def _insert(conn, session_id, project, scope):
    return conn.execute(
        "INSERT INTO rules (session_id, project, scope) VALUES (?, ?, ?)", (session_id, project, scope)
    ).lastrowid


def test_migration_rewrites_only_legacy_auto_rules(rules_db):
    legacy = [_insert(rules_db, "auto_consolidate", p, "project") for p in (None, "", "  ", "general", " alpha ")]
    manual = _insert(rules_db, "manual", "beta", "project")
    current = _insert(rules_db, "auto_consolidate", "gamma", "project:gamma")
    sql = MIGRATION.read_text(encoding="utf-8")

    for _ in range(2):
        rules_db.executescript(sql)

    rows = [tuple(rules_db.execute("SELECT project, scope FROM rules WHERE id=?", (i,)).fetchone()) for i in legacy]
    assert rows == [("general", "global")] * 4 + [("alpha", "project:alpha")]
    assert tuple(rules_db.execute("SELECT project, scope FROM rules WHERE id=?", (manual,)).fetchone()) == ("beta", "project")
    assert tuple(rules_db.execute("SELECT project, scope FROM rules WHERE id=?", (current,)).fetchone()) == ("gamma", "project:gamma")


WHITESPACE_REPAIR = Path(__file__).resolve().parent.parent / "migrations" / "057_auto_rule_scope_whitespace.sql"


def test_whitespace_repair_matches_python_normalization(rules_db):
    projects = ["\t\n", " \t\r\n", " \talpha\r\n", "general\n", "alpha"]
    ids = [_insert(rules_db, "auto_consolidate", p, "project") for p in projects]
    manual = _insert(rules_db, "manual", "\tbeta\t", "project")
    for _ in range(2):
        rules_db.executescript(MIGRATION.read_text(encoding="utf-8"))
        rules_db.executescript(WHITESPACE_REPAIR.read_text(encoding="utf-8"))

    rows = [tuple(rules_db.execute("SELECT project, scope FROM rules WHERE id=?", (i,)).fetchone()) for i in ids]
    assert rows == [(normalize_rule_project(p), automatic_rule_scope(p)) for p in projects]
    assert rows[0] == (GENERAL_PROJECT, "global")
    assert tuple(rules_db.execute("SELECT project, scope FROM rules WHERE id=?", (manual,)).fetchone()) == ("\tbeta\t", "project")
