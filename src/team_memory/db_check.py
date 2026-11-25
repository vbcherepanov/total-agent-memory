"""The Test button (plan 4.7): six checks against a PostgreSQL DSN that may not be saved.

Driver errors never reach the caller: they are mapped to a fixed ErrorCategory with a fixed
message, because libpq text can echo host names, user names or parts of the DSN. Failed checks
carry the exact SQL a DBA runs to fix them. Nothing here stores or logs the DSN.
"""
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from stdio_utf8 import use_utf8_stdio
from tam_db.contracts import (
    CONTROL_SCHEMA,
    EXTENSIONS_SCHEMA,
    MIN_SERVER_VERSION_NUM,
    REQUIRED_EXTENSIONS,
)
from team_memory.database_contracts import (
    CHECK_ORDER,
    CHECK_TIMEOUT_SECONDS,
    PASSWORD_MASK,
    CheckId,
    CheckReport,
    CheckStatus,
    DatabaseCheck,
    DatabaseDsn,
    DsnOrigin,
    ErrorCategory,
    TargetState,
)

LOGGER = logging.getLogger(__name__)
MILLISECONDS = 1000
REQUIRED_ENCODING = "UTF8"
BUILTIN_PROVIDER = "b"
LIBC_PROVIDER = "c"
# Ordering must be bytewise (SQLite BINARY) and character classes Unicode-aware: under a plain
# C ctype lower()/upper() and the FTS tokenizer only fold ASCII, so "Привет" never matches "привет".
UNICODE_BUILTIN_LOCALE = "C.UTF-8"
BYTEWISE_LIBC_COLLATIONS = frozenset({"C", "POSIX"})
UTF8_LOCALE = re.compile(r"\.utf-?8$", re.IGNORECASE)
APPLICATION_NAME = "tam-db-check"
# Probe lock of the pooling check; team_memory.pg_provision and tam_db.pg_schema use +0..+3.
POOL_PROBE_NAMESPACE = 0x54414D00 + 4
POOL_PROBE_RANGE = 2 ** 31
TRANSACTION_POOLING_MESSAGE = ("The connection goes through a transaction-pooling proxy (e.g. PgBouncer "
                               "pool_mode=transaction): session advisory locks and settings do not survive it. "
                               "Connect TAM directly to PostgreSQL or use pool_mode=session.")
ISOLATED_ENV_KEEP = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"})
ISOLATED_TIMEOUT_FACTOR = 2
ISOLATED_STARTUP_SECONDS = 20
MAX_STDERR_CHARS = 2000
EMPTY_PASSFILE = "empty.pgpass"
META_INSTANCE_KEY = "instance_id"
WORKSPACE_DATABASE_PRIVILEGES = ("CONNECT", "TEMPORARY")

AUTH_SQLSTATES = frozenset({"28P01", "28000"})
NO_DATABASE_SQLSTATES = frozenset({"3D000"})
PERMISSION_SQLSTATES = frozenset({"42501"})
TIMEOUT_SQLSTATES = frozenset({"57014", "55P03"})
SSL_MARKERS = ("ssl", "encryption", "certificate")
TIMEOUT_MARKERS = ("timeout", "timed out")
PERMISSION_MARKER = "permission denied"

CATEGORY_MESSAGES = {
    ErrorCategory.AUTH_FAILED: "Authentication failed: check the user name and password, and pg_hba.conf.",
    ErrorCategory.UNREACHABLE: "The server could not be reached: check the host, port, firewall and that PostgreSQL runs.",
    ErrorCategory.SSL_REQUIRED: "The TLS settings do not match the server: check sslmode and the certificates.",
    ErrorCategory.TIMEOUT: f"The server did not answer within {CHECK_TIMEOUT_SECONDS} seconds.",
    ErrorCategory.NO_DATABASE: "The database does not exist; ask the DBA to create it (see the SQL).",
    ErrorCategory.PERMISSION: "The user lacks a privilege; when the connection itself is refused, CONNECT on the database.",
    ErrorCategory.UNEXPECTED: "The server returned an unexpected error; see the server log for details.",
}


def redact(text: str, dsn: DatabaseDsn | None) -> str:
    """Remove every form of the DSN's password (raw and percent-encoded) from ``text``."""
    if dsn is None or dsn.password is None:
        return text
    secret = dsn.password.get_secret_value()
    for form in sorted({secret, quote(secret, safe=""), quote(secret)}, key=len, reverse=True):
        if form:
            text = text.replace(form, PASSWORD_MASK)
    return text


def categorize(exc: BaseException) -> ErrorCategory:
    """Fixed category for a driver exception; its text is inspected, never returned."""
    import psycopg

    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in AUTH_SQLSTATES:
        text = str(exc).lower()
        return ErrorCategory.SSL_REQUIRED if any(marker in text for marker in SSL_MARKERS) else ErrorCategory.AUTH_FAILED
    if sqlstate in NO_DATABASE_SQLSTATES:
        return ErrorCategory.NO_DATABASE
    if sqlstate in PERMISSION_SQLSTATES:
        return ErrorCategory.PERMISSION
    if sqlstate in TIMEOUT_SQLSTATES or isinstance(exc, (psycopg.errors.ConnectionTimeout, TimeoutError)):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, psycopg.OperationalError) and sqlstate is None:
        text = str(exc).lower()
        if any(marker in text for marker in TIMEOUT_MARKERS):
            return ErrorCategory.TIMEOUT
        if any(marker in text for marker in SSL_MARKERS):
            return ErrorCategory.SSL_REQUIRED
        if "password" in text or "authentication" in text:
            return ErrorCategory.AUTH_FAILED
        if "does not exist" in text and "database" in text:
            return ErrorCategory.NO_DATABASE
        # libpq reports a refused login (no CONNECT on the database) without a SQLSTATE.
        if PERMISSION_MARKER in text:
            return ErrorCategory.PERMISSION
        return ErrorCategory.UNREACHABLE
    return ErrorCategory.UNEXPECTED


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_database_sql(database: str, owner: str) -> str:
    return (f"CREATE DATABASE {quote_ident(database)} OWNER {quote_ident(owner)} TEMPLATE template0 "
            "ENCODING 'UTF8' LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8';")


def workspace_database_access_sql(database: str, admin: str) -> str:
    """What lets a TAM role that does not own the database give workspace roles access to it."""
    return (f"GRANT {', '.join(WORKSPACE_DATABASE_PRIVILEGES)} ON DATABASE {quote_ident(database)} "
            f"TO {quote_ident(admin)} WITH GRANT OPTION;")


def extension_sql() -> tuple[str, ...]:
    return (f"CREATE SCHEMA IF NOT EXISTS {EXTENSIONS_SCHEMA};",
            *(f"CREATE EXTENSION IF NOT EXISTS {name} SCHEMA {EXTENSIONS_SCHEMA};" for name in REQUIRED_EXTENSIONS),
            f"GRANT USAGE ON SCHEMA {EXTENSIONS_SCHEMA} TO PUBLIC;")


def connect_kwargs(timeout_seconds: int, application_name: str) -> dict[str, Any]:
    timeout_ms = timeout_seconds * MILLISECONDS
    return {"connect_timeout": timeout_seconds, "application_name": application_name, "autocommit": True,
            "options": f"-c statement_timeout={timeout_ms} -c lock_timeout={timeout_ms}"}


@dataclass(frozen=True)
class ExtensionState:
    name: str
    schema: str | None
    available: bool
    trusted: bool


@dataclass(frozen=True)
class ServerFacts:
    """Everything the checks need, read in one short session."""

    version_num: int
    version: str
    encoding: str
    provider: str
    collate: str
    ctype: str
    locale: str | None
    superuser: bool
    createrole: bool
    create_on_database: bool
    workspace_database_access: bool
    extensions: tuple[ExtensionState, ...]
    extensions_schema_usable: bool
    control_schema: bool
    instance_id: str | None
    meta_readable: bool


def read_facts(connection) -> ServerFacts:
    """Catalog probes shared by the Test button and the startup check (pg_provision)."""
    version_num = int(connection.execute("SELECT current_setting('server_version_num')").fetchone()[0])
    version = connection.execute("SELECT current_setting('server_version')").fetchone()[0]
    encoding, provider, collate, ctype, locale = connection.execute(
        # datlocale exists from PostgreSQL 17 on; reading it through jsonb keeps older servers
        # answering so the version check can report them.
        "SELECT pg_encoding_to_char(d.encoding), d.datlocprovider::text, d.datcollate, d.datctype, "
        "to_jsonb(d) ->> 'datlocale' "
        "FROM pg_database d WHERE d.datname = current_database()").fetchone()
    superuser, createrole = connection.execute(
        "SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()
    create_on_database = connection.execute(
        "SELECT has_database_privilege(current_database(), 'CREATE')").fetchone()[0]
    # Workspace roles log in and their migrations create a temporary table: each privilege must
    # be held by PUBLIC or be grantable by this user (pg_provision._grant_database_access).
    workspace_database_access = connection.execute(
        "SELECT bool_and(has_database_privilege('public', current_database(), privilege) "
        "OR has_database_privilege(current_database(), privilege || ' WITH GRANT OPTION')) "
        "FROM unnest(%s::text[]) AS privilege", (list(WORKSPACE_DATABASE_PRIVILEGES),)).fetchone()[0]
    extensions = []
    for name in REQUIRED_EXTENSIONS:
        installed = connection.execute(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = %s",
            (name,)).fetchone()
        available = connection.execute(
            "SELECT v.trusted FROM pg_available_extensions a JOIN pg_available_extension_versions v "
            "ON v.name = a.name AND v.version = a.default_version WHERE a.name = %s", (name,)).fetchone()
        extensions.append(ExtensionState(name=name, schema=installed[0] if installed else None,
                                         available=available is not None,
                                         trusted=bool(available[0]) if available else False))
    usable = connection.execute(
        "SELECT pg_get_userbyid(nspowner) = current_user OR EXISTS ("
        " SELECT 1 FROM aclexplode(COALESCE(nspacl, acldefault('n', nspowner))) acl"
        " WHERE acl.grantee = 0 AND acl.privilege_type = 'USAGE') "
        "FROM pg_namespace WHERE nspname = %s", (EXTENSIONS_SCHEMA,)).fetchone()
    control = connection.execute("SELECT to_regnamespace(%s) IS NOT NULL", (CONTROL_SCHEMA,)).fetchone()[0]
    instance_id, meta_readable = None, True
    if control:
        meta = connection.execute("SELECT to_regclass(%s)", (CONTROL_SCHEMA + ".meta",)).fetchone()[0]
        if meta is not None:
            readable = connection.execute("SELECT has_table_privilege(%s, 'SELECT')",
                                          (CONTROL_SCHEMA + ".meta",)).fetchone()[0]
            if readable:
                row = connection.execute(f"SELECT value FROM {CONTROL_SCHEMA}.meta WHERE key = %s",
                                         (META_INSTANCE_KEY,)).fetchone()
                instance_id = row[0] if row else None
            else:
                meta_readable = False
    return ServerFacts(version_num=version_num, version=version, encoding=encoding, provider=provider,
                       collate=collate, ctype=ctype, locale=locale, superuser=bool(superuser), createrole=bool(createrole),
                       create_on_database=bool(create_on_database),
                       workspace_database_access=bool(workspace_database_access), extensions=tuple(extensions),
                       extensions_schema_usable=bool(usable and usable[0]), control_schema=bool(control),
                       instance_id=instance_id, meta_readable=meta_readable)


def _passed(check_id: CheckId, message: str) -> DatabaseCheck:
    return DatabaseCheck(id=check_id, status=CheckStatus.PASSED, message=message)


def _failed(check_id: CheckId, message: str, *sql: str, category: ErrorCategory | None = None) -> DatabaseCheck:
    return DatabaseCheck(id=check_id, status=CheckStatus.FAILED, message=message, category=category, dba_sql=sql)


def check_version(facts: ServerFacts) -> DatabaseCheck:
    if facts.version_num >= MIN_SERVER_VERSION_NUM:
        return _passed(CheckId.SERVER_VERSION, f"PostgreSQL {facts.version}")
    return _failed(CheckId.SERVER_VERSION, f"PostgreSQL {facts.version} is too old; version 17 or newer is required.")


def supported_locale(facts: ServerFacts) -> bool:
    """Builtin C.UTF-8, or libc with collation C/POSIX and a UTF-8 ctype."""
    if facts.provider == BUILTIN_PROVIDER:
        return facts.locale == UNICODE_BUILTIN_LOCALE
    return (facts.provider == LIBC_PROVIDER and facts.collate in BYTEWISE_LIBC_COLLATIONS
            and bool(UTF8_LOCALE.search(facts.ctype)))


def check_encoding(facts: ServerFacts, dsn: DatabaseDsn) -> DatabaseCheck:
    if facts.encoding == REQUIRED_ENCODING and supported_locale(facts):
        return _passed(CheckId.ENCODING, f"Encoding {facts.encoding} with bytewise collation and Unicode ctype")
    if facts.provider == BUILTIN_PROVIDER:
        shown = f"builtin locale {facts.locale}"
    else:
        shown = f"collation {facts.collate} and ctype {facts.ctype}"
    return _failed(CheckId.ENCODING,
                   f"The database uses encoding {facts.encoding} with {shown}; TAM needs UTF8 with the builtin "
                   "C.UTF-8 locale (bytewise ordering as in SQLite, Unicode case folding for search). "
                   "Create a new database:",
                   create_database_sql(dsn.database, dsn.user))


def check_extensions(facts: ServerFacts) -> DatabaseCheck:
    problems = []
    for extension in facts.extensions:
        if extension.schema is None:
            creatable = extension.available and facts.create_on_database and (facts.superuser or extension.trusted)
            if not creatable:
                problems.append(f"{extension.name} is not installed" +
                                ("" if extension.available else " and not available on the server"))
        elif extension.schema != EXTENSIONS_SCHEMA:
            problems.append(f"{extension.name} is installed in schema {extension.schema}, not {EXTENSIONS_SCHEMA}")
    if not facts.extensions_schema_usable and any(extension.schema for extension in facts.extensions):
        problems.append(f"schema {EXTENSIONS_SCHEMA} is not usable by workspace roles")
    if not problems:
        return _passed(CheckId.EXTENSIONS, "Extensions " + ", ".join(REQUIRED_EXTENSIONS) + " are installed or creatable")
    moves = tuple(f"ALTER EXTENSION {extension.name} SET SCHEMA {EXTENSIONS_SCHEMA};" for extension in facts.extensions
                  if extension.schema not in (None, EXTENSIONS_SCHEMA))
    return _failed(CheckId.EXTENSIONS, "; ".join(problems).capitalize() + ". Ask the DBA to run:",
                   *extension_sql(), *moves)


def check_privileges(facts: ServerFacts, dsn: DatabaseDsn) -> DatabaseCheck:
    missing = []
    sql = []
    if not (facts.superuser or facts.createrole):
        missing.append("CREATEROLE (one LOGIN role per workspace isolates the data)")
        sql.append(f"ALTER ROLE {quote_ident(dsn.user)} CREATEROLE;")
    if not facts.create_on_database:
        missing.append("CREATE on the database (schemas per workspace)")
        sql.append(f"GRANT CREATE ON DATABASE {quote_ident(dsn.database)} TO {quote_ident(dsn.user)};")
    if not facts.workspace_database_access:
        missing.append("the right to grant " + " and ".join(WORKSPACE_DATABASE_PRIVILEGES) +
                       " on the database (workspace roles must log in; PUBLIC does not hold them)")
        sql.append(workspace_database_access_sql(dsn.database, dsn.user))
    if not missing:
        return _passed(CheckId.PRIVILEGES, "The user may create roles and schemas")
    return _failed(CheckId.PRIVILEGES, "The user lacks " + " and ".join(missing) + ".", *sql)


def target_state(facts: ServerFacts, instance_id: UUID | None) -> tuple[TargetState, UUID | None]:
    if not facts.control_schema:
        return TargetState.EMPTY, None
    if not facts.meta_readable or facts.instance_id is None:
        return TargetState.UNKNOWN, None
    try:
        found = UUID(facts.instance_id)
    except ValueError:
        return TargetState.UNKNOWN, None
    if instance_id is not None and found == instance_id:
        return TargetState.SAME_INSTALLATION, found
    return TargetState.FOREIGN_INSTALLATION, found


def check_target(state: TargetState, facts: ServerFacts) -> DatabaseCheck:
    if state is TargetState.EMPTY:
        return _passed(CheckId.TARGET_STATE, "The database holds no TAM installation yet")
    if state is TargetState.SAME_INSTALLATION:
        return _passed(CheckId.TARGET_STATE, "The database already holds this TAM installation")
    if state is TargetState.FOREIGN_INSTALLATION:
        return _failed(CheckId.TARGET_STATE, "The database holds another TAM installation; use an empty database.")
    if not facts.meta_readable:
        return _failed(CheckId.TARGET_STATE, f"Schema {CONTROL_SCHEMA} exists but this user cannot read it.",
                       category=ErrorCategory.PERMISSION)
    return _failed(CheckId.TARGET_STATE, f"Schema {CONTROL_SCHEMA} exists without an installation id; "
                                         "use an empty database.")


def transaction_pooled(first, open_second: Callable[[], Any]) -> bool:
    """Whether the DSN reaches PostgreSQL through a transaction-pooling proxy (PgBouncer and alike).

    Behind such a proxy two client sessions may share one backend, a session lock is taken on one
    backend and looked up on another, or the backend changes between statements. Any of these
    breaks advisory leases and session settings, so the check refuses the DSN."""
    pid = first.execute("SELECT pg_backend_pid()").fetchone()[0]
    probe = secrets.randbelow(POOL_PROBE_RANGE)
    with open_second() as second:
        if second.execute("SELECT pg_backend_pid()").fetchone()[0] == pid:
            return True
        if not first.execute("SELECT pg_try_advisory_lock(%s, %s)", (POOL_PROBE_NAMESPACE, probe)).fetchone()[0]:
            return False
        try:
            visible = second.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = %s "
                "AND classid = %s::bigint::oid AND objid = %s::bigint::oid AND objsubid = 2 AND granted)",
                (pid, POOL_PROBE_NAMESPACE, probe)).fetchone()[0]
            return not visible or first.execute("SELECT pg_backend_pid()").fetchone()[0] != pid
        finally:
            first.execute("SELECT pg_advisory_unlock(%s, %s)", (POOL_PROBE_NAMESPACE, probe))


def sanitized_environment(home: Path) -> dict[str, str]:
    """The environment of an isolated check: no PG* variables (PGPASSWORD, PGSERVICE, PGSSLCERT, ...),
    an empty HOME (no ~/.pgpass, ~/.pg_service.conf or ~/.postgresql certificates)."""
    kept = {name: value for name, value in os.environ.items() if name in ISOLATED_ENV_KEEP}
    return {**kept, "HOME": str(home), "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)}


class PgDatabaseChecker:
    """DatabaseChecker (database_contracts) over psycopg; every call opens and closes its own sessions.

    ``isolated=True`` is for DSNs typed into the web UI or the setup wizard: the check runs in a child
    process with a sanitized environment and an empty passfile, so the server's own libpq settings
    (PGPASSWORD, service files, client certificates, ~/.pgpass) can never be borrowed by a browser.
    """

    def __init__(self, timeout_seconds: int = CHECK_TIMEOUT_SECONDS, clock: Callable[[], float] = time.monotonic,
                 *, isolated: bool = False):
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.isolated = isolated

    def check(self, dsn: DatabaseDsn, *, instance_id: UUID | None) -> CheckReport:
        if self.isolated:
            return self._check_isolated(dsn, instance_id)
        return self.check_here(dsn, instance_id=instance_id)

    def _check_isolated(self, dsn: DatabaseDsn, instance_id: UUID | None) -> CheckReport:
        dsn.validate_for(DsnOrigin.WEB)
        started = self.clock()
        request = json.dumps({"dsn": dsn.to_uri(), "instance_id": str(instance_id) if instance_id else None,
                              "timeout_seconds": self.timeout_seconds})
        with tempfile.TemporaryDirectory(prefix="tam-db-check-") as home:
            try:
                result = subprocess.run([sys.executable, "-m", "team_memory.db_check"], input=request,
                                        capture_output=True, text=True, encoding="utf-8", errors="replace", env=sanitized_environment(Path(home)),
                                        timeout=self.timeout_seconds * ISOLATED_TIMEOUT_FACTOR + ISOLATED_STARTUP_SECONDS,
                                        check=False)
            except subprocess.TimeoutExpired:
                return self._report(dsn, {CheckId.CONNECT: _failed(CheckId.CONNECT, CATEGORY_MESSAGES[ErrorCategory.TIMEOUT],
                                                                   category=ErrorCategory.TIMEOUT)},
                                    TargetState.UNKNOWN, None, None, started)
        if result.returncode != 0:
            LOGGER.error(json.dumps({"event": "database_check_process_failed", "returncode": result.returncode,
                                     "stderr": redact(result.stderr[-MAX_STDERR_CHARS:], dsn)}))
            return self._report(dsn, {CheckId.CONNECT: _failed(CheckId.CONNECT,
                                                               CATEGORY_MESSAGES[ErrorCategory.UNEXPECTED],
                                                               category=ErrorCategory.UNEXPECTED)},
                                TargetState.UNKNOWN, None, None, started)
        return CheckReport.model_validate_json(result.stdout)

    def check_here(self, dsn: DatabaseDsn, *, instance_id: UUID | None,
                   connect_overrides: Mapping[str, str] | None = None) -> CheckReport:
        import psycopg

        started = self.clock()
        timeout = min(self.timeout_seconds, dsn.connect_timeout or self.timeout_seconds)
        kwargs = connect_kwargs(timeout, APPLICATION_NAME)
        kwargs.update(connect_overrides or {})
        checks: dict[CheckId, DatabaseCheck] = {}
        state, found, version = TargetState.UNKNOWN, None, None
        try:
            with psycopg.connect(dsn.to_uri(), **kwargs) as connection:
                if transaction_pooled(connection, lambda: psycopg.connect(dsn.to_uri(), **kwargs)):
                    checks[CheckId.CONNECT] = _failed(CheckId.CONNECT, TRANSACTION_POOLING_MESSAGE)
                    return self._report(dsn, checks, state, found, version, started)
                checks[CheckId.CONNECT] = _passed(CheckId.CONNECT, "Connected and authenticated")
                facts = read_facts(connection)
        except (psycopg.Error, OSError) as exc:
            category = categorize(exc)
            phase = CheckId.CONNECT if CheckId.CONNECT not in checks else CheckId.SERVER_VERSION
            LOGGER.warning(json.dumps({"event": "database_check_failed", "category": category.value,
                                       "phase": phase.value, "target": dsn.host_db()}))
            sql = (create_database_sql(dsn.database, dsn.user),) if category is ErrorCategory.NO_DATABASE else ()
            checks[phase] = _failed(phase, CATEGORY_MESSAGES[category], *sql, category=category)
        else:
            version = facts.version
            state, found = target_state(facts, instance_id)
            checks.update({CheckId.SERVER_VERSION: check_version(facts),
                           CheckId.ENCODING: check_encoding(facts, dsn),
                           CheckId.EXTENSIONS: check_extensions(facts),
                           CheckId.PRIVILEGES: check_privileges(facts, dsn),
                           CheckId.TARGET_STATE: check_target(state, facts)})
        return self._report(dsn, checks, state, found, version, started)

    def _report(self, dsn: DatabaseDsn, checks: dict[CheckId, DatabaseCheck], state: TargetState,
                found: UUID | None, version: str | None, started: float) -> CheckReport:
        for check_id in CHECK_ORDER:
            checks.setdefault(check_id, DatabaseCheck(id=check_id, status=CheckStatus.SKIPPED,
                                                      message="Skipped: an earlier check failed"))
        report = CheckReport(dsn_masked=dsn.masked(), checks=tuple(checks[check_id] for check_id in CHECK_ORDER),
                             target_state=state, target_instance_id=found, server_version=version,
                             warnings=dsn.warnings(), checked_at=datetime.now(UTC),
                             duration_ms=int((self.clock() - started) * MILLISECONDS))
        LOGGER.info(json.dumps({"event": "database_checked", "target": dsn.host_db(), "ok": report.ok,
                                "target_state": state.value, "duration_ms": report.duration_ms}))
        return report


def _isolated_main() -> None:
    """Child side of an isolated check: request JSON on stdin, CheckReport JSON on stdout."""
    use_utf8_stdio()
    request = json.loads(sys.stdin.read())
    dsn = DatabaseDsn.parse(request["dsn"], DsnOrigin.WEB)
    instance_id = UUID(request["instance_id"]) if request["instance_id"] else None
    passfile = Path(os.environ["HOME"]) / EMPTY_PASSFILE
    descriptor = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    report = PgDatabaseChecker(int(request["timeout_seconds"])).check_here(
        dsn, instance_id=instance_id, connect_overrides=dsn.connect_overrides(DsnOrigin.WEB, passfile))
    sys.stdout.write(report.model_dump_json())


if __name__ == "__main__":
    _isolated_main()
