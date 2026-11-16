import argparse
import contextlib
import functools
import importlib.util
import json
import logging
import os
import signal
import sys
import uuid
from pathlib import Path

from team_memory.accounts import AccountPolicy, Accounts, Invite
from team_memory.contracts import ORG_ROLES, Conflict, DomainError
from team_memory.database_contracts import (
    MIGRATION_DIR,
    DsnOrigin,
    MaintenanceReason,
    MigrationPhase,
    MigrationProgress,
)
from team_memory.registry import Registry
from team_memory.replication import URL_ENV as REPLICA_URL_ENV
from team_memory.replication_cli import add_commands as add_replication_commands

DSN_ENV_HELP = "name of the environment variable holding the postgresql:// DSN (never the DSN itself)"
CLI_ACTOR = "cli"
# Non-zero so systemd/compose restart the server after another one took the PostgreSQL lease.
EXIT_LEASE_LOST = 75
MUTATING_COMMANDS = frozenset({"user-add", "team-add", "member", "bootstrap-admin", "user-role", "invite",
                               "user-disable", "user-enable", "user-purge", "setup-token", "token-create",
                               "token-revoke", "login-unlock"})
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3737
# Bounds the whole uvicorn shutdown, including asyncio Server.wait_closed(), which never returns on
# Windows when a client resets its connection (proactor skips detaching the transport).
GRACEFUL_SHUTDOWN_SEC = 10
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
LOGGER = logging.getLogger(__name__)


def local_url(host: str, port: int) -> str:
    shown = "127.0.0.1" if host in WILDCARD_HOSTS else host
    return f"http://[{shown}]:{port}" if ":" in shown else f"http://{shown}:{port}"


def bootstrap_admin(registry: Registry, user_id: str, name: str) -> Invite:
    if any(user["org_role"] == "superadmin" and user["active"] for user in registry.list_users()):
        raise Conflict("A superadmin already exists; use `user-role` and `invite` instead")
    registry.add_user(user_id, name)
    registry.set_org_role(user_id, "superadmin")
    return Accounts(registry, AccountPolicy.from_env()).issue_invite(user_id)


def print_invite(invite: Invite) -> None:
    sys.stdout.write(f"Invite code for {invite.user_id}: {invite.code}\n"
                     f"Valid until {invite.expires_at}; single use. Open /dashboard/ and choose 'Use invite code'.\n")


def dsn_from_env(parser: argparse.ArgumentParser, variable: str):
    from team_memory.database_contracts import DatabaseDsn, DsnOrigin, InvalidDsn

    raw = os.environ.get(variable, "").strip()
    if not raw:
        parser.exit(2, f"Environment variable {variable} is empty or unset\n")
    try:
        return DatabaseDsn.parse(raw, origin=DsnOrigin.ENV)
    except InvalidDsn as exc:
        parser.exit(2, f"{variable}: {exc}\n")


def migration_running(root: Path) -> bool:
    """A migration journal that is not finished: the copy is running, or the server stopped during it."""
    directory = root.resolve() / MIGRATION_DIR
    if not directory.is_dir():
        return False
    for path in directory.glob("*.json"):
        try:
            progress = MigrationProgress.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not progress.terminal:
            return True
    return False


def refuse_during_migration(parser: argparse.ArgumentParser, root: Path) -> None:
    """Writes to identity/learning after they were copied would be lost, so the CLI waits for the migration."""
    if migration_running(root):
        parser.exit(1, "A database migration is running (or was interrupted; start the server to close it). "
                       "Retry when it has finished.\n")


def postgres_available() -> bool:
    return importlib.util.find_spec("psycopg") is not None


def require_postgres(parser: argparse.ArgumentParser) -> None:
    if not postgres_available():
        parser.exit(2, "PostgreSQL support is not installed: pip install 'total-agent-memory[postgres]'\n")


def recover_archive(root: Path) -> None:
    """Finish a database switch that stopped between writing database.json and moving the SQLite files."""
    if not postgres_available():
        return
    from team_memory.database_config import FileDatabaseConfigStore
    from team_memory.migration_service import SqliteArchiver

    SqliteArchiver(root).recover(FileDatabaseConfigStore(root).load())


def print_report(report) -> None:
    for check in report.checks:
        sys.stdout.write(f"{check.status.value:8} {check.id.value:15} {check.message}\n")
        for statement in check.dba_sql:
            sys.stdout.write(f"{'':25}{statement}\n")
    for warning in report.warnings:
        sys.stdout.write(f"warning: {warning}\n")
    sys.stdout.write(f"target: {report.target_state.value}\n")


def db_check(parser: argparse.ArgumentParser, args) -> None:
    require_postgres(parser)
    from team_memory.database import sqlite_instance_id
    from team_memory.db_check import PgDatabaseChecker

    dsn = dsn_from_env(parser, args.dsn_env)
    local = sqlite_instance_id(args.root.resolve(), create=False)
    report = PgDatabaseChecker().check(dsn, instance_id=uuid.UUID(local) if local else None)
    print_report(report)
    if not report.ok:
        parser.exit(1)


class NoWorkers:
    """The pool of a stopped server: nothing to recycle."""

    def recycle(self) -> int:
        return 0


def migration_runner(root: Path, registry: Registry, pool, gate, lease=None, *, isolated_checks: bool = False):
    """MigrationService wired to the server's control plane (also used by serve)."""
    from team_memory.database_config import (
        FileDatabaseConfigStore,
        record_database_event,
    )
    from team_memory.db_check import PgDatabaseChecker
    from team_memory.migration_service import (
        DatabaseSwitch,
        MigrationService,
        PgMigrationTargets,
        SqliteArchiver,
    )
    from team_memory.settings import load_master_key

    plane = registry.plane
    switch = DatabaseSwitch(FileDatabaseConfigStore(root), plane, pool, SqliteArchiver(root), lease=lease)
    # A DSN typed into the dashboard is checked in a child without this process's PG* environment and
    # passfile, so it can only pass (and be migrated to) with credentials of its own.
    return MigrationService(root, PgDatabaseChecker(isolated=isolated_checks), PgMigrationTargets(load_master_key(root), plane.settings),
                            gate, switch, functools.partial(record_database_event, plane),
                            lambda: registry.organization().get("name", ""))


def print_plan(plan) -> None:
    print_report(plan.report)
    for database in plan.databases:
        sys.stdout.write(f"{database.kind.value:9} {database.name}: {database.rows} rows, {database.bytes} bytes\n")
    for item in plan.quarantine:
        kind = "audit rows" if item.audit else "rows"
        sys.stdout.write(f"quarantine {item.database} {item.table}: {item.rows} orphan {kind} "
                         f"({'; '.join(item.reasons)}) e.g. {', '.join(item.sample_pks)}\n")
    sys.stdout.write(f"estimated time: {plan.estimated_seconds} s\n")
    for blocker in plan.blockers:
        sys.stdout.write(f"blocker: {blocker}\n")


def db_migrate(parser: argparse.ArgumentParser, args) -> None:
    require_postgres(parser)
    from team_memory.lifecycle import ServerLease
    from team_memory.migration_service import Maintenance, ServerLeaseHolder

    dsn = dsn_from_env(parser, args.dsn_env)
    root = args.root.resolve()
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(ServerLease(root))
            recover_archive(root)
            registry = Registry(root)
            lease = stack.enter_context(ServerLeaseHolder())
            runner = migration_runner(root, registry, NoWorkers(), Maintenance(), lease)
            plan = runner.plan(dsn, CLI_ACTOR, DsnOrigin.ENV)
            print_plan(plan)
            if not plan.ready:
                parser.exit(1, "The migration is blocked; fix the blockers above and run it again\n")
            if args.dry_run:
                return
            progress = runner.run(plan.plan_id, CLI_ACTOR)
    except DomainError as exc:
        parser.exit(1, f"{exc}\n")
    if progress.phase is not MigrationPhase.DONE:
        parser.exit(1, f"Migration {progress.phase.value}: {progress.error or 'cancelled'}\n")
    sys.stdout.write(f"Migrated {progress.rows_copied} rows ({progress.quarantined_rows} quarantined); "
                     "the server now uses PostgreSQL. SQLite files were archived under archive/.\n")


def request_shutdown() -> None:
    """Ask the running server to stop (uvicorn handles SIGTERM gracefully); main() then exits non-zero."""
    # raise_signal runs uvicorn's handler everywhere; on Windows os.kill(own pid) terminates at once.
    signal.raise_signal(signal.SIGTERM)


def stop_serving(gate, pools) -> None:
    """The PostgreSQL server lease was lost: another server owns the installation now. Final
    maintenance (503, no new workers), every running worker stopped, and the process shuts down with
    EXIT_LEASE_LOST so its supervisor restarts it and it takes the lease again (or refuses to start)."""
    gate.stop_serving()
    for pool in pools:
        pool.recycle()
    request_shutdown()


def run_server(registry: Registry, args) -> None:
    import uvicorn

    from team_memory.app import create_app
    from team_memory.dashboard_service import DashboardService
    from team_memory.gateway_llm import GatewayLLM
    from team_memory.metrics import Metrics
    from team_memory.service import MemoryService
    from team_memory.settings import SettingsStore, load_cipher
    from team_memory.worker import (
        DEFAULT_OPERATION_TIMEOUT,
        DEFAULT_QUEUE_TIMEOUT,
        DEFAULT_WORKERS,
        WorkerPool,
    )

    logging.basicConfig(level=logging.INFO, format='%(message)s')
    root = registry.root
    settings = SettingsStore(registry, load_cipher(root))
    plane = registry.plane
    gate = runner = database = lease = None
    if postgres_available():
        from team_memory.database_config import DatabaseSettingsService
        from team_memory.migration_service import Maintenance, ServerLeaseHolder

        gate = Maintenance()
        # Session touches and other writes-on-read skip themselves while the gate is closed.
        plane.watch_maintenance(lambda: gate.state() is not None)
        # One holder for the process: taken now on PostgreSQL, or later when a migration activates it.
        lease = ServerLeaseHolder(on_lost=lambda: stop_serving(gate, pools))
        lease.acquire(plane.current())
    pools = []
    pool = WorkerPool(root, int(os.environ.get("TAM_TEAM_MAX_WORKERS", DEFAULT_WORKERS)),
                      float(os.environ.get("TAM_TEAM_OPERATION_TIMEOUT", DEFAULT_OPERATION_TIMEOUT)),
                      environment=settings.overrides, registry=registry, maintenance=gate,
                      queue_timeout=float(os.environ.get("TAM_TEAM_QUEUE_TIMEOUT", DEFAULT_QUEUE_TIMEOUT)))
    pools.append(pool)
    if gate is not None:
        runner = migration_runner(root, registry, pool, gate, lease, isolated_checks=True)
        database = DatabaseSettingsService(root, plane, on_activated=pool.recycle, migration=runner,
                                           lease_handover=lease.handover)
    else:
        LOGGER.info(json.dumps({"event": "postgres_support_missing",
                                "detail": "database settings and migration are unavailable"}))
    service = MemoryService(registry, pool, llm=GatewayLLM(settings))
    dashboard = DashboardService(registry, Accounts(registry, AccountPolicy.from_env()), service, settings, Metrics(),
                                 database=database, migration=runner)
    app = create_app(service, dashboard, base_url=local_url(args.host, args.port), database=database,
                     migration=runner, maintenance=gate)
    with contextlib.ExitStack() as stack:
        if lease is not None:
            stack.enter_context(lease)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False,
                    timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC)
    state = gate.state() if gate is not None else None
    if state is not None and state.reason is MaintenanceReason.LEASE_LOST:
        LOGGER.error(json.dumps({"event": "server_stopped_lease_lost"}))
        raise SystemExit(EXIT_LEASE_LOST)


def main():
    parser = argparse.ArgumentParser(prog="tam-team")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("TAM_TEAM_DIR", "~/.tam-server")).expanduser())
    commands = parser.add_subparsers(dest="command", required=True)
    from team_memory.remote import add_arguments as add_remote_arguments
    add_remote_arguments(commands.add_parser("remote", help="Connect stdio MCP to a remote server with capability checks"))
    for command in ("user-add", "team-add"):
        sub = commands.add_parser(command)
        sub.add_argument("id")
        sub.add_argument("name")
    member = commands.add_parser("member")
    member.add_argument("user")
    member.add_argument("team")
    member.add_argument("role", choices=("reader", "editor", "manager", "remove"))
    bootstrap = commands.add_parser("bootstrap-admin", help="Create the first superadmin and print an invite code")
    bootstrap.add_argument("id")
    bootstrap.add_argument("name")
    role = commands.add_parser("user-role", help="Set a user's organisation role")
    role.add_argument("id")
    role.add_argument("role", choices=ORG_ROLES)
    invite = commands.add_parser("invite", help="Issue a one-time dashboard invite / password reset code")
    invite.add_argument("id")
    unlock = commands.add_parser("login-unlock", help="Clear a user's sign-in lockout (all addresses and devices); "
                                                      "--ip also clears that address")
    unlock.add_argument("id")
    unlock.add_argument("--ip", help="also lift the lock on this client address")
    for command, text in (("user-disable", "Offboard a user: revoke all tokens, end sessions, block sign-in"),
                          ("user-enable", "Lift the block; the user then needs a new invite or token")):
        sub = commands.add_parser(command, help=text)
        sub.add_argument("id")
    export = commands.add_parser("user-export", help="Write a disabled user's personal records and history as JSONL")
    export.add_argument("id")
    export.add_argument("--out", type=Path, required=True)
    purge = commands.add_parser("user-purge", help="Delete a disabled user's personal area (server stopped)")
    purge.add_argument("id")
    purge.add_argument("--confirm", required=True, help="Repeat the user ID")
    commands.add_parser("setup-token", help="Issue a new one-time code for the web setup wizard")
    token = commands.add_parser("token-create")
    token.add_argument("user")
    token.add_argument("--client", required=True)
    token.add_argument("--out", type=Path, required=True)
    revoke = commands.add_parser("token-revoke")
    revoke.add_argument("--file", type=Path, required=True)
    snapshot = commands.add_parser("backup", help="SQLite: verified snapshot (server stopped); "
                                                  "PostgreSQL: pg_dump of the TAM schemas (online)")
    snapshot.add_argument("--out", type=Path, required=True)
    recovery = commands.add_parser("restore", help="SQLite snapshot into a new --root; PostgreSQL backup into "
                                                   "the empty database named by --dsn-env")
    recovery.add_argument("--from", dest="snapshot", type=Path, required=True)
    recovery.add_argument("--dsn-env", metavar="VAR", help=DSN_ENV_HELP)
    check = commands.add_parser("db-check", help="Check a PostgreSQL database against TAM's requirements")
    check.add_argument("--dsn-env", metavar="VAR", required=True, help=DSN_ENV_HELP)
    migrate = commands.add_parser("db-migrate", help="Move this server's SQLite data to PostgreSQL (server stopped)")
    migrate.add_argument("--dsn-env", metavar="VAR", required=True, help=DSN_ENV_HELP)
    migrate.add_argument("--dry-run", action="store_true", help="Check and estimate only; write nothing")
    add_replication_commands(commands)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", DEFAULT_HOST))
    serve.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    if args.command == "remote":
        from team_memory.remote import run
        run(args, parser)
        return
    if args.command == "replication":
        from team_memory.replication_cli import run
        run(args, parser)
        return
    if args.command in ("backup", "restore"):
        from team_memory.lifecycle import backup, restore
        try:
            if args.command == "backup":
                backup(args.root, args.out)
            else:
                dsn = dsn_from_env(parser, args.dsn_env) if args.dsn_env else None
                restore(args.snapshot, args.root, dsn)
        except (DomainError, FileExistsError) as exc:
            parser.exit(1, f"{exc}\n")
        return
    if args.command == "db-check":
        db_check(parser, args)
        return
    if args.command == "db-migrate":
        db_migrate(parser, args)
        return
    if args.command == "serve":
        recover_archive(args.root)
    elif args.command in MUTATING_COMMANDS:
        refuse_during_migration(parser, args.root)
    registry = Registry(args.root)
    if args.command == "user-add":
        registry.add_user(args.id, args.name)
    elif args.command == "team-add":
        registry.add_team(args.id, args.name)
    elif args.command == "member":
        registry.membership(args.user, args.team, None if args.role == "remove" else args.role)
    elif args.command == "bootstrap-admin":
        print_invite(bootstrap_admin(registry, args.id, args.name))
    elif args.command == "user-role":
        registry.set_org_role(args.id, args.role)
    elif args.command in ("user-disable", "user-enable"):
        try:
            effect = registry.set_active(args.id, args.command == "user-enable")
        except (Conflict, DomainError) as exc:
            parser.exit(1, f"{exc}\n")
        if args.command == "user-disable":
            sys.stdout.write(f"Disabled {args.id}: {effect['tokens_revoked']} tokens revoked, "
                             f"{effect['sessions_ended']} sessions ended, {effect['invites_voided']} invites voided.\n")
        else:
            sys.stdout.write(f"Enabled {args.id}. Issue a new invite or token; earlier ones stay revoked.\n")
    elif args.command in ("user-export", "user-purge"):
        from team_memory.offboarding import export_personal, purge_personal
        try:
            if args.command == "user-export":
                done = export_personal(registry, args.id, args.out)
                sys.stdout.write(f"Exported {done['records']} records, {done['history']} history events and "
                                 f"{done['onboarding_notes']} onboarding notes to {args.out}\n")
            else:
                done = purge_personal(registry, args.id, args.confirm)
                sys.stdout.write(f"Deleted the personal area of {args.id} ({done['records']} records, "
                                 f"{done['onboarding_notes']} onboarding notes). Backups taken earlier still contain it.\n")
                if os.environ.get(REPLICA_URL_ENV):
                    sys.stdout.write("The Litestream replica keeps it until you run: tam-team replication drop "
                                     f"--user {args.id} --confirm {args.id}\n")
        except (DomainError, FileExistsError) as exc:
            parser.exit(1, f"{exc}\n")
    elif args.command == "login-unlock":
        removed = Accounts(registry, AccountPolicy.from_env()).unlock(args.id, args.ip)
        sys.stdout.write(f"Cleared {removed} sign-in counters for {args.id}.\n")
    elif args.command == "invite":
        print_invite(Accounts(registry, AccountPolicy.from_env()).issue_invite(args.id))
    elif args.command == "setup-token":
        from team_memory.metrics import Metrics
        from team_memory.setup import SetupPolicy, SetupService
        try:
            issued = SetupService(registry, Accounts(registry, AccountPolicy.from_env()), Metrics(),
                                  SetupPolicy.from_env()).issue_token()
        except Conflict as exc:
            parser.exit(1, f"{exc}\n")
        sys.stdout.write(f"Setup code: {issued.token}\nValid until {issued.expires_at}; single use. "
                         "Open /dashboard/ on the server and enter it.\n")
    elif args.command == "token-create":
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            destination.write(registry.issue_token(args.user, args.client) + "\n")
    elif args.command == "token-revoke":
        registry.revoke(args.file.read_text(encoding="utf-8").strip())
    elif args.command == "serve":
        run_server(registry, args)
    else:
        parser.error("Unknown command")


if __name__ == "__main__":
    main()
