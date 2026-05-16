"""Tests for parent_content_hash drift detection (Wave B — B1).

When a knowledge record's content changes, any pre-existing summary/keywords
view is now stale. Recall must (a) detect the mismatch via sha256(parent
content) vs ``knowledge_representations.parent_content_hash`` and (b) re-
enqueue the record so the worker regenerates the views.

These tests exercise the low-level pieces (hash helper, upsert with hash,
queue worker propagation) — the full recall integration is covered by an
end-to-end smoke in ``tests/test_e2e_v8_workflow.py``-style runs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import config
from multi_repr_store import MultiReprStore, content_hash
from representations_queue import RepresentationsQueue


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def repr_db():
    """In-memory SQLite with the minimum schema for representations."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT, project TEXT DEFAULT 'general',
            status TEXT DEFAULT 'active', created_at TEXT
        );
    """)
    conn.executescript((MIGRATIONS_DIR / "002_multi_representation.sql").read_text())
    conn.executescript((MIGRATIONS_DIR / "005_representations_queue.sql").read_text())
    conn.executescript((MIGRATIONS_DIR / "027_repr_freshness.sql").read_text())
    yield conn
    conn.close()


# ──────────────────────────────────────────────
# content_hash
# ──────────────────────────────────────────────


def test_content_hash_is_deterministic():
    a = content_hash("hello world")
    b = content_hash("hello world")
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_content_hash_changes_on_any_edit():
    a = content_hash("Hello world")
    b = content_hash("Hello world.")
    c = content_hash("hello world")
    assert a != b
    assert a != c
    assert b != c


def test_content_hash_handles_none_and_empty():
    h_none = content_hash(None)  # type: ignore[arg-type]
    h_empty = content_hash("")
    assert h_none == h_empty
    assert len(h_none) == 64


# ──────────────────────────────────────────────
# MultiReprStore.upsert with parent_content_hash
# ──────────────────────────────────────────────


def test_upsert_stores_parent_hash(repr_db):
    repr_db.execute(
        "INSERT INTO knowledge (content, status, created_at) VALUES (?, 'active', '2026-04-14T00:00:00Z')",
        ("Symfony deployment notes",),
    )
    kid = repr_db.execute("SELECT id FROM knowledge").fetchone()["id"]
    store = MultiReprStore(repr_db)
    parent_hash = content_hash("Symfony deployment notes")

    store.upsert(kid, "summary", "Notes about Symfony deploy", [0.1] * 4, "test-model",
                 parent_content_hash=parent_hash)

    row = repr_db.execute(
        "SELECT parent_content_hash, last_confirmed FROM knowledge_representations "
        "WHERE knowledge_id=? AND representation='summary'", (kid,)
    ).fetchone()
    assert row["parent_content_hash"] == parent_hash
    assert row["last_confirmed"] is not None


def test_upsert_replaces_hash_on_regeneration(repr_db):
    repr_db.execute(
        "INSERT INTO knowledge (content, status, created_at) VALUES (?, 'active', '2026-04-14T00:00:00Z')",
        ("v1",),
    )
    kid = repr_db.execute("SELECT id FROM knowledge").fetchone()["id"]
    store = MultiReprStore(repr_db)

    store.upsert(kid, "summary", "view-v1", [0.1] * 4, "m",
                 parent_content_hash=content_hash("v1"))
    store.upsert(kid, "summary", "view-v2", [0.2] * 4, "m",
                 parent_content_hash=content_hash("v2"))

    row = repr_db.execute(
        "SELECT content, parent_content_hash FROM knowledge_representations "
        "WHERE knowledge_id=? AND representation='summary'", (kid,)
    ).fetchone()
    assert row["content"] == "view-v2"
    assert row["parent_content_hash"] == content_hash("v2")


def test_upsert_without_hash_keeps_column_null(repr_db):
    """Backward compatibility: callers that omit the new arg leave the
    column NULL — recall will treat that as "legacy view, no drift info"."""
    repr_db.execute(
        "INSERT INTO knowledge (content, status, created_at) VALUES (?, 'active', '2026-04-14T00:00:00Z')",
        ("legacy",),
    )
    kid = repr_db.execute("SELECT id FROM knowledge").fetchone()["id"]
    store = MultiReprStore(repr_db)
    store.upsert(kid, "summary", "legacy view", [0.5] * 4, "m")  # no hash
    row = repr_db.execute(
        "SELECT parent_content_hash FROM knowledge_representations "
        "WHERE knowledge_id=?", (kid,)
    ).fetchone()
    assert row["parent_content_hash"] is None


# ──────────────────────────────────────────────
# RepresentationsQueue propagates hash
# ──────────────────────────────────────────────


def test_queue_worker_writes_parent_hash(repr_db):
    """End-to-end through the queue: enqueue → process_pending → row carries
    the sha256 of the parent content."""
    repr_db.execute(
        "INSERT INTO knowledge (content, status, created_at) VALUES (?, 'active', '2026-04-14T00:00:00Z')",
        ("Vue Composition API patterns",),
    )
    kid = repr_db.execute("SELECT id FROM knowledge").fetchone()["id"]

    queue = RepresentationsQueue(repr_db)
    queue.enqueue(kid)

    def fake_generator(_content):
        return {"summary": "Vue patterns digest"}

    def fake_embedder(_text):
        return [0.7] * 4

    stats = queue.process_pending(fake_generator, fake_embedder, "fake-model", limit=5)
    assert stats["processed"] == 1
    rows = repr_db.execute(
        "SELECT representation, parent_content_hash FROM knowledge_representations "
        "WHERE knowledge_id=?", (kid,)
    ).fetchall()
    assert rows, "expected at least one view to be stored"
    expected_hash = content_hash("Vue Composition API patterns")
    for r in rows:
        assert r["parent_content_hash"] == expected_hash, (
            f"view {r['representation']} did not carry parent_content_hash"
        )


def test_queue_worker_detects_post_edit_drift_via_hash(repr_db):
    """If the parent content is edited after the view is generated, the
    stored hash no longer matches a fresh ``content_hash(current_content)``
    — this is the signal recall uses to penalise + re-enqueue."""
    repr_db.execute(
        "INSERT INTO knowledge (content, status, created_at) VALUES (?, 'active', '2026-04-14T00:00:00Z')",
        ("Initial content",),
    )
    kid = repr_db.execute("SELECT id FROM knowledge").fetchone()["id"]
    queue = RepresentationsQueue(repr_db)
    queue.enqueue(kid)
    queue.process_pending(
        generator=lambda c: {"summary": "initial summary"},
        embedder=lambda t: [0.3] * 4,
        model_name="m",
    )

    # Edit the parent content without regenerating the view
    repr_db.execute(
        "UPDATE knowledge SET content=? WHERE id=?",
        ("Heavily edited content with different meaning", kid),
    )
    repr_db.commit()

    stored = repr_db.execute(
        "SELECT parent_content_hash FROM knowledge_representations "
        "WHERE knowledge_id=? AND representation='summary'", (kid,)
    ).fetchone()["parent_content_hash"]

    current = content_hash("Heavily edited content with different meaning")
    assert stored != current, (
        "drift must be visible as hash mismatch between stored and current"
    )
