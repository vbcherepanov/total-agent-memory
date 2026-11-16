"""SQLite -> PostgreSQL migration job, maintenance, activation, archive and rollback (team_memory.migration_service)."""
import functools
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tam_db.contracts import ActiveDatabase, Backend, schema_for
from team_memory.contracts import Conflict, DomainError, Forbidden, NotFound
from team_memory.database_contracts import (
    DatabaseConfig,
    DatabaseDsn,
    MaintenanceReason,
    MigrationPhase,
    MigrationProgress,
    maintenance_allows,
)

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra: pip install '.[postgres]'")

from team_memory.migration_service import (
    DatabaseSwitch,
    Maintenance,
    MigrationService,
    PgMigrationTargets,
    SqliteArchiver,
)
from tests.pg_migration_support import create_quarantine, mirror_schema

ORGANIZATION = "Acme Robotics"
ACTOR = "root-admin"
TEAM_KEY = "team_" + "c" * 64
WORKSPACE_KEYS = ("shared", TEAM_KEY)
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def write_workspace(root, key, notes):
    path = root / "workspaces" / key / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                                                  project TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS tam_history (sequence INTEGER PRIMARY KEY, record_id INTEGER NOT NULL,
                                                    operation TEXT NOT NULL);
        """)
        for note in notes:
            record = db.execute("INSERT INTO knowledge(content, project, created_at) VALUES (?, 'ops', ?)",
                                (note, NOW.isoformat())).lastrowid
            db.execute("INSERT INTO tam_history(record_id, operation) VALUES (?, 'insert')", (record,))
        db.commit()
    return path


class Pool:
    def __init__(self):
        self.recycled = 0

    def recycle(self) -> int:
        self.recycled += 1
        return 0


class Installer:
    """Workspace schema installer standing in for the PostgreSQL baseline: mirrors the source memory.db."""

    def __init__(self, root):
        self.root = root

    def __call__(self, database, overrides):
        key = next(key for key in WORKSPACE_KEYS if schema_for(key) == database.schema)
        with psycopg.connect(database.url, autocommit=True) as connection:
            exists = connection.execute("SELECT to_regclass(%s)", (f"{database.schema}.knowledge",)).fetchone()[0]
            if exists is None:
                mirror_schema(self.root / "workspaces" / key / "memory.db", connection, database.schema,
                              create_schema=False)
                create_quarantine(connection, database.schema)


class Setup:
    def __init__(self, root, pg_database, hooks=(), workspaces=True):
        from team_memory.database import SwitchableControlPlane
        from team_memory.database_config import (
            FileDatabaseConfigStore,
            record_database_event,
        )
        from team_memory.db_check import PgDatabaseChecker
        from team_memory.learning.repository import LearningRepository
        from team_memory.registry import Registry
        from team_memory.settings import load_master_key

        self.root = root
        self.environ = {}
        base = SwitchableControlPlane.sqlite(root)
        self.plane = SwitchableControlPlane(root, base.current(), master_key=lambda: load_master_key(root, self.environ))
        self.registry = Registry(root, self.plane)
        self.registry.add_user("anna", "Anna")
        self.registry.set_organization({"name": ORGANIZATION})
        LearningRepository(root, self.plane)
        if workspaces:
            for key, notes in zip(WORKSPACE_KEYS, (["shared note one", "shared note two"], ["team decision"]),
                                  strict=True):
                write_workspace(root, key, notes)
        self.store = FileDatabaseConfigStore(root, self.environ)
        self.pool = Pool()
        self.gate = Maintenance()
        self.archiver = SqliteArchiver(root)
        self.switch = DatabaseSwitch(self.store, self.plane, self.pool, self.archiver)
        self.targets = PgMigrationTargets(load_master_key(root, self.environ), self.plane.settings, Installer(root),
                                          hooks)
        self.dsn = DatabaseDsn.parse(pg_database.url)
        self.service = MigrationService(root, PgDatabaseChecker(), self.targets, self.gate, self.switch,
                                        functools.partial(record_database_event, self.plane),
                                        lambda: self.registry.organization().get("name", ""))

    def migrate(self) -> MigrationProgress:
        plan = self.service.plan(self.dsn, ACTOR)
        assert plan.ready, plan.blockers
        return self.service.run(plan.plan_id, ACTOR)

    def events(self) -> list[str]:
        return [event["action"] for event in reversed(self.registry.audit_events(limit=100))
                if event["action"].startswith("database_")]


# Maintenance and archive: no PostgreSQL needed


def test_maintenance_gate_holds_one_reason_at_a_time():
    gate = Maintenance(clock=lambda: NOW)
    assert gate.state() is None
    job = uuid.uuid4()
    state = gate.enter(MaintenanceReason.MIGRATION, job)
    assert (state.reason, state.job_id, state.since) == (MaintenanceReason.MIGRATION, job, NOW)
    with pytest.raises(Conflict, match="already in maintenance"):
        gate.enter(MaintenanceReason.ROLLBACK, None)
    assert not maintenance_allows("POST", "/mcp")
    gate.leave()
    assert gate.state() is None
    gate.leave()


def make_sqlite_root(root):
    (root / "identity.db").write_bytes(b"identity")
    (root / "identity.db-wal").write_bytes(b"wal")
    (root / "learning.db").write_bytes(b"learning")
    for key in WORKSPACE_KEYS:
        (root / "workspaces" / key).mkdir(parents=True)
        (root / "workspaces" / key / "memory.db").write_bytes(key.encode())
        (root / "workspaces" / key / ".server.lock").write_bytes(b"0")
    (root / "master.key").write_bytes(b"key")


def test_archive_moves_only_database_files_and_restores_them(tmp_path):
    make_sqlite_root(tmp_path)
    archiver = SqliteArchiver(tmp_path)
    name = archiver.name_for(NOW)
    assert name == "archive/sqlite-20260925T120000000000Z"
    archive = archiver.archive(name)
    assert archive.bytes == len(b"identitywallearning") + sum(len(key) for key in WORKSPACE_KEYS)
    assert not (tmp_path / "identity.db").exists() and not (tmp_path / "identity.db-wal").exists()
    assert (tmp_path / "master.key").exists()
    assert (tmp_path / "workspaces" / "shared" / ".server.lock").exists()
    assert (tmp_path / name / "workspaces" / TEAM_KEY / "memory.db").read_bytes() == TEAM_KEY.encode()
    assert archiver.info(name).bytes == archive.bytes
    assert archiver.archive_paths(TEAM_KEY) == [tmp_path / name / "workspaces" / TEAM_KEY]
    assert archiver.archive_paths("shared") == [tmp_path / name / "workspaces" / "shared"]
    archiver.restore(name)
    assert (tmp_path / "identity.db").read_bytes() == b"identity"
    assert (tmp_path / "workspaces" / "shared" / "memory.db").read_bytes() == b"shared"
    assert not (tmp_path / name).exists()
    assert archiver.archive_paths(TEAM_KEY) == []


def test_archive_restore_refuses_to_overwrite_live_databases(tmp_path):
    make_sqlite_root(tmp_path)
    archiver = SqliteArchiver(tmp_path)
    name = archiver.name_for(NOW)
    archiver.archive(name)
    (tmp_path / "identity.db").write_bytes(b"new")
    with pytest.raises(Conflict, match="overwrite"):
        archiver.restore(name)
    assert (tmp_path / name / "identity.db").exists()


def test_archive_paths_stay_inside_the_archive_directory(tmp_path):
    archiver = SqliteArchiver(tmp_path)
    with pytest.raises(Conflict, match="inside"):
        archiver.archive("archive/../escape")
    with pytest.raises(Conflict, match="inside"):
        archiver.info("workspaces/sqlite-x")


def pg_config(archive: str | None, generation: int = 1) -> DatabaseConfig:
    return DatabaseConfig(backend=Backend.POSTGRES, dsn_token="token", instance_id=uuid.UUID(int=1),
                          generation=generation, updated_at=NOW, updated_by=ACTOR, archive=archive)


def test_recover_finishes_an_interrupted_archive_and_an_interrupted_rollback(tmp_path):
    make_sqlite_root(tmp_path)
    archiver = SqliteArchiver(tmp_path)
    name = archiver.name_for(NOW)
    config = pg_config(name)
    archiver.recover(config)
    assert not (tmp_path / "identity.db").exists() and archiver.info(name) is not None
    rolled_back = DatabaseConfig(backend=Backend.SQLITE, instance_id=config.instance_id, generation=2,
                                 updated_at=NOW, updated_by=ACTOR, previous=config.snapshot())
    archiver.recover(rolled_back)
    assert (tmp_path / "identity.db").exists() and archiver.info(name) is None
    archiver.recover(None)


class MemoryConfigStore:
    def __init__(self, fail_save: bool = False):
        self.config: DatabaseConfig | None = None
        self.fail_save = fail_save

    def load(self):
        return self.config

    def save(self, config):
        if self.fail_save:
            raise OSError("disk full")
        self.config = config

    def seal(self, dsn):
        return "sealed:" + dsn.host_db()


class Plane:
    def __init__(self):
        self.target = ActiveDatabase(backend=Backend.SQLITE, instance_id=str(uuid.UUID(int=1)), generation=0)
        self.history = []

    def current(self):
        return self.target

    def activate(self, target):
        self.history.append(target.backend)
        self.target = target


def test_switch_activation_order_and_revert_when_the_config_cannot_be_saved(tmp_path):
    make_sqlite_root(tmp_path)
    dsn = DatabaseDsn.parse("postgresql://tam:secret@db.internal:5432/tam?sslmode=require")
    plane, pool = Plane(), Pool()
    from team_memory.migration_service import ServerLeaseHolder

    lease = ServerLeaseHolder(lambda url, settings, **kwargs: FakeLease([]))
    switch = DatabaseSwitch(MemoryConfigStore(fail_save=True), plane, pool, SqliteArchiver(tmp_path), clock=lambda: NOW,
                            lease=lease)
    with pytest.raises(OSError):
        switch.activate_postgres(dsn, ACTOR)
    assert plane.history == [Backend.POSTGRES, Backend.SQLITE] and plane.target.backend is Backend.SQLITE
    assert pool.recycled == 0 and (tmp_path / "identity.db").exists()

    store = MemoryConfigStore()
    switch = DatabaseSwitch(store, Plane(), pool, SqliteArchiver(tmp_path), clock=lambda: NOW, lease=lease)
    config = switch.activate_postgres(dsn, ACTOR)
    assert (config.backend, config.generation, config.dsn_token, config.previous) == (
        Backend.POSTGRES, 1, "sealed:db.internal:5432/tam", None)
    assert switch.plane.current().url == dsn.to_uri()
    assert pool.recycled == 1 and not (tmp_path / "identity.db").exists()
    assert switch.rollback_available()

    rolled = switch.rollback_to_sqlite(ACTOR)
    assert (rolled.backend, rolled.generation, rolled.previous.backend) == (Backend.SQLITE, 2, Backend.POSTGRES)
    assert switch.plane.current().backend is Backend.SQLITE and (tmp_path / "identity.db").exists()
    assert not switch.rollback_available()
    with pytest.raises(Conflict, match="Rollback needs"):
        switch.rollback_to_sqlite(ACTOR)


# Full job against PostgreSQL


@pytest.fixture
def setup(tmp_path, pg_database):
    return Setup(tmp_path / "server", pg_database)


def pg_rows(dsn: DatabaseDsn, query: str, *args):
    with psycopg.connect(dsn.to_uri(), autocommit=True) as connection:
        return connection.execute(query, args or None).fetchall()


@pytest.mark.postgres
def test_dry_run_writes_nothing_and_estimates(setup):
    plan = setup.service.plan(setup.dsn, ACTOR)
    assert plan.ready and not plan.resumable and plan.report.ok
    assert plan.target == setup.dsn.masked() and "secret" not in plan.model_dump_json()
    names = [database.name for database in plan.databases]
    assert names == ["identity", "learning", "shared", TEAM_KEY]
    shared = next(database for database in plan.databases if database.name == "shared")
    assert {table.name: table.rows for table in shared.tables} == {"knowledge": 2, "tam_history": 2}
    assert plan.total_rows >= 6 and plan.estimated_seconds >= 1
    assert pg_rows(setup.dsn, "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tam\\_%' OR nspname LIKE 'ws\\_%'") == []
    assert setup.events() == ["database_tested"]


@pytest.mark.postgres
def test_migration_copies_verifies_activates_and_archives(setup):
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.DONE, progress.error
    assert progress.workspace_index == progress.workspace_total == 2 and progress.percent == 100.0
    assert set(progress.completed_workspaces) == set(WORKSPACE_KEYS)
    config = setup.store.load()
    assert config.backend is Backend.POSTGRES and config.generation == 1
    assert config.archive and setup.store.unseal(config).to_uri() == setup.dsn.to_uri()
    assert setup.plane.current().backend is Backend.POSTGRES
    assert setup.gate.state() is None and setup.pool.recycled >= 2
    assert not (setup.root / "identity.db").exists() and not (setup.root / "learning.db").exists()
    assert not list((setup.root / "workspaces").glob("*/memory.db"))
    assert (setup.root / config.archive / "workspaces" / TEAM_KEY / "memory.db").is_file()
    assert pg_rows(setup.dsn, "SELECT id, name FROM tam_control.users") == [("anna", "Anna")]
    assert pg_rows(setup.dsn, "SELECT value FROM tam_control.meta WHERE key = 'instance_id'") == [
        (setup.plane.current().instance_id,)]
    for key, count in (("shared", 2), (TEAM_KEY, 1)):
        assert pg_rows(setup.dsn, f"SELECT count(*) FROM {schema_for(key)}.knowledge") == [(count,)]
    # The registry now reads PostgreSQL: the organization and the audit trail moved with the data.
    assert setup.registry.organization()["name"] == ORGANIZATION
    assert setup.events() == ["database_tested", "database_migration_started", "database_migration_finished",
                              "database_activated"]
    journal = json.loads((setup.root / "migration" / f"{progress.job_id}.json").read_text(encoding="utf-8"))
    assert journal["phase"] == "done"
    assert (setup.root / "migration" / f"{progress.job_id}.json").stat().st_mode & 0o077 == 0


@pytest.mark.postgres
def test_second_plan_is_blocked_once_on_postgres(setup):
    setup.migrate()
    plan = setup.service.plan(setup.dsn, ACTOR)
    assert not plan.ready and any("already runs on PostgreSQL" in blocker for blocker in plan.blockers)
    with pytest.raises(Conflict, match="already runs on PostgreSQL"):
        setup.service.start(plan.plan_id, ACTOR)


@pytest.mark.postgres
def test_maintenance_is_active_while_copying(tmp_path, pg_database):
    seen = []
    holder = {}

    def hook(connection, schema):
        seen.append(holder["setup"].gate.state())

    holder["setup"] = setup = Setup(tmp_path / "server", pg_database, hooks=(hook,))
    assert setup.migrate().phase is MigrationPhase.DONE
    assert [state.reason for state in seen] == [MaintenanceReason.MIGRATION] * 2
    assert setup.gate.state() is None


@pytest.mark.postgres
def test_cancel_keeps_sqlite_and_config_and_a_later_job_resumes(tmp_path, pg_database):
    holder = {}

    def cancel_on_second_workspace(connection, schema):
        if schema == schema_for(TEAM_KEY):
            holder["setup"].service.cancel(ACTOR)

    holder["setup"] = setup = Setup(tmp_path / "server", pg_database, hooks=(cancel_on_second_workspace,))
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.CANCELLED and progress.cancel_requested
    assert progress.completed_workspaces == ("shared",)
    assert setup.store.load() is None and setup.plane.current().backend is Backend.SQLITE
    assert (setup.root / "identity.db").is_file() and setup.gate.state() is None
    assert pg_rows(setup.dsn, "SELECT to_regnamespace(%s)::text", schema_for(TEAM_KEY)) == [(None,)]
    assert pg_rows(setup.dsn, f"SELECT count(*) FROM {schema_for('shared')}.knowledge") == [(2,)]
    with pytest.raises(Conflict, match="No cancellable"):
        setup.service.cancel(ACTOR)

    # The shared workspace changes after the first job: the resumed job must notice and recopy it.
    write_workspace(setup.root, "shared", ["written between the jobs"])
    holder["setup"] = None
    setup.targets.hooks = ()
    plan = setup.service.plan(setup.dsn, ACTOR)
    assert plan.resumable and plan.ready
    started = setup.service.start(plan.plan_id, ACTOR)
    assert started.resumed and started.completed_workspaces == ("shared",)
    setup.service._job.thread.join()
    assert setup.service.progress().phase is MigrationPhase.DONE, setup.service.progress().error
    assert pg_rows(setup.dsn, f"SELECT count(*) FROM {schema_for('shared')}.knowledge") == [(3,)]


@pytest.mark.postgres
def test_failure_reports_the_error_and_leaves_sqlite_active(tmp_path, pg_database):
    def broken(connection, schema):
        raise psycopg.errors.InsufficientPrivilege("permission denied for secret-table")

    setup = Setup(tmp_path / "server", pg_database, hooks=(broken,))
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.FAILED
    assert progress.error_category.value == "permission" and "secret-table" not in progress.error
    assert setup.store.load() is None and (setup.root / "identity.db").is_file()
    assert setup.gate.state() is None
    assert pg_rows(setup.dsn, "SELECT to_regnamespace(%s)::text", schema_for("shared")) == [(None,)]
    assert setup.events()[-1] == "database_migration_failed"


@pytest.mark.postgres
def test_rollback_needs_the_organization_name_and_restores_sqlite(setup):
    setup.migrate()
    setup.registry.add_user("boris", "Boris")
    with pytest.raises(Forbidden, match="organization name"):
        setup.service.rollback("acme robotics", ACTOR)
    from team_memory.pg_provision import server_lease

    with pytest.raises(Conflict, match="Another TAM server"):
        server_lease(setup.dsn.to_uri()).acquire()
    config = setup.service.rollback(f"  {ORGANIZATION} ", ACTOR)
    server_lease(setup.dsn.to_uri()).acquire().release()
    assert config.backend is Backend.SQLITE and config.generation == 2 and config.previous.backend is Backend.POSTGRES
    assert setup.plane.current().backend is Backend.SQLITE and setup.gate.state() is None
    assert (setup.root / "identity.db").is_file() and (setup.root / "workspaces" / TEAM_KEY / "memory.db").is_file()
    users = [user["id"] for user in setup.registry.list_users()]
    assert users == ["anna"]
    assert setup.events()[-1] == "database_rolled_back"
    assert not setup.switch.rollback_available()


@pytest.mark.postgres
def test_foreign_target_is_blocked(tmp_path, pg_database):
    from team_memory.pg_provision import PgProvisioner

    PgProvisioner(pg_database.url).bootstrap(str(uuid.uuid4()))
    setup = Setup(tmp_path / "server", pg_database)
    plan = setup.service.plan(setup.dsn, ACTOR)
    assert not plan.ready and any("another TAM installation" in blocker for blocker in plan.blockers)


@pytest.mark.postgres
def test_unknown_or_expired_plans_are_not_found(setup):
    with pytest.raises(NotFound):
        setup.service.start(uuid.uuid4(), ACTOR)
    plan = setup.service.plan(setup.dsn, ACTOR)
    setup.service.clock = lambda: datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(NotFound, match="expired"):
        setup.service.start(plan.plan_id, ACTOR)


def test_interrupted_journal_is_closed_as_failed_on_start(tmp_path):
    journal_dir = tmp_path / "migration"
    journal_dir.mkdir()
    job = MigrationProgress(job_id=uuid.uuid4(), plan_id=uuid.uuid4(), phase=MigrationPhase.COPY_WORKSPACES,
                            started_at=NOW, updated_at=NOW, started_by=ACTOR, target="postgresql://tam@db/tam",
                            completed_workspaces=("shared",))
    (journal_dir / f"{job.job_id}.json").write_text(job.model_dump_json(), encoding="utf-8")
    (journal_dir / "garbage.json").write_text("{not json", encoding="utf-8")
    service = MigrationService(tmp_path, checker=None, targets=None, gate=Maintenance(), switch=None,
                               audit=None, organization=lambda: ORGANIZATION, clock=lambda: NOW)
    progress = service.progress()
    assert progress.job_id == job.job_id and progress.phase is MigrationPhase.FAILED
    assert "resume" in progress.error and progress.completed_workspaces == ("shared",)


def test_actor_is_validated(tmp_path):
    service = MigrationService(tmp_path, checker=None, targets=None, gate=Maintenance(), switch=None,
                               audit=None, organization=lambda: ORGANIZATION)
    with pytest.raises(DomainError, match="Actor"):
        service.cancel(" ")
    assert service.progress() is None


@pytest.mark.postgres
def test_real_workspace_migrates_onto_the_baseline_and_serves_recall(tmp_path, pg_database, monkeypatch):
    """End to end on the real schema: a Runtime-written memory.db, the bundled workspace migrations and
    audit triggers as installer, verification, then a PostgreSQL Runtime answering from the copy."""
    import server
    from team_memory.contracts import Actor, Save, Scope, Update, Work, Workspace
    from team_memory.worker import Runtime

    root = tmp_path / "server"
    data_dir = root / "workspaces" / "shared"
    monkeypatch.setattr(server, "MEMORY_DIR", data_dir)
    for key, value in {"TAM_MEMORY_DIR": str(data_dir), "CLAUDE_MEMORY_DIR": str(data_dir),
                       "MEMORY_QUALITY_GATE_ENABLED": "false", "MEMORY_ASYNC_ENRICHMENT": "false",
                       "USE_BINARY_SEARCH": "true"}.items():
        monkeypatch.setenv(key, value)
    actor = Actor(user_id="anna", display_name="Anna", client="test")
    shared = Workspace(key="shared", scope=Scope(), writable=True)

    def work(runtime, operation, arguments):
        return runtime.execute(Work(actor=actor, workspace=shared, operation=operation, arguments=arguments))

    runtime = Runtime(str(data_dir))
    try:
        saved = [work(runtime, "memory_save", Save(content=content, project="ops").model_dump(
            mode="json", exclude={"scope"})) for content in (
            "Payroll runs on the 25th; the finance lead approves corrections before the 20th.",
            "The staging cluster is rebuilt every Sunday night from the golden image.",
            "Customer escalations go to the support duty manager within 30 minutes.")]
        work(runtime, "memory_update", Update(id=saved[1]["id"], expected_revision=1,
                                              content="The staging cluster is rebuilt every Saturday night.",
                                              reason="schedule moved")
             .model_dump(mode="json", exclude={"scope"}))
    finally:
        runtime.store.db.close()
    setup = Setup(root, pg_database, workspaces=False)
    setup.targets = PgMigrationTargets(setup.targets.master_key, setup.plane.settings)
    setup.service.targets = setup.targets
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.DONE, progress.error
    schema = schema_for("shared")
    knowledge = pg_rows(setup.dsn, f"SELECT count(*) FROM {schema}.knowledge")[0][0]
    assert pg_rows(setup.dsn, f"SELECT count(*) FROM {schema}.knowledge_tsv") == [(knowledge,)]
    assert pg_rows(setup.dsn, f"SELECT doc_count FROM {schema}.fts_stats WHERE source = 'knowledge_tsv'") == [
        (knowledge,)]
    assert pg_rows(setup.dsn, f"SELECT count(*) FROM {schema}.tam_history")[0][0] == 5

    database = setup.targets.open(setup.dsn, setup.plane.current().instance_id).workspaces.store_database("shared")
    migrated = Runtime(str(data_dir), database)
    try:
        answer = work(migrated, "memory_recall", {"query": "When is payroll run?", "limit": 3})
        assert answer and answer[0]["id"] == saved[0]["id"]
        assert answer[0]["created_by"]["user_id"] == "anna"
        fresh = work(migrated, "memory_save", Save(content="New rule written after the migration.").model_dump(
            mode="json", exclude={"scope"}))
        assert fresh["id"] > max(record["id"] for record in saved)
    finally:
        migrated.store.db.close()
    assert not list(data_dir.glob("*.db"))


@pytest.mark.postgres
def test_orphans_are_reported_by_the_dry_run_and_quarantined_by_the_job(tmp_path, pg_database):
    root = tmp_path / "server"
    setup = Setup(root, pg_database)
    with closing(sqlite3.connect(root / "workspaces" / TEAM_KEY / "memory.db")) as db:
        db.executescript("""
            CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, knowledge_id INTEGER REFERENCES knowledge(id));
            INSERT INTO knowledge_nodes VALUES (1, 1), (2, 404);
            INSERT INTO tam_history(record_id, operation) VALUES (1, 'update');
        """)
        db.commit()
    plan = setup.service.plan(setup.dsn, ACTOR)
    assert plan.ready and plan.quarantined_rows == 1
    [item] = plan.quarantine
    assert (item.database, item.table, item.rows, item.sample_pks, item.audit) == (
        TEAM_KEY, "knowledge_nodes", 1, ('{"id": 2}',), False)
    assert item.reasons == ("fk:knowledge_nodes.knowledge_id->knowledge.id",)
    progress = setup.service.run(plan.plan_id, ACTOR)
    assert progress.phase is MigrationPhase.DONE, progress.error
    assert progress.quarantine == plan.quarantine and progress.quarantined_rows == 1
    schema = schema_for(TEAM_KEY)
    assert pg_rows(setup.dsn, f"SELECT id FROM {schema}.knowledge_nodes") == [(1,)]
    assert pg_rows(setup.dsn, f"SELECT source_table, source_pk FROM {schema}.migration_quarantine") == [
        ("knowledge_nodes", '{"id": 2}')]


def runtime_workspace(root, monkeypatch):
    """A real shared-workspace memory.db written by the worker Runtime (one saved record)."""
    import server
    from team_memory.contracts import Actor, Save, Scope, Work, Workspace
    from team_memory.worker import Runtime

    data_dir = root / "workspaces" / "shared"
    monkeypatch.setattr(server, "MEMORY_DIR", data_dir)
    for key, value in {"TAM_MEMORY_DIR": str(data_dir), "CLAUDE_MEMORY_DIR": str(data_dir),
                       "MEMORY_QUALITY_GATE_ENABLED": "false", "MEMORY_ASYNC_ENRICHMENT": "false",
                       "USE_BINARY_SEARCH": "true"}.items():
        monkeypatch.setenv(key, value)
    runtime = Runtime(str(data_dir))
    try:
        runtime.execute(Work(actor=Actor(user_id="anna", display_name="Anna", client="test"),
                             workspace=Workspace(key="shared", scope=Scope(), writable=True), operation="memory_save",
                             arguments=Save(content="Seeded counters survive the move.").model_dump(
                                 mode="json", exclude={"scope"})))
    finally:
        runtime.store.db.close()
    return data_dir / "memory.db"


@pytest.mark.postgres
def test_seeded_counters_take_the_sqlite_values_and_the_ledger_is_not_copied(tmp_path, pg_database, monkeypatch):
    root = tmp_path / "server"
    path = runtime_workspace(root, monkeypatch)
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE privacy_counters SET value = 7 WHERE key = 'private_redactions_total'")
        db.execute("UPDATE vector_index_revision SET revision = 42")
        db.execute("UPDATE representation_index_revision SET revision = 73")
        db.commit()
    setup = Setup(root, pg_database, workspaces=False)
    setup.targets = setup.service.targets = PgMigrationTargets(setup.targets.master_key, setup.plane.settings)
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.DONE, progress.error
    schema = schema_for("shared")
    assert pg_rows(setup.dsn, f"SELECT value FROM {schema}.privacy_counters "
                              "WHERE key = 'private_redactions_total'") == [(7,)]
    assert pg_rows(setup.dsn, f"SELECT revision FROM {schema}.vector_index_revision") == [(42,)]
    assert pg_rows(setup.dsn, f"SELECT revision FROM {schema}.representation_index_revision") == [(73,)]
    with closing(sqlite3.connect(root / setup.store.load().archive / "workspaces" / "shared" / "memory.db")) as db:
        versions = {row[0] for row in db.execute("SELECT version FROM migrations")}
    assert versions <= {row[0] for row in pg_rows(setup.dsn, f"SELECT version FROM {schema}.migrations")}


@pytest.mark.postgres
def test_workspace_from_a_newer_tam_blocks_the_plan(tmp_path, pg_database, monkeypatch):
    root = tmp_path / "server"
    path = runtime_workspace(root, monkeypatch)
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO migrations (version, description, applied_at) VALUES ('999', 'future', 'now')")
        db.commit()
    setup = Setup(root, pg_database, workspaces=False)
    plan = setup.service.plan(setup.dsn, ACTOR)
    assert not plan.ready and any("newer TAM (migrations 999)" in blocker for blocker in plan.blockers)


class FakeLease:
    def __init__(self, log, busy=False):
        self.log, self.busy = log, busy

    def acquire(self):
        if self.busy:
            raise Conflict("Another TAM server is already running against this PostgreSQL database")
        self.log.append("acquired")
        return self

    def release(self):
        self.log.append("released")


def test_activation_takes_the_server_lease_first_and_rollback_releases_it(tmp_path):
    from team_memory.migration_service import ServerLeaseHolder

    make_sqlite_root(tmp_path)
    dsn = DatabaseDsn.parse("postgresql://tam:secret@db.internal:5432/tam?sslmode=require")
    log = []
    busy = ServerLeaseHolder(lambda url, settings, **kwargs: FakeLease(log, busy=True))
    store, plane, pool = MemoryConfigStore(), Plane(), Pool()
    switch = DatabaseSwitch(store, plane, pool, SqliteArchiver(tmp_path), clock=lambda: NOW, lease=busy)
    with pytest.raises(Conflict, match="Another TAM server"):
        switch.activate_postgres(dsn, ACTOR)
    assert plane.history == [] and store.config is None and (tmp_path / "identity.db").exists()

    holder = ServerLeaseHolder(lambda url, settings, **kwargs: FakeLease(log))
    switch = DatabaseSwitch(store, plane, pool, SqliteArchiver(tmp_path), clock=lambda: NOW, lease=holder)
    switch.activate_postgres(dsn, ACTOR)
    assert holder.held and log == ["acquired"]
    switch.rollback_to_sqlite(ACTOR)
    assert not holder.held and log == ["acquired", "released"]


def test_failed_config_save_releases_the_lease(tmp_path):
    from team_memory.migration_service import ServerLeaseHolder

    make_sqlite_root(tmp_path)
    log = []
    holder = ServerLeaseHolder(lambda url, settings, **kwargs: FakeLease(log))
    switch = DatabaseSwitch(MemoryConfigStore(fail_save=True), Plane(), Pool(), SqliteArchiver(tmp_path),
                            clock=lambda: NOW, lease=holder)
    with pytest.raises(OSError):
        switch.activate_postgres(DatabaseDsn.parse("postgresql://tam:secret@db.internal/tam"), ACTOR)
    assert log == ["acquired", "released"] and not holder.held


# Review fixes: stale plans, archive failure, writes after the copy


def test_start_refuses_when_the_config_already_points_to_postgres(tmp_path):
    store, plane = MemoryConfigStore(), Plane()
    store.config = pg_config("archive/sqlite-x")
    switch = DatabaseSwitch(store, plane, Pool(), SqliteArchiver(tmp_path), clock=lambda: NOW)
    service = MigrationService(tmp_path, checker=None, targets=None, gate=Maintenance(), switch=switch,
                               audit=None, organization=lambda: ORGANIZATION, clock=lambda: NOW)
    with pytest.raises(Conflict, match="already runs on PostgreSQL"):
        service.start(uuid.uuid4(), ACTOR)


def test_archive_failure_reverts_the_activation_and_keeps_every_sqlite_file(tmp_path, monkeypatch):
    from team_memory import migration_service
    from team_memory.migration_service import MigrationError, ServerLeaseHolder

    make_sqlite_root(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.db*"))
    real_replace, calls = migration_service.os.replace, []

    def failing_replace(source, target):
        calls.append(source)
        if len(calls) == 3:
            raise OSError("disk full")
        real_replace(source, target)

    monkeypatch.setattr(migration_service.os, "replace", failing_replace)
    log = []
    lease = ServerLeaseHolder(lambda url, settings, **kwargs: FakeLease(log))
    store, plane, pool = MemoryConfigStore(), Plane(), Pool()
    switch = DatabaseSwitch(store, plane, pool, SqliteArchiver(tmp_path), clock=lambda: NOW, lease=lease)
    with pytest.raises(MigrationError, match="stays on SQLite"):
        switch.activate_postgres(DatabaseDsn.parse("postgresql://tam:secret@db.internal/tam"), ACTOR)
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.db*")) == before
    assert plane.current().backend is Backend.SQLITE and store.config.backend is Backend.SQLITE
    assert store.config.generation == 2 and store.config.previous.backend is Backend.POSTGRES
    assert log == ["acquired", "released"] and not lease.held
    SqliteArchiver(tmp_path).recover(store.config)
    assert (tmp_path / "identity.db").is_file()


@pytest.mark.postgres
def test_a_second_plan_cannot_run_after_the_first_activated(setup):
    first = setup.service.plan(setup.dsn, ACTOR)
    second = setup.service.plan(setup.dsn, ACTOR)
    assert setup.service.run(first.plan_id, ACTOR).phase is MigrationPhase.DONE
    with pytest.raises(Conflict, match="already runs on PostgreSQL"):
        setup.service.start(second.plan_id, ACTOR)
    assert setup.service._plans == {}


@pytest.mark.postgres
def test_a_control_plane_write_after_its_copy_fails_verification(tmp_path, pg_database):
    holder = {}

    def write_identity(connection, schema):
        with closing(sqlite3.connect(holder["root"] / "identity.db")) as db:
            db.execute("INSERT OR IGNORE INTO users (id, name) VALUES ('late', 'Late signup')")
            db.commit()

    root = tmp_path / "server"
    holder["root"] = root
    setup = Setup(root, pg_database, hooks=(write_identity,))
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.FAILED
    assert "identity changed after it was copied" in progress.error
    assert setup.store.load() is None and (root / "identity.db").is_file()


@pytest.mark.postgres
def test_a_workspace_write_after_its_copy_is_recopied(tmp_path, pg_database):
    def write_shared(connection, schema):
        if schema == schema_for(TEAM_KEY):
            write_workspace(tmp_path / "server", "shared", ["written after the shared copy"])

    setup = Setup(tmp_path / "server", pg_database, hooks=(write_shared,))
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.DONE, progress.error
    assert pg_rows(setup.dsn, f"SELECT count(*) FROM {schema_for('shared')}.knowledge") == [(3,)]


@pytest.mark.postgres
def test_text_that_postgres_cannot_hold_blocks_the_dry_run(setup):
    with closing(sqlite3.connect(setup.root / "workspaces" / TEAM_KEY / "memory.db")) as db:
        db.execute("INSERT INTO knowledge (id, content) VALUES (77, 'bad' || char(0) || 'text')")
        db.commit()
    plan = setup.service.plan(setup.dsn, ACTOR)
    assert not plan.ready
    assert any(f'{TEAM_KEY}: knowledge.content row {{"id": 77}}: text contains NUL' in blocker
               for blocker in plan.blockers)


# Lease loss and handover


def test_stop_serving_is_final_until_restart():
    gate = Maintenance(clock=lambda: NOW)
    gate.enter(MaintenanceReason.MIGRATION, None)
    state = gate.stop_serving()
    assert state.reason is MaintenanceReason.LEASE_LOST and gate.state() == state
    gate.leave()
    assert gate.state() == state
    with pytest.raises(Conflict, match="lease_lost"):
        gate.enter(MaintenanceReason.ROLLBACK, None)
    assert gate.stop_serving() == state


class RecordingLease(FakeLease):
    def handover(self, url, connect_overrides=None):
        self.log.append(("handover", url))


def test_holder_passes_on_lost_and_hands_over_on_repoint():
    from team_memory.migration_service import ServerLeaseHolder

    log, seen = [], {}

    def factory(url, settings, *, on_lost=None, connect_overrides=None):
        seen["on_lost"], seen["url"] = on_lost, url
        return RecordingLease(log)

    def lost():
        log.append("lost")

    holder = ServerLeaseHolder(factory, on_lost=lost)
    first = ActiveDatabase(backend=Backend.POSTGRES, instance_id=str(uuid.UUID(int=1)), generation=1,
                           url="postgresql://tam@db1/tam")
    holder.handover(first)
    assert seen == {"on_lost": lost, "url": first.url} and log == ["acquired"]
    second = ActiveDatabase(backend=Backend.POSTGRES, instance_id=first.instance_id, generation=2,
                            url="postgresql://tam@db2/tam")
    holder.handover(second)
    assert log[-1] == ("handover", second.url) and holder.held
    holder.release()
    assert log[-1] == "released" and not holder.held


@pytest.mark.postgres
def test_a_lost_server_lease_stops_serving(pg_database, monkeypatch):
    import time

    from team_memory import cli
    from team_memory.cli import stop_serving
    from team_memory.migration_service import ServerLeaseHolder

    monkeypatch.setenv("TAM_TEAM_PG_LEASE_CHECK_SECONDS", "0.2")
    shutdowns = []
    monkeypatch.setattr(cli, "request_shutdown", lambda: shutdowns.append("sigterm"))
    gate, pool = Maintenance(), Pool()
    holder = ServerLeaseHolder(on_lost=lambda: stop_serving(gate, [pool]))
    target = ActiveDatabase(backend=Backend.POSTGRES, instance_id=str(uuid.uuid4()), generation=1,
                            url=pg_database.url)
    holder.acquire(target)
    try:
        with psycopg.connect(pg_database.url, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE application_name = 'tam-lease' AND datname = current_database()")
        deadline = time.monotonic() + 10
        while gate.state() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gate.state() is not None and gate.state().reason is MaintenanceReason.LEASE_LOST
        assert pool.recycled == 1 and shutdowns == ["sigterm"]
    finally:
        holder.release()


@pytest.mark.postgres
def test_repoint_hands_the_server_lease_to_the_new_dsn(pg_database):
    from team_memory.migration_service import ServerLeaseHolder
    from team_memory.pg_provision import server_lease

    instance = str(uuid.uuid4())
    first = ActiveDatabase(backend=Backend.POSTGRES, instance_id=instance, generation=1, url=pg_database.url)
    repointed = DatabaseDsn.parse(pg_database.url).model_copy(update={"connect_timeout": 7}).to_uri()
    second = ActiveDatabase(backend=Backend.POSTGRES, instance_id=instance, generation=2, url=repointed)
    holder = ServerLeaseHolder()
    holder.acquire(first)
    try:
        holder.handover(second)
        assert holder._lease.url == repointed
        with pytest.raises(Conflict, match="Another TAM server"):
            server_lease(pg_database.url).acquire()
    finally:
        holder.release()
    server_lease(pg_database.url).acquire().release()


def test_web_plans_refuse_dsns_that_only_the_operator_may_use(tmp_path):
    from team_memory.database_contracts import DsnOrigin, InvalidDsn

    service = MigrationService(tmp_path, checker=None, targets=None, gate=Maintenance(), switch=None,
                               audit=None, organization=lambda: ORGANIZATION)
    passwordless = DatabaseDsn.parse("postgresql://tam@db.internal/tam", DsnOrigin.ENV)
    with pytest.raises(InvalidDsn, match="password"):
        service.plan(passwordless, ACTOR)


@pytest.mark.postgres
def test_web_migrations_connect_with_the_web_overrides(setup):
    opened = []
    real_open = setup.targets.open

    def spy(dsn, instance_id, overrides):
        opened.append(dict(overrides))
        return real_open(dsn, instance_id, overrides)

    setup.targets.open = spy
    assert setup.migrate().phase is MigrationPhase.DONE
    [overrides] = opened
    assert overrides["sslcertmode"] == "disable" and overrides["gssencmode"] == "disable"
    assert "scram-sha-256" in overrides["require_auth"]
    passfile = Path(overrides["passfile"])
    assert passfile.stat().st_size == 0 and passfile.stat().st_mode & 0o077 == 0
    assert passfile == setup.root / ".empty-pgpass"
    assert setup.store.load().origin.value == "web"
    assert setup.plane.current().connect_kwargs() == overrides
    assert setup.switch.lease.held
