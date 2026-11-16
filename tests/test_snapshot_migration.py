import sqlite3
import uuid
from pathlib import Path

import pytest

from memory_core.snapshot import Snapshot

ROOT = Path(__file__).resolve().parents[1]


def seed(db):
    for kid, state, successor, created in (
        (1, "active", None, "2026-01-01T01:00:00Z"),
        (2, "superseded", 3, "2026-01-01T01:00:00Z"),
        (3, "active", None, "2026-01-01T02:00:00Z"),
        (4, "deleted", None, "2026-01-01T01:00:00Z"),
    ):
        db.execute("INSERT INTO knowledge(id,session_id,type,content,project,status,superseded_by,created_at) "
                   "VALUES (?,'legacy','fact','legacy','p',?,?,?)", (kid, state, successor, created))
    db.commit()


def assert_migrated(db):
    assert db.execute("SELECT state FROM knowledge_state_events WHERE knowledge_id=4").fetchone()[0] == "unknown"
    for moment, expected in (("2026-01-01T00:00:00Z", set()),
                             ("2026-01-01T01:00:00Z", {1, 2}),
                             ("2026-01-01T02:00:00Z", {1, 3})):
        snapshot = Snapshot.at(moment)
        assert {kid for kid in range(1, 5) if snapshot.allows(db, kid)} == expected


def test_sqlite_legacy_snapshot_migration():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE knowledge(id INTEGER PRIMARY KEY,session_id TEXT,type TEXT,content TEXT,project TEXT,"
                   "status TEXT,superseded_by INTEGER,created_at TEXT)")
        seed(db)
        db.executescript((ROOT / "migrations" / "055_knowledge_state_history.sql").read_text(encoding="utf-8"))
        assert_migrated(db)


@pytest.mark.postgres
def test_postgres_legacy_snapshot_migration(pg_database, monkeypatch):
    from tam_db import pg_schema
    from tam_db.pg_connection import connect
    from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

    instance = PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance, b"s" * 32)
    migrations = pg_schema.workspace_migrations()
    snapshot_migration = next(migration for migration in migrations
                              if migration.path.name == "0018_knowledge_state_history.sql")
    legacy_migrations = tuple(migration for migration in migrations if migration.version < snapshot_migration.version)
    with monkeypatch.context() as patch:
        patch.setattr(pg_schema, "workspace_migrations", lambda _directory=None: legacy_migrations)
        provisioner.ensure("legacy")
    db = connect(provisioner.store_database("legacy"))
    try:
        seed(db)
        provisioner.ensure("legacy")
        assert_migrated(db)
    finally:
        db.close()
