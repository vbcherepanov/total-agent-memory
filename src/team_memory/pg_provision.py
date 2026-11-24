"""PostgreSQL provisioning for the team server (plan 1.3-1.5).

The database itself is external: the DBA creates it (and, if TAM may not, the extensions).
TAM, connected as its admin role, creates the control schemas ``tam_control`` and
``tam_learning``, one schema plus one LOGIN role per workspace, and takes advisory locks so
only one server serves an installation. Workspace role passwords are never stored: they are
HMAC-SHA256 of the role name under the master key, so ``ensure`` can always reset them.

psycopg is used directly here: this module issues DDL with quoted identifiers and needs no
SQLite compatibility.
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self

from pydantic import SecretStr

from tam_db.contracts import (
    COMPAT_SCHEMA,
    CONTROL_SCHEMA,
    EXTENSIONS_SCHEMA,
    LEARNING_SCHEMA,
    REQUIRED_EXTENSIONS,
    SERIALIZATION_SQLSTATES,
    DatabaseSettings,
    StoreDatabase,
    WorkspaceTarget,
    schema_for,
)
from team_memory.contracts import Conflict, DomainError, Unavailable
from team_memory.database_contracts import CheckReport, DatabaseDsn, DsnOrigin
from team_memory.db_check import (
    CATEGORY_MESSAGES,
    TRANSACTION_POOLING_MESSAGE,
    categorize,
    extension_sql,
)

LOGGER = logging.getLogger(__name__)
MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "migrations" / "postgres"
COMPAT_MIGRATIONS_DIR = MIGRATIONS_ROOT / "compat"
CONTROL_MIGRATIONS_DIR = MIGRATIONS_ROOT / "control"
# (file, schema it is applied in). Order is the application order.
CONTROL_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("0001_identity.sql", CONTROL_SCHEMA),
    ("0002_learning.sql", LEARNING_SCHEMA),
    ("0003_workspaces.sql", CONTROL_SCHEMA),
    ("0004_workspace_fingerprint.sql", CONTROL_SCHEMA),
    ("0005_token_expiry.sql", CONTROL_SCHEMA),
    ("0006_known_devices.sql", CONTROL_SCHEMA),
)
LEDGER_TABLE = "control_migrations"
META_INSTANCE_KEY = "instance_id"
WORKSPACE_TABLE = "workspace_schemas"

# Advisory lock keys: pg_advisory_lock(namespace int4, object int4). "TAM\0" in ASCII.
LOCK_NAMESPACE = 0x54414D00
LOCK_SERVER = 1
LOCK_MIGRATIONS = 2
LOCK_NAMESPACE_PROVISION = LOCK_NAMESPACE + 1
LOCK_NAMESPACE_WORKSPACE = LOCK_NAMESPACE + 2
LOCK_DIGEST_BYTES = 4
# ALTER keyword per pg_class.relkind of an object a restored schema may hold.
ADOPT_RELATION_KINDS = {"r": "TABLE", "p": "TABLE", "S": "SEQUENCE", "v": "VIEW", "m": "MATERIALIZED VIEW",
                        "f": "FOREIGN TABLE"}

EMPTY_PASSFILE = ".empty-pgpass"
INTERNAL_ERROR_SQLSTATE = "XX000"
CONCURRENT_UPDATE_MARKER = "tuple concurrently updated"
PROVISION_RETRY_SECONDS = 0.05
LEASE_CHECK_ENV = "TAM_TEAM_PG_LEASE_CHECK_SECONDS"
DEFAULT_LEASE_CHECK_SECONDS = 10.0
JOIN_GRACE_SECONDS = 5.0
OID_MASK = 0xFFFFFFFF

PASSWORD_CONTEXT = b"tam-workspace-role\0"
ADMIN_APPLICATION = "tam-control"
LEASE_APPLICATION = "tam-lease"
WORKER_APPLICATION = "tam-worker"
MILLISECONDS = 1000


def _transient(exc: BaseException) -> bool:
    """Serialization failures and PostgreSQL's "tuple concurrently updated" on shared catalogs."""
    sqlstate = getattr(exc, "sqlstate", None)
    return sqlstate in SERIALIZATION_SQLSTATES or (
        sqlstate == INTERNAL_ERROR_SQLSTATE and CONCURRENT_UPDATE_MARKER in str(exc))


class PrerequisitesMissing(DomainError):
    """The target database does not meet plan 1.4; ``report`` carries the failed checks and DBA SQL."""

    def __init__(self, message: str, report: CheckReport | None = None, dba_sql: tuple[str, ...] = ()):
        super().__init__(message)
        self.report = report
        self.dba_sql = dba_sql or (tuple(sql for check in report.checks for sql in check.dba_sql) if report else ())


def lock_object(key: str) -> int:
    """Signed 32-bit advisory lock object for a workspace key."""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:LOCK_DIGEST_BYTES], "big", signed=True)


def role_password(master_key: bytes, role: str) -> str:
    return hmac.new(master_key, PASSWORD_CONTEXT + role.encode("utf-8"), hashlib.sha256).hexdigest()


def session_options(settings: DatabaseSettings, *, idle_session: bool = False) -> str:
    options = [f"-c statement_timeout={settings.statement_timeout_ms}",
               f"-c lock_timeout={settings.lock_timeout_ms}",
               f"-c idle_in_transaction_session_timeout={settings.idle_in_transaction_timeout_ms}",
               # Admin sessions inherit workspace roles; should a workspace table ever carry a
               # row-level security policy, reading it must fail loudly instead of returning a
               # silently filtered (or bypassed) view.
               "-c row_security=off"]
    if idle_session:
        # A lease connection sits idle for the server's lifetime; a server-wide
        # idle_session_timeout would silently release the lock.
        options.append("-c idle_session_timeout=0")
    return " ".join(options)


NO_OVERRIDES: Mapping[str, str] = MappingProxyType({})


def empty_passfile(root: Path) -> Path:
    """``<root>/.empty-pgpass``: an empty 0600 file for WEB-origin connections
    (DatabaseDsn.connect_overrides), so ~/.pgpass of the server's user is never read."""
    path = root / EMPTY_PASSFILE
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.stat().st_size or path.stat().st_mode & 0o077:
            # Anything written into it would be used as a password source; start empty again.
            path.write_bytes(b"")
            path.chmod(0o600)
        return path
    os.close(descriptor)
    return path


def admin_connect(url: str, settings: DatabaseSettings, application_name: str = ADMIN_APPLICATION,
                  *, idle_session: bool = False, connect_overrides: Mapping[str, str] = NO_OVERRIDES):
    """An autocommit psycopg connection as the TAM admin role; callers open transactions explicitly.

    ``connect_overrides``: DatabaseDsn.connect_overrides(origin, passfile) of the target (empty for
    an operator-supplied DSN). Driver errors are re-raised as Unavailable with a fixed, secret-free message.
    """
    import psycopg

    try:
        return psycopg.connect(url, autocommit=True, connect_timeout=settings.connect_timeout_seconds,
                               application_name=application_name,
                               options=session_options(settings, idle_session=idle_session), **connect_overrides)
    except psycopg.Error as exc:
        category = categorize(exc)
        LOGGER.error(json.dumps({"event": "database_connect_failed", "category": category.value}))
        raise Unavailable(CATEGORY_MESSAGES[category]) from None


def _identifier(name: str):
    from psycopg import sql

    return sql.Identifier(name)


def _migration_files() -> list[tuple[str, Path, str]]:
    """(ledger name, file, schema) in application order: compatibility functions, then control."""
    files = [(f"compat/{path.name}", path, COMPAT_SCHEMA) for path in sorted(COMPAT_MIGRATIONS_DIR.glob("*.sql"))]
    files += [(f"control/{name}", CONTROL_MIGRATIONS_DIR / name, schema) for name, schema in CONTROL_MIGRATIONS]
    return files


@dataclass(frozen=True)
class InstallationState:
    instance_id: str | None
    control_schema: bool


def installation_state(connection) -> InstallationState:
    exists = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{CONTROL_SCHEMA}.meta",)).fetchone()[0]
    if not exists:
        control = connection.execute("SELECT to_regnamespace(%s) IS NOT NULL", (CONTROL_SCHEMA,)).fetchone()[0]
        return InstallationState(instance_id=None, control_schema=bool(control))
    row = connection.execute(f"SELECT value FROM {CONTROL_SCHEMA}.meta WHERE key = %s", (META_INSTANCE_KEY,)).fetchone()
    return InstallationState(instance_id=row[0] if row else None, control_schema=True)


class PgProvisioner:
    """Control schemas, extensions and the installation id of one organization database."""

    def __init__(self, url: str, settings: DatabaseSettings | None = None, *,
                 connect_overrides: Mapping[str, str] = NO_OVERRIDES):
        self.url = url
        self.settings = settings or DatabaseSettings()
        self.connect_overrides = dict(connect_overrides)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        connection = admin_connect(self.url, self.settings, connect_overrides=self.connect_overrides)
        try:
            yield connection
        finally:
            connection.close()

    def state(self) -> InstallationState:
        with self.connection() as connection:
            return installation_state(connection)

    def bootstrap(self, instance_id: str) -> str:
        """Idempotent. Creates what is missing and returns the installation id stored in the target:
        ``instance_id`` for a new installation, the existing id otherwise (the caller compares)."""
        with self.connection() as connection:
            self._ensure_extensions(connection)
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_MIGRATIONS))
                applied = self._apply_migrations(connection)
                connection.execute(f"INSERT INTO {CONTROL_SCHEMA}.meta (key, value) VALUES (%s, %s) "
                                   "ON CONFLICT (key) DO NOTHING", (META_INSTANCE_KEY, instance_id))
                stored = connection.execute(f"SELECT value FROM {CONTROL_SCHEMA}.meta WHERE key = %s",
                                            (META_INSTANCE_KEY,)).fetchone()[0]
        LOGGER.info(json.dumps({"event": "control_plane_provisioned", "migrations_applied": applied,
                                "new_installation": stored == instance_id and bool(applied)}))
        return stored

    def _ensure_extensions(self, connection) -> None:
        import psycopg
        from psycopg import sql

        installed = {name: schema for name, schema in connection.execute(
            "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace")}
        misplaced = [name for name in REQUIRED_EXTENSIONS if name in installed and installed[name] != EXTENSIONS_SCHEMA]
        if misplaced:
            raise PrerequisitesMissing(
                "Extensions installed outside schema extensions: " + ", ".join(misplaced),
                dba_sql=tuple(f"ALTER EXTENSION {name} SET SCHEMA {EXTENSIONS_SCHEMA};" for name in misplaced))
        try:
            with connection.transaction():
                connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(_identifier(EXTENSIONS_SCHEMA)))
                for name in REQUIRED_EXTENSIONS:
                    if name not in installed:
                        connection.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {} SCHEMA {}").format(
                            _identifier(name), _identifier(EXTENSIONS_SCHEMA)))
        except psycopg.Error as exc:
            LOGGER.error(json.dumps({"event": "extensions_missing", "category": categorize(exc).value}))
            raise PrerequisitesMissing("Required extensions (" + ", ".join(REQUIRED_EXTENSIONS) +
                                       ") are missing and TAM may not create them",
                                       dba_sql=extension_sql()) from None
        owned, public_usage = connection.execute(
            "SELECT pg_get_userbyid(nspowner) = current_user, EXISTS (SELECT 1 FROM aclexplode("
            "COALESCE(nspacl, acldefault('n', nspowner))) acl WHERE acl.grantee = 0 AND acl.privilege_type = 'USAGE') "
            "FROM pg_namespace WHERE nspname = %s", (EXTENSIONS_SCHEMA,)).fetchone()
        if public_usage:
            return
        if not owned:
            raise PrerequisitesMissing("Workspace roles cannot use schema extensions",
                                       dba_sql=(f"GRANT USAGE ON SCHEMA {EXTENSIONS_SCHEMA} TO PUBLIC;",))
        # Only extension types and functions live here; every workspace role needs them.
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(_identifier(EXTENSIONS_SCHEMA)))

    def _apply_migrations(self, connection) -> int:
        from psycopg import sql

        for schema in (CONTROL_SCHEMA, LEARNING_SCHEMA, COMPAT_SCHEMA):
            connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(_identifier(schema)))
        # Control data is private to the admin role: workspace roles get nothing here.
        for schema in (CONTROL_SCHEMA, LEARNING_SCHEMA):
            connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(_identifier(schema)))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(_identifier(COMPAT_SCHEMA)))
        connection.execute(f"CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.{LEDGER_TABLE} ("
                           "name text PRIMARY KEY, schema text NOT NULL, sha256 text NOT NULL, "
                           "applied_at timestamptz NOT NULL DEFAULT clock_timestamp())")
        ledger = dict(connection.execute(f"SELECT name, sha256 FROM {CONTROL_SCHEMA}.{LEDGER_TABLE}").fetchall())
        # A restore of tam_control brings the ledger back but not tam_compat (pg_backup dumps
        # control and workspace schemas only): functions are then re-created.
        compat_present = connection.execute("SELECT EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = "
                                            "to_regnamespace(%s))", (COMPAT_SCHEMA,)).fetchone()[0]
        applied = 0
        for name, path, schema in _migration_files():
            script = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
            if ledger.get(name) == digest and (schema != COMPAT_SCHEMA or compat_present):
                continue
            if name in ledger and schema != COMPAT_SCHEMA:
                raise Conflict(f"PostgreSQL migration {name} changed after it was applied")
            # Compatibility functions are idempotent by design and re-run when a release changes them.
            connection.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(_identifier(schema)))
            connection.execute(script)
            connection.execute(f"INSERT INTO {CONTROL_SCHEMA}.{LEDGER_TABLE} (name, schema, sha256) "
                               "VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET sha256 = excluded.sha256, "
                               "applied_at = clock_timestamp()", (name, schema, digest))
            applied += 1
        connection.execute("SET LOCAL search_path TO pg_catalog")
        return applied


class PgWorkspaceProvisioner:
    """WorkspaceProvisioner (tam_db.contracts) for PostgreSQL: one schema and one LOGIN role per workspace.

    The role owns only its schema, has no privileges on any other schema but the shared
    ``tam_compat`` functions and ``extensions`` types, and cannot log in with more than
    ``workspace_connection_limit`` sessions. The admin role becomes a member WITH SET TRUE,
    INHERIT TRUE: it can act as the role (drop) and read every workspace (pg_dump backups); the
    admin DSN already controls all roles, so this grants it nothing new.
    """

    def __init__(self, url: str, instance_id: str, master_key: bytes, settings: DatabaseSettings | None = None,
                 *, connect_overrides: Mapping[str, str] = NO_OVERRIDES):
        self.url = url
        # Applied to the admin sessions and to every workspace-role session (store_database).
        self.connect_overrides = dict(connect_overrides)
        self.instance_id = instance_id
        self.master_key = master_key
        self.settings = settings or DatabaseSettings()
        self.dsn = DatabaseDsn.parse(url, DsnOrigin.ENV)
        self._secrets = (self.dsn.password.get_secret_value(),) if self.dsn.password is not None else ()

    def target(self, key: str) -> WorkspaceTarget:
        return WorkspaceTarget.for_key(key, self.instance_id)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        """An admin session; driver errors inside the block become tam_db contract errors (redacted)."""
        import psycopg

        from tam_db.pg_connection import map_error

        connection = admin_connect(self.url, self.settings, connect_overrides=self.connect_overrides)
        try:
            yield connection
        except psycopg.Error as exc:
            raise map_error(exc, self._secrets) from exc
        finally:
            connection.close()

    def fingerprint(self, target: WorkspaceTarget) -> str:
        """Digest of the role state ensure() applies; the password enters only as its SHA-256."""
        password = hashlib.sha256(role_password(self.master_key, target.role).encode()).hexdigest()
        state = {"password": password, "connection_limit": self.settings.workspace_connection_limit,
                 "settings": [(name, value.as_string()) for name, value in self._role_settings(target)]}
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    def ensure(self, key: str, *, migrate: bool = True, force: bool = False) -> WorkspaceTarget:
        """Idempotent and cheap: when role, schema, membership, map row (with the current
        fingerprint) and, if ``migrate``, the migration ledger are all in place, nothing is
        written. Otherwise role (password = HMAC), schema, grants and map row are (re)applied,
        retrying a concurrent catalog update, then the workspace migrations run as the role.
        ``force`` skips the check (repair after the role was changed behind TAM's back)."""
        target = self.target(key)
        fingerprint = self.fingerprint(target)
        with self._connection() as connection:
            if not force and self._current(connection, target, fingerprint, migrate):
                return target
            created, adopted = self._provision(connection, target, fingerprint)
        if migrate:
            self._migrate(key)
        LOGGER.info(json.dumps({"event": "workspace_provisioned", "schema": target.schema, "created": created,
                                "adopted": adopted}))
        return target

    def _current(self, connection, target: WorkspaceTarget, fingerprint: str, migrate: bool) -> bool:
        row = connection.execute(
            "SELECT r.rolcanlogin AND NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication "
            " OR r.rolbypassrls OR r.rolinherit) AND r.rolconnlimit = %(limit)s, "
            " n.nspowner = r.oid, "
            " EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.roleid = r.oid AND m.member = "
            "  (SELECT oid FROM pg_roles WHERE rolname = current_user) AND m.set_option AND m.inherit_option), "
            f" EXISTS (SELECT 1 FROM {CONTROL_SCHEMA}.{WORKSPACE_TABLE} w WHERE w.key = %(key)s "
            "  AND w.schema = %(schema)s AND w.role = %(role)s AND w.fingerprint = %(fingerprint)s) "
            "FROM pg_roles r JOIN pg_namespace n ON n.nspname = %(schema)s WHERE r.rolname = %(role)s",
            {"limit": self.settings.workspace_connection_limit, "key": target.key, "schema": target.schema,
             "role": target.role, "fingerprint": fingerprint}).fetchone()
        if row is None or not all(row) or not self._has_database_access(connection, target):
            return False
        return not migrate or self._ledger_current(connection, target)

    @staticmethod
    def _ledger_current(connection, target: WorkspaceTarget) -> bool:
        from psycopg import sql

        from tam_db import pg_schema

        table = f"{target.schema}.{pg_schema.LEDGER_TABLE}"
        if connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            return False
        applied = set(connection.execute(sql.SQL("SELECT version, checksum FROM {}").format(
            sql.Identifier(target.schema, pg_schema.LEDGER_TABLE))).fetchall())
        return applied == {(migration.version, migration.checksum) for migration in pg_schema.workspace_migrations()}

    def _provision(self, connection, target: WorkspaceTarget, fingerprint: str) -> tuple[bool, bool]:
        """(created, adopted). A concurrent ALTER ROLE / GRANT on the shared role catalog can fail
        with "tuple concurrently updated"; the transaction is then retried."""
        import psycopg

        attempts = self.settings.serializable_attempts
        for attempt in range(1, attempts + 1):
            try:
                with connection.transaction():
                    return self._provision_once(connection, target, fingerprint)
            except psycopg.Error as exc:
                if attempt >= attempts or not _transient(exc):
                    raise
                LOGGER.warning(json.dumps({"event": "workspace_provisioning_retried", "schema": target.schema,
                                           "sqlstate": exc.sqlstate, "attempt": attempt}))
                time.sleep(PROVISION_RETRY_SECONDS * attempt)
        raise Conflict("Workspace provisioning kept colliding with a concurrent change")

    def _provision_once(self, connection, target: WorkspaceTarget, fingerprint: str) -> tuple[bool, bool]:
        from psycopg import sql

        key = target.key
        role, schema = _identifier(target.role), _identifier(target.schema)
        verifier = connection.pgconn.encrypt_password(
            role_password(self.master_key, target.role).encode(), target.role.encode()).decode()
        connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE_PROVISION, lock_object(key)))
        mapped = self._check_map(connection, target)
        exists = connection.execute(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
            "FROM pg_roles WHERE rolname = %s", (target.role,)).fetchone()
        if exists and exists[0]:
            raise Conflict("Workspace role carries elevated attributes; refusing to provision it")
        attributes = sql.SQL("LOGIN NOINHERIT CONNECTION LIMIT {} PASSWORD {}").format(
            sql.Literal(self.settings.workspace_connection_limit), sql.Literal(verifier))
        if exists:
            connection.execute(sql.SQL("ALTER ROLE {} WITH {}").format(role, attributes))
        else:
            connection.execute(sql.SQL("CREATE ROLE {} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                       "NOREPLICATION NOBYPASSRLS {}").format(role, attributes))
        # SET TRUE: drop() acts as the owner. INHERIT TRUE: a non-superuser admin's pg_dump
        # (pg_backup) must read every ws_* schema; the admin can SET ROLE to any workspace
        # role anyway, so inheriting grants it nothing new. Workspace data is otherwise read
        # only as the workspace role itself (gateway readers, workers).
        connection.execute(sql.SQL("GRANT {} TO CURRENT_USER WITH INHERIT TRUE, SET TRUE").format(role))
        self._grant_database_access(connection, target)
        owner = connection.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s",
                                   (target.schema,)).fetchone()
        adopted = False
        if owner is None:
            connection.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(schema, role))
        elif owner[0] != target.role:
            admin = connection.execute("SELECT current_user").fetchone()[0]
            if owner[0] != admin or not mapped:
                raise Conflict("Workspace schema is owned by an unexpected role; refusing to provision it")
            self._adopt(connection, target)
            adopted = True
        for name, value in self._role_settings(target):
            connection.execute(sql.SQL("ALTER ROLE {} SET {} TO {}").format(role, sql.SQL(name), value))
        connection.execute(f"INSERT INTO {CONTROL_SCHEMA}.{WORKSPACE_TABLE} (key, schema, role, fingerprint) "
                           "VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET fingerprint = excluded.fingerprint",
                           (key, target.schema, target.role, fingerprint))
        return not exists, adopted

    @staticmethod
    def _has_database_access(connection, target: WorkspaceTarget) -> bool:
        return connection.execute(
            "SELECT has_database_privilege(%(role)s, current_database(), 'CONNECT') "
            "AND has_database_privilege(%(role)s, current_database(), 'TEMPORARY')", {"role": target.role}).fetchone()[0]

    def _grant_database_access(self, connection, target: WorkspaceTarget) -> None:
        """CONNECT and TEMPORARY on the database for the workspace role.

        PUBLIC holds both by default, but the DBA SQL (docs/TEAM_POSTGRES.md) revokes them. The
        role must log in, and the workspace migrations create a temporary table. A database
        owner can grant them; an admin that cannot gets the statement for the DBA instead of a
        workspace that never connects.
        """
        from psycopg import sql

        if self._has_database_access(connection, target):
            return
        database = connection.execute("SELECT current_database()").fetchone()[0]
        connection.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            _identifier(database), _identifier(target.role)))
        if not self._has_database_access(connection, target):
            admin = connection.execute("SELECT current_user").fetchone()[0]
            statement = sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {} WITH GRANT OPTION;").format(
                _identifier(database), _identifier(admin)).as_string(connection)
            raise PrerequisitesMissing("Workspace roles cannot connect to the database", dba_sql=(statement,))

    @staticmethod
    def _adopt(connection, target: WorkspaceTarget) -> None:
        """Hand a schema restored with pg_dump --no-owner (everything owned by the admin) to its role.

        Only called for schema_for(key) of a key present in the workspace map. Indexes, identity or
        serial sequences and array types follow their parent object; a SECURITY DEFINER routine
        would run with the owner's rights and is refused instead of adopted. Afterwards nothing in
        the schema may remain owned by anyone but the role.
        """
        from psycopg import sql

        schema, role = _identifier(target.schema), _identifier(target.role)
        namespace = connection.execute("SELECT to_regnamespace(%s)::oid", (target.schema,)).fetchone()[0]
        definers = connection.execute("SELECT count(*) FROM pg_proc WHERE pronamespace = %s AND prosecdef",
                                      (namespace,)).fetchone()[0]
        if definers:
            raise Conflict("Restored workspace schema contains SECURITY DEFINER routines; refusing to adopt it")
        connection.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(schema, role))
        for name, kind in connection.execute(
                "SELECT c.relname, c.relkind FROM pg_class c WHERE c.relnamespace = %s "
                "AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f') AND NOT (c.relkind = 'S' AND EXISTS ("
                " SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid"
                " AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('i', 'a'))) ORDER BY c.relname",
                (namespace,)).fetchall():
            connection.execute(sql.SQL("ALTER {} {} OWNER TO {}").format(
                sql.SQL(ADOPT_RELATION_KINDS[kind]), sql.Identifier(target.schema, name), role))
        for signature, kind in connection.execute(
                "SELECT p.oid::regprocedure::text, p.prokind FROM pg_proc p WHERE p.pronamespace = %s "
                "ORDER BY 1", (namespace,)).fetchall():
            connection.execute(sql.SQL("ALTER {} {} OWNER TO {}").format(
                sql.SQL("AGGREGATE" if kind == "a" else "ROUTINE"), sql.SQL(signature), role))
        for name, kind in connection.execute(
                "SELECT t.typname, t.typtype FROM pg_type t LEFT JOIN pg_class c ON c.oid = t.typrelid "
                "WHERE t.typnamespace = %s AND (t.typtype IN ('d', 'e') OR (t.typtype = 'c' AND c.relkind = 'c')) "
                "ORDER BY 1", (namespace,)).fetchall():
            connection.execute(sql.SQL("ALTER {} {} OWNER TO {}").format(
                sql.SQL("DOMAIN" if kind == "d" else "TYPE"), sql.Identifier(target.schema, name), role))
        for catalog, column, keyword in (("pg_ts_config", "cfg", "TEXT SEARCH CONFIGURATION"),
                                         ("pg_ts_dict", "dict", "TEXT SEARCH DICTIONARY")):
            for (name,) in connection.execute(
                    f"SELECT {column}name FROM {catalog} WHERE {column}namespace = %s ORDER BY 1",
                    (namespace,)).fetchall():
                connection.execute(sql.SQL("ALTER {} {} OWNER TO {}").format(
                    sql.SQL(keyword), sql.Identifier(target.schema, name), role))
        foreign = connection.execute(
            "SELECT (SELECT count(*) FROM pg_class WHERE relnamespace = %(ns)s AND relowner <> %(role)s::regrole) "
            "+ (SELECT count(*) FROM pg_proc WHERE pronamespace = %(ns)s AND proowner <> %(role)s::regrole) "
            "+ (SELECT count(*) FROM pg_type WHERE typnamespace = %(ns)s AND typowner <> %(role)s::regrole)",
            {"ns": namespace, "role": target.role}).fetchone()[0]
        if foreign:
            raise Conflict("Restored workspace schema holds objects TAM cannot adopt; refusing to provision it")
        LOGGER.info(json.dumps({"event": "workspace_schema_adopted", "schema": target.schema}))

    def _migrate(self, key: str) -> None:
        import psycopg

        from tam_db import pg_schema

        try:
            connection = psycopg.connect(self.role_url(key), autocommit=True,
                                         connect_timeout=self.settings.connect_timeout_seconds,
                                         application_name=ADMIN_APPLICATION, **self.connect_overrides)
        except psycopg.Error as exc:
            category = categorize(exc)
            LOGGER.error(json.dumps({"event": "workspace_connect_failed", "category": category.value}))
            raise Unavailable(CATEGORY_MESSAGES[category]) from None
        with connection:
            pg_schema.ensure(connection)

    def _role_settings(self, target: WorkspaceTarget):
        from psycopg import sql

        path = sql.SQL(", ").join(_identifier(name) for name in (target.schema, COMPAT_SCHEMA, EXTENSIONS_SCHEMA))
        return (("search_path", path),
                ("statement_timeout", sql.Literal(str(self.settings.statement_timeout_ms))),
                ("lock_timeout", sql.Literal(str(self.settings.lock_timeout_ms))),
                ("idle_in_transaction_session_timeout", sql.Literal(str(self.settings.idle_in_transaction_timeout_ms))))

    @staticmethod
    def _check_map(connection, target: WorkspaceTarget) -> bool:
        """Whether the key is in the workspace map; Conflict when its row names another schema or role."""
        row = connection.execute(f"SELECT schema, role FROM {CONTROL_SCHEMA}.{WORKSPACE_TABLE} WHERE key = %s",
                                 (target.key,)).fetchone()
        if row is not None and tuple(row) != (target.schema, target.role):
            raise Conflict("Workspace map disagrees with the installation id; refusing to provision")
        return row is not None

    def exists(self, key: str) -> bool:
        schema = schema_for(key)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {CONTROL_SCHEMA}.{WORKSPACE_TABLE} w JOIN pg_namespace n ON n.nspname = w.schema "
                "WHERE w.key = %s AND w.schema = %s", (key, schema)).fetchone()
        return row is not None

    def drop(self, key: str) -> None:
        """DROP SCHEMA ... CASCADE, DROP ROLE and the map row in one transaction.

        The schema is dropped while acting as its owner, so a non-superuser admin needs no
        ownership of workspace objects. Sessions of the role must be gone (server stopped).
        """
        from psycopg import sql

        target = self.target(key)
        with self._connection() as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE_PROVISION, lock_object(key)))
            role_exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.role,)).fetchone()
            schema_owner = connection.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s",
                                              (target.schema,)).fetchone()
            if schema_owner is not None:
                acting = role_exists is not None and schema_owner[0] == target.role
                if acting:
                    connection.execute(sql.SQL("SET LOCAL ROLE {}").format(_identifier(target.role)))
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(_identifier(target.schema)))
                if acting:
                    connection.execute("RESET ROLE")
            if role_exists is not None:
                connection.execute(sql.SQL("DROP ROLE {}").format(_identifier(target.role)))
            connection.execute(f"DELETE FROM {CONTROL_SCHEMA}.{WORKSPACE_TABLE} WHERE key = %s", (key,))
        LOGGER.info(json.dumps({"event": "workspace_dropped", "schema": target.schema}))

    def workspace_keys(self) -> list[str]:
        with self._connection() as connection:
            return [row[0] for row in connection.execute(
                f"SELECT key FROM {CONTROL_SCHEMA}.{WORKSPACE_TABLE} ORDER BY key")]

    def role_url(self, key: str) -> str:
        target = self.target(key)
        password = role_password(self.master_key, target.role)
        return self.dsn.model_copy(update={"user": target.role, "password": SecretStr(password),
                                           "application_name": WORKER_APPLICATION}).to_uri()

    def store_database(self, key: str) -> StoreDatabase:
        """Worker target; carries this installation's connect overrides (WEB origin) into the worker."""
        return StoreDatabase.postgres(self.role_url(key), schema_for(key), self.settings,
                                      connect_options=tuple(sorted(self.connect_overrides.items())))


def lease_check_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Heartbeat interval of advisory leases (TAM_TEAM_PG_LEASE_CHECK_SECONDS)."""
    raw = (os.environ if environ is None else environ).get(LEASE_CHECK_ENV, "").strip()
    if not raw:
        return DEFAULT_LEASE_CHECK_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{LEASE_CHECK_ENV} must be a positive number of seconds") from exc
    if value <= 0:
        raise ValueError(f"{LEASE_CHECK_ENV} must be a positive number of seconds")
    return value


def _as_oid(value: int) -> int:
    """pg_locks shows the int4 halves of a two-key advisory lock as unsigned oids."""
    return value & OID_MASK


def advisory_lock_held(connection, namespace: int, obj: int) -> bool:
    """Whether this session (its current backend) holds the two-key advisory lock, per pg_locks."""
    return bool(connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
        "AND classid = %s::bigint::oid AND objid = %s::bigint::oid AND objsubid = 2 AND granted)",
        (_as_oid(namespace), _as_oid(obj))).fetchone()[0])


class AdvisoryLease:
    """A session-level advisory lock on a dedicated connection, held until ``release``.

    Used so only one TAM server serves an installation (plan 1.5; the file ServerLease only works
    on one host) and so one worker serves a workspace. A session lock silently disappears with its
    connection (network cut, server restart, idle timeout), so a daemon thread re-verifies it in
    pg_locks every ``check_seconds``; on loss it logs an error, marks ``lost`` and calls
    ``on_lost`` once, whose job is to stop serving (maintenance for the server, exit for a worker).
    """

    def __init__(self, url: str, settings: DatabaseSettings, namespace: int, obj: int, busy_message: str,
                 *, on_lost: Callable[[], None] | None = None, check_seconds: float | None = None,
                 connect_overrides: Mapping[str, str] = NO_OVERRIDES):
        self.url, self.settings = url, settings
        self.connect_overrides = dict(connect_overrides)
        self.namespace, self.obj = namespace, obj
        self.busy_message = busy_message
        self.on_lost = on_lost
        self.check_seconds = check_seconds if check_seconds is not None else lease_check_seconds()
        self.connection = None
        self.lost = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _take(self, url: str, connect_overrides: Mapping[str, str]):
        """A new session holding the lock, or None when another session holds it."""
        connection = admin_connect(url, self.settings, LEASE_APPLICATION, idle_session=True,
                                   connect_overrides=connect_overrides)
        try:
            acquired = connection.execute("SELECT pg_try_advisory_lock(%s, %s)",
                                          (self.namespace, self.obj)).fetchone()[0]
            if acquired and not advisory_lock_held(connection, self.namespace, self.obj):
                raise PrerequisitesMissing(TRANSACTION_POOLING_MESSAGE)
        except BaseException:
            connection.close()
            raise
        if not acquired:
            connection.close()
            return None
        return connection

    def acquire(self) -> Self:
        connection = self._take(self.url, self.connect_overrides)
        if connection is None:
            raise Conflict(self.busy_message)
        with self._lock:
            self.connection, self.lost = connection, False
        self._stop.clear()
        self._thread = threading.Thread(target=self._heartbeat, name="tam-lease-heartbeat", daemon=True)
        self._thread.start()
        return self

    def verify(self) -> bool:
        """Re-check the lock in pg_locks; False when the session or the lock is gone."""
        import psycopg

        with self._lock:
            if self.connection is None or self.connection.closed:
                return False
            try:
                return advisory_lock_held(self.connection, self.namespace, self.obj)
            except psycopg.Error as exc:
                LOGGER.warning(json.dumps({"event": "advisory_lease_check_failed", "category": categorize(exc).value}))
                return False

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.check_seconds):
            if not self.verify():
                self._lose()
                return

    def _lose(self) -> None:
        if self._stop.is_set() or self.lost:
            return
        self.lost = True
        LOGGER.error(json.dumps({"event": "advisory_lease_lost", "namespace": self.namespace, "object": self.obj}))
        if self.on_lost is not None:
            try:
                self.on_lost()
            except Exception:
                LOGGER.exception(json.dumps({"event": "advisory_lease_lost_handler_failed"}))

    def handover(self, url: str, connect_overrides: Mapping[str, str] | None = None) -> None:
        """Move the lock to a session on ``url`` (repoint to the same database via a new DSN).

        The new session cannot take the lock while the old one holds it, so the old lock is released
        first and taken back if the new session loses the race; Conflict then."""
        with self._lock:
            old = self.connection
            if old is not None and not old.closed:
                old.execute("SELECT pg_advisory_unlock(%s, %s)", (self.namespace, self.obj))
            try:
                overrides = self.connect_overrides if connect_overrides is None else dict(connect_overrides)
                new = self._take(url, overrides)
            except BaseException:
                self._retake(old)
                raise
            if new is None:
                self._retake(old)
                raise Conflict(self.busy_message)
            self.connection, self.url, self.connect_overrides = new, url, overrides
        if old is not None:
            old.close()
        LOGGER.info(json.dumps({"event": "advisory_lease_handed_over", "namespace": self.namespace}))

    def _retake(self, connection) -> None:
        if connection is None or connection.closed:
            return
        if not connection.execute("SELECT pg_try_advisory_lock(%s, %s)", (self.namespace, self.obj)).fetchone()[0]:
            connection.close()
            self.connection = None
            threading.Thread(target=self._lose, name="tam-lease-lost", daemon=True).start()

    def release(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.check_seconds + JOIN_GRACE_SECONDS)
        import psycopg

        with self._lock:
            connection, self.connection = self.connection, None
        if connection is None:
            return
        try:
            if not connection.closed and not self.lost:
                connection.execute("SELECT pg_advisory_unlock(%s, %s)", (self.namespace, self.obj))
        except psycopg.Error as exc:
            # The session is gone, and with it the lock: closing is all that is left to do.
            LOGGER.warning(json.dumps({"event": "advisory_lease_release_failed", "category": categorize(exc).value}))
        finally:
            connection.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()


def server_lease(url: str, settings: DatabaseSettings | None = None, *,
                 on_lost: Callable[[], None] | None = None,
                 connect_overrides: Mapping[str, str] = NO_OVERRIDES) -> AdvisoryLease:
    """``on_lost`` must stop the server from serving (enter maintenance); without it only an error is logged."""
    return AdvisoryLease(url, settings or DatabaseSettings(), LOCK_NAMESPACE, LOCK_SERVER,
                         "Another TAM server is already running against this PostgreSQL database", on_lost=on_lost,
                         connect_overrides=connect_overrides)


def workspace_lease(url: str, key: str, settings: DatabaseSettings | None = None, *,
                    on_lost: Callable[[], None] | None = None,
                    connect_overrides: Mapping[str, str] = NO_OVERRIDES) -> AdvisoryLease:
    """``on_lost`` must end the worker (it may no longer write); without it only an error is logged."""
    return AdvisoryLease(url, settings or DatabaseSettings(), LOCK_NAMESPACE_WORKSPACE, lock_object(key),
                         "Another process already serves this workspace", on_lost=on_lost,
                         connect_overrides=connect_overrides)
