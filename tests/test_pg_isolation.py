"""DB-level workspace isolation on PostgreSQL (plan 1.3, 6.4).

Every workspace is a schema owned by its own LOGIN role. Connected as workspace A, every route
to workspace B, the control plane or the server itself must be refused by PostgreSQL, not by
TAM code. Runs against both a superuser admin and a minimal (CREATEROLE + CREATE) admin.
"""
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra: pip install '.[postgres]'")
from psycopg import errors, sql
from pydantic import SecretStr

from tam_db.contracts import (
    COMPAT_SCHEMA,
    CONTROL_SCHEMA,
    EXTENSIONS_SCHEMA,
    LEARNING_SCHEMA,
    DatabaseSettings,
    PgDatabaseError,
    role_for,
    schema_for,
)
from team_memory import pg_provision
from team_memory.contracts import Conflict
from team_memory.database_contracts import DatabaseDsn, DsnOrigin
from team_memory.pg_provision import (
    PgProvisioner,
    PgWorkspaceProvisioner,
    PrerequisitesMissing,
    role_password,
    server_lease,
    workspace_lease,
)
from tests.team_db_helpers import pg_admin

pytestmark = pytest.mark.postgres

MASTER_KEY = b"k" * 32
KEY_A = "personal_" + "a" * 64
KEY_B = "team_" + "b" * 64
SECRET_ROW = "B's confidential note"
DENIED = (errors.InsufficientPrivilege, errors.UndefinedTable, errors.UndefinedFunction, errors.UndefinedObject,
          errors.FeatureNotSupported)


@pytest.fixture(params=["superuser", "createrole"])
def installation(request, pg_database):
    with pg_admin(pg_database, superuser=request.param == "superuser") as url:
        instance_id = str(uuid.uuid4())
        assert PgProvisioner(url).bootstrap(instance_id) == instance_id
        provisioner = PgWorkspaceProvisioner(url, instance_id, MASTER_KEY, DatabaseSettings(workspace_connection_limit=2))
        provisioner.ensure(KEY_A)
        provisioner.ensure(KEY_B)
        with psycopg.connect(provisioner.store_database(KEY_B).url, autocommit=True) as b:
            b.execute("CREATE TABLE probe (id bigint PRIMARY KEY, content text)")
            b.execute("INSERT INTO probe VALUES (1, %s)", (SECRET_ROW,))
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"INSERT INTO {CONTROL_SCHEMA}.users (id, name) VALUES ('u1', 'User One')")
        yield {"url": url, "database": pg_database, "provisioner": provisioner, "instance_id": instance_id}


def as_role(installation, key):
    return psycopg.connect(installation["provisioner"].store_database(key).url, autocommit=True)


def refused(connection, statement, params=None):
    with pytest.raises(DENIED):
        connection.execute(statement, params)


def test_role_sees_its_own_schema_through_search_path(installation):
    with as_role(installation, KEY_B) as b:
        assert b.execute("SELECT content FROM probe").fetchone()[0] == SECRET_ROW
        path = b.execute("SHOW search_path").fetchone()[0]
    assert path == f"{schema_for(KEY_B)}, {COMPAT_SCHEMA}, {EXTENSIONS_SCHEMA}"


def _revoke_public_database_privileges(database):
    """What the DBA SQL in docs/TEAM_POSTGRES.md and docker/postgres-init/01-tam.sql do."""
    with psycopg.connect(database.url, autocommit=True) as superuser:
        superuser.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database.name)))


def test_workspace_role_works_when_public_database_privileges_are_revoked(pg_database):
    _revoke_public_database_privileges(pg_database)
    instance_id = str(uuid.uuid4())
    PgProvisioner(pg_database.url).bootstrap(instance_id)
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance_id, MASTER_KEY)
    # ensure() logs in as the role and runs the workspace migrations, one of which uses a temporary table.
    provisioner.ensure(KEY_A)
    with psycopg.connect(provisioner.store_database(KEY_A).url, autocommit=True) as a:
        assert a.execute("SELECT has_database_privilege(current_user, current_database(), 'CONNECT')").fetchone()[0]
        a.execute("CREATE TEMP TABLE scratch (x int)")


def test_admin_that_cannot_grant_database_access_gets_the_dba_statement(pg_database):
    with pg_admin(pg_database, superuser=False) as url:
        admin_role = DatabaseDsn.parse(url).user
        _revoke_public_database_privileges(pg_database)
        with psycopg.connect(pg_database.url, autocommit=True) as superuser:
            superuser.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(pg_database.name), sql.Identifier(admin_role)))
        instance_id = str(uuid.uuid4())
        PgProvisioner(url).bootstrap(instance_id)
        with pytest.raises(PrerequisitesMissing) as raised:
            PgWorkspaceProvisioner(url, instance_id, MASTER_KEY).ensure(KEY_A)
        assert raised.value.dba_sql == (
            f'GRANT CONNECT, TEMPORARY ON DATABASE "{pg_database.name}" TO "{admin_role}" WITH GRANT OPTION;',)


@pytest.mark.parametrize("superuser", [True, False])
def test_purge_and_reprovision_work_when_public_database_privileges_are_revoked(pg_database, superuser):
    """PostgreSQL refuses to drop a role that still holds privileges on the database."""
    with pg_admin(pg_database, superuser=superuser) as url:
        _revoke_public_database_privileges(pg_database)
        if not superuser:
            with psycopg.connect(pg_database.url, autocommit=True) as dba:
                dba.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {} WITH GRANT OPTION").format(
                    sql.Identifier(pg_database.name), sql.Identifier(DatabaseDsn.parse(url).user)))
        instance_id = str(uuid.uuid4())
        PgProvisioner(url).bootstrap(instance_id)
        provisioner = PgWorkspaceProvisioner(url, instance_id, MASTER_KEY)
        provisioner.ensure(KEY_A)
        provisioner.drop(KEY_A)
        with psycopg.connect(url, autocommit=True) as admin:
            assert admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                 (role_for(KEY_A, instance_id),)).fetchone() is None
        provisioner.ensure(KEY_A)
        with psycopg.connect(provisioner.store_database(KEY_A).url, autocommit=True) as a:
            a.execute("CREATE TEMP TABLE scratch (x int)")


def test_database_access_revoked_after_provisioning_is_repaired_by_ensure(pg_database):
    """An installation provisioned while PUBLIC still held the privileges, hardened afterwards."""
    instance_id = str(uuid.uuid4())
    PgProvisioner(pg_database.url).bootstrap(instance_id)
    provisioner = PgWorkspaceProvisioner(pg_database.url, instance_id, MASTER_KEY)
    provisioner.ensure(KEY_A)
    _revoke_public_database_privileges(pg_database)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(provisioner.store_database(KEY_A).url).close()
    provisioner.ensure(KEY_A)
    with psycopg.connect(provisioner.store_database(KEY_A).url, autocommit=True) as a:
        a.execute("CREATE TEMP TABLE scratch (x int)")


def test_other_workspace_is_denied_by_qualified_name_and_search_path(installation):
    other = schema_for(KEY_B)
    with as_role(installation, KEY_A) as a:
        refused(a, sql.SQL("SELECT * FROM {}.probe").format(sql.Identifier(other)))
        a.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(other)))
        refused(a, "SELECT * FROM probe")
        refused(a, sql.SQL("GRANT USAGE ON SCHEMA {} TO CURRENT_USER").format(sql.Identifier(other)))
        refused(a, sql.SQL("CREATE TABLE {}.planted (x int)").format(sql.Identifier(other)))


def test_changing_own_role_defaults_does_not_open_other_schema(installation):
    other = schema_for(KEY_B)
    with as_role(installation, KEY_A) as a:
        a.execute(sql.SQL("ALTER ROLE CURRENT_USER SET search_path TO {}").format(sql.Identifier(other)))
    with as_role(installation, KEY_A) as a:
        refused(a, "SELECT * FROM probe")


def test_control_plane_is_denied(installation):
    with as_role(installation, KEY_A) as a:
        for table in (f"{CONTROL_SCHEMA}.users", f"{CONTROL_SCHEMA}.workspace_schemas", f"{CONTROL_SCHEMA}.meta",
                      f"{LEARNING_SCHEMA}.personal_outbox"):
            refused(a, f"SELECT * FROM {table}")


def test_server_files_programs_and_large_objects_are_denied(installation):
    with as_role(installation, KEY_A) as a:
        a.execute("CREATE TABLE mine (x text)")
        refused(a, "SELECT pg_read_file('/etc/passwd')")
        refused(a, "SELECT pg_ls_dir('.')")
        refused(a, "COPY mine TO PROGRAM 'id'")
        refused(a, "COPY mine FROM PROGRAM 'id'")
        refused(a, "COPY mine FROM '/etc/passwd'")
        refused(a, "COPY mine TO '/tmp/tam-leak'")
        refused(a, "SELECT lo_import('/etc/passwd')")
        refused(a, "SELECT lo_export(0, '/tmp/tam-leak')")


def test_foreign_data_and_extensions_are_denied(installation):
    with as_role(installation, KEY_A) as a:
        refused(a, "CREATE EXTENSION dblink")
        refused(a, "CREATE EXTENSION postgres_fdw")
        refused(a, "CREATE SCHEMA planted")
        refused(a, f"CREATE FUNCTION {COMPAT_SCHEMA}.strftime(text) RETURNS text LANGUAGE sql AS 'SELECT $1'")
        refused(a, f"CREATE TABLE {EXTENSIONS_SCHEMA}.planted (x int)")


def test_role_switching_is_denied(installation):
    other_role = role_for(KEY_B, installation["instance_id"])
    admin_user = psycopg.conninfo.conninfo_to_dict(installation["url"])["user"]
    with as_role(installation, KEY_A) as a:
        refused(a, sql.SQL("SET ROLE {}").format(sql.Identifier(other_role)))
        refused(a, sql.SQL("SET ROLE {}").format(sql.Identifier(admin_user)))
        refused(a, sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(admin_user)))
        refused(a, sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(other_role)))
        refused(a, sql.SQL("ALTER ROLE {} PASSWORD 'x'").format(sql.Identifier(other_role)))


def test_role_cannot_log_in_as_another_workspace(installation):
    provisioner = installation["provisioner"]
    wrong = provisioner.dsn.model_copy(update={"user": role_for(KEY_B, installation["instance_id"])})
    url = wrong.model_copy(update={"password": provisioner.dsn.password}).to_uri()
    wrong_password = provisioner.store_database(KEY_A).url.replace(
        role_password(MASTER_KEY, role_for(KEY_A, installation["instance_id"])), "0" * 64)
    for candidate in (url, wrong_password):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(candidate, connect_timeout=5).close()


def test_other_sessions_queries_are_hidden(installation):
    with psycopg.connect(installation["url"], autocommit=True) as admin:
        admin.execute("SELECT 'tam-visible-marker'")
        with as_role(installation, KEY_A) as a:
            rows = a.execute("SELECT query FROM pg_stat_activity WHERE usename <> current_user "
                             "AND query IS NOT NULL").fetchall()
    assert rows and all("tam-visible-marker" not in (row[0] or "") for row in rows)


def test_connection_limit_is_enforced(installation):
    connections = [as_role(installation, KEY_A) for _ in range(2)]
    try:
        with pytest.raises(psycopg.OperationalError):
            as_role(installation, KEY_A).close()
    finally:
        for connection in connections:
            connection.close()


def test_role_carries_no_elevated_attributes(installation):
    role = role_for(KEY_A, installation["instance_id"])
    with psycopg.connect(installation["url"], autocommit=True) as admin:
        row = admin.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit, "
                            "rolconnlimit FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    assert row == (False, False, False, False, False, False, 2)


def role_state(installation, key):
    """What ensure() could rewrite: the role's catalog row version and password, its settings, the map row."""
    with psycopg.connect(installation["database"].url, autocommit=True) as superuser:
        return superuser.execute(
            "SELECT r.xmin::text, r.rolpassword, s.setconfig, w.fingerprint FROM pg_authid r "
            "LEFT JOIN pg_db_role_setting s ON s.setrole = r.oid "
            "JOIN tam_control.workspace_schemas w ON w.role = r.rolname WHERE r.rolname = %s",
            (role_for(key, installation["instance_id"]),)).fetchone()


def test_ensure_of_a_current_workspace_writes_nothing(installation):
    provisioner = installation["provisioner"]
    before = role_state(installation, KEY_A)
    assert provisioner.ensure(KEY_A) == provisioner.ensure(KEY_A, migrate=False)
    assert role_state(installation, KEY_A) == before


def test_force_and_a_new_master_key_reset_the_password(installation):
    provisioner = installation["provisioner"]
    role = role_for(KEY_A, installation["instance_id"])
    with psycopg.connect(installation["url"], autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} PASSWORD 'changed-behind-tam'").format(sql.Identifier(role)))
    provisioner.ensure(KEY_A)
    with pytest.raises(psycopg.OperationalError):
        as_role(installation, KEY_A).close()
    provisioner.ensure(KEY_A, force=True)
    as_role(installation, KEY_A).close()
    rotated = PgWorkspaceProvisioner(installation["url"], installation["instance_id"], b"n" * 32,
                                     DatabaseSettings(workspace_connection_limit=2))
    rotated.ensure(KEY_A)
    with psycopg.connect(rotated.store_database(KEY_A).url) as a:
        assert a.execute("SELECT current_user").fetchone()[0] == role
    assert sorted(provisioner.workspace_keys()) == sorted([KEY_A, KEY_B])


def test_concurrent_ensure_calls_all_succeed(installation):
    provisioner = installation["provisioner"]
    keys = [f"team_{index:064d}" for index in range(4)] + [KEY_A] * 3
    with ThreadPoolExecutor(max_workers=len(keys)) as executor:
        futures = [executor.submit(provisioner.ensure, key, force=True, migrate=False) for key in keys]
    assert {future.result().key for future in futures} == set(keys)


def test_provisioning_errors_are_contract_errors(installation, pg_database):
    lacking = "tam_lacking_" + uuid.uuid4().hex[:8]
    with psycopg.connect(pg_database.url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'lackingpw'").format(sql.Identifier(lacking)))
        admin.execute(sql.SQL("GRANT USAGE ON SCHEMA tam_control TO {}").format(sql.Identifier(lacking)))
    try:
        url = DatabaseDsn.parse(pg_database.url).model_copy(
            update={"user": lacking, "password": SecretStr("lackingpw")}).to_uri()
        weak = PgWorkspaceProvisioner(url, installation["instance_id"], MASTER_KEY)
        with pytest.raises(PgDatabaseError) as caught:
            weak.ensure("team_" + "e" * 64)
        assert not isinstance(caught.value, psycopg.Error) and "lackingpw" not in str(caught.value)
    finally:
        with psycopg.connect(pg_database.url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(lacking)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(lacking)))


def test_tampered_workspace_role_is_refused(installation):
    with psycopg.connect(installation["url"]) as admin:
        superuser = admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    if not superuser:
        pytest.skip("only a superuser can give a role elevated attributes")
    role = role_for(KEY_A, installation["instance_id"])
    with psycopg.connect(installation["url"], autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(role)))
    with pytest.raises(Conflict):
        installation["provisioner"].ensure(KEY_A)


def test_purge_leaves_no_schema_role_or_map_row(installation):
    provisioner = installation["provisioner"]
    provisioner.drop(KEY_B)
    provisioner.drop(KEY_B)
    with psycopg.connect(installation["url"], autocommit=True) as admin:
        assert admin.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema_for(KEY_B),)).fetchone() is None
        assert admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                             (role_for(KEY_B, installation["instance_id"]),)).fetchone() is None
        assert admin.execute(f"SELECT 1 FROM {CONTROL_SCHEMA}.workspace_schemas WHERE key = %s",
                             (KEY_B,)).fetchone() is None
    assert not provisioner.exists(KEY_B)
    assert provisioner.exists(KEY_A)


def test_two_installations_on_one_cluster_get_distinct_roles(installation):
    other_instance = str(uuid.uuid4())
    assert role_for("shared", other_instance) != role_for("shared", installation["instance_id"])
    assert schema_for("shared") == schema_for("shared")


def test_single_active_server_and_workspace_leases(installation):
    url = installation["url"]
    with server_lease(url), pytest.raises(Conflict):
        server_lease(url).acquire()
    with server_lease(url):
        pass
    with workspace_lease(url, KEY_A):
        with pytest.raises(Conflict):
            workspace_lease(url, KEY_A).acquire()
        with workspace_lease(url, KEY_B):
            pass


KEY_C = "team_" + "c" * 64
RESTORED_OBJECTS = """
CREATE TABLE {s}.notes (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, body text NOT NULL);
CREATE TABLE {s}.legacy (id serial PRIMARY KEY, body text);
CREATE SEQUENCE {s}.counter;
CREATE VIEW {s}.note_view AS SELECT id, body FROM {s}.notes;
CREATE MATERIALIZED VIEW {s}.note_count AS SELECT count(*) AS n FROM {s}.notes;
CREATE TYPE {s}.mood AS ENUM ('calm', 'busy');
CREATE DOMAIN {s}.positive AS integer CHECK (VALUE > 0);
CREATE TYPE {s}.pair AS (a integer, b integer);
CREATE FUNCTION {s}.touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.body := trim(NEW.body); RETURN NEW; END $$;
CREATE TRIGGER notes_touch BEFORE INSERT ON {s}.notes FOR EACH ROW EXECUTE FUNCTION {s}.touch();
CREATE AGGREGATE {s}.total(integer) (SFUNC = int4pl, STYPE = integer, INITCOND = '0');
"""


def restore_as_admin(installation, key, objects=RESTORED_OBJECTS, mapped=True):
    """The state pg_restore --no-owner --no-privileges leaves: schema and objects owned by the admin."""
    schema = schema_for(key)
    with psycopg.connect(installation["url"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        admin.execute(objects.format(s=sql.Identifier(schema).as_string(admin)))
        if mapped:
            admin.execute(f"INSERT INTO {CONTROL_SCHEMA}.workspace_schemas (key, schema, role) VALUES (%s, %s, %s)",
                          (key, schema, role_for(key, installation["instance_id"])))


def owners(installation, key):
    with psycopg.connect(installation["url"], autocommit=True) as admin:
        namespace = admin.execute("SELECT to_regnamespace(%s)::oid", (schema_for(key),)).fetchone()[0]
        return set(admin.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relnamespace = %(ns)s UNION "
            "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE pronamespace = %(ns)s UNION "
            "SELECT pg_get_userbyid(typowner) FROM pg_type WHERE typnamespace = %(ns)s UNION "
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE oid = %(ns)s", {"ns": namespace}).fetchall())


def test_ensure_adopts_a_schema_restored_without_owners(installation):
    restore_as_admin(installation, KEY_C)
    installation["provisioner"].ensure(KEY_C, migrate=False)
    assert owners(installation, KEY_C) == {(role_for(KEY_C, installation["instance_id"]),)}
    with as_role(installation, KEY_C) as c:
        c.execute("INSERT INTO notes (body) VALUES ('  padded  ')")
        c.execute("INSERT INTO legacy (body) VALUES ('x')")
        assert c.execute("SELECT body FROM note_view").fetchone()[0] == "padded"
        assert c.execute("SELECT nextval('counter')").fetchone()[0] == 1
        c.execute("REFRESH MATERIALIZED VIEW note_count")
        assert c.execute("SELECT total(x) FROM (VALUES (2), (3)) v(x)").fetchone()[0] == 5
        c.execute("ALTER TABLE notes DISABLE TRIGGER USER")
    with as_role(installation, KEY_B) as b:
        refused(b, sql.SQL("SELECT * FROM {}.notes").format(sql.Identifier(schema_for(KEY_C))))
    installation["provisioner"].ensure(KEY_C, migrate=False)


def test_adoption_refuses_security_definer_and_unmapped_schemas(installation):
    definer = RESTORED_OBJECTS + "CREATE FUNCTION {s}.escalate() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1';"
    restore_as_admin(installation, KEY_C, definer)
    with pytest.raises(Conflict):
        installation["provisioner"].ensure(KEY_C, migrate=False)
    other = "team_" + "d" * 64
    restore_as_admin(installation, other, mapped=False)
    with pytest.raises(Conflict):
        installation["provisioner"].ensure(other, migrate=False)
    assert owners(installation, other) == {(psycopg.conninfo.conninfo_to_dict(installation["url"])["user"],)}


@pytest.mark.skipif(shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
                    reason="pg_dump / pg_restore are not installed")
def test_dump_restore_without_owners_then_ensure(installation, pg_server, tmp_path):
    from tests.pg_support import fresh_database

    dump = tmp_path / "tam.dump"
    subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--schema", CONTROL_SCHEMA,
                    "--schema", LEARNING_SCHEMA, "--schema", "ws_*", "--file", str(dump), installation["url"]],
                   check=True, capture_output=True, timeout=120)
    with fresh_database(pg_server) as target:
        # Same cluster, same admin role (it holds ADMIN on the existing workspace roles).
        admin = DatabaseDsn.parse(installation["url"])
        url = admin.model_copy(update={"database": target.name}).to_uri()
        with psycopg.connect(target.url, autocommit=True) as superuser:
            superuser.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(target.name), sql.Identifier(admin.user)))
            superuser.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(sql.Identifier(EXTENSIONS_SCHEMA)))
        subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "--dbname", url, str(dump)],
                       check=True, capture_output=True, timeout=120)
        assert PgProvisioner(url).bootstrap(str(uuid.uuid4())) == installation["instance_id"]
        restored = PgWorkspaceProvisioner(url, installation["instance_id"], MASTER_KEY,
                                          DatabaseSettings(workspace_connection_limit=2))
        for key in restored.workspace_keys():
            restored.ensure(key)
        with psycopg.connect(restored.store_database(KEY_B).url, autocommit=True) as b:
            assert b.execute("SELECT content FROM probe").fetchone()[0] == SECRET_ROW
            b.execute("INSERT INTO probe VALUES (2, 'after restore')")
            assert b.execute("SELECT strftime('%Y', '2026-09-25')").fetchone()[0] == "2026"
        with psycopg.connect(restored.store_database(KEY_A).url, autocommit=True) as a:
            refused(a, sql.SQL("SELECT * FROM {}.probe").format(sql.Identifier(schema_for(KEY_B))))


def test_concurrent_catalog_update_is_retried(installation, monkeypatch):
    provisioner = installation["provisioner"]
    original = PgWorkspaceProvisioner._provision_once
    calls = []

    def flaky(self, connection, target, fingerprint):
        calls.append(target.key)
        if len(calls) == 1:
            raise psycopg.errors.lookup("XX000")("tuple concurrently updated")
        return original(self, connection, target, fingerprint)

    monkeypatch.setattr(PgWorkspaceProvisioner, "_provision_once", flaky)
    assert provisioner.ensure(KEY_C, migrate=False).key == KEY_C and len(calls) == 2

    def broken(self, connection, target, fingerprint):
        raise psycopg.errors.lookup("42501")("permission denied for database")

    monkeypatch.setattr(PgWorkspaceProvisioner, "_provision_once", broken)
    with pytest.raises(PgDatabaseError) as caught:
        provisioner.ensure("team_" + "f" * 64, migrate=False)
    assert not isinstance(caught.value, psycopg.Error)


FAST_CHECK_SECONDS = 0.1
LOSS_WAIT_SECONDS = 5


def terminate(installation, lease):
    pid = lease.connection.info.backend_pid
    with psycopg.connect(installation["database"].url, autocommit=True) as superuser:
        assert superuser.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]


@pytest.mark.parametrize("factory", ["server", "workspace"])
def test_lost_lease_is_detected_and_reported(installation, factory):
    lost = threading.Event()
    make = (lambda: server_lease(installation["url"], on_lost=lost.set)) if factory == "server" else \
        (lambda: workspace_lease(installation["url"], KEY_A, on_lost=lost.set))
    lease = make()
    lease.check_seconds = FAST_CHECK_SECONDS
    lease.acquire()
    try:
        assert not lost.wait(FAST_CHECK_SECONDS * 5) and lease.verify()
        terminate(installation, lease)
        assert lost.wait(LOSS_WAIT_SECONDS) and lease.lost and not lease.verify()
    finally:
        lease.release()
    with make():
        pass


def test_lease_handover_moves_the_lock_to_the_new_dsn(installation):
    url = installation["url"]
    renamed = DatabaseDsn.parse(url, DsnOrigin.ENV).model_copy(update={"application_name": "tam-repointed"}).to_uri()
    lease = server_lease(url).acquire()
    try:
        old = lease.connection
        lease.handover(renamed)
        assert old.closed and lease.connection is not old and lease.url == renamed and lease.verify()
        with pytest.raises(Conflict):
            server_lease(url).acquire()
    finally:
        lease.release()
    server_lease(url).acquire().release()


def test_lease_refuses_a_transaction_pooler(installation, monkeypatch):
    monkeypatch.setattr(pg_provision, "advisory_lock_held", lambda *_args: False)
    with pytest.raises(PrerequisitesMissing):
        server_lease(installation["url"]).acquire()
    monkeypatch.undo()
    server_lease(installation["url"]).acquire().release()
