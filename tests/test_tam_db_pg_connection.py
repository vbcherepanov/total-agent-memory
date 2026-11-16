"""PgConnection behaves like sqlite3.Connection for the code the team server runs."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tam_db.contracts import (
    DatabaseSettings,
    PgIntegrityError,
    PgOperationalError,
    PgSerializationFailure,
    StoreDatabase,
    UntranslatableSQL,
    schema_for,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[1]
COMPAT_DIR = ROOT / "migrations" / "postgres" / "compat"

SCHEMA_SCRIPT = """
    CREATE TABLE knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL UNIQUE,
        project TEXT DEFAULT 'general',
        score REAL,
        payload BLOB,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        CHECK (length(content) > 0)
    );
    CREATE INDEX knowledge_project ON knowledge(project);
    CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT, n INTEGER DEFAULT 7);
    CREATE TABLE relations (from_id INTEGER, to_id INTEGER, type TEXT);
"""


@dataclass(frozen=True)
class Workspace:
    url: str
    schema: str


@pytest.fixture
def workspace(pg_database) -> Workspace:
    import psycopg
    from psycopg import sql

    schema = schema_for("connection-tests")
    with psycopg.connect(pg_database.url, autocommit=True) as admin:
        for migration in sorted(COMPAT_DIR.glob("*.sql")):
            admin.execute(migration.read_text(encoding="utf-8"))
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    return Workspace(url=pg_database.url, schema=schema)


@pytest.fixture
def db(workspace) -> Iterator[object]:
    from tam_db.pg_connection import connect_url

    connection = connect_url(workspace.url, schema=workspace.schema)
    connection.executescript(SCHEMA_SCRIPT)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def lite() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SCRIPT)
    try:
        yield connection
    finally:
        connection.close()


def _other(workspace):
    from tam_db.pg_connection import connect_url

    return connect_url(workspace.url, schema=workspace.schema)


# ── connecting ──

def test_connect_store_database_sets_session(workspace):
    from tam_db.pg_connection import PgConnection, connect

    settings = DatabaseSettings(statement_timeout_ms=12_345, lock_timeout_ms=2_000)
    connection = connect(StoreDatabase.postgres(workspace.url, workspace.schema, settings))
    try:
        assert isinstance(connection, PgConnection)
        assert connection.execute("SELECT current_schema()").fetchone()[0] == workspace.schema
        path = connection.execute("SHOW search_path").fetchone()[0]
        assert path.split(", ")[1:] == ["tam_compat", "extensions"]
        assert connection.execute("SHOW TimeZone").fetchone()[0] == "UTC"
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == "12345ms"
        assert connection.execute("SHOW lock_timeout").fetchone()[0] == "2s"
        assert not connection.in_transaction and connection.total_changes == 0
    finally:
        connection.close()
    with pytest.raises(ValueError):
        connect(StoreDatabase.sqlite())


def test_connection_errors_are_redacted(workspace):
    import psycopg

    from tam_db.pg_connection import connect_url, redact

    secret = "s3cr3t-Password"
    conninfo = psycopg.conninfo.conninfo_to_dict(workspace.url)
    bad = (f"postgresql://{conninfo['user']}:{secret}@{conninfo['host']}:{conninfo['port']}/"
           f"{conninfo['dbname']}?sslmode=disable")
    with pytest.raises(PgOperationalError) as raised:
        connect_url(bad, schema=workspace.schema)
    assert secret not in str(raised.value) and "postgresql://" not in str(raised.value)
    assert isinstance(raised.value, sqlite3.OperationalError)
    assert redact(f"failed for {bad} password={secret} x", (secret,)) == \
        "failed for [redacted] password=[redacted] x"


def test_connect_options_reach_libpq_and_stay_out_of_errors(workspace, tmp_path):
    import psycopg

    from tam_db.pg_connection import connect_url

    db = connect_url(workspace.url, schema=workspace.schema, connect_options={"sslmode": "disable",
                                                                              "gssencmode": "disable"})
    try:
        assert db.raw.info.get_parameters()["application_name"] == "tam-team-server"
        effective = {option.keyword.decode(): (option.val or b"").decode() for option in db.raw.pgconn.info}
        assert effective["gssencmode"] == "disable"
    finally:
        db.close()
    secret_file = str(tmp_path / "secret-passfile-name")
    conninfo = psycopg.conninfo.conninfo_to_dict(workspace.url)
    passwordless = (f"postgresql://{conninfo['user']}@{conninfo['host']}:{conninfo['port']}/{conninfo['dbname']}"
                    "?sslmode=disable")
    with pytest.raises(PgOperationalError) as raised:
        connect_url(passwordless, schema=workspace.schema,
                    connect_options={"passfile": secret_file, "require_auth": "none"})
    assert secret_file not in str(raised.value)


def test_store_database_connect_options_are_applied(workspace):
    from tam_db.pg_connection import connect

    database = StoreDatabase.postgres(workspace.url, workspace.schema,
                                      connect_options=(("gssencmode", "disable"), ("require_auth", "scram-sha-256")))
    db = connect(database)
    try:
        effective = {option.keyword.decode(): (option.val or b"").decode() for option in db.raw.pgconn.info}
        assert effective["gssencmode"] == "disable" and effective["require_auth"] == "scram-sha-256"
        assert db.execute("SELECT current_schema()").fetchone()[0] == workspace.schema
    finally:
        db.close()


def test_connect_options_cannot_override_session_keywords(workspace):
    from tam_db.pg_connection import connect_url

    for keyword in ("autocommit", "connect_timeout", "application_name"):
        with pytest.raises(ValueError, match=keyword):
            connect_url(workspace.url, schema=workspace.schema, connect_options={keyword: "1"})


# ── transactions ──

def test_implicit_transactions_follow_sqlite3(db, workspace):
    db.execute("SELECT count(*) FROM knowledge").fetchone()
    assert not db.in_transaction
    db.execute("INSERT INTO knowledge(content) VALUES (?)", ("a",))
    assert db.in_transaction
    other = _other(workspace)
    try:
        assert other.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0
        db.commit()
        assert not db.in_transaction
        assert other.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 1
        db.execute("UPDATE knowledge SET project = ? WHERE content = ?", ("p", "a"))
        db.rollback()
        assert db.execute("SELECT project FROM knowledge").fetchone()[0] == "general"
        with db:
            db.execute("DELETE FROM knowledge")
        assert other.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0
        with pytest.raises(RuntimeError), db:
            db.execute("INSERT INTO knowledge(content) VALUES ('b')")
            raise RuntimeError("abort")
        assert db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0
        db.commit()
        db.rollback()
    finally:
        other.close()


def test_failed_statement_undoes_only_itself(db):
    db.execute("INSERT INTO knowledge(content) VALUES ('first')")
    with pytest.raises(sqlite3.IntegrityError) as raised:
        db.execute("INSERT INTO knowledge(content) VALUES ('first')")
    assert isinstance(raised.value, PgIntegrityError) and raised.value.sqlstate == "23505"
    assert db.in_transaction
    with pytest.raises(sqlite3.OperationalError):
        db.execute("SELECT missing_column FROM knowledge")
    db.execute("INSERT INTO knowledge(content) VALUES ('second')")
    db.commit()
    assert [row[0] for row in db.execute("SELECT content FROM knowledge ORDER BY id")] == ["first", "second"]


def test_failure_opening_a_transaction_leaves_none_open(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO knowledge(content) VALUES ('')")
    assert not db.in_transaction
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO knowledge(project) VALUES ('p')")
    db.execute("INSERT INTO knowledge(content) VALUES ('ok')")
    db.commit()
    assert db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 1


def test_without_statement_savepoints_a_failure_rolls_back_the_transaction(workspace):
    from tam_db.pg_connection import connect_url

    db = connect_url(workspace.url, schema=workspace.schema, statement_savepoints=False)
    try:
        db.executescript(SCHEMA_SCRIPT)
        db.execute("INSERT INTO kv(k) VALUES ('a')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO kv(k) VALUES ('a')")
        assert not db.in_transaction
        assert db.execute("SELECT count(*) FROM kv").fetchone()[0] == 0
    finally:
        db.close()


def test_explicit_transaction_statements(db, lite):
    for connection in (db, lite):
        connection.execute("BEGIN IMMEDIATE")
        assert connection.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
            connection.execute("BEGIN")
        connection.execute("INSERT INTO kv(k) VALUES ('x')")
        connection.execute("COMMIT")
        assert not connection.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="no transaction is active"):
            connection.execute("COMMIT")
        with pytest.raises(sqlite3.OperationalError, match="no transaction is active"):
            connection.execute("ROLLBACK")
        connection.execute("SAVEPOINT outer_sp")
        assert connection.in_transaction
        connection.execute("INSERT INTO kv(k) VALUES ('y')")
        connection.execute("SAVEPOINT inner_sp")
        connection.execute("INSERT INTO kv(k) VALUES ('z')")
        connection.execute("ROLLBACK TO SAVEPOINT inner_sp")
        connection.execute("RELEASE SAVEPOINT inner_sp")
        assert connection.in_transaction
        connection.execute("RELEASE outer_sp")
        assert not connection.in_transaction
        assert [row[0] for row in connection.execute("SELECT k FROM kv ORDER BY k")] == ["x", "y"]


def test_begin_with_postgres_transaction_modes(db):
    db.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
    assert db.in_transaction
    assert db.execute("SHOW transaction_isolation").fetchone()[0] == "serializable"
    db.rollback()
    db.execute("BEGIN READ ONLY")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO kv(k) VALUES ('x')")
    db.rollback()


def test_isolation_level_none_is_autocommit(workspace):
    from tam_db.pg_connection import connect_url

    db = connect_url(workspace.url, schema=workspace.schema, isolation_level=None)
    other = _other(workspace)
    try:
        assert db.isolation_level is None
        db.executescript(SCHEMA_SCRIPT)
        db.execute("INSERT INTO kv(k) VALUES ('auto')")
        assert not db.in_transaction
        assert other.execute("SELECT count(*) FROM kv").fetchone()[0] == 1
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO kv(k) VALUES ('explicit')")
        assert other.execute("SELECT count(*) FROM kv").fetchone()[0] == 1
        db.execute("ROLLBACK")
        db.isolation_level = "DEFERRED"
        db.execute("INSERT INTO kv(k) VALUES ('implicit')")
        assert db.in_transaction
        db.isolation_level = None
        assert not db.in_transaction
        assert other.execute("SELECT count(*) FROM kv").fetchone()[0] == 2
        with pytest.raises(ValueError):
            db.isolation_level = "SERIALIZABLE"
    finally:
        db.close()
        other.close()


def test_serialization_failure_rolls_back_the_whole_transaction(db):
    db.execute("INSERT INTO kv(k) VALUES ('kept?')")
    with pytest.raises(PgSerializationFailure) as raised:
        db.execute_native("DO $$ BEGIN RAISE EXCEPTION 'conflict' USING ERRCODE = '40001'; END $$")
    assert raised.value.sqlstate == "40001"
    assert isinstance(raised.value, sqlite3.OperationalError)
    assert not db.in_transaction
    assert db.execute("SELECT count(*) FROM kv").fetchone()[0] == 0


def test_trigger_raise_is_an_integrity_error(db):
    db.execute_native("""
        CREATE FUNCTION kv_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'kv is read-only'; END $$""")
    db.execute_native("CREATE TRIGGER kv_guard BEFORE INSERT ON kv FOR EACH ROW EXECUTE FUNCTION kv_guard()")
    with pytest.raises(sqlite3.IntegrityError, match="read-only"):
        db.execute("INSERT INTO kv(k) VALUES ('x')")


def test_close_discards_pending_work_and_blocks_use(db, workspace):
    db.execute("INSERT INTO kv(k) VALUES ('lost')")
    db.close()
    db.close()
    assert db.closed
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")
    other = _other(workspace)
    try:
        assert other.execute("SELECT count(*) FROM kv").fetchone()[0] == 0
    finally:
        other.close()


# ── cursors and rows ──

def test_rows_and_row_factories(db, lite):
    for connection in (db, lite):
        connection.execute("INSERT INTO kv(k, v, n) VALUES ('a', 'x', 1)")
        assert connection.execute("SELECT k, n FROM kv").fetchone() == ("a", 1)
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT k, v AS Value, n FROM kv").fetchone()
        assert row["k"] == "a" and row["VALUE"] == "x" and row["value"] == "x" and row[2] == 1
        assert row.keys() == ["k", "value" if connection is db else "Value", "n"]
        assert dict(row) == {"k": "a", row.keys()[1]: "x", "n": 1}
        assert tuple(row) == ("a", "x", 1) and len(row) == 3 and row[1:] == ("x", 1)
        with pytest.raises(IndexError):
            row["missing"]
        connection.row_factory = lambda cursor, values: {d[0]: v for d, v in zip(cursor.description, values)}
        assert connection.execute("SELECT k, n FROM kv").fetchone() == {"k": "a", "n": 1}
        connection.row_factory = None


def test_cursor_surface(db, lite):
    for connection in (db, lite):
        connection.executemany("INSERT INTO kv(k, n) VALUES (?, ?)", [("a", 1), ("b", 2), ("c", 3)])
        cursor = connection.execute("SELECT k FROM kv ORDER BY k")
        assert cursor.rowcount == -1
        assert [d[0] for d in cursor.description] == ["k"]
        assert cursor.fetchone() == ("a",)
        assert cursor.fetchmany(1) == [("b",)]
        assert list(cursor) == [("c",)]
        assert cursor.fetchone() is None and cursor.fetchall() == []
        update = connection.execute("UPDATE kv SET n = n + 1 WHERE n >= ?", (2,))
        assert update.rowcount == 2 and update.description is None
        many = connection.executemany("DELETE FROM kv WHERE k = ?", [("a",), ("zz",)])
        assert many.rowcount == 1
        cursor = connection.cursor()
        assert cursor.execute("SELECT count(*) FROM kv") is cursor and cursor.fetchone() == (2,)
        cursor.close()
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.execute("SELECT 1")
        connection.commit()


def test_total_changes_counts_dml_rows(db, lite):
    for connection in (db, lite):
        start = connection.total_changes
        connection.execute("INSERT INTO kv(k) VALUES ('a')")
        connection.executemany("INSERT INTO kv(k) VALUES (?)", [("b",), ("c",)])
        connection.execute("UPDATE kv SET v = 'x'")
        connection.execute("SELECT * FROM kv").fetchall()
        connection.execute("DELETE FROM kv WHERE k = 'a'")
        assert connection.total_changes - start == 7
        connection.commit()


def test_lastrowid_and_last_insert_rowid(db, lite):
    for connection in (db, lite):
        first = connection.execute("INSERT INTO knowledge(content) VALUES (?)", ("one",))
        assert first.lastrowid == 1
        second = connection.execute("INSERT INTO knowledge(content, project) VALUES (?, ?)", ("two", "p"))
        assert second.lastrowid == 2 and second.rowcount == 1
        assert connection.execute("SELECT last_insert_rowid()").fetchone()[0] == 2
        ignored = connection.execute("INSERT OR IGNORE INTO knowledge(content) VALUES ('one')")
        assert ignored.rowcount == 0 and ignored.lastrowid == 2
        keyed = connection.execute("INSERT INTO kv(k) VALUES ('k')")
        # A table without an integer primary key has a hidden rowid in SQLite only; on
        # PostgreSQL the previous value is kept.
        assert keyed.lastrowid == (2 if connection is db else 1)
        if connection is lite:
            connection.execute("INSERT INTO knowledge(content) VALUES ('filler')")
        # PostgreSQL consumes an identity value even when ON CONFLICT DO NOTHING skips the
        # row (ids are never reused), so the next id may be higher than SQLite's.
        returned = connection.execute("INSERT INTO knowledge(content) VALUES ('three') RETURNING id, content")
        (new_id, content), = returned.fetchall()
        assert content == "three" and new_id >= 3
        assert connection.execute("SELECT last_insert_rowid()").fetchone()[0] == new_id
        select = connection.execute("SELECT 1")
        assert select.lastrowid == new_id
        assert connection.cursor().lastrowid is None
        connection.commit()


@pytest.mark.filterwarnings("ignore:The default (datetime|date) adapter is deprecated:DeprecationWarning")
def test_values_are_adapted_like_sqlite(db, lite):
    moment = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    for connection in (db, lite):
        connection.execute("INSERT INTO knowledge(content, score, payload) VALUES (?, ?, ?)",
                           ("v", 0.25, b"\x00\x01\xff"))
        row = connection.execute("SELECT score, payload, sum(id), count(*), avg(id) FROM knowledge "
                                 "GROUP BY score, payload").fetchone()
        assert row == (0.25, b"\x00\x01\xff", 1, 1, 1.0)
        assert [type(value) for value in row] == [float, bytes, int, int, float]
        assert connection.execute("SELECT 1 = 1, 1 > 2").fetchone() == (1, 0)
        connection.execute("INSERT INTO kv(k, v, n) VALUES (?, ?, ?)", ("flag", moment, True))
        connection.execute("INSERT INTO kv(k, v) VALUES (?, ?)", ("day", date(2024, 5, 6)))
        assert connection.execute("SELECT v, n FROM kv WHERE k = 'flag'").fetchone() == (
            "2024-05-06 07:08:09.123456+00:00", 1)
        assert connection.execute("SELECT v FROM kv WHERE k = 'day'").fetchone() == ("2024-05-06",)
        assert connection.execute("SELECT json_object('a', 1, 'b', ?)", ("x",)).fetchone() == ('{"a":1,"b":"x"}',)
        connection.commit()
    assert db.execute("SELECT '{\"a\": 1}'::jsonb, '[1]'::json").fetchone() == ('{"a": 1}', "[1]")
    assert db.execute("SELECT 2.50::numeric, 3::numeric, NULL::numeric").fetchone() == (2.5, 3, None)


# ── translation at execution time ──

def test_replace_and_rowid_use_the_catalog(db, lite):
    for connection in (db, lite):
        connection.execute("INSERT INTO kv(k, v, n) VALUES ('a', 'old', 1)")
        connection.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", ("a", "new"))
        connection.execute("REPLACE INTO kv VALUES ('b', 'x', 5)")
        assert connection.execute("SELECT k, v, n FROM kv ORDER BY k").fetchall() == [("a", "new", 7), ("b", "x", 5)]
        connection.execute("INSERT INTO knowledge(content) VALUES ('r')")
        assert connection.execute("SELECT rowid, content FROM knowledge WHERE rowid = ?", (1,)).fetchall() == [
            (1, "r")]
        connection.commit()
    with pytest.raises(UntranslatableSQL):
        db.execute("SELECT rowid FROM relations")


def test_executescript_commits_first_and_invalidates_the_catalog(db, workspace):
    db.execute("INSERT INTO kv(k) VALUES ('pending')")
    db.executescript("""
        CREATE TABLE later (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO later(name) VALUES ('one');
    """)
    assert not db.in_transaction
    other = _other(workspace)
    try:
        assert other.execute("SELECT count(*) FROM kv").fetchone()[0] == 1
        assert other.execute("SELECT name FROM later").fetchall() == [("one",)]
    finally:
        other.close()
    assert db.execute("INSERT INTO later(name) VALUES ('two')").lastrowid == 2
    db.execute("DROP TABLE later")
    db.execute("CREATE TABLE later (code TEXT PRIMARY KEY, name TEXT)")
    assert db.execute("INSERT INTO later VALUES ('c', 'n')").lastrowid == 2
    db.commit()


def test_statement_errors(db):
    with pytest.raises(UntranslatableSQL):
        db.execute("SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH ?", ("x",))
    with pytest.raises(sqlite3.ProgrammingError, match="Incorrect number of bindings"):
        db.execute("SELECT ?", (1, 2))
    with pytest.raises(sqlite3.ProgrammingError, match="one statement"):
        db.execute("SELECT 1; SELECT 2")
    with pytest.raises(sqlite3.ProgrammingError, match="DML"):
        db.executemany("SELECT ?", [(1,)])
    with pytest.raises(sqlite3.OperationalError) as raised:
        db.execute("SELEC 1")
    assert raised.value.sqlstate == "42601"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO knowledge(content) VALUES (NULL)")
    assert db.execute("SELECT 1").fetchone() == (1,)


def test_execute_native_uses_psycopg_placeholders(db):
    from psycopg import sql

    cursor = db.execute_native("INSERT INTO kv(k, v) VALUES (%s, %s)", ("n", "100%"))
    assert cursor.rowcount == 1 and db.in_transaction
    db.commit()
    assert db.execute_native("SELECT v FROM kv WHERE k LIKE %s", ("n",)).fetchone() == ("100%",)
    composed = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("kv"))
    assert db.execute_native(composed).fetchone() == (1,)


# ── PRAGMA ──

def test_pragmas_match_sqlite(db, lite):
    def info(connection):
        return [(row[0], row[1], row[3], row[5]) for row in connection.execute("PRAGMA table_info(knowledge)")]

    assert info(db) == info(lite)
    types = {row[1]: row[2] for row in db.execute("PRAGMA table_info('knowledge')")}
    assert types["id"] == "BIGINT" and types["score"] == "DOUBLE PRECISION" and types["payload"] == "BYTEA"
    assert db.execute("PRAGMA table_info(missing)").fetchall() == []
    indexes = {row[1]: (row[2], row[3]) for row in db.execute("PRAGMA index_list(knowledge)")}
    assert indexes["knowledge_project"] == (0, "c")
    assert (1, "pk") in indexes.values() and (1, "u") in indexes.values()
    assert [row[2] for row in db.execute("PRAGMA index_info(knowledge_project)")] == ["project"]
    for connection in (db, lite):
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("PRAGMA synchronous=NORMAL").fetchall() == []
        assert connection.execute("PRAGMA busy_timeout=5000").fetchone()[0] == 5000
        assert isinstance(connection.execute("PRAGMA data_version").fetchone()[0], int)
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    version = db.execute("PRAGMA data_version").fetchone()[0]
    db.execute("INSERT INTO kv(k) VALUES ('x')")
    db.commit()
    assert db.execute("PRAGMA data_version").fetchone()[0] == version
    with pytest.raises(UntranslatableSQL):
        db.execute("PRAGMA database_list")


# ── parity over a representative workload ──

PARITY_QUERIES = [
    ("SELECT id, content, project FROM knowledge WHERE project = ? ORDER BY id", ("alpha",)),
    ("SELECT content FROM knowledge WHERE content LIKE ? ORDER BY id", ("%NOTE%",)),
    ("SELECT content FROM knowledge WHERE content LIKE ? ORDER BY id", ("50\\%%",)),
    ("SELECT content FROM knowledge WHERE score IS ? ORDER BY id", (None,)),
    ("SELECT content FROM knowledge WHERE score IS NOT ? ORDER BY id", (0.5,)),
    # NULL placement follows SQLite (first on ASC, last on DESC) via the translator.
    ("SELECT project, count(*), max(score), sum(id) FROM knowledge GROUP BY project ORDER BY project", ()),
    ("SELECT id, score FROM knowledge ORDER BY score DESC, id", ()),
    ("SELECT id, score FROM knowledge ORDER BY score, id DESC LIMIT 3", ()),
    ("SELECT id, row_number() OVER (ORDER BY score DESC) FROM knowledge ORDER BY id", ()),
    ("SELECT group_concat(project, ',' ORDER BY project) FROM knowledge", ()),
    ("SELECT group_concat(content, '|') FROM (SELECT content FROM knowledge ORDER BY id)", ()),
    ("SELECT json_extract(meta, '$.tags[0]'), json_extract(meta, '$.owner') FROM notes ORDER BY id", ()),
    ("SELECT substr(created_at, 1, 4) = strftime('%Y', 'now') FROM knowledge ORDER BY id LIMIT 1", ()),
    ("SELECT count(*) FROM knowledge WHERE julianday('now') - julianday(created_at) < 1", ()),
    ("SELECT id FROM knowledge WHERE instr(content, ':') > 0 ORDER BY id", ()),
    ("SELECT id FROM knowledge WHERE id IN (?, ?, ?) ORDER BY id DESC LIMIT 2", (1, 3, 4)),
    ("SELECT id FROM knowledge ORDER BY id LIMIT 1, 2", ()),
    ("SELECT ifnull(score, -1), max(id, 2), min(id, 2) FROM knowledge ORDER BY id", ()),
    ("SELECT CASE WHEN score > 0.4 THEN 'hi' ELSE 'lo' END FROM knowledge ORDER BY id", ()),
    ("SELECT k.id, n.id FROM knowledge k JOIN notes n ON n.knowledge_id = k.id ORDER BY k.id, n.id", ()),
    ("SELECT count(*) FROM knowledge WHERE id NOT IN ()", ()),
    ("SELECT upper(project) || ':' || id FROM knowledge WHERE project IS NOT NULL ORDER BY id", ()),
    ("SELECT datetime('2024-01-31', '+1 month'), date('2024-03-10', 'weekday 0')", ()),
    ("SELECT CAST(score * 100 AS INTEGER) FROM knowledge WHERE score IS NOT NULL ORDER BY id", ()),
]


def test_parity_with_sqlite_over_a_workload(db, lite):
    setup = [
        ("CREATE TABLE notes (id INTEGER PRIMARY KEY, knowledge_id INTEGER, meta TEXT)", ()),
        ("INSERT INTO knowledge(content, project, score) VALUES (?, ?, ?)", ("first note", "alpha", 0.5)),
        ("INSERT INTO knowledge(content, project, score) VALUES (?, ?, ?)", ("Second NOTE", "beta", None)),
        ("INSERT INTO knowledge(content, project, score) VALUES (?, ?, ?)", ("a:b", "alpha", 0.25)),
        ("INSERT INTO knowledge(content, project, score) VALUES (?, ?, ?)", ("50% done", None, 0.75)),
        ("INSERT INTO notes(knowledge_id, meta) VALUES (?, ?)", (1, '{"tags":["x","y"],"owner":"ann"}')),
        ("INSERT INTO notes(knowledge_id, meta) VALUES (?, ?)", (1, '{"tags":[],"owner":null}')),
        ("INSERT INTO notes(knowledge_id, meta) VALUES (?, ?)", (3, '{"owner":{"name":"bo"}}')),
        ("UPDATE knowledge SET project = ? WHERE id = ?", ("gamma", 2)),
        ("DELETE FROM notes WHERE id = ?", (2,)),
    ]
    for connection in (db, lite):
        for statement, parameters in setup:
            connection.execute(statement, parameters)
        connection.commit()
    for statement, parameters in PARITY_QUERIES:
        expected = lite.execute(statement, parameters).fetchall()
        actual = [tuple(row) for row in db.execute(statement, parameters).fetchall()]
        assert actual == expected, statement


def test_subclass_factory_hooks_commit(workspace):
    from tam_db.pg_connection import PgConnection, connect_url

    class Recording(PgConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.commits = 0

        def commit(self):
            self.commits += 1
            super().commit()

    db = connect_url(workspace.url, schema=workspace.schema, factory=Recording)
    try:
        assert isinstance(db, Recording)
        db.executescript("CREATE TABLE t (a TEXT)")
        with db:
            db.execute("INSERT INTO t VALUES ('x')")
        assert db.commits >= 2
    finally:
        db.close()
