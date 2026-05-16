"""v9.0 D9 — `ctm-lookup` / `lookup-memory` CLI."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_total_memory.lookup as lookup  # noqa: E402


def _make_db(tmp_path: Path, *, with_fts: bool = True) -> Path:
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            project TEXT,
            type TEXT,
            content TEXT,
            context TEXT,
            tags TEXT,
            confidence REAL,
            created_at TEXT,
            last_confirmed TEXT,
            recall_count INTEGER,
            status TEXT,
            branch TEXT
        )
        """
    )
    if with_fts:
        conn.execute(
            "CREATE VIRTUAL TABLE knowledge_fts USING fts5(content, content='knowledge', content_rowid='id')"
        )
    rows = [
        ("vito", "fact", "vito uses Postgres for primary storage", '["reusable","postgres"]', "active"),
        ("vito", "solution", "Fix for slow Wave query: add index on user_id", '["reusable","postgres","index"]', "active"),
        ("learning-project", "fact", "Anki SRS interval is doubled after good review", '["anki"]', "active"),
        ("vito", "fact", "Old archived note about lemons", '["fruit"]', "archived"),
    ]
    for project, typ, content, tags, status in rows:
        conn.execute(
            "INSERT INTO knowledge (session_id, project, type, content, context, tags, "
            "confidence, created_at, last_confirmed, recall_count, status, branch) "
            "VALUES (?,?,?,?,?,?,1.0,'2026-04-25T00:00:00','2026-04-25T00:00:00',0,?,'')",
            ("s1", project, typ, content, "", tags, status),
        )
    if with_fts:
        conn.execute(
            "INSERT INTO knowledge_fts(rowid, content) "
            "SELECT id, content FROM knowledge"
        )
    conn.commit()
    conn.close()
    return db


def test_db_path_resolution_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TAM_MEMORY_DIR", str(tmp_path))
    p = lookup._resolve_db_path()
    assert p == tmp_path / "memory.db"


def test_search_finds_match_via_fts(tmp_path):
    db = _make_db(tmp_path, with_fts=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = lookup.search(conn, "Postgres", limit=10)
    assert any("Postgres" in r["content"] for r in out)


def test_search_filters_archived_rows(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = lookup.search(conn, "lemons", limit=10)
    # archived row must NOT appear
    assert all("lemon" not in r["content"].lower() for r in out)


def test_search_filters_by_project(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = lookup.search(conn, "review", project="learning-project", limit=10)
    assert all(r["project"] == "learning-project" for r in out)


def test_search_filters_by_type(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = lookup.search(conn, "Postgres", types=["solution"], limit=10)
    assert all(r["type"] == "solution" for r in out)


def test_search_filters_by_tag(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = lookup.search(conn, "query", tags=["index"], limit=10)
    assert all('"index"' in (r["tags"] or "") for r in out)


def test_search_falls_back_when_fts_absent(tmp_path):
    db = _make_db(tmp_path, with_fts=False)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    out = lookup.search(conn, "Postgres", limit=10)
    assert any("Postgres" in r["content"] for r in out)


def test_render_human_handles_empty():
    assert "no matches" in lookup.render_human([]).lower()


def test_render_human_includes_type_and_project(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = lookup.search(conn, "Postgres", limit=2)
    text = lookup.render_human(rows)
    assert "[fact|vito]" in text or "[solution|vito]" in text


def test_main_json_output(monkeypatch, tmp_path, capsys):
    db = _make_db(tmp_path)
    rc = lookup.main(["--db", str(db), "--json", "Postgres"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert any("Postgres" in r["content"] for r in parsed)


def test_main_human_output(monkeypatch, tmp_path, capsys):
    db = _make_db(tmp_path)
    rc = lookup.main(["--db", str(db), "Postgres"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Postgres" in out


def test_main_missing_db_exits_nonzero(tmp_path, capsys):
    bogus = tmp_path / "nonexistent.db"
    with pytest.raises(SystemExit) as exc:
        lookup.main(["--db", str(bogus), "Postgres"])
    assert exc.value.code == 2


def test_fts_query_quotes_special_chars():
    assert lookup._fts_query('foo bar') == '"foo" "bar"'
    assert lookup._fts_query('hello "world"') == '"hello" """world"""'
    assert lookup._fts_query('') == '""'
