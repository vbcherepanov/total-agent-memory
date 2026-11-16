"""The PostgreSQL workspace baseline against a fresh SQLite Store.

Catalog parity: tables, columns (order, type, NOT NULL, defaults), primary keys,
UNIQUE constraints, CHECK constraints, indexes and triggers of a fresh SQLite
memory.db must reappear in migrations/postgres/workspace. Everything that differs
on purpose is listed in the allowlists below; a new SQLite migration without a
PostgreSQL counterpart fails here by design.

Behaviour parity: the same statements, run through the tam_db translator on
PostgreSQL and natively on SQLite, leave the trigger-maintained tables in the same
state.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import uuid
from pathlib import Path

import pytest

from tam_db.contracts import PgIntegrityError

pytestmark = pytest.mark.postgres

# SQLite objects with no PostgreSQL table: FTS5 virtual tables with their shadow
# tables (replaced by <table>_tsv side tables) and AUTOINCREMENT bookkeeping.
FTS5_TABLES = ("knowledge_fts", "errors_fts", "atomic_facts_fts", "evidence_passages_fts", "episodes_v11_fts")
FTS5_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")
SQLITE_ONLY_TABLES = frozenset({"sqlite_sequence", *FTS5_TABLES,
                                *(table + suffix for table in FTS5_TABLES for suffix in FTS5_SHADOW_SUFFIXES)})
# PostgreSQL-only tables: FTS side tables, BM25 statistics, migration ledger and the
# SQLite -> PostgreSQL migration quarantine.
PG_ONLY_TABLES = frozenset({"knowledge_tsv", "errors_tsv", "atomic_facts_tsv", "evidence_passages_tsv",
                            "episodes_v11_tsv", "fts_stats", "pg_migrations", "migration_quarantine"})
# PostgreSQL-only columns: the pgvector copy of each embedding.
PG_ONLY_COLUMNS = {"embeddings": ("embedding",)}
# PostgreSQL-only triggers: the episode FTS rows SQLite's extractor writes by hand.
PG_ONLY_TRIGGERS = frozenset({"episodes_v11_fts_insert", "episodes_v11_fts_update", "episodes_v11_fts_delete"})
TYPE_MAP = {"INTEGER": "bigint", "BOOLEAN": "bigint", "TEXT": "text", "REAL": "double precision",
            "BLOB": "bytea", "JSON": "text"}
EXPECTED_TRIGGERS = 39
WORKSPACE_KEY = "parity-workspace"
MASTER_KEY_BYTES = 32


@pytest.fixture(scope="module")
def sqlite_store(tmp_path_factory):
    import server

    root = tmp_path_factory.mktemp("sqlite-parity")
    previous = server.MEMORY_DIR
    server.MEMORY_DIR = root
    try:
        store = server.Store()
        yield store
        store.db.close()
    finally:
        server.MEMORY_DIR = previous


def _bundled_versions() -> tuple[int, ...]:
    """Versions a fresh workspace applies: every bundled workspace migration."""
    from tam_db import pg_schema

    return tuple(migration.version for migration in pg_schema.workspace_migrations())


def provision_unmigrated(admin_url: str, key: str):
    """Workspace schema and role without the workspace migrations; its StoreDatabase."""
    from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

    instance_id = PgProvisioner(admin_url).bootstrap(str(uuid.uuid4()))
    provisioner = PgWorkspaceProvisioner(admin_url, instance_id, secrets.token_bytes(MASTER_KEY_BYTES))
    provisioner.ensure(key, migrate=False)
    return provisioner.store_database(key)


@pytest.fixture
def workspace(pg_database):
    """A PgConnection (translator path, workspace role) to a freshly migrated workspace schema."""
    from tam_db import pg_connection, pg_schema

    connection = pg_connection.connect(provision_unmigrated(pg_database.url, WORKSPACE_KEY))
    connection.row_factory = sqlite3.Row
    assert pg_schema.ensure(connection) == _bundled_versions()
    yield connection
    connection.close()


# ─── catalog readers ────────────────────────────────────────────────────────

def _sqlite_tables(db) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") if row[0] not in SQLITE_ONLY_TABLES]


def _sqlite_columns(db, table: str) -> list[tuple[str, str, bool, bool, bool]]:
    """(name, mapped type, not null, has default, primary key) in declaration order."""
    columns = []
    for _, name, declared, notnull, default, pk, hidden in db.execute(f"PRAGMA table_xinfo('{table}')"):
        # DEFAULT NULL is no default at all; PostgreSQL does not record it.
        has_default = (default is not None and default.upper() != "NULL") or hidden != 0
        columns.append((name, TYPE_MAP[declared.upper()], bool(notnull), has_default, bool(pk)))
    return columns


def _pg_tables(pg) -> list[str]:
    return [row[0] for row in pg.execute_native(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename").fetchall()
            if row[0] not in PG_ONLY_TABLES]


def _pg_columns(pg, table: str) -> list[tuple[str, str, bool, bool]]:
    rows = pg.execute_native(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
        "a.atthasdef OR a.attidentity <> '' OR a.attgenerated <> '' "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = %s "
        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum", (table,)).fetchall()
    return [(row[0], row[1], bool(row[2]), bool(row[3])) for row in rows]


def _pg_constraint_columns(pg, table: str, kind: str) -> set[tuple[str, ...]]:
    rows = pg.execute_native(
        "SELECT array_agg(a.attname ORDER BY k.ordinality) FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ordinality) "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = %s AND con.contype = %s "
        "GROUP BY con.oid", (table, kind)).fetchall()
    return {tuple(row[0]) for row in rows}


def _pg_check_count(pg, table: str) -> int:
    return pg.execute_native(
        "SELECT count(*) FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
        "WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = %s AND con.contype = 'c'",
        (table,)).fetchone()[0]


def _sqlite_indexes(db) -> dict[str, tuple[str, bool, bool, tuple[str, ...]]]:
    """name -> (table, unique, partial, key columns; None marks an expression)."""
    indexes = {}
    for name, table in db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                                  "AND sql IS NOT NULL"):
        if table in SQLITE_ONLY_TABLES:
            continue
        info = db.execute(f"PRAGMA index_list('{table}')").fetchall()
        unique, partial = next((bool(row[2]), bool(row[4])) for row in info if row[1] == name)
        columns = tuple(row[2] for row in db.execute(f"PRAGMA index_xinfo('{name}')") if row[5])
        indexes[name] = (table, unique, partial, columns)
    return indexes


def _pg_indexes(pg) -> dict[str, tuple[str, bool, bool, tuple[str, ...]]]:
    rows = pg.execute_native(
        "SELECT i.relname, t.relname, x.indisunique, x.indpred IS NOT NULL, "
        "ARRAY(SELECT CASE WHEN k.attnum = 0 THEN NULL ELSE a.attname END "
        "      FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ordinality) "
        "      LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
        "      ORDER BY k.ordinality) "
        "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid "
        "WHERE t.relnamespace = current_schema()::regnamespace "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = x.indexrelid)").fetchall()
    return {row[0]: (row[1], row[2], row[3], tuple(row[4])) for row in rows if row[1] not in PG_ONLY_TABLES}


def _sqlite_triggers(db) -> dict[str, str]:
    return {row[0]: row[1] for row in db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='trigger'")}


def _pg_triggers(pg) -> dict[str, str]:
    rows = pg.execute_native(
        "SELECT t.tgname, c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relnamespace = current_schema()::regnamespace AND NOT t.tgisinternal").fetchall()
    return {row[0]: row[1] for row in rows}


def _check_count(sql: str) -> int:
    return len(re.findall(r"\bCHECK\s*\(", sql, re.IGNORECASE))


# ─── catalog parity ─────────────────────────────────────────────────────────

def test_tables_match(sqlite_store, workspace):
    assert _pg_tables(workspace) == _sqlite_tables(sqlite_store.db)


def test_columns_match_in_order_with_mapped_types(sqlite_store, workspace):
    for table in _sqlite_tables(sqlite_store.db):
        expected = _sqlite_columns(sqlite_store.db, table)
        actual = [column for column in _pg_columns(workspace, table)
                  if column[0] not in PG_ONLY_COLUMNS.get(table, ())]
        assert [column[0] for column in actual] == [column[0] for column in expected], table
        for (name, mapped, notnull, has_default, pk), (_, pg_type, pg_notnull, pg_default) in zip(
                expected, actual, strict=True):
            assert pg_type == mapped, f"{table}.{name}"
            if not pk:
                assert pg_notnull == notnull, f"{table}.{name} NOT NULL"
                assert pg_default == has_default, f"{table}.{name} DEFAULT"


def test_pg_only_columns_are_appended_last(workspace):
    for table, extra in PG_ONLY_COLUMNS.items():
        names = [column[0] for column in _pg_columns(workspace, table)]
        assert tuple(names[-len(extra):]) == extra
    assert _pg_columns(workspace, "embeddings")[-1][1] == "vector"


def test_integer_primary_keys_are_identities(sqlite_store, workspace):
    for table in _sqlite_tables(sqlite_store.db):
        columns = _sqlite_columns(sqlite_store.db, table)
        pk = tuple(row[1] for row in sorted(
            (row for row in sqlite_store.db.execute(f"PRAGMA table_info('{table}')") if row[5]),
            key=lambda row: row[5]))
        assert _pg_constraint_columns(workspace, table, "p") == ({pk} if pk else set()), table
        if len(pk) == 1 and {c[0]: c[1] for c in columns}[pk[0]] == "bigint":
            identity = workspace.execute_native(
                "SELECT is_identity FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = %s AND column_name = %s", (table, pk[0])).fetchone()[0]
            # Incident metadata borrows the knowledge ID; it must never allocate one.
            expected_identity = "NO" if (table, pk[0]) == ("knowledge_incidents", "knowledge_id") else "YES"
            assert identity == expected_identity, f"{table}.{pk[0]}"


def test_unique_and_check_constraints_match(sqlite_store, workspace):
    for table in _sqlite_tables(sqlite_store.db):
        expected = set()
        for row in sqlite_store.db.execute(f"PRAGMA index_list('{table}')"):
            if row[3] == "u":
                expected.add(tuple(info[2] for info in sqlite_store.db.execute(f"PRAGMA index_info('{row[1]}')")))
        assert _pg_constraint_columns(workspace, table, "u") == expected, table
        sql = sqlite_store.db.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        assert _pg_check_count(workspace, table) == _check_count(sql), table


def test_foreign_keys_are_not_declared(workspace):
    # memory.db runs with PRAGMA foreign_keys=OFF: references and their ON DELETE
    # actions are not enforced on SQLite, so PostgreSQL must not enforce them either.
    assert workspace.execute_native(
        "SELECT count(*) FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
        "WHERE c.relnamespace = current_schema()::regnamespace AND con.contype = 'f'").fetchone()[0] == 0


def test_indexes_match(sqlite_store, workspace):
    expected = _sqlite_indexes(sqlite_store.db)
    actual = _pg_indexes(workspace)
    assert set(actual) == set(expected)
    for name, (table, unique, partial, columns) in expected.items():
        pg_table, pg_unique, pg_partial, pg_columns = actual[name]
        assert (pg_table, pg_unique, pg_partial) == (table, unique, partial), name
        assert pg_columns == columns, name


# pg_index.indoption bits per key column.
INDOPTION_DESC = 1
INDOPTION_NULLS_FIRST = 2


def test_btree_keys_carry_sqlite_null_placement(sqlite_store, workspace):
    """Each key keeps SQLite's direction. Keys on nullable columns or expressions sort
    NULLs as SQLite does (ASC first, DESC last), like the ORDER BY clause the
    translator emits for them; keys on NOT NULL columns keep PostgreSQL's default,
    because the translator adds no NULLS clause for those."""
    directions = {}
    for name, table in sqlite_store.db.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
        if table not in SQLITE_ONLY_TABLES:
            directions[name] = tuple(bool(row[3]) for row in sqlite_store.db.execute(
                f"PRAGMA index_xinfo('{name}')") if row[5])
    rows = workspace.execute_native(
        "SELECT i.relname, x.indoption::int2[], "
        "ARRAY(SELECT COALESCE(a.attnotnull, false) FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, n) "
        "      LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum ORDER BY k.n) "
        "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid "
        "JOIN pg_am am ON am.oid = i.relam "
        "WHERE t.relnamespace = current_schema()::regnamespace AND am.amname = 'btree' "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = x.indexrelid)").fetchall()
    btree = {row[0]: tuple(zip(row[1], row[2], strict=True)) for row in rows}
    assert set(directions) <= set(btree)
    for name, keys in btree.items():
        for option, notnull in keys:
            descending = bool(option & INDOPTION_DESC)
            nulls_first = bool(option & INDOPTION_NULLS_FIRST)
            assert nulls_first == (descending if notnull else not descending), name
        if name in directions:
            assert tuple(bool(option & INDOPTION_DESC) for option, _ in keys) == directions[name], name


NEIGHBOURS = "SELECT id FROM knowledge WHERE project = 'p' AND session_id = 's' AND status = 'active' "
# ORDER BY in the form the translator emits it: a NULLS clause only for nullable terms.
TRANSLATED_ORDER_QUERIES = {
    "idx_e_created": ("SELECT id FROM errors ORDER BY created_at DESC LIMIT 5",
                      "SELECT id FROM errors ORDER BY created_at LIMIT 5"),
    "idx_k_last_confirmed": ("SELECT id FROM knowledge ORDER BY last_confirmed DESC NULLS LAST LIMIT 5",
                             "SELECT id FROM knowledge ORDER BY last_confirmed NULLS FIRST LIMIT 5"),
    "idx_knowledge_neighbors": (
        NEIGHBOURS + "ORDER BY created_at DESC, id DESC LIMIT 10",
        NEIGHBOURS + "ORDER BY created_at, id LIMIT 10"),
    "knowledge_pkey": ("SELECT id FROM knowledge ORDER BY id DESC LIMIT 10",
                       "SELECT id FROM knowledge ORDER BY id LIMIT 10"),
}


@pytest.mark.parametrize("index", sorted(TRANSLATED_ORDER_QUERIES))
def test_translated_order_by_uses_the_index_both_ways(workspace, index):
    """Both directions of a translated ORDER BY are served by an index scan, no Sort."""
    for query in TRANSLATED_ORDER_QUERIES[index]:
        workspace.execute("BEGIN")
        try:
            workspace.execute_native("SET LOCAL enable_seqscan = off")
            workspace.execute_native("SET LOCAL enable_sort = off")
            workspace.execute_native("SET LOCAL enable_bitmapscan = off")
            plan = "\n".join(row[0] for row in workspace.execute_native("EXPLAIN " + query))
        finally:
            workspace.rollback()
        assert index in plan, query
        assert "Sort" not in plan.replace("Sort Key", ""), query


class _Recorder:
    """Records the statements a helper sends and answers with no rows."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, list(params)))
        return self

    def fetchall(self):
        return []


@pytest.mark.parametrize("project", ["p", None])
def test_cross_encoder_neighbour_lookup_is_an_index_scan(workspace, project):
    """session_window_texts runs once per recall with up to 100 lookups: each must be an index
    probe on PostgreSQL, not a scan and sort of the whole session (IS ? would prevent that)."""
    from memory_core.cross_rerank import session_window_texts
    from tam_db.translate import translate

    recorder = _Recorder()
    session_window_texts(recorder, [{"id": 7, "session_id": "s", "project": project, "status": "active",
                                     "created_at": "2026-09-26T10:00:00Z", "content": "anchor"}], side_chars=10)
    ((sql, params),) = recorder.statements
    translation = translate(sql)
    text = translation.render(workspace._catalog) if translation.needs_catalog else translation.sql
    workspace.execute("BEGIN")
    try:
        workspace.execute_native("SET LOCAL enable_seqscan = off")
        workspace.execute_native("SET LOCAL enable_sort = off")
        workspace.execute_native("SET LOCAL enable_bitmapscan = off")
        plan = "\n".join(row[0] for row in workspace.execute_native("EXPLAIN " + text, translation.bind(params)))
    finally:
        workspace.rollback()
    assert plan.count("idx_knowledge_neighbors") == 2, plan
    if project is not None:
        assert "Sort" not in plan.replace("Sort Key", ""), plan
    # project IS NULL (legacy rows only: saves always set a project) filters through the index, but
    # PostgreSQL does not treat IS NULL as an equality for ordering, so it still sorts the matches.


def test_triggers_match(sqlite_store, workspace):
    expected = _sqlite_triggers(sqlite_store.db)
    actual = _pg_triggers(workspace)
    assert len(expected) == EXPECTED_TRIGGERS
    assert {name: table for name, table in actual.items() if name not in PG_ONLY_TRIGGERS} == expected
    assert PG_ONLY_TRIGGERS <= set(actual)


def test_seed_rows_match(sqlite_store, workspace):
    for table, key in (("migrations", "version"), ("privacy_counters", "key"),
                       ("vector_index_revision", "singleton")):
        sqlite_rows = [tuple(row) for row in sqlite_store.db.execute(f"SELECT * FROM {table} ORDER BY {key}")]
        pg_rows = [tuple(row) for row in workspace.execute(f"SELECT * FROM {table} ORDER BY {key}")]
        if table == "migrations":
            sqlite_rows = [row[:2] for row in sqlite_rows]
            pg_rows = [row[:2] for row in pg_rows]
        assert pg_rows == sqlite_rows, table


def test_fts_side_tables_cover_every_fts5_column(workspace):
    from memory_core.pg_fts import FTS_SOURCES, ColumnKind

    assert set(FTS_SOURCES) == set(FTS5_TABLES)
    for source in FTS_SOURCES.values():
        names = {column[0] for column in _pg_columns(workspace, source.side_table)}
        for column in source.columns:
            assert column.name in names
            if column.kind is ColumnKind.TOKENS:
                assert f"{column.name}_lexemes" in names


# ─── migration runner ───────────────────────────────────────────────────────

def test_ensure_is_idempotent_and_records_checksums(workspace):
    from tam_db import pg_schema

    assert pg_schema.ensure(workspace) == ()
    ledger = workspace.execute_native("SELECT version, name, checksum FROM pg_migrations").fetchall()
    bundled = pg_schema.workspace_migrations()
    assert [tuple(row) for row in ledger] == [(m.version, m.name, m.checksum) for m in bundled]


def test_ensure_refuses_drifted_schema(workspace):
    from tam_db import pg_schema

    workspace.execute_native("UPDATE pg_migrations SET checksum = 'edited' WHERE version = 1")
    workspace.commit()
    with pytest.raises(pg_schema.WorkspaceSchemaError, match="differ"):
        pg_schema.ensure(workspace)


def test_ensure_refuses_unknown_versions(workspace):
    from tam_db import pg_schema

    workspace.execute_native("INSERT INTO pg_migrations (version, name, checksum) VALUES (99, 'future', 'x')")
    workspace.commit()
    with pytest.raises(pg_schema.WorkspaceSchemaError, match="does not ship"):
        pg_schema.ensure(workspace)


def test_ensure_refuses_a_non_workspace_schema(pg_database):
    import psycopg

    from tam_db import pg_schema

    with psycopg.connect(pg_database.url, autocommit=True) as raw:
        raw.execute("SET search_path TO public")
        with pytest.raises(pg_schema.WorkspaceSchemaError, match="workspace schema"):
            pg_schema.ensure(raw)


def test_ensure_checks_the_expected_schema(workspace):
    from tam_db import pg_schema
    from tam_db.contracts import schema_for

    current = workspace.execute_native("SELECT current_schema()").fetchone()[0]
    assert pg_schema.ensure(workspace, schema=current) == ()
    with pytest.raises(pg_schema.WorkspaceSchemaError, match="different workspace schema"):
        pg_schema.ensure(workspace, schema=schema_for("another-workspace"))


def test_ensure_refuses_an_open_transaction(workspace):
    from tam_db import pg_schema

    workspace.execute("INSERT INTO sessions (id, started_at) VALUES ('s', '2026-01-01T00:00:00Z')")
    assert workspace.in_transaction
    with pytest.raises(pg_schema.WorkspaceSchemaError, match="outside any transaction"):
        pg_schema.ensure(workspace)
    workspace.rollback()


def test_concurrent_ensure_applies_once(pg_database):
    import threading

    from tam_db import pg_connection, pg_schema

    database = provision_unmigrated(pg_database.url, "concurrent")
    connections = [pg_connection.connect(database) for _ in range(3)]
    results, errors = [], []

    def run(connection):
        try:
            results.append(pg_schema.ensure(connection))
        except Exception as exc:  # noqa: BLE001 — collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(connection,)) for connection in connections]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for connection in connections:
        connection.close()
    assert errors == []
    assert sorted(results) == [(), (), _bundled_versions()]


def test_migration_files_must_be_numbered_without_gaps(tmp_path: Path):
    from tam_db import pg_schema

    (tmp_path / "0001_baseline.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_later.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(pg_schema.WorkspaceSchemaError, match="without gaps"):
        pg_schema.workspace_migrations(tmp_path)
    (tmp_path / "notes.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(pg_schema.WorkspaceSchemaError, match="Unexpected file"):
        pg_schema.workspace_migrations(tmp_path)
    with pytest.raises(pg_schema.WorkspaceSchemaError, match="missing"):
        pg_schema.workspace_migrations(tmp_path / "absent")


def test_failed_migration_rolls_back_and_raises(pg_database, tmp_path: Path):
    from tam_db import pg_connection, pg_schema
    from tam_db.contracts import PgOperationalError

    (tmp_path / "0001_broken.sql").write_text("CREATE TABLE half_done (id bigint); SELECT no_such_function();", encoding="utf-8")
    connection = pg_connection.connect(provision_unmigrated(pg_database.url, "broken"))
    try:
        with pytest.raises(PgOperationalError, match="migration failed"):
            pg_schema.ensure(connection, tmp_path)
        assert connection.execute_native("SELECT to_regclass('half_done')").fetchone()[0] is None
    finally:
        connection.close()


def test_sync_identity_sequences_after_explicit_ids(workspace):
    from tam_db import pg_schema

    workspace.execute("INSERT INTO knowledge (id, session_id, type, content, created_at) "
                      "VALUES (41, 's', 'fact', 'copied', '2026-01-01T00:00:00Z')")
    workspace.commit()
    assert pg_schema.sync_identity_sequences(workspace) > 0
    workspace.commit()
    cursor = workspace.execute("INSERT INTO knowledge (session_id, type, content, created_at) "
                               "VALUES ('s', 'fact', 'next', '2026-01-01T00:00:00Z')")
    workspace.commit()
    assert cursor.lastrowid == 42


# ─── behaviour ──────────────────────────────────────────────────────────────

def test_link_guard_raises_integrity_error(workspace):
    with pytest.raises(sqlite3.IntegrityError, match="Graph link requires existing knowledge and node"):
        workspace.execute("INSERT INTO knowledge_nodes (knowledge_id, node_id) VALUES (1, 'missing')")
    workspace.rollback()
    with pytest.raises(PgIntegrityError):
        workspace.execute("INSERT INTO knowledge_nodes (knowledge_id, node_id) VALUES (1, 'missing')")
    workspace.rollback()


def test_defaults_and_generated_values(workspace):
    workspace.execute("INSERT INTO graph_nodes (id, type, name) VALUES ('n1', 'concept', '  Ärger Node ')")
    workspace.execute("INSERT INTO episodes_v11 (project, started_at, ended_at, summary) "
                      "VALUES ('p', '2026-01-01', '2026-01-01', 's')")
    for project, token in (("general", "p67656e6572616c"), ("бета", "pd0b1d0b5d182d0b0"), (None, "p")):
        workspace.execute("INSERT INTO knowledge (session_id, type, content, created_at, project) "
                          "VALUES ('s', 'fact', 'x', '2026-01-01T00:00:00Z', ?)", (project,))
        assert workspace.execute("SELECT fts_project FROM knowledge ORDER BY id DESC LIMIT 1").fetchone()[0] == token
    workspace.commit()
    node = workspace.execute("SELECT name_norm, first_seen_at FROM graph_nodes WHERE id='n1'").fetchone()
    assert node[0] == "ärger node"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", node[1])
    created = workspace.execute("SELECT created_at FROM episodes_v11").fetchone()[0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", created)


def test_fts_project_token_matches_sqlite(sqlite_store, workspace):
    for project in ("general", "бета", "a b/c", ""):
        expected = sqlite_store.db.execute("SELECT 'p' || lower(hex(?))", (project,)).fetchone()[0]
        assert workspace.execute_native("SELECT tam_fts_project_token(%s)", (project,)).fetchone()[0] == expected


SCENARIO = [
    (("INSERT INTO knowledge (id, session_id, type, content, context, tags, project, created_at) "
      "VALUES (?, 's', 'fact', ?, 'ctx', '[\"t\"]', 'proj', '2026-01-01T00:00:00Z')"),
     [(1, "alpha one"), (2, "beta two"), (3, "gamma three")]),
    ("INSERT INTO graph_nodes (id, type, name) VALUES (?, 'concept', ?)", [("n1", "Alpha"), ("n2", "Beta")]),
    ("INSERT INTO knowledge_nodes (knowledge_id, node_id) VALUES (?, ?)", [(1, "n1"), (2, "n1"), (2, "n2")]),
    (("INSERT INTO atomic_facts (id, knowledge_id, subject, predicate, object, content) "
      "VALUES (?, ?, 's', 'p', 'o', ?)"), [(10, 1, "fact one"), (11, 2, "fact two"), (12, 3, "fact three")]),
    ("INSERT INTO atomic_fact_sources (fact_id, knowledge_id, quote) VALUES (?, ?, 'q')", [(10, 1), (11, 2), (12, 1)]),
    ("INSERT INTO atomic_fact_runs (knowledge_id, source_content, fact_count, model) VALUES (?, 'c', 1, 'm')",
     [(1,), (2,), (3,)]),
    ("INSERT INTO atomic_fact_dependencies (target_id, source_id) VALUES (?, ?)", [(3, 1), (2, 1)]),
    ("INSERT INTO passage_sources (knowledge_id, fingerprint, model) VALUES (?, 'f', 'm')", [(1,), (2,)]),
    (("INSERT INTO evidence_passages (knowledge_id, ordinal, start_char, end_char, speaker, content) "
      "VALUES (?, ?, 0, 5, 'user', ?)"), [(1, 0, "passage a"), (1, 1, "passage b"), (2, 0, "passage c")]),
    (("INSERT INTO embeddings (knowledge_id, binary_vector, float32_vector, embed_model, embed_dim, created_at) "
      "VALUES (?, X'00', X'00000000', 'm', 1, 'now')"), [(1,), (2,), (3,)]),
    ("UPDATE embeddings SET embed_model = 'm2' WHERE knowledge_id = ?", [(2,)]),
    ("UPDATE knowledge SET status = 'archived' WHERE id = ?", [(3,)]),
    ("UPDATE knowledge SET recall_count = recall_count + 1 WHERE id = ?", [(2,)]),
    ("UPDATE knowledge SET content = 'beta changed' WHERE id = ?", [(2,)]),
    ("DELETE FROM knowledge WHERE id = ?", [(1,)]),
    ("DELETE FROM graph_nodes WHERE id = ?", [("n2",)]),
    ("DELETE FROM atomic_facts WHERE id = ?", [(12,)]),
]
COMPARED_TABLES = {
    "knowledge": "SELECT id, content, status FROM knowledge ORDER BY id",
    "knowledge_nodes": "SELECT knowledge_id, node_id FROM knowledge_nodes ORDER BY 1, 2",
    "graph_nodes": "SELECT id, name_norm FROM graph_nodes ORDER BY id",
    "atomic_facts": "SELECT id, knowledge_id FROM atomic_facts ORDER BY id",
    "atomic_fact_sources": "SELECT fact_id, knowledge_id FROM atomic_fact_sources ORDER BY 1, 2",
    "atomic_fact_runs": "SELECT knowledge_id FROM atomic_fact_runs ORDER BY 1",
    "atomic_fact_dependencies": "SELECT target_id, source_id FROM atomic_fact_dependencies ORDER BY 1, 2",
    "atomic_fact_rebuild": "SELECT knowledge_id FROM atomic_fact_rebuild ORDER BY 1",
    "passage_sources": "SELECT knowledge_id FROM passage_sources ORDER BY 1",
    "evidence_passages": "SELECT id, knowledge_id, content FROM evidence_passages ORDER BY id",
    "vector_index_revision": "SELECT revision FROM vector_index_revision",
    "vector_changes": "SELECT revision, knowledge_id FROM vector_changes ORDER BY seq",
}


def _run_scenario(connection) -> dict[str, list[tuple]]:
    for statement, rows in SCENARIO:
        for params in rows:
            connection.execute(statement, params)
    connection.commit()
    return {table: [tuple(row) for row in connection.execute(query)] for table, query in COMPARED_TABLES.items()}


def test_triggers_behave_like_sqlite(workspace, tmp_path):
    import server

    previous = server.MEMORY_DIR
    server.MEMORY_DIR = tmp_path
    try:
        store = server.Store()
        try:
            expected = _run_scenario(store.db)
        finally:
            store.db.close()
    finally:
        server.MEMORY_DIR = previous
    assert _run_scenario(workspace) == expected


def test_fts_side_tables_follow_the_scenario(workspace):
    _run_scenario(workspace)
    knowledge = workspace.execute_native("SELECT id, content FROM knowledge_tsv ORDER BY id").fetchall()
    assert [(row[0], list(row[1])) for row in knowledge] == [(2, ["beta", "changed"]), (3, ["gamma", "three"])]
    for side, table in (("atomic_facts_tsv", "atomic_facts"), ("evidence_passages_tsv", "evidence_passages")):
        indexed = workspace.execute_native(f"SELECT id FROM {side} ORDER BY id").fetchall()
        stored = workspace.execute_native(f"SELECT id FROM {table} ORDER BY id").fetchall()
        assert [row[0] for row in indexed] == [row[0] for row in stored], side
    stats = {row[0]: (row[1], row[2]) for row in workspace.execute_native(
        "SELECT source, doc_count, total_length FROM fts_stats").fetchall()}
    for source, table in (("knowledge_tsv", "knowledge_tsv"), ("atomic_facts_tsv", "atomic_facts_tsv"),
                          ("evidence_passages_tsv", "evidence_passages_tsv")):
        count, total = workspace.execute_native(
            f"SELECT count(*), COALESCE(sum(doc_length), 0) FROM {table}").fetchone()
        assert stats[source] == (count, total)


def test_embedding_fingerprints_table_exists_with_its_key(workspace):
    columns = {column[0]: column for column in _pg_columns(workspace, "embedding_fingerprints")}
    assert list(columns) == ["space", "model", "provider", "dim", "pooling", "library", "probe_vector", "recorded_at"]
    assert columns["probe_vector"][1] == "bytea"
    workspace.execute("INSERT INTO embedding_fingerprints(space,model,provider,dim,probe_vector,recorded_at) "
                      "VALUES ('s','m','p',3,?,'now')", (b"x",))
    with pytest.raises(PgIntegrityError):
        workspace.execute("INSERT INTO embedding_fingerprints(space,model,provider,dim,probe_vector,recorded_at) "
                          "VALUES ('s','m','p',3,?,'now')", (b"x",))
