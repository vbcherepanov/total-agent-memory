import uuid

import pytest

from tam_db.contracts import COMPAT_SCHEMA, CONTROL_SCHEMA
from team_memory import pg_provision
from team_memory.pg_provision import PgProvisioner, PgWorkspaceProvisioner
from tests.team_db_helpers import pg_admin

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra: pip install '.[postgres]'")

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("migration_name,expression,before,after", [
    ("compat/0002_fractional_second_clamp.sql", "date('2024-12-31 23:59:59.9996')", "2025-01-01", "2024-12-31"),
    ("compat/0003_midnight_normalization.sql", "datetime('24:00')", "2000-01-02 24:00:00", "2000-01-02 00:00:00"),
])
@pytest.mark.parametrize("superuser", [True, False])
def test_compat_upgrades_existing_roles_and_preserves_data(pg_database, monkeypatch, superuser,
                                                           migration_name, expression, before, after):
    files = pg_provision._migration_files()
    assert any(name == migration_name for name, _, _ in files)
    with pg_admin(pg_database, superuser=superuser) as url:
        instance = str(uuid.uuid4())
        with monkeypatch.context() as patch:
            patch.setattr(pg_provision, "_migration_files",
                          lambda: [entry for entry in files if not entry[0].startswith("compat/") or entry[0] < migration_name])
            PgProvisioner(url).bootstrap(instance)
            workspaces = PgWorkspaceProvisioner(url, instance, b"t" * 32)
            workspaces.ensure("existing")
        database = workspaces.store_database("existing")
        with psycopg.connect(database.url, autocommit=True) as workspace:
            workspace.execute("CREATE TABLE migration_probe (value text)")
            workspace.execute("INSERT INTO migration_probe VALUES ('keep existing workspace data')")
            statement = f"SELECT {COMPAT_SCHEMA}.{expression}"
            assert workspace.execute(statement).fetchone()[0] == before
            PgProvisioner(url).bootstrap(instance)
            assert workspace.execute(statement).fetchone()[0] == after
            assert workspace.execute("SELECT value FROM migration_probe").fetchone()[0] == "keep existing workspace data"
            PgProvisioner(url).bootstrap(instance)
            assert workspace.execute(statement).fetchone()[0] == after
        with psycopg.connect(url) as admin:
            assert admin.execute(f"SELECT count(*) FROM {CONTROL_SCHEMA}.control_migrations WHERE name=%s",
                                 (migration_name,)).fetchone()[0] == 1
