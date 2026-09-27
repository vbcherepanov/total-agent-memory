"""Tests for compressed representation — the 5th view (raw+summary+keywords+questions+compressed)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

# These tests stub the LLM seam; `llm_enabled` skips the has_llm() probe
# so they do not depend on a live Ollama being present on the machine.
pytestmark = pytest.mark.usefixtures("llm_enabled")


@pytest.fixture
def cmp_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    root = Path(__file__).parent.parent
    conn.executescript((root / "migrations" / "001_v5_schema.sql").read_text())
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, status TEXT DEFAULT 'active', created_at TEXT
        );
        """
    )
    conn.executescript((root / "migrations" / "002_multi_representation.sql").read_text())
    conn.executescript((root / "migrations" / "005_representations_queue.sql").read_text())
    yield conn
    conn.close()


def _fake_emb(text: str, dim: int = 8) -> list[float]:
    h = hashlib.md5(text.encode()).digest()
    return [b / 255.0 for b in h[:dim]]


def _add(db, content: str) -> int:
    return db.execute(
        "INSERT INTO knowledge (content, created_at) VALUES (?, '2026-04-14T00:00:00Z')",
        (content,),
    ).lastrowid


# ──────────────────────────────────────────────
# representations.generate_representations — compressed key
# ──────────────────────────────────────────────


def test_generate_includes_compressed_for_long_content(monkeypatch):
    from representations import generate_representations

    def fake_llm(prompt: str, **_):
        p = prompt.lower()
        if "compressed" in p or "rewrite the text" in p:
            return "compressed form here"
        if "summar" in p:
            return "short"
        if "keyword" in p:
            return "a, b, c"
        if "question" in p:
            return "Q?\nR?"
        return ""

    monkeypatch.setattr("representations._llm_complete", fake_llm)

    long = "x" * 2000  # > MIN_CHARS_FOR_COMPRESSION
    result = generate_representations(long)
    assert result.get("compressed") == "compressed form here"


def test_generate_skips_compressed_for_short_content(monkeypatch):
    from representations import generate_representations

    calls: list[str] = []

    def fake_llm(prompt: str, **_):
        calls.append(prompt)
        return "x"

    monkeypatch.setattr("representations._llm_complete", fake_llm)

    result = generate_representations("short content " * 10)  # ~150 chars
    # compressed should NOT have been generated
    assert result.get("compressed") == ""
    assert not any("rewrite the text" in p.lower() for p in calls)


# ──────────────────────────────────────────────
# Queue worker validator guard
# ──────────────────────────────────────────────


def test_queue_stores_valid_compressed(cmp_db):
    """Valid compressed output (URLs preserved) is stored."""
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(cmp_db)
    original = "See https://example.com/api for details. " + ("filler text " * 100)
    kid = _add(cmp_db, original)
    q.enqueue(kid)

    def gen(content, project=None):
        return {"compressed": "See https://example.com/api for details."}

    stats = q.process_pending(gen, _fake_emb, "fake", limit=1)
    assert stats["processed"] == 1

    rows = cmp_db.execute(
        "SELECT representation, content FROM knowledge_representations WHERE knowledge_id=?",
        (kid,),
    ).fetchall()
    kinds = {r["representation"]: r["content"] for r in rows}
    assert "compressed" in kinds
    assert "https://example.com/api" in kinds["compressed"]


@pytest.mark.parametrize(
    "original",
    [
        pytest.param("Docs at https://critical.example/doc — do not lose this URL. " * 30, id="url"),
        pytest.param("Edit /Users/alice/project/src/server.py - do not lose this path. " * 30, id="path"),
    ],
)
def test_queue_rejects_compressed_that_loses_url(cmp_db, original):
    """Compressed output missing URLs or paths is silently dropped (raw still stored)."""
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(cmp_db)
    kid = _add(cmp_db, original)
    q.enqueue(kid)

    def bad_gen(content, project=None):
        return {"compressed": "Short version without the URL."}

    stats = q.process_pending(bad_gen, _fake_emb, "fake", limit=1)
    assert stats["processed"] == 1  # processing completed

    kinds = {
        r["representation"]
        for r in cmp_db.execute(
            "SELECT representation FROM knowledge_representations WHERE knowledge_id=?",
            (kid,),
        ).fetchall()
    }
    assert "raw" in kinds       # raw always stored
    assert "compressed" not in kinds  # compressed rejected by validator


def test_queue_stores_compressed_preserving_code_block(cmp_db):
    from representations_queue import RepresentationsQueue

    q = RepresentationsQueue(cmp_db)
    code_block = "```python\ndef f():\n    return 42\n```"
    original = f"Example function:\n\n{code_block}\n\n" + ("padding " * 200)
    kid = _add(cmp_db, original)
    q.enqueue(kid)

    def gen(content, project=None):
        # Byte-for-byte preservation of the code block
        return {"compressed": f"Example function:\n\n{code_block}"}

    q.process_pending(gen, _fake_emb, "fake", limit=1)

    row = cmp_db.execute(
        "SELECT content FROM knowledge_representations "
        "WHERE knowledge_id=? AND representation='compressed'",
        (kid,),
    ).fetchone()
    assert row is not None
    assert "def f():" in row["content"]


# ──────────────────────────────────────────────
# Search tier includes compressed
# ──────────────────────────────────────────────


def test_multi_repr_search_covers_compressed(cmp_db):
    """multi_repr_search should hit records whose ONLY matching view is 'compressed'."""
    from multi_repr_store import MultiReprStore
    from multi_repr_search import search

    store = MultiReprStore(cmp_db)
    kid = _add(cmp_db, "original full content")
    # Only compressed representation exists and it matches the query
    store.upsert(kid, "compressed", "the answer is xyz123", _fake_emb("the answer is xyz123"), "fake")

    q_emb = _fake_emb("the answer is xyz123")
    results = search(cmp_db, q_emb, top_n=5)
    ids = [r[0] for r in results]
    assert kid in ids
