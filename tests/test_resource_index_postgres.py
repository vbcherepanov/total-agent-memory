from __future__ import annotations

import sqlite3

import pytest

from resource_index.extraction import ResourceExtractor
from resource_index.models import IndexLimits
from resource_index.repository import IndexRepository
from resource_index.service import ResourceIndex
from tests.test_pg_schema_parity import provision_unmigrated

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_index(pg_database, tmp_path):
    from tam_db import pg_connection, pg_schema
    connection = pg_connection.connect(provision_unmigrated(pg_database.url, 'resource-index'))
    connection.row_factory = sqlite3.Row
    pg_schema.ensure(connection)
    limits = IndexLimits()
    root = tmp_path / 'sources'
    root.mkdir()
    yield ResourceIndex(IndexRepository(connection), ResourceExtractor(limits), limits), root, connection
    connection.close()


def test_postgres_index_search_update_delete_and_scope(pg_index):
    index, root, db = pg_index
    source = root / 'notes.unknown'
    source.write_text('OriginalResource данные', encoding='utf-8')
    first = index.index(root, 'alpha')
    assert first.total_files == 1
    assert index.search(root, 'alpha', 'данные')
    assert not index.search(root, 'beta', 'данные')
    assert index.index(root, 'alpha').unchanged_files == 1
    source.write_text('ReplacementResource данные', encoding='utf-8')
    index.index(root, 'alpha')
    assert not index.search(root, 'alpha', 'OriginalResource')
    assert index.search(root, 'alpha', 'ReplacementResource')
    source.unlink()
    assert index.index(root, 'alpha').removed_files == 1
    assert db.execute('SELECT COUNT(*) FROM resource_terms').fetchone()[0] == 0


def test_postgres_index_failure_preserves_transaction(pg_index, monkeypatch):
    index, root, db = pg_index
    source = root / 'notes.unknown'
    source.write_text('OriginalResource', encoding='utf-8')
    index.index(root, 'alpha')
    source.write_text('ReplacementResource', encoding='utf-8')
    def fail(*args):
        raise RuntimeError('injected write failure')
    monkeypatch.setattr(index.repository, '_insert', fail)
    with pytest.raises(RuntimeError, match='injected'):
        index.index(root, 'alpha')
    assert index.search(root, 'alpha', 'OriginalResource')
    assert not db.in_transaction


def test_resource_index_survives_sqlite_to_postgres_migration(tmp_path, pg_database, monkeypatch):
    from tam_db import pg_connection
    from contextlib import closing
    from team_memory.database_contracts import MigrationPhase
    from team_memory.migration_service import PgMigrationTargets
    from tests.test_migration_service import Setup, runtime_workspace
    root = tmp_path / 'server'
    database_path = runtime_workspace(root, monkeypatch)
    source = tmp_path / 'sources'
    source.mkdir()
    (source / 'notes.unknown').write_text('MigrationResourceSentinel', encoding='utf-8')
    limits = IndexLimits()
    with closing(sqlite3.connect(database_path)) as db:
        ResourceIndex(IndexRepository(db), ResourceExtractor(limits), limits).index(source, 'alpha')
        db.commit()
    setup = Setup(root, pg_database, workspaces=False)
    setup.targets = setup.service.targets = PgMigrationTargets(setup.targets.master_key, setup.plane.settings)
    progress = setup.migrate()
    assert progress.phase is MigrationPhase.DONE, progress.error
    database = setup.targets.open(setup.dsn, setup.plane.current().instance_id).workspaces.store_database('shared')
    with closing(pg_connection.connect(database)) as db:
        db.row_factory = sqlite3.Row
        index = ResourceIndex(IndexRepository(db), ResourceExtractor(limits), limits)
        assert index.search(source, 'alpha', 'MigrationResourceSentinel')
        assert index.index(source, 'alpha').unchanged_files == 1
