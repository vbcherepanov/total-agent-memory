from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from filelock import FileLock

from resource_index.models import IndexErrorBase, IndexLimits, ScanIncomplete
from resource_index.tool import IndexAction, IndexRequest, execute
from resource_index.watcher import ResourceWatcher, WatchRepository, WatchSettings


@pytest.fixture
def watched(tmp_path):
    database = tmp_path / 'memory.db'
    db = sqlite3.connect(database)
    db.execute('PRAGMA journal_mode=WAL')
    migrations = Path(__file__).parents[1] / 'migrations'
    for name in ('050_resource_index.sql', '051_resource_terms_compact.sql', '052_resource_watches.sql'):
        db.executescript((migrations / name).read_text(encoding='utf-8'))
    root = tmp_path / 'project'
    root.mkdir()
    def allowed(raw):
        result = Path(raw).resolve()
        if result != root:
            raise ValueError('Outside test root')
        return result
    watcher = ResourceWatcher(database, allowed, WatchSettings(interval_seconds=0.02), IndexLimits())
    yield db, root, watcher
    watcher.close()
    db.close()


def index(db, root, **kwargs):
    return execute(db, IndexRequest(root, **kwargs))


def search(db, root, query, project='general'):
    return index(db, root, action=IndexAction.SEARCH, query=query, project=project)['results']


def test_watch_updates_creates_deletes_and_restarts(watched):
    db, root, watcher = watched
    source = root / 'note.txt'
    source.write_text('OriginalSentinel', encoding='utf-8')
    index(db, root)
    assert watcher.tick() == 1
    source.write_text('ChangedSentinel', encoding='utf-8')
    (root / 'new.txt').write_text('AddedSentinel', encoding='utf-8')
    restarted = ResourceWatcher(watcher.database, watcher.allowed_path, watcher.settings, watcher.limits)
    assert restarted.tick() == 1
    assert search(db, root, 'ChangedSentinel') and search(db, root, 'AddedSentinel')
    assert not search(db, root, 'OriginalSentinel')
    source.unlink()
    assert restarted.tick() == 1
    assert not search(db, root, 'ChangedSentinel')
    assert restarted.tick() == 0


def test_watch_preserves_filters_and_project_isolation(watched):
    db, root, watcher = watched
    (root / 'note.txt').write_text('TextSentinel', encoding='utf-8')
    (root / 'code.py').write_text('PythonSentinel = 1', encoding='utf-8')
    index(db, root, project='text', include=('.txt',))
    index(db, root, project='python', include=('.py',))
    assert watcher.tick() == 2
    assert search(db, root, 'TextSentinel', 'text')
    assert not search(db, root, 'PythonSentinel', 'text')
    assert search(db, root, 'PythonSentinel', 'python')
    assert not search(db, root, 'TextSentinel', 'python')


def test_preview_and_opt_out_do_not_watch(watched):
    db, root, watcher = watched
    (root / 'note.txt').write_text('Sentinel', encoding='utf-8')
    index(db, root, action=IndexAction.PREVIEW)
    assert not WatchRepository(db).roots()
    index(db, root, watch=False)
    assert watcher.tick() == 0
    assert not index(db, root, action=IndexAction.STATUS)['watch']['enabled']


def test_live_background_updates_without_index_call(watched):
    db, root, watcher = watched
    source = root / 'note.txt'
    source.write_text('BeforeSentinel', encoding='utf-8')
    index(db, root)
    watcher.start()
    source.write_text('AfterSentinel', encoding='utf-8')
    deadline = time.monotonic() + 5
    while not search(db, root, 'AfterSentinel') and time.monotonic() < deadline:
        time.sleep(0.02)
    assert search(db, root, 'AfterSentinel')
    thread = watcher._thread
    watcher.close()
    assert not thread.is_alive()


def test_multiple_sessions_have_one_scanner(watched):
    db, root, watcher = watched
    index(db, root)
    with FileLock(str(watcher.database) + '.resource-watch.lock'):
        assert watcher.tick() == 0
        assert index(db, root, action=IndexAction.STATUS)['watch']['checked_at'] == 0
    assert watcher.tick() == 1


def test_missing_root_preserves_index_and_reports_error(watched):
    db, root, watcher = watched
    (root / 'note.txt').write_text('KeepSentinel', encoding='utf-8')
    index(db, root)
    moved = root.with_name('moved')
    root.rename(moved)
    assert watcher.tick() == 0
    assert search(db, root, 'KeepSentinel')
    assert index(db, root, action=IndexAction.STATUS)['watch']['error'] == 'ScanIncomplete'
    moved.rename(root)
    assert watcher.tick() == 1
    assert index(db, root, action=IndexAction.STATUS)['watch']['error'] == ''


def test_cancelled_scan_never_publishes_partial_state(watched):
    from resource_index.extraction import ResourceExtractor
    from resource_index.repository import IndexRepository
    from resource_index.service import ResourceIndex
    db, root, watcher = watched
    source = root / 'note.txt'
    source.write_text('BeforeSentinel', encoding='utf-8')
    index(db, root)
    source.write_text('AfterSentinel', encoding='utf-8')
    service = ResourceIndex(IndexRepository(db), ResourceExtractor(watcher.limits), watcher.limits)
    with pytest.raises(ScanIncomplete, match='cancelled'):
        service.index(root, 'general', cancelled=lambda: True)
    assert search(db, root, 'BeforeSentinel')
    assert not search(db, root, 'AfterSentinel')


def test_manual_policy_change_during_watch_cannot_be_overwritten(watched, monkeypatch):
    db, root, watcher = watched
    (root / 'note.txt').write_text('Sentinel', encoding='utf-8')
    index(db, root)
    original = watcher._signature
    def change_policy(service, path, exclude):
        signature = original(service, path, exclude)
        index(db, root, include=('.py',), watch=False)
        return signature
    monkeypatch.setattr(watcher, '_signature', change_policy)
    assert watcher.tick() == 0
    assert not search(db, root, 'Sentinel')
    assert not WatchRepository(db).roots()


def test_forced_reconciliation_and_no_writes_for_unchanged_poll(watched):
    db, root, watcher = watched
    index(db, root)
    assert watcher.tick() == 1
    revision = db.execute('SELECT revision FROM resource_roots').fetchone()[0]
    data_version = db.execute('PRAGMA data_version').fetchone()[0]
    assert watcher.tick() == 0
    assert db.execute('PRAGMA data_version').fetchone()[0] == data_version
    db.execute('UPDATE resource_watches SET checked_at=0')
    db.commit()
    assert watcher.tick() == 1
    assert db.execute('SELECT revision FROM resource_roots').fetchone()[0] == revision + 1


@pytest.mark.parametrize('value', [0, -1, float('inf'), float('nan'), True])
def test_invalid_settings_rejected(value):
    with pytest.raises(IndexErrorBase):
        WatchSettings(interval_seconds=value)


def test_invalid_watch_boolean_rejected(watched):
    db, root, _ = watched
    with pytest.raises(IndexErrorBase, match='boolean'):
        index(db, root, watch='false')
    assert db.execute('SELECT COUNT(*) FROM resource_roots').fetchone()[0] == 0
