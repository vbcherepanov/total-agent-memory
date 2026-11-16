"""The Test button checks (plan 4.7): statuses, DBA SQL, error categories and password secrecy."""
import json
import logging
import secrets
import socket
import threading
import uuid
from contextlib import contextmanager
from urllib.parse import quote

import pytest

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra: pip install '.[postgres]'")
from psycopg import sql
from pydantic import SecretStr

from team_memory.database_contracts import (
    CHECK_ORDER,
    CheckId,
    CheckStatus,
    DatabaseDsn,
    DsnOrigin,
    ErrorCategory,
    InvalidDsn,
    TargetState,
)
from team_memory.db_check import (
    PgDatabaseChecker,
    categorize,
    redact,
    sanitized_environment,
    transaction_pooled,
)
from team_memory.pg_provision import PgProvisioner
from tests.team_db_helpers import pg_admin

SHORT_TIMEOUT_SECONDS = 2
DISTINCTIVE_PASSWORD = "Pw-" + secrets.token_hex(8) + "/?&%"


def dsn_of(url: str, **update) -> DatabaseDsn:
    return DatabaseDsn.parse(url).model_copy(update=update)


def statuses(report) -> dict[CheckId, CheckStatus]:
    return {check.id: check.status for check in report.checks}


def test_redact_removes_every_form_of_the_password():
    dsn = DatabaseDsn.parse("postgresql://tam:" + "p%40ss%2Fw0rd" + "@db.internal:5432/tam?sslmode=require")
    text = "failed for p@ss/w0rd, p%40ss%2Fw0rd and p%40ss/w0rd"
    cleaned = redact(text, dsn)
    assert "p@ss/w0rd" not in cleaned and "p%40ss%2Fw0rd" not in cleaned and "p%40ss/w0rd" not in cleaned
    assert redact("nothing secret", DatabaseDsn.parse("postgresql://tam@db/tam", DsnOrigin.ENV)) == "nothing secret"


@pytest.mark.parametrize("message, sqlstate, expected", [
    ("connection timeout expired", None, ErrorCategory.TIMEOUT),
    ("server does not support SSL, but SSL was required", None, ErrorCategory.SSL_REQUIRED),
    ("could not translate host name \"nowhere\"", None, ErrorCategory.UNREACHABLE),
    ("password authentication failed for user \"tam\"", "28P01", ErrorCategory.AUTH_FAILED),
    ("no pg_hba.conf entry for host, no encryption", "28000", ErrorCategory.SSL_REQUIRED),
    ("database \"tam\" does not exist", "3D000", ErrorCategory.NO_DATABASE),
    ("permission denied", "42501", ErrorCategory.PERMISSION),
    ("canceling statement due to statement timeout", "57014", ErrorCategory.TIMEOUT),
])
def test_driver_errors_map_to_fixed_categories(message, sqlstate, expected):
    error = psycopg.OperationalError(message)
    if sqlstate is not None:
        error = type("Coded", (psycopg.OperationalError,), {"sqlstate": sqlstate})(message)
    assert categorize(error) is expected


def test_unreachable_server_skips_the_remaining_checks():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    secret = quote(DISTINCTIVE_PASSWORD, safe="")
    report = PgDatabaseChecker().check(DatabaseDsn.parse(f"postgresql://tam:{secret}@127.0.0.1:{port}/tam"),
                                       instance_id=None)
    assert report.check(CheckId.CONNECT).category is ErrorCategory.UNREACHABLE
    assert [check.status for check in report.checks[1:]] == [CheckStatus.SKIPPED] * (len(CHECK_ORDER) - 1)
    assert not report.ok and report.target_state is TargetState.UNKNOWN
    assert DISTINCTIVE_PASSWORD not in report.model_dump_json()


def test_silent_server_times_out():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True)
    thread.start()
    try:
        port = listener.getsockname()[1]
        report = PgDatabaseChecker(timeout_seconds=SHORT_TIMEOUT_SECONDS).check(
            DatabaseDsn.parse(f"postgresql://tam@127.0.0.1:{port}/tam?sslmode=disable", DsnOrigin.ENV), instance_id=None)
    finally:
        for connection, _address in accepted:
            connection.close()
        listener.close()
    assert report.check(CheckId.CONNECT).category is ErrorCategory.TIMEOUT
    assert report.duration_ms < (SHORT_TIMEOUT_SECONDS + 3) * 1000


@pytest.mark.postgres
def test_prepared_database_passes_every_check(pg_database):
    report = PgDatabaseChecker().check(DatabaseDsn.parse(pg_database.url), instance_id=None)
    assert report.ok, report.model_dump_json(indent=1)
    assert set(statuses(report).values()) == {CheckStatus.PASSED}
    assert report.target_state is TargetState.EMPTY and report.target_instance_id is None
    assert report.server_version


@pytest.mark.postgres
def test_minimal_admin_role_passes(pg_database):
    with pg_admin(pg_database, superuser=False) as url:
        report = PgDatabaseChecker().check(DatabaseDsn.parse(url), instance_id=None)
    assert report.ok, report.model_dump_json(indent=1)


@pytest.mark.postgres
def test_role_without_createrole_or_create_gets_dba_sql(pg_database):
    name = "tam_plain_" + secrets.token_hex(4)
    with psycopg.connect(pg_database.url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal("plainpw")))
    try:
        report = PgDatabaseChecker().check(dsn_of(pg_database.url, user=name, password=SecretStr("plainpw")),
                                           instance_id=None)
    finally:
        with psycopg.connect(pg_database.server.admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
    privileges = report.check(CheckId.PRIVILEGES)
    assert privileges.status is CheckStatus.FAILED
    assert f'ALTER ROLE "{name}" CREATEROLE;' in privileges.dba_sql
    assert f'GRANT CREATE ON DATABASE "{pg_database.name}" TO "{name}";' in privileges.dba_sql
    assert report.check(CheckId.EXTENSIONS).status is CheckStatus.FAILED
    assert not report.ok


@pytest.mark.postgres
def test_wrong_encoding_and_collation_get_create_database_sql(pg_server):
    name = "tam_test_badcoll_" + secrets.token_hex(4)
    with psycopg.connect(pg_server.admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER icu "
                              "ICU_LOCALE 'und' LOCALE 'C.UTF-8'").format(sql.Identifier(name)))
    try:
        report = PgDatabaseChecker().check(DatabaseDsn.parse(pg_server.url_for(name)), instance_id=None)
    finally:
        with psycopg.connect(pg_server.admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    encoding = report.check(CheckId.ENCODING)
    assert encoding.status is CheckStatus.FAILED
    assert any("LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8'" in line and name in line for line in encoding.dba_sql)


@pytest.mark.postgres
def test_missing_database_is_categorized(pg_server):
    report = PgDatabaseChecker().check(DatabaseDsn.parse(pg_server.url_for("tam_test_absent_" + secrets.token_hex(4))),
                                       instance_id=None)
    connect = report.check(CheckId.CONNECT)
    assert connect.category is ErrorCategory.NO_DATABASE
    assert connect.dba_sql and connect.dba_sql[0].startswith("CREATE DATABASE")


@pytest.mark.postgres
def test_wrong_password_never_leaks(pg_database, caplog):
    caplog.set_level(logging.DEBUG)
    report = PgDatabaseChecker().check(dsn_of(pg_database.url, password=SecretStr(DISTINCTIVE_PASSWORD)),
                                       instance_id=None)
    assert report.check(CheckId.CONNECT).category is ErrorCategory.AUTH_FAILED
    assert DISTINCTIVE_PASSWORD not in report.model_dump_json()
    assert DISTINCTIVE_PASSWORD not in caplog.text
    assert "••••" in report.dsn_masked


@pytest.mark.postgres
def test_target_state_same_foreign_and_unknown(pg_database):
    instance = uuid.uuid4()
    PgProvisioner(pg_database.url).bootstrap(str(instance))
    dsn = DatabaseDsn.parse(pg_database.url)
    same = PgDatabaseChecker().check(dsn, instance_id=instance)
    assert same.target_state is TargetState.SAME_INSTALLATION and same.target_instance_id == instance and same.ok
    foreign = PgDatabaseChecker().check(dsn, instance_id=uuid.uuid4())
    assert foreign.target_state is TargetState.FOREIGN_INSTALLATION
    assert foreign.check(CheckId.TARGET_STATE).status is CheckStatus.FAILED and not foreign.ok
    anonymous = PgDatabaseChecker().check(dsn, instance_id=None)
    assert anonymous.target_state is TargetState.FOREIGN_INSTALLATION
    with psycopg.connect(pg_database.url, autocommit=True) as admin:
        admin.execute("DELETE FROM tam_control.meta")
    unknown = PgDatabaseChecker().check(dsn, instance_id=instance)
    assert unknown.target_state is TargetState.UNKNOWN and not unknown.ok


@pytest.mark.postgres
def test_report_is_json_serializable_with_computed_ok(pg_database):
    report = PgDatabaseChecker().check(DatabaseDsn.parse(pg_database.url), instance_id=None)
    payload = json.loads(report.model_dump_json())
    assert payload["ok"] is True and [check["id"] for check in payload["checks"]] == [c.value for c in CHECK_ORDER]


@contextmanager
def locale_database(pg_server, clause: str):
    """A throwaway database created with ``clause`` (locale options) plus the vector extension."""
    name = "tam_test_locale_" + secrets.token_hex(4)
    with psycopg.connect(pg_server.admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' " + clause).format(
            sql.Identifier(name)))
    try:
        url = pg_server.url_for(name)
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute("CREATE SCHEMA extensions")
            connection.execute("CREATE EXTENSION vector SCHEMA extensions")
        yield url
    finally:
        with psycopg.connect(pg_server.admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.postgres
@pytest.mark.parametrize("clause, accepted", [
    ("LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8'", True),
    ("LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C.UTF-8'", True),
    ("LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C'", False),
    ("LOCALE_PROVIDER libc LOCALE 'C'", False),
    ("LOCALE_PROVIDER libc LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'", False),
])
def test_locale_must_order_bytewise_and_fold_unicode(pg_server, clause, accepted):
    with locale_database(pg_server, clause) as url:
        with psycopg.connect(url) as connection:
            folds = connection.execute("SELECT lower('ПРИВЕТ') = 'привет'").fetchone()[0]
        encoding = PgDatabaseChecker().check(DatabaseDsn.parse(url), instance_id=None).check(CheckId.ENCODING)
    assert (encoding.status is CheckStatus.PASSED) is accepted, encoding.message
    if accepted:
        assert folds
    else:
        assert any("BUILTIN_LOCALE 'C.UTF-8'" in line for line in encoding.dba_sql)


class FakeSession:
    def __init__(self, pids, locks_visible=True):
        self.pids, self.locks_visible, self.statements = list(pids), locks_visible, []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if "pg_backend_pid()" in statement and "pg_locks" not in statement:
            value = self.pids.pop(0) if len(self.pids) > 1 else self.pids[0]
        elif "pg_locks" in statement:
            value = self.locks_visible
        else:
            value = True
        return type("Result", (), {"fetchone": lambda _self: (value,)})()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.mark.parametrize("first, second, visible, pooled", [
    ([10, 10], [11], True, False),
    ([10, 10], [10], True, True),
    ([10, 10], [11], False, True),
    ([10, 12], [11], True, True),
])
def test_transaction_pooling_is_detected(first, second, visible, pooled):
    assert transaction_pooled(FakeSession(first), lambda: FakeSession(second, visible)) is pooled


@pytest.mark.postgres
def test_direct_connection_is_not_reported_as_pooled(pg_database):
    with psycopg.connect(pg_database.url, autocommit=True) as first:
        assert not transaction_pooled(first, lambda: psycopg.connect(pg_database.url, autocommit=True))


@pytest.mark.postgres
def test_isolated_check_ignores_the_servers_libpq_environment(pg_database, monkeypatch):
    # The test server has no TLS: a borrowed PGSSLMODE=require makes the in-process check fail.
    without_sslmode = DatabaseDsn.parse(pg_database.url).model_copy(update={"sslmode": None})
    with monkeypatch.context() as context:
        context.setenv("PGSSLMODE", "require")
        borrowed = PgDatabaseChecker().check(without_sslmode, instance_id=None)
        assert borrowed.check(CheckId.CONNECT).category is ErrorCategory.SSL_REQUIRED
        assert PgDatabaseChecker(isolated=True).check(without_sslmode, instance_id=None).ok


def test_isolated_check_refuses_server_local_dsns_before_spawning():
    for raw in ("postgresql://tam@db.internal/tam", "postgresql://tam:pw@%2Fvar%2Frun%2Fpostgresql/tam"):
        with pytest.raises(InvalidDsn):
            PgDatabaseChecker(isolated=True).check(DatabaseDsn.parse(raw, DsnOrigin.ENV), instance_id=None)


def test_isolated_environment_drops_libpq_variables(monkeypatch, tmp_path):
    for name in ("PGPASSWORD", "PGSERVICE", "PGSSLCERT", "PGSSLKEY", "PGPASSFILE", "PGHOST"):
        monkeypatch.setenv(name, "leak")
    environment = sanitized_environment(tmp_path)
    assert not [name for name in environment if name.startswith("PG")]
    assert environment["HOME"] == str(tmp_path)
