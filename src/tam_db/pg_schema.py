"""Workspace schema migrations on PostgreSQL (migrations/postgres/workspace).

``ensure(connection)`` brings the schema the connection works in (its
``current_schema()``, the workspace schema at the head of the workspace role's
search_path) up to the newest bundled migration. It runs once per worker start,
in one transaction serialized by a transaction-level advisory lock, and records
every applied file with its SHA-256 in ``pg_migrations``. A recorded checksum that
no longer matches its file, or a recorded version this build does not ship, stops
the worker: the schema was changed by something other than these files.

Migration files are ``NNNN_name.sql`` numbered from 0001 without gaps and are run
verbatim (PL/pgSQL bodies included), never through the SQLite translator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from tam_db.contracts import PgDatabaseError, PgOperationalError, is_workspace_schema

LOGGER = logging.getLogger(__name__)

WORKSPACE_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "postgres" / "workspace"
MIGRATION_FILE_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
LEDGER_TABLE = "pg_migrations"
# Advisory lock keys follow team_memory.pg_provision: namespace "TAM\0" + n as int4.
# +0..+2 are the server, provisioning and workspace leases; +3 is workspace migrations.
LOCK_NAMESPACE_WORKSPACE_MIGRATIONS = 0x54414D00 + 3
LOCK_NAMESPACE_VECTOR_INDEXES = 0x54414D00 + 4
LOCK_DIGEST_BYTES = 4
# Attribute of tam_db.pg_connection.PgConnection exposing its psycopg connection.
NATIVE_ATTRIBUTE = "raw"
HNSW_INDEX_PREFIX = "embeddings_hnsw_"
MAX_VECTOR_DIMENSION = 16000


class WorkspaceSchemaError(PgDatabaseError):
    """The workspace schema cannot be migrated safely (drift, unknown version, wrong schema)."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @cached_property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @cached_property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def workspace_migrations(directory: Path = WORKSPACE_MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """The bundled workspace migrations in version order; the numbering must be 1..N."""
    if not directory.is_dir():
        raise WorkspaceSchemaError(f"Bundled PostgreSQL workspace migrations are missing: {directory}")
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILE_PATTERN.fullmatch(path.name)
        if match is None:
            raise WorkspaceSchemaError(f"Unexpected file among workspace migrations: {path.name}")
        migrations.append(Migration(int(match.group(1)), match.group(2), path))
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise WorkspaceSchemaError(f"Workspace migrations must be numbered 1..N without gaps, got {versions}")
    return tuple(migrations)


def native_connection(connection: Any) -> Any:
    """The psycopg connection behind ``connection`` (a PgConnection or psycopg itself)."""
    native = getattr(connection, NATIVE_ATTRIBUTE, None)
    if native is not None:
        return native
    import psycopg

    if isinstance(connection, psycopg.Connection):
        return connection
    raise TypeError("expected a tam_db PgConnection or a psycopg.Connection")


def lock_object(schema: str) -> int:
    """Signed 32-bit advisory lock object for a workspace schema."""
    digest = hashlib.sha256(schema.encode("utf-8")).digest()[:LOCK_DIGEST_BYTES]
    return int.from_bytes(digest, "big", signed=True)


def current_workspace_schema(native: Any) -> str:
    schema = native.execute("SELECT current_schema()").fetchone()[0]
    if schema is None or not is_workspace_schema(schema):
        raise WorkspaceSchemaError("The connection's search_path does not start with a workspace schema")
    return schema


def _require_idle(native: Any) -> None:
    from psycopg.pq import TransactionStatus

    if native.info.transaction_status is not TransactionStatus.IDLE:
        raise WorkspaceSchemaError("Workspace migrations need a connection outside any transaction")


def _ledger(native: Any, schema: str) -> dict[int, str]:
    from psycopg import sql

    table = sql.Identifier(schema, LEDGER_TABLE)
    native.execute(sql.SQL(
        "CREATE TABLE IF NOT EXISTS {} (version integer PRIMARY KEY, name text NOT NULL, "
        "checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())").format(table))
    rows = native.execute(sql.SQL("SELECT version, checksum FROM {} ORDER BY version").format(table)).fetchall()
    return {int(row[0]): row[1] for row in rows}


def _verify(applied: dict[int, str], bundled: tuple[Migration, ...]) -> None:
    known = {migration.version: migration for migration in bundled}
    unknown = sorted(version for version in applied if version not in known)
    if unknown:
        raise WorkspaceSchemaError(f"Workspace schema has migrations this build does not ship: {unknown}")
    changed = sorted(version for version, checksum in applied.items() if known[version].checksum != checksum)
    if changed:
        raise WorkspaceSchemaError(f"Applied workspace migrations differ from the bundled files: {changed}")


def ensure(connection: Any, directory: Path = WORKSPACE_MIGRATIONS_DIR, *,
           schema: str | None = None) -> tuple[int, ...]:
    """Apply pending workspace migrations; returns the versions applied now (often none).

    Runs as the workspace role over ``connection`` (a PgConnection or an idle psycopg
    connection) in its ``current_schema()``; ``schema``, when given, must be that schema.
    Takes its own transaction-level advisory lock, so callers need none.
    """
    import psycopg
    from psycopg import sql

    bundled = workspace_migrations(directory)
    native = native_connection(connection)
    _require_idle(native)
    current = current_workspace_schema(native)
    if schema is not None and schema != current:
        raise WorkspaceSchemaError("The connection's search_path starts with a different workspace schema")
    schema = current
    applied_now: list[int] = []
    try:
        with native.transaction():
            native.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                           (LOCK_NAMESPACE_WORKSPACE_MIGRATIONS, lock_object(schema)))
            applied = _ledger(native, schema)
            _verify(applied, bundled)
            for migration in bundled:
                if migration.version in applied:
                    continue
                native.execute(migration.sql)
                native.execute(sql.SQL("INSERT INTO {} (version, name, checksum) VALUES (%s, %s, %s)").format(
                    sql.Identifier(schema, LEDGER_TABLE)), (migration.version, migration.name, migration.checksum))
                applied_now.append(migration.version)
    except WorkspaceSchemaError:
        LOGGER.error(json.dumps({"event": "workspace_schema_refused", "schema": schema}))
        raise
    except psycopg.Error as exc:
        LOGGER.error(json.dumps({"event": "workspace_migration_failed", "schema": schema,
                                 "sqlstate": exc.sqlstate}))
        raise PgOperationalError("Workspace schema migration failed", sqlstate=exc.sqlstate) from exc
    if applied_now:
        LOGGER.info(json.dumps({"event": "workspace_migrated", "schema": schema, "versions": applied_now}))
    return tuple(applied_now)


def sync_identity_sequences(connection: Any) -> int:
    """Move every identity sequence of the workspace schema past the largest stored id.

    Rows copied with explicit ids (SQLite -> PostgreSQL migration) do not advance
    GENERATED BY DEFAULT identities; without this the next default id collides.
    Returns the number of sequences adjusted. Runs in the caller's transaction.
    """
    from psycopg import sql

    native = native_connection(connection)
    schema = current_workspace_schema(native)
    columns = native.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND is_identity = 'YES' ORDER BY table_name", (schema,)).fetchall()
    for table, column in columns:
        native.execute(sql.SQL(
            "SELECT setval(pg_get_serial_sequence(format('%%I.%%I', %s::text, %s::text), %s), "
            "COALESCE(max({column}), 1), max({column}) IS NOT NULL) FROM {table}").format(
                column=sql.Identifier(column), table=sql.Identifier(schema, table)),
            (schema, table, column))
    return len(columns)


def hnsw_index_name(dimension: int) -> str:
    return f"{HNSW_INDEX_PREFIX}{_dimension(dimension)}"


def _dimension(dimension: int) -> int:
    if type(dimension) is not int or not 1 <= dimension <= MAX_VECTOR_DIMENSION:
        raise ValueError(f"vector dimension must be an integer in 1..{MAX_VECTOR_DIMENSION}")
    return dimension


def ensure_hnsw_index(connection: Any, dimension: int) -> None:
    """Partial HNSW cosine index over embeddings of one dimension (idempotent).

    pgvector indexes need a fixed dimension; ``embedding`` has none, so the index is
    on ``embedding::vector(n)`` for the rows with ``embed_dim = n``. Run from
    an administrative connection in autocommit mode, outside any transaction.
    """
    from psycopg import sql
    from psycopg.pq import TransactionStatus

    native = native_connection(connection)
    schema = current_workspace_schema(native)
    size = _dimension(dimension)
    if not native.autocommit or native.info.transaction_status is not TransactionStatus.IDLE:
        raise WorkspaceSchemaError('Concurrent HNSW provisioning requires an idle autocommit connection')
    statement = sql.SQL(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} USING hnsw ((embedding::vector({size})) vector_cosine_ops) "
        "WHERE embed_dim = {size}").format(name=sql.Identifier(hnsw_index_name(size)),
                                           table=sql.Identifier(schema, "embeddings"), size=sql.Literal(size))
    acquired = native.execute('SELECT pg_try_advisory_lock(%s,%s)',
                              (LOCK_NAMESPACE_VECTOR_INDEXES, lock_object(schema))).fetchone()[0]
    if not acquired:
        raise WorkspaceSchemaError('Another vector index provisioning operation is running')
    try:
        state = _hnsw_index_state(connection, size)
        if state == 'invalid':
            native.execute(sql.SQL('REINDEX INDEX CONCURRENTLY {}').format(
                sql.Identifier(schema, hnsw_index_name(size))))
        elif state == 'missing':
            native.execute(statement)
        if not hnsw_index_ready(connection, size):
            raise WorkspaceSchemaError('HNSW index is not ready after provisioning')
    finally:
        native.execute('SELECT pg_advisory_unlock(%s,%s)', (LOCK_NAMESPACE_VECTOR_INDEXES, lock_object(schema)))
    LOGGER.info(json.dumps({"event": "workspace_hnsw_index_ready", "schema": schema, "dimension": size}))


def _hnsw_index_state(connection: Any, dimension: int) -> str:
    native = native_connection(connection)
    schema = current_workspace_schema(native)
    size = _dimension(dimension)
    row = native.execute(
        "SELECT i.indisvalid, i.indisready, t.relname, a.amname, pg_get_indexdef(c.oid), "
        "pg_get_expr(i.indpred,i.indrelid) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_index i ON i.indexrelid=c.oid LEFT JOIN pg_class t ON t.oid=i.indrelid "
        "LEFT JOIN pg_am a ON a.oid=c.relam WHERE n.nspname=%s AND c.relname=%s",
        (schema, hnsw_index_name(size)),
    ).fetchone()
    if row is None:
        return 'missing'
    if (row[2] != 'embeddings' or row[3] != 'hnsw' or f'vector({size})' not in (row[4] or '')
            or 'vector_cosine_ops' not in (row[4] or '') or row[5] != f'(embed_dim = {size})'):
        raise WorkspaceSchemaError('Existing HNSW index name has an incompatible definition')
    return 'ready' if row[0] and row[1] else 'invalid'


def hnsw_index_ready(connection: Any, dimension: int) -> bool:
    return _hnsw_index_state(connection, dimension) == 'ready'


__all__ = [
    "LEDGER_TABLE", "LOCK_NAMESPACE_WORKSPACE_MIGRATIONS", "NATIVE_ATTRIBUTE", "WORKSPACE_MIGRATIONS_DIR",
    "Migration", "WorkspaceSchemaError", "current_workspace_schema", "ensure", "ensure_hnsw_index",
    "hnsw_index_name", "lock_object", "native_connection", "sync_identity_sequences", "workspace_migrations",
]
