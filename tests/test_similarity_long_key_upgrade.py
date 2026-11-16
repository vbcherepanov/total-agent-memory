"""Issue #43 follow-up: the 24-character similarity keys arrive through new migrations (059 / PG 0022).

Migrations 054 and 0017 were already applied to v15 databases and the PostgreSQL runner rejects an edited file,
so both must stay byte-identical to the shipped content and the long-key indexes must come from the new files.
"""
import hashlib
import sqlite3
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SQLITE_054 = ROOT / "migrations" / "054_similarity_candidates.sql"
SQLITE_059 = ROOT / "migrations" / "059_similarity_long_keys.sql"
SHIPPED_054_SHA256 = "a2ad832b05f0aa36c5a19f6c1ebf440b49f07ef1425efb848b228b662c9accea"
SHIPPED_0017_SHA256 = "3448cd29909a392fa8092d97222fd30e3de4e3ac4fb22f6f994704b33b28d742"
LONG_INDEXES = {"idx_k_similarity_long_prefix", "idx_k_similarity_long_suffix"}
SHORT_INDEXES = {"idx_k_similarity_prefix", "idx_k_similarity_suffix"}
TARGET = "the billing ledger is stored in rack seven of the primary datacenter in production."


def sha256(path: Path) -> str:
    # Same as tam_db.pg_schema.Migration.checksum: read_text() folds CRLF from Windows checkouts.
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def test_shipped_migrations_are_unchanged():
    assert sha256(SQLITE_054) == SHIPPED_054_SHA256
    assert sha256(ROOT / "migrations" / "postgres" / "workspace" / "0017_similarity_candidates.sql") == SHIPPED_0017_SHA256


def sqlite_indexes(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_k_similarity%'")}


def sqlite_store() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE knowledge(id INTEGER PRIMARY KEY,session_id TEXT,type TEXT,content TEXT,project TEXT,"
               "status TEXT DEFAULT 'active',created_at TEXT)")
    return db


def test_sqlite_upgrade_from_old_054_adds_long_key_indexes():
    db = sqlite_store()
    db.executescript(SQLITE_054.read_text(encoding="utf-8"))
    assert sqlite_indexes(db) == SHORT_INDEXES
    db.executescript(SQLITE_059.read_text(encoding="utf-8"))
    assert sqlite_indexes(db) == SHORT_INDEXES | LONG_INDEXES
    db.executescript(SQLITE_059.read_text(encoding="utf-8"))  # idempotent
    assert sqlite_indexes(db) == SHORT_INDEXES | LONG_INDEXES


def test_lookup_without_long_key_indexes_returns_the_same_matches():
    from memory_core.similar import find_similar

    db = sqlite_store()
    db.executescript(SQLITE_054.read_text(encoding="utf-8"))
    db.execute("INSERT INTO knowledge(session_id,type,content,project) VALUES ('s','fact',?,'p')", (TARGET,))
    for index in range(100):
        db.execute("INSERT INTO knowledge(session_id,type,content,project) VALUES ('s','fact',?,'p')",
                   (f"the unrelated note number {index} about something else entirely in production.",))
    db.commit()
    variant = TARGET.replace("rack seven", "rack sevem")
    without = find_similar(db, variant, "p", 0)
    db.executescript(SQLITE_059.read_text(encoding="utf-8"))
    with_indexes = find_similar(db, variant, "p", 0)
    assert [row.id for row in without.matches] == [row.id for row in with_indexes.matches] == [1]


@pytest.mark.postgres
def test_postgres_upgrade_keeps_0017_checksum_and_adds_long_key_indexes(pg_database, monkeypatch):
    from tam_db import pg_schema
    from tam_db.pg_connection import connect
    from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

    instance = PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance, b"s" * 32)
    migrations = pg_schema.workspace_migrations()
    new = next(m for m in migrations if m.path.name == "0022_similarity_long_keys.sql")
    assert max(m.version for m in migrations) == new.version
    with monkeypatch.context() as patch:
        patch.setattr(pg_schema, "workspace_migrations",
                      lambda _directory=None: tuple(m for m in migrations if m.version < new.version))
        provisioner.ensure("upgrade")
    db = connect(provisioner.store_database("upgrade"))
    try:
        def names():
            return {row[0] for row in db.execute(
                "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'idx_k_similarity%'").fetchall()}
        assert names() == SHORT_INDEXES
        provisioner.ensure("upgrade")  # validates every recorded checksum, including 0017
        provisioner.ensure("upgrade")
        assert names() == SHORT_INDEXES | LONG_INDEXES
        assert db.execute("SELECT count(*) FROM migrations WHERE version='059'").fetchone()[0] == 1
    finally:
        db.close()
    assert next(m for m in migrations if m.path.name == "0017_similarity_candidates.sql").checksum == SHIPPED_0017_SHA256
