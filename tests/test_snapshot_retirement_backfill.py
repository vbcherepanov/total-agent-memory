import sqlite3
import uuid
from pathlib import Path

import pytest

from memory_core.snapshot import Snapshot

ROOT = Path(__file__).resolve().parents[1]
REPAIR = ROOT / "migrations" / "058_knowledge_state_retirement_backfill.sql"
LEGACY_ROWS = (
    # id, status, superseded_by, created_at, last_confirmed
    (1, "active", None, "2026-01-01T01:00:00Z", None),
    (2, "deleted", None, "2026-01-01T01:00:00Z", "2026-01-01T05:00:00Z"),
    (3, "superseded", 4, "2026-01-01T01:00:00Z", "2026-01-01T09:00:00Z"),
    (4, "active", None, "2026-01-01T03:00:00Z", None),
    (5, "archived", None, "2026-01-01T01:00:00Z", None),
    (6, "deleted", None, "2026-01-01T01:00:00Z", "2026-01-01T04:00:00Z"),
    (8, "purged", None, "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z"),
    (9, "superseded", 99, "2026-01-01T01:00:00Z", "2026-01-01T06:00:00Z"),
)
UNTOUCHED = (1, 3, 4, 5, 6, 8, 10)


def seed_legacy(db):
    for kid, status, successor, created, confirmed in LEGACY_ROWS:
        db.execute("INSERT INTO knowledge(id,session_id,type,content,project,status,superseded_by,created_at,"
                   "last_confirmed) VALUES (?,'legacy','fact','legacy','p',?,?,?,?)",
                   (kid, status, successor, created, confirmed))
    db.commit()


def write_v15_history(db):
    # Rows written after the upgrade: the triggers record these histories, 058 must not alter them.
    db.execute("INSERT INTO knowledge(id,session_id,type,content,project,status,created_at) "
               "VALUES (10,'v15','fact','v15','p','active','2026-03-01T00:00:00Z')")
    db.execute("UPDATE knowledge SET status='deleted', state_changed_at='2026-03-02T00:00:00Z' WHERE id=10")
    db.execute("UPDATE knowledge SET status='archived', state_changed_at='2026-03-03T00:00:00Z' WHERE id=6")
    db.commit()


def events(db):
    rows = db.execute("SELECT knowledge_id,state,occurred_at FROM knowledge_state_events "
                      "ORDER BY knowledge_id,id").fetchall()
    return [tuple(row) for row in rows]


def visible(db, moment):
    snapshot = Snapshot.at(moment)
    return {kid for kid in range(1, 11) if snapshot.allows(db, kid)}


def assert_repaired(db, before):
    after = events(db)
    by_id = {kid: [event for event in after if event[0] == kid] for kid in range(1, 11)}
    assert by_id[2] == [(2, "active", "2026-01-01T01:00:00Z"), (2, "deleted", "2026-01-01T05:00:00Z")]
    assert by_id[9] == [(9, "active", "2026-01-01T01:00:00Z"), (9, "superseded", "2026-01-01T06:00:00Z")]
    for kid in (5, 8):  # no timestamp later than created_at: documented fallback, stays unknown
        assert by_id[kid] == [(kid, "unknown", "2026-01-01T01:00:00Z")]
    for kid in UNTOUCHED:
        assert by_id[kid] == [event for event in before if event[0] == kid]
    assert by_id[6] == [(6, "unknown", "2026-01-01T01:00:00Z"), (6, "archived", "2026-03-03T00:00:00Z")]
    assert visible(db, "2026-01-01T00:00:00Z") == set()
    assert visible(db, "2026-01-01T02:00:00Z") == {1, 2, 3, 9}
    assert visible(db, "2026-01-01T04:00:00Z") == {1, 2, 4, 9}
    assert visible(db, "2026-01-01T05:30:00Z") == {1, 4, 9}
    assert visible(db, "2026-01-01T07:00:00Z") == {1, 4}
    assert visible(db, "2026-03-01T12:00:00Z") == {1, 4, 10}
    assert visible(db, "2026-03-04T00:00:00Z") == {1, 4}
    return after


def sqlite_migrated():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE knowledge(id INTEGER PRIMARY KEY,session_id TEXT,type TEXT,content TEXT,project TEXT,"
               "status TEXT,superseded_by INTEGER,created_at TEXT,last_confirmed TEXT)")
    seed_legacy(db)
    db.executescript((ROOT / "migrations" / "055_knowledge_state_history.sql").read_text(encoding="utf-8"))
    write_v15_history(db)
    return db


def test_sqlite_retired_records_keep_their_past():
    db = sqlite_migrated()
    try:
        before = events(db)
        assert visible(db, "2026-01-01T04:00:00Z") == {1, 4}  # retired records 2 and 9 are still invisible
        db.executescript(REPAIR.read_text(encoding="utf-8"))
        after = assert_repaired(db, before)
        db.executescript(REPAIR.read_text(encoding="utf-8"))
        assert events(db) == after
    finally:
        db.close()


def build_postgres(pg_database, monkeypatch, name):
    from tam_db import pg_schema
    from tam_db.pg_connection import connect
    from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

    instance = PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance, b"s" * 32)
    migrations = pg_schema.workspace_migrations()
    first = next(m for m in migrations if m.path.name == "0018_knowledge_state_history.sql")
    last = next(m for m in migrations if m.path.name == "0021_knowledge_state_retirement_backfill.sql")
    with monkeypatch.context() as patch:
        patch.setattr(pg_schema, "workspace_migrations",
                      lambda _directory=None: tuple(m for m in migrations if m.version < first.version))
        provisioner.ensure(name)
    db = connect(provisioner.store_database(name))
    seed_legacy(db)
    with monkeypatch.context() as patch:
        patch.setattr(pg_schema, "workspace_migrations",
                      lambda _directory=None: tuple(m for m in migrations if m.version < last.version))
        provisioner.ensure(name)
    write_v15_history(db)
    return provisioner, db


@pytest.mark.postgres
def test_postgres_retired_records_keep_their_past(pg_database, monkeypatch):
    provisioner, db = build_postgres(pg_database, monkeypatch, "retired")
    try:
        before = events(db)
        provisioner.ensure("retired")
        after = assert_repaired(db, before)
        provisioner.ensure("retired")
        assert events(db) == after
        assert db.execute("SELECT count(*) FROM migrations WHERE version='058'").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.postgres
def test_sqlite_and_postgres_backfill_identical_events(pg_database, monkeypatch):
    lite = sqlite_migrated()
    provisioner, db = build_postgres(pg_database, monkeypatch, "parity")
    try:
        lite.executescript(REPAIR.read_text(encoding="utf-8"))
        provisioner.ensure("parity")
        assert events(db) == events(lite)
    finally:
        db.close()
        lite.close()
