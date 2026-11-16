"""SQLite -> PostgreSQL migration job, activation, SQLite archive and rollback (plan 5, decisions D2/D3).

The job runs in a dedicated thread of the server (or in the foreground for ``tam-team db-migrate``):
preflight checks, maintenance mode (503 for MCP and mutating dashboard calls, workers stopped and
their workspace leases held), copy of identity and learning into the control schemas, one
transaction per workspace, verification of every table, then activation: the control plane and
``database.json`` switch to PostgreSQL, workers are recycled and the SQLite files move to
``<root>/archive/sqlite-<ts>/`` so nothing opens them again. A failure or a cancel leaves the
configuration unchanged, drops the workspace that was being copied, lifts maintenance and never
touches the SQLite files. The journal ``<root>/migration/<job_id>.json`` lets a later job resume
after the workspaces that were already copied (they are re-verified, and recopied if they changed).

Rollback (D2) moves the archived SQLite files back and switches to SQLite; everything written to
PostgreSQL after the switch is lost, so it requires typing the organization name.
"""

import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, Self

import psycopg

from tam_db.contracts import (
    CONTROL_SCHEMA,
    LEARNING_SCHEMA,
    ActiveDatabase,
    Backend,
    DatabaseSettings,
    StoreDatabase,
)
from team_memory.contracts import Conflict, DomainError, Forbidden, NotFound
from team_memory.database import archived_workspaces
from team_memory.database_contracts import (
    ARCHIVE_DIR,
    ARCHIVE_PREFIX,
    MIGRATION_DIR,
    PLAN_TTL_SECONDS,
    TERMINAL_PHASES,
    CheckReport,
    CheckStatus,
    DatabaseChecker,
    DatabaseConfig,
    DatabaseConfigStore,
    DatabaseDsn,
    DatabaseKind,
    DsnOrigin,
    ErrorCategory,
    MaintenanceReason,
    MaintenanceState,
    MigrationPhase,
    MigrationPlan,
    MigrationProgress,
    QuarantineEstimate,
    SqliteArchive,
    TargetState,
)
from team_memory.lifecycle import ServerLease
from team_memory.migrate_to_pg import (
    IDENTITY_DB,
    LEARNING_DB,
    MEMORY_DB,
    WORKSPACES_DIR,
    DatabaseDigest,
    MigrationCancelled,
    MigrationError,
    PostCopy,
    SourceDatabase,
    Throughput,
    bundled_sqlite_migrations,
    compare,
    copy_database,
    discover_sources,
    estimate,
    foreign_key_violations,
    has_rows,
    quarantine_summary,
    source_digest,
    target_digest,
    text_problems,
    truncate_copied_tables,
    unknown_migrations,
)

LOGGER = logging.getLogger(__name__)

ARCHIVE_TIMESTAMP = "%Y%m%dT%H%M%S%fZ"
SQLITE_SIDECARS = ("", "-wal", "-shm", "-journal")
JOURNAL_SUFFIX = ".json"
JOURNAL_INTERVAL_SECONDS = 1.0
MAX_ACTOR_CHARS = 128
# Copy speed on a laptop-class host with PostgreSQL on the same machine; the dry run shows it as an
# estimate only (plan risk 10), so it errs on the slow side.
DEFAULT_THROUGHPUT = Throughput(rows_per_second=4000.0, bytes_per_second=8 * 1024 * 1024, seconds_per_database=0.5)
SERVER_STOPPED_ERROR = "The server stopped while the migration was running; start it again to resume"
SCHEMAS = {DatabaseKind.IDENTITY: CONTROL_SCHEMA, DatabaseKind.LEARNING: LEARNING_SCHEMA}
# Control tables that provisioning seeds (instance_id): merged with the SQLite rows instead of replaced.
SEEDED_TABLES = frozenset({"meta"})
# Workspace rows the baseline seeds (migrations/postgres/workspace/0001_baseline.sql). SQLite is
# authoritative for the counters: the seeded rows are replaced. The SQLite migration ledger is not
# copied; its versions must already be in the PostgreSQL ledger (else the file is from a newer TAM).
REPLACED_WORKSPACE_TABLES = frozenset({"vector_index_revision", "representation_index_revision", "privacy_counters"})
WORKSPACE_LEDGERS = frozenset({"migrations"})

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class AuditSink(Protocol):
    """database_config.record_database_event bound to the control plane: admin_events plus a JSON log line."""

    def __call__(self, action: str, actor: str, dsn: DatabaseDsn | None = None,
                 **fields: str | int | bool | None) -> None: ...


class WorkerRecycler(Protocol):
    def recycle(self) -> int: ...


class MigrationTarget(Protocol):
    """A PostgreSQL database being migrated into, built from the DSN and this installation's instance_id."""

    def admin(self) -> AbstractContextManager[psycopg.Connection]:
        """Autocommit connection as the TAM role that owns tam_control and tam_learning."""
        ...

    def prepare_control(self) -> None:
        """Idempotent: extensions check, tam_compat, control schemas and the instance_id claim."""
        ...

    def prepare_workspace(self, key: str) -> StoreDatabase:
        """Idempotent: schema, role and the workspace tables; returns the role's connection target."""
        ...

    def connect(self, database: StoreDatabase) -> AbstractContextManager[psycopg.Connection]:
        """Autocommit connection as a workspace role (``prepare_workspace``'s result)."""
        ...

    def workspace_exists(self, key: str) -> bool: ...

    def drop_workspace(self, key: str) -> None: ...

    def post_copy(self, kind: DatabaseKind) -> Sequence[PostCopy]:
        """Hooks rebuilding PostgreSQL-only derived state after a bulk copy (lexical statistics)."""
        ...


class MigrationTargets(Protocol):
    def open(self, dsn: DatabaseDsn, instance_id: str,
             overrides: Mapping[str, str] = MappingProxyType({})) -> MigrationTarget:
        """``overrides``: DatabaseDsn.connect_overrides of the DSN's origin, for every connection."""
        ...


WorkspaceSchemaInstaller = Callable[[StoreDatabase, Mapping[str, str]], None]


def rebuild_full_text(connection: psycopg.Connection, schema: str) -> None:
    """Re-derive the full-text side tables and fts_stats from the copied rows (user triggers were off)."""
    from memory_core import pg_fts

    pg_fts.rebuild(connection)


DEFAULT_POST_COPY: tuple[PostCopy, ...] = (rebuild_full_text,)


def install_workspace_schema(database: StoreDatabase, overrides: Mapping[str, str]) -> None:
    """The workspace tables exactly as a PostgreSQL worker creates them: the bundled workspace
    migrations plus the audit tables and triggers, run as the workspace role (which owns them)."""
    from tam_db import pg_connection, pg_schema
    from team_memory.audit import install_postgres, pg_audited_connection

    raw = _connect(database.url, database.settings, {**overrides, **database.connect_kwargs()})
    try:
        pg_connection.configure_session(raw, database.schema, database.settings)
    except BaseException:
        raw.close()
        raise
    password = DatabaseDsn.parse(database.url, DsnOrigin.ENV).password
    connection = pg_audited_connection()(raw, secrets=(password.get_secret_value(),) if password else ())
    try:
        pg_schema.ensure(connection)
        install_postgres(connection)
    finally:
        connection.close()


class PgMigrationTarget:
    """MigrationTarget over pg_provision: PgProvisioner for the control plane, PgWorkspaceProvisioner for
    workspaces, and the workspace schema installer run as the workspace role (which then owns the tables)."""

    def __init__(self, dsn: DatabaseDsn, instance_id: str, master_key: bytes, settings: DatabaseSettings,
                 install_schema: WorkspaceSchemaInstaller = install_workspace_schema,
                 hooks: Sequence[PostCopy] = DEFAULT_POST_COPY,
                 overrides: Mapping[str, str] = MappingProxyType({})):
        from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner

        self.url, self.instance_id, self.settings = dsn.to_uri(), instance_id, settings
        self.overrides = dict(overrides)
        self.control = PgProvisioner(self.url, settings, connect_overrides=self.overrides)
        self.workspaces = PgWorkspaceProvisioner(self.url, instance_id, master_key, settings,
                                                 connect_overrides=self.overrides)
        self.install_schema, self.hooks = install_schema, tuple(hooks)

    @contextmanager
    def admin(self) -> Iterator[psycopg.Connection]:
        from team_memory.pg_provision import admin_connect

        connection = admin_connect(self.url, self.settings, connect_overrides=self.overrides)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def connect(self, database: StoreDatabase) -> Iterator[psycopg.Connection]:
        connection = _connect(database.url, database.settings, {**self.overrides, **database.connect_kwargs()})
        try:
            yield connection
        finally:
            connection.close()

    def prepare_control(self) -> None:
        stored = self.control.bootstrap(self.instance_id)
        if stored != self.instance_id:
            raise MigrationError("The target database belongs to another TAM installation")

    def prepare_workspace(self, key: str) -> StoreDatabase:
        self.workspaces.ensure(key, migrate=False)
        database = self.workspaces.store_database(key)
        self.install_schema(database, self.overrides)
        return database

    def workspace_exists(self, key: str) -> bool:
        return self.workspaces.exists(key)

    def drop_workspace(self, key: str) -> None:
        self.workspaces.drop(key)

    def post_copy(self, kind: DatabaseKind) -> Sequence[PostCopy]:
        return self.hooks if kind is DatabaseKind.WORKSPACE else ()


class PgMigrationTargets:
    def __init__(self, master_key: bytes, settings: DatabaseSettings,
                 install_schema: WorkspaceSchemaInstaller = install_workspace_schema,
                 hooks: Sequence[PostCopy] = DEFAULT_POST_COPY):
        self.master_key, self.settings, self.install_schema, self.hooks = master_key, settings, install_schema, hooks

    def open(self, dsn: DatabaseDsn, instance_id: str,
             overrides: Mapping[str, str] = MappingProxyType({})) -> PgMigrationTarget:
        return PgMigrationTarget(dsn, instance_id, self.master_key, self.settings, self.install_schema, self.hooks,
                                 overrides)


class ActivePlane(Protocol):
    """The part of ControlPlane the switch needs."""

    def current(self) -> ActiveDatabase: ...

    def activate(self, target: ActiveDatabase) -> None: ...


class Maintenance:
    """Process-wide MaintenanceGate: one reason at a time, consulted by the app middleware and WorkerPool.

    ``stop_serving`` is final: after the PostgreSQL server lease was lost another server owns the
    installation, so this process stays in maintenance until it is restarted, whatever ``leave`` says.
    """

    def __init__(self, clock: Clock = utc_now):
        self._clock = clock
        self._lock = threading.Lock()
        self._state: MaintenanceState | None = None
        self._final: MaintenanceState | None = None

    def state(self) -> MaintenanceState | None:
        with self._lock:
            return self._final or self._state

    def enter(self, reason: MaintenanceReason, job_id: uuid.UUID | None) -> MaintenanceState:
        with self._lock:
            current = self._final or self._state
            if current is not None:
                raise Conflict(f"The server is already in maintenance ({current.reason.value})")
            self._state = MaintenanceState(reason=reason, job_id=job_id, since=self._clock())
            state = self._state
        LOGGER.info(json.dumps({"event": "maintenance_entered", "reason": reason.value,
                                "job_id": str(job_id) if job_id else None}))
        return state

    def leave(self) -> None:
        with self._lock:
            state, self._state = self._state, None
        if state is not None:
            LOGGER.info(json.dumps({"event": "maintenance_left", "reason": state.reason.value}))

    def stop_serving(self) -> MaintenanceState:
        """Enter maintenance for good (lost server lease); idempotent."""
        with self._lock:
            if self._final is None:
                self._final = MaintenanceState(reason=MaintenanceReason.LEASE_LOST, job_id=None, since=self._clock())
            state = self._final
        LOGGER.error(json.dumps({"event": "maintenance_final", "reason": state.reason.value}))
        return state


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write: temporary file, fsync, replace."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class SqliteArchiver:
    """Moves the SQLite databases to ``<root>/archive/sqlite-<ts>/`` and back (same relative paths)."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def name_for(self, moment: datetime) -> str:
        return ARCHIVE_DIR + "/" + ARCHIVE_PREFIX + moment.astimezone(UTC).strftime(ARCHIVE_TIMESTAMP)

    def _files(self, base: Path) -> list[Path]:
        names = [IDENTITY_DB, LEARNING_DB]
        workspaces = base / WORKSPACES_DIR
        if workspaces.is_dir():
            names.extend(f"{WORKSPACES_DIR}/{path.parent.name}/{MEMORY_DB}"
                         for path in sorted(workspaces.glob(f"*/{MEMORY_DB}")))
        files = []
        for name in names:
            for suffix in SQLITE_SIDECARS:
                path = base / (name + suffix)
                if path.is_file() and not path.is_symlink():
                    files.append(path)
        return files

    def _directory(self, archive: str) -> Path:
        directory = (self.root / archive).resolve()
        if directory.parent != (self.root / ARCHIVE_DIR).resolve():
            raise Conflict("Archive path must stay inside the server's archive directory")
        return directory

    def live_files(self) -> list[Path]:
        return self._files(self.root)

    def archive(self, archive: str) -> SqliteArchive:
        directory = self._directory(archive)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / ARCHIVE_DIR).chmod(0o700)
        total = 0
        moved: list[tuple[Path, Path]] = []
        try:
            for source in self._files(self.root):
                target = directory / source.relative_to(self.root)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                total += source.stat().st_size
                os.replace(source, target)
                moved.append((source, target))
        except OSError:
            # All or nothing: a half-archived root would leave live SQLite files next to an archive.
            for source, target in reversed(moved):
                os.replace(target, source)
            raise
        LOGGER.info(json.dumps({"event": "sqlite_archived", "archive": archive, "bytes": total}))
        return SqliteArchive(path=archive, created_at=utc_now(), bytes=total)

    def info(self, archive: str) -> SqliteArchive | None:
        directory = self._directory(archive)
        files = self._files(directory)
        if not files:
            return None
        created = datetime.fromtimestamp(directory.stat().st_mtime, UTC)
        return SqliteArchive(path=archive, created_at=created, bytes=sum(path.stat().st_size for path in files))

    def restore(self, archive: str) -> None:
        directory = self._directory(archive)
        files = self._files(directory)
        if not any(path.name == IDENTITY_DB for path in files):
            raise Conflict("The SQLite archive has no identity database; rollback is not possible")
        occupied = [path for path in files if (self.root / path.relative_to(directory)).exists()]
        if occupied:
            raise Conflict("SQLite databases already exist in server data; rollback would overwrite them")
        for source in files:
            target = self.root / source.relative_to(directory)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(source, target)
        shutil.rmtree(directory)
        LOGGER.info(json.dumps({"event": "sqlite_restored", "archive": archive}))

    def archive_paths(self, key: str) -> list[Path]:
        """Every archived copy of workspace ``key``: a PostgreSQL user purge deletes them too."""
        return archived_workspaces(self.root, key)

    def recover(self, config: DatabaseConfig | None) -> None:
        """Finish a switch interrupted between writing database.json and moving the files."""
        if config is None:
            return
        if config.backend is Backend.POSTGRES and config.archive and self.live_files():
            self.archive(config.archive)
        elif (config.backend is Backend.SQLITE and config.previous is not None and config.previous.archive
              and not (self.root / IDENTITY_DB).exists() and self.info(config.previous.archive) is not None):
            self.restore(config.previous.archive)


class Lease(Protocol):
    def acquire(self) -> "Lease": ...

    def release(self) -> None: ...

    def handover(self, url: str, connect_overrides: Mapping[str, str] | None = None) -> None: ...


class LeaseFactory(Protocol):
    def __call__(self, url: str, settings: DatabaseSettings, *, on_lost: Callable[[], None] | None = None,
                 connect_overrides: Mapping[str, str] = MappingProxyType({})) -> Lease: ...


def postgres_server_lease(url: str, settings: DatabaseSettings, *, on_lost: Callable[[], None] | None = None,
                          connect_overrides: Mapping[str, str] = MappingProxyType({})) -> Lease:
    from team_memory.pg_provision import server_lease

    return server_lease(url, settings, on_lost=on_lost, connect_overrides=connect_overrides)


class ServerLeaseHolder:
    """The PostgreSQL server lease of this process (plan 1.5): taken when the server starts on
    PostgreSQL or switches to it, kept for the server's lifetime, handed over on repoint, released on
    rollback and shutdown. ``on_lost`` runs (from the lease's heartbeat thread) when another server
    took the lock; it must stop this process from serving."""

    def __init__(self, factory: LeaseFactory = postgres_server_lease, on_lost: Callable[[], None] | None = None):
        self._factory = factory
        self._on_lost = on_lost
        self._lease: Lease | None = None
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._lease is not None

    def acquire(self, target: ActiveDatabase) -> None:
        """Conflict when another server holds the lease; a SQLite target needs none."""
        if target.backend is not Backend.POSTGRES:
            return
        with self._lock:
            if self._lease is None:
                self._lease = self._factory(target.url, target.settings, on_lost=self._on_lost,
                                            connect_overrides=target.connect_kwargs()).acquire()

    def handover(self, target: ActiveDatabase) -> None:
        """Repoint: move the lock to a session on ``target``'s DSN (Conflict when it cannot be taken)."""
        with self._lock:
            lease = self._lease
        if lease is None:
            self.acquire(target)
            return
        if target.backend is not Backend.POSTGRES:
            self.release()
            return
        lease.handover(target.url, connect_overrides=target.connect_kwargs())

    def release(self) -> None:
        with self._lock:
            lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class DatabaseSwitch:
    """Activation and rollback: server lease, control plane, database.json, worker recycle and SQLite
    archive, in a safe order."""

    def __init__(self, config_store: DatabaseConfigStore, plane: ActivePlane, pool: WorkerRecycler,
                 archiver: SqliteArchiver, clock: Clock = utc_now, lease: ServerLeaseHolder | None = None):
        self.config_store, self.plane, self.pool, self.archiver, self.clock = (
            config_store, plane, pool, archiver, clock)
        self.lease = lease or ServerLeaseHolder()

    def current(self) -> ActiveDatabase:
        return self.plane.current()

    def activate_postgres(self, dsn: DatabaseDsn, actor: str, origin: DsnOrigin = DsnOrigin.ENV) -> DatabaseConfig:
        before = self.plane.current()
        previous = self.config_store.load()
        now = self.clock()
        generation = max(before.generation, previous.generation if previous else 0) + 1
        config = DatabaseConfig(backend=Backend.POSTGRES, dsn_token=self.config_store.seal(dsn),
                                instance_id=uuid.UUID(before.instance_id), generation=generation, updated_at=now,
                                updated_by=actor, archive=self.archiver.name_for(now), origin=origin,
                                previous=previous.snapshot() if previous else None)
        options = tuple(sorted(connect_overrides(dsn, origin, self.archiver.root).items()))
        target = ActiveDatabase(backend=Backend.POSTGRES, instance_id=before.instance_id, generation=generation,
                                url=dsn.to_uri(), settings=before.settings, connect_options=options)
        # Before anything switches: a second server on this database must make the activation fail.
        self.lease.acquire(target)
        try:
            self.plane.activate(target)
        except BaseException:
            self.lease.release()
            raise
        try:
            self.config_store.save(config)
        except BaseException:
            self.plane.activate(before)
            self.lease.release()
            raise
        self.pool.recycle()
        try:
            self.archiver.archive(config.archive)
        except OSError as exc:
            self._revert_activation(before, config)
            LOGGER.error(json.dumps({"event": "sqlite_archive_failed", "archive": config.archive,
                                     "error": type(exc).__name__}))
            raise MigrationError("The SQLite files could not be moved to the archive; the server stays on "
                                 "SQLite. Free disk space or fix permissions in the data directory and run the "
                                 "migration again") from exc
        return config

    def _revert_activation(self, before: ActiveDatabase, activated: DatabaseConfig) -> None:
        """Back to SQLite after the PostgreSQL config was written: a newer SQLite config, the old plane."""
        reverted = DatabaseConfig(backend=Backend.SQLITE, instance_id=activated.instance_id,
                                  generation=activated.generation + 1, updated_at=self.clock(),
                                  updated_by=activated.updated_by, previous=activated.snapshot())
        self.config_store.save(reverted)
        self.plane.activate(ActiveDatabase(backend=Backend.SQLITE, instance_id=before.instance_id,
                                           generation=reverted.generation, settings=before.settings))
        self.lease.release()
        self.pool.recycle()

    def rollback_to_sqlite(self, actor: str) -> DatabaseConfig:
        current = self.config_store.load()
        if current is None or current.backend is not Backend.POSTGRES or current.archive is None:
            raise Conflict("Rollback needs an active PostgreSQL configuration made by a migration")
        before = self.plane.current()
        config = DatabaseConfig(backend=Backend.SQLITE, instance_id=current.instance_id,
                                generation=current.generation + 1, updated_at=self.clock(), updated_by=actor,
                                previous=current.snapshot())
        self.pool.recycle()
        self.archiver.restore(current.archive)
        try:
            self.plane.activate(ActiveDatabase(backend=Backend.SQLITE, instance_id=before.instance_id,
                                               generation=config.generation, settings=before.settings))
            self.config_store.save(config)
        except BaseException:
            self.plane.activate(before)
            self.archiver.archive(current.archive)
            raise
        self.lease.release()
        self.pool.recycle()
        return config

    def rollback_available(self) -> bool:
        config = self.config_store.load()
        return (config is not None and config.backend is Backend.POSTGRES and config.archive is not None
                and self.archiver.info(config.archive) is not None)


def connect_overrides(dsn: DatabaseDsn, origin: DsnOrigin, root: Path) -> Mapping[str, str]:
    """libpq keywords for every connection with ``dsn``: none for an operator DSN, the web hardening
    (empty passfile under ``root``, no client certificates, password authentication only) otherwise."""
    if origin is DsnOrigin.ENV:
        return dsn.connect_overrides(origin, "")
    from team_memory.pg_provision import empty_passfile

    return dsn.connect_overrides(origin, empty_passfile(root))


def _connect(url: str | None, settings: DatabaseSettings,
             overrides: Mapping[str, str] = MappingProxyType({})) -> psycopg.Connection:
    if not url:
        raise Conflict("The PostgreSQL target has no connection URL")
    return psycopg.connect(url, autocommit=True, connect_timeout=settings.connect_timeout_seconds, **overrides)


def quarantine_estimates(source: SourceDatabase) -> list[QuarantineEstimate]:
    return [QuarantineEstimate(database=item.database, table=item.table, rows=item.rows, reasons=item.reasons,
                               sample_pks=item.sample_pks, audit=item.audit) for item in quarantine_summary(source)]


def error_category(exc: BaseException) -> ErrorCategory:
    if isinstance(exc, psycopg.Error):
        from team_memory.db_check import categorize

        return categorize(exc)
    return ErrorCategory.UNEXPECTED


def failure_message(exc: BaseException) -> str:
    """Our own errors verbatim; driver errors as a fixed text with the SQLSTATE (driver text may echo data)."""
    if isinstance(exc, DomainError):
        return str(exc)
    if isinstance(exc, psycopg.Error):
        sqlstate = exc.sqlstate or "none"
        return f"PostgreSQL error {type(exc).__name__} (SQLSTATE {sqlstate}) during the migration"
    return f"Unexpected {type(exc).__name__} during the migration"


@dataclass
class _Plan:
    plan: MigrationPlan
    dsn: DatabaseDsn
    origin: DsnOrigin


class _Job:
    def __init__(self, progress: MigrationProgress):
        self.progress = progress
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_journal = 0.0


class MigrationService:
    """MigrationRunner: dry run, job thread, progress, cancel and rollback."""

    def __init__(self, root: Path, checker: DatabaseChecker, targets: MigrationTargets, gate: Maintenance,
                 switch: DatabaseSwitch, audit: AuditSink, organization: Callable[[], str], *,
                 throughput: Throughput = DEFAULT_THROUGHPUT, clock: Clock = utc_now):
        self.root = root.resolve()
        self.checker, self.targets, self.gate, self.switch = checker, targets, gate, switch
        self.audit, self.organization = audit, organization
        self.throughput, self.clock = throughput, clock
        self._lock = threading.Lock()
        self._plans: dict[uuid.UUID, _Plan] = {}
        self._job: _Job | None = None
        self._close_interrupted_journals()

    # Journal

    @property
    def journal_dir(self) -> Path:
        return self.root / MIGRATION_DIR

    def _journals(self) -> list[MigrationProgress]:
        if not self.journal_dir.is_dir():
            return []
        journals = []
        for path in self.journal_dir.glob("*" + JOURNAL_SUFFIX):
            try:
                journals.append(MigrationProgress.model_validate_json(path.read_text(encoding="utf-8")))
            except ValueError as exc:
                LOGGER.warning(json.dumps({"event": "migration_journal_unreadable", "path": path.name,
                                           "error": type(exc).__name__}))
        return sorted(journals, key=lambda journal: journal.updated_at)

    def _write_journal(self, progress: MigrationProgress) -> None:
        _write_private(self.journal_dir / f"{progress.job_id}{JOURNAL_SUFFIX}", progress.model_dump_json(indent=2))

    def _close_interrupted_journals(self) -> None:
        for journal in self._journals():
            if journal.phase not in TERMINAL_PHASES:
                now = self.clock()
                self._write_journal(journal.model_copy(update={
                    "phase": MigrationPhase.FAILED, "updated_at": now, "finished_at": now,
                    "error": SERVER_STOPPED_ERROR, "error_category": ErrorCategory.UNEXPECTED}))

    # Dry run

    def plan(self, dsn: DatabaseDsn, actor: str, origin: DsnOrigin = DsnOrigin.WEB) -> MigrationPlan:
        """``origin`` WEB (dashboard, setup wizard: the default) or ENV (``tam-team db-migrate``): WEB DSNs
        must meet the web rules and every connection of the job uses the WEB connection overrides."""
        actor = self._actor(actor)
        dsn = dsn.validate_for(origin)
        with self._lock:
            if self._job is not None and not self._job.progress.terminal:
                raise Conflict("A migration is already running")
        current = self.switch.current()
        report = self.checker.check(dsn, instance_id=uuid.UUID(current.instance_id))
        self._audit("database_tested", dsn, actor)
        # Once on PostgreSQL the SQLite files are archived: there is nothing left to estimate.
        sources = discover_sources(self.root) if current.backend is Backend.SQLITE else []
        estimates = tuple(estimate(source) for source in sources)
        blockers = self._blockers(report, current, sources)
        quarantine = tuple(item for source in sources if source.kind is DatabaseKind.WORKSPACE
                           for item in quarantine_estimates(source))
        now = self.clock()
        plan = MigrationPlan(plan_id=uuid.uuid4(), created_at=now, expires_at=now + timedelta(seconds=PLAN_TTL_SECONDS),
                             created_by=actor, target=dsn.masked(), report=report, databases=estimates,
                             estimated_seconds=self.throughput.seconds(estimates), blockers=tuple(blockers),
                             quarantine=quarantine)
        with self._lock:
            self._plans = {key: value for key, value in self._plans.items() if value.plan.expires_at > now}
            self._plans[plan.plan_id] = _Plan(plan=plan, dsn=dsn, origin=origin)
        return plan

    @staticmethod
    def _blockers(report: CheckReport, current: ActiveDatabase, sources: Sequence[SourceDatabase]) -> list[str]:
        blockers = [f"{check.id.value}: {check.message}" for check in report.checks
                    if check.status is CheckStatus.FAILED]
        if report.target_state is TargetState.FOREIGN_INSTALLATION:
            blockers.append("The target database belongs to another TAM installation")
        elif report.target_state is TargetState.UNKNOWN and not blockers:
            blockers.append("The target database state could not be determined")
        if current.backend is Backend.POSTGRES:
            blockers.append("The server already runs on PostgreSQL; use repoint to change the DSN")
        # Workspace orphans are quarantined (a warning); identity and learning enforce their foreign
        # keys, so orphans there mean a damaged file and have nowhere to go.
        known = bundled_sqlite_migrations()
        for source in sources:
            blockers.extend(text_problems(source))
            if source.kind is DatabaseKind.WORKSPACE:
                newer = unknown_migrations(source, known)
                if newer:
                    blockers.append(f"{source.name}: written by a newer TAM (migrations {', '.join(newer[:5])}); "
                                    "upgrade this server first")
                continue
            broken = foreign_key_violations(source.path)
            if broken:
                blockers.append(f"{source.name}: rows violate foreign keys in {', '.join(broken)}")
        return blockers

    # Job

    def start(self, plan_id: uuid.UUID, actor: str) -> MigrationProgress:
        actor = self._actor(actor)
        now = self.clock()
        with self._lock:
            if self._job is not None and not self._job.progress.terminal:
                raise Conflict("A migration is already running")
            self._require_sqlite()
            entry = self._plans.get(plan_id)
            if entry is None or entry.plan.expires_at <= now:
                self._plans.pop(plan_id, None)
                raise NotFound("The migration plan is unknown or expired; run the dry run again")
            if not entry.plan.ready:
                raise Conflict("The migration plan has blockers: " + "; ".join(entry.plan.blockers))
            del self._plans[plan_id]
            completed = self._resumable(entry.plan)
            progress = MigrationProgress(job_id=uuid.uuid4(), plan_id=plan_id, phase=MigrationPhase.PREFLIGHT,
                                         started_at=now, updated_at=now, started_by=actor, target=entry.plan.target,
                                         completed_workspaces=completed, resumed=bool(completed))
            job = _Job(progress)
            self._job = job
        self._write_journal(progress)
        job.thread = threading.Thread(target=self._run, args=(job, entry.dsn, entry.origin, actor), name="tam-db-migration",
                                      daemon=True)
        job.thread.start()
        return progress

    def _require_sqlite(self) -> None:
        """A plan made while the server was on SQLite must never run once PostgreSQL is active."""
        config = self.switch.config_store.load()
        if self.switch.current().backend is Backend.POSTGRES or (config is not None
                                                                 and config.backend is Backend.POSTGRES):
            raise Conflict("The server already runs on PostgreSQL; use repoint to change the DSN")

    def run(self, plan_id: uuid.UUID, actor: str) -> MigrationProgress:
        """Start and wait: the headless ``tam-team db-migrate`` path."""
        self.start(plan_id, actor)
        job = self._job
        if job is not None and job.thread is not None:
            job.thread.join()
        return self.progress()

    def _resumable(self, plan: MigrationPlan) -> tuple[str, ...]:
        if not plan.resumable:
            return ()
        for journal in reversed(self._journals()):
            if journal.target == plan.target and journal.phase in (MigrationPhase.FAILED, MigrationPhase.CANCELLED):
                return journal.completed_workspaces
        return ()

    def progress(self) -> MigrationProgress | None:
        with self._lock:
            if self._job is not None:
                return self._job.progress
        journals = self._journals()
        return journals[-1] if journals else None

    def cancel(self, actor: str) -> MigrationProgress:
        actor = self._actor(actor)
        with self._lock:
            job = self._job
            if job is None or not job.progress.cancellable:
                raise Conflict("No cancellable migration is running")
            job.cancel.set()
        progress = self._update(job, cancel_requested=True)
        LOGGER.info(json.dumps({"event": "database_migration_cancel_requested", "job_id": str(progress.job_id),
                                "actor": actor}))
        return progress

    def _update(self, job: _Job, *, journal: bool = True, **changes) -> MigrationProgress:
        with self._lock:
            changes.setdefault("updated_at", self.clock())
            job.progress = MigrationProgress.model_validate({**job.progress.model_dump(), **changes})
            progress = job.progress
        if journal:
            self._write_journal(progress)
            job.last_journal = progress.updated_at.timestamp()
        return progress

    def _run(self, job: _Job, dsn: DatabaseDsn, origin: DsnOrigin, actor: str) -> None:
        workspace_in_progress: str | None = None
        target: MigrationTarget | None = None
        with ExitStack() as stack:
            try:
                self._require_sqlite()
                current = self.switch.current()
                target = self.targets.open(dsn, current.instance_id, connect_overrides(dsn, origin, self.root))
                report = self.checker.check(dsn, instance_id=uuid.UUID(current.instance_id))
                if not report.ok or report.target_state not in (TargetState.EMPTY, TargetState.SAME_INSTALLATION):
                    raise Conflict("The target database no longer passes the checks; run the dry run again")
                self._update(job, phase=MigrationPhase.MAINTENANCE)
                self.gate.enter(MaintenanceReason.MIGRATION, job.progress.job_id)
                stack.callback(self.gate.leave)
                self.switch.pool.recycle()
                sources = discover_sources(self.root)
                for source in sources:
                    if source.kind is DatabaseKind.WORKSPACE:
                        stack.enter_context(ServerLease(source.path.parent))
                self._audit("database_migration_started", dsn, actor, job=job)
                estimates = [estimate(source) for source in sources]
                workspaces = [source for source in sources if source.kind is DatabaseKind.WORKSPACE]
                self._update(job, phase=MigrationPhase.COPY_CONTROL, rows_total=sum(e.rows for e in estimates),
                             workspace_total=len(workspaces))
                observer = _Observer(self, job)
                target.prepare_control()
                digests: dict[str, DatabaseDigest] = {}
                with target.admin() as admin:
                    for source in sources:
                        if source.kind is DatabaseKind.WORKSPACE:
                            continue
                        schema = SCHEMAS[source.kind]
                        if has_rows(source, admin, schema, SEEDED_TABLES):
                            with admin.transaction():
                                truncate_copied_tables(source, admin, schema, SEEDED_TABLES)
                        digests[source.name] = copy_database(source, admin, schema, observer,
                                                             target.post_copy(source.kind), SEEDED_TABLES)
                self._update(job, phase=MigrationPhase.COPY_WORKSPACES)
                completed = list(job.progress.completed_workspaces)
                for index, source in enumerate(workspaces, start=1):
                    if source.name in completed and target.workspace_exists(source.name):
                        observer.rows_copied(sum(e.rows for e in estimates if e.name == source.name))
                        self._update(job, workspace_index=index, **self._quarantined(job, source))
                        continue
                    workspace_in_progress = source.name
                    self._update(job, workspace_index=index - 1, database=source.name, table=None)
                    digests[source.name] = self._copy_workspace(target, source, observer)
                    completed.append(source.name)
                    workspace_in_progress = None
                    self._update(job, workspace_index=index, completed_workspaces=tuple(dict.fromkeys(completed)),
                                 **self._quarantined(job, source))
                self._update(job, phase=MigrationPhase.VERIFY, database=None, table=None)
                self._verify(target, sources, digests, observer)
                self._update(job, phase=MigrationPhase.ACTIVATE)
                self.switch.activate_postgres(dsn, actor, origin)
                with self._lock:
                    self._plans.clear()
            except MigrationCancelled:
                self._abandon(target, workspace_in_progress)
                self._finish(job, MigrationPhase.CANCELLED)
                LOGGER.info(json.dumps({"event": "database_migration_cancelled", "job_id": str(job.progress.job_id)}))
                return
            except Exception as exc:  # noqa: BLE001 — the job thread records every failure in its journal
                self._abandon(target, workspace_in_progress)
                message, category = failure_message(exc), error_category(exc)
                self._finish(job, MigrationPhase.FAILED, error=message, error_category=category)
                self._audit("database_migration_failed", dsn, actor, job=job, error=category.value)
                LOGGER.error(json.dumps({"event": "database_migration_failed", "job_id": str(job.progress.job_id),
                                         "error": message, "category": category.value}))
                return
        self._finish(job, MigrationPhase.DONE, rows_copied=job.progress.rows_total)
        self._audit("database_migration_finished", dsn, actor, job=job)
        self._audit("database_activated", dsn, actor, job=job)

    @staticmethod
    def _quarantined(job: _Job, source: SourceDatabase) -> dict:
        """Progress fields after a workspace: its quarantined rows added to the job's report."""
        items = [item for item in job.progress.quarantine if item.database != source.name]
        items += quarantine_estimates(source)
        return {"quarantine": tuple(items), "quarantined_rows": sum(item.rows for item in items)}

    def _copy_workspace(self, target: MigrationTarget, source: SourceDatabase, observer: "_Observer") -> DatabaseDigest:
        database = target.prepare_workspace(source.name)
        with target.connect(database) as connection:
            stale = has_rows(source, connection, database.schema)
        if stale:
            target.drop_workspace(source.name)
            database = target.prepare_workspace(source.name)
        with target.connect(database) as connection:
            return copy_database(source, connection, database.schema, observer,
                                 target.post_copy(DatabaseKind.WORKSPACE), replace=REPLACED_WORKSPACE_TABLES,
                                 ledgers=WORKSPACE_LEDGERS)

    def _verify(self, target: MigrationTarget, sources: Sequence[SourceDatabase], digests: dict[str, DatabaseDigest],
                observer: "_Observer") -> None:
        """Each database is read again: the SQLite side must still equal what was copied (a write
        after the copy would otherwise be lost), and PostgreSQL must equal the SQLite side."""
        with target.admin() as admin:
            for source in sources:
                if source.kind is DatabaseKind.WORKSPACE:
                    continue
                observer.checkpoint()
                schema = SCHEMAS[source.kind]
                if compare(digests[source.name], source_digest(source, admin, schema)):
                    raise MigrationError(f"{source.name} changed after it was copied (a write during the "
                                         "migration); run the migration again")
                self._check(source, digests[source.name], target_digest(source, admin, schema, SEEDED_TABLES))
        for source in sources:
            if source.kind is not DatabaseKind.WORKSPACE:
                continue
            observer.checkpoint()
            database = target.prepare_workspace(source.name)
            with target.connect(database) as connection:
                fresh = source_digest(source, connection, database.schema, WORKSPACE_LEDGERS)
                actual = target_digest(source, connection, database.schema, ledgers=WORKSPACE_LEDGERS)
            copied = digests.get(source.name)
            if (copied is not None and compare(copied, fresh)) or compare(fresh, actual):
                # Changed after its copy (or after an earlier job copied it on resume): copy it again.
                LOGGER.info(json.dumps({"event": "database_migration_recopy", "workspace": source.name}))
                fresh = self._copy_workspace(target, source, observer)
                with target.connect(database) as connection:
                    actual = target_digest(source, connection, database.schema, ledgers=WORKSPACE_LEDGERS)
            self._check(source, fresh, actual)

    @staticmethod
    def _check(source: SourceDatabase, expected: DatabaseDigest, actual: DatabaseDigest) -> None:
        mismatches = compare(expected, actual)
        if mismatches:
            details = "; ".join(f"{item.table}: {item.reason}" for item in mismatches[:5])
            raise MigrationError(f"Verification failed for {source.name}: {details}")

    def _abandon(self, target: MigrationTarget | None, workspace: str | None) -> None:
        if target is None or workspace is None:
            return
        try:
            target.drop_workspace(workspace)
        except (psycopg.Error, DomainError) as exc:
            LOGGER.error(json.dumps({"event": "database_migration_cleanup_failed", "workspace": workspace,
                                     "error": failure_message(exc)}))

    def _finish(self, job: _Job, phase: MigrationPhase, **changes) -> None:
        now = self.clock()
        self._update(job, phase=phase, finished_at=now, updated_at=now, **changes)

    # Rollback

    def rollback(self, organization: str, actor: str) -> DatabaseConfig:
        actor = self._actor(actor)
        expected = self.organization()
        if not expected or organization.strip() != expected:
            raise Forbidden("Type the organization name exactly to confirm the rollback")
        with self._lock:
            if self._job is not None and not self._job.progress.terminal:
                raise Conflict("A migration is running")
        self.gate.enter(MaintenanceReason.ROLLBACK, None)
        try:
            config = self.switch.rollback_to_sqlite(actor)
            with self._lock:
                self._plans.clear()
        finally:
            self.gate.leave()
        self._audit("database_rolled_back", None, actor, generation=config.generation)
        return config

    # Helpers

    @staticmethod
    def _actor(actor: str) -> str:
        actor = (actor or "").strip()
        if not actor or len(actor) > MAX_ACTOR_CHARS:
            raise DomainError(f"Actor must be 1-{MAX_ACTOR_CHARS} characters")
        return actor

    def _audit(self, action: str, dsn: DatabaseDsn | None, actor: str, *, job: _Job | None = None,
               **fields: str | int | bool | None) -> None:
        if job is not None:
            fields["job_id"] = str(job.progress.job_id)
        try:
            self.audit(action, actor, dsn, **fields)
        except (DomainError, OSError, psycopg.Error, sqlite3.Error) as exc:
            # The operation itself succeeded; a lost audit row must not turn it into a failure.
            LOGGER.error(json.dumps({"event": "database_audit_failed", "action": action,
                                     "error": type(exc).__name__}))


class _Observer:
    """CopyObserver feeding the job progress; checkpoints raise MigrationCancelled after a cancel request."""

    def __init__(self, service: MigrationService, job: _Job):
        self.service, self.job = service, job

    def table_started(self, database: str, table: str, rows: int) -> None:
        self.service._update(self.job, database=database, table=table)

    def rows_copied(self, rows: int) -> None:
        progress = self.job.progress
        copied = min(progress.rows_copied + rows, progress.rows_total)
        now = self.service.clock()
        journal = now.timestamp() - self.job.last_journal >= JOURNAL_INTERVAL_SECONDS
        self.service._update(self.job, journal=journal, rows_copied=copied, updated_at=now)

    def checkpoint(self) -> None:
        if self.job.cancel.is_set():
            raise MigrationCancelled("The migration was cancelled")
