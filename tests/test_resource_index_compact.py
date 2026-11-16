from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memory_core.schema_migration import Migration, MigrationFailed, MigrationRunner

MIGRATIONS = Path(__file__).parents[1] / 'migrations'


@pytest.mark.parametrize('inject_failure', [False, True])
def test_compact_migration_preserves_postings_or_rolls_back(inject_failure):
    with sqlite3.connect(':memory:') as db:
        db.executescript((MIGRATIONS / '050_resource_index.sql').read_text(encoding='utf-8'))
        db.execute('CREATE TABLE migrations(version TEXT PRIMARY KEY, description TEXT, applied_at TEXT)')
        rows = [('term', 'a'), ('термин', 'a'), ('term', 'b')]
        db.executemany('INSERT INTO resource_terms VALUES(?,?)', rows)
        db.commit()
        script = (MIGRATIONS / '051_resource_terms_compact.sql').read_text(encoding='utf-8')
        if inject_failure:
            script += '\nINSERT INTO nonexistent_resource_migration_table VALUES(1);'
            with pytest.raises(MigrationFailed):
                MigrationRunner(db).apply(Migration('051', 'compact terms', script))
        else:
            MigrationRunner(db).apply(Migration('051', 'compact terms', script))
        assert sorted(db.execute('SELECT term,chunk_id FROM resource_terms')) == sorted(rows)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='resource_terms'").fetchone()[0]
        assert ('WITHOUT ROWID' in sql) is not inject_failure
        assert db.execute('SELECT COUNT(*) FROM migrations').fetchone()[0] == int(not inject_failure)
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_terms_compact'").fetchone()
        assert not db.in_transaction
