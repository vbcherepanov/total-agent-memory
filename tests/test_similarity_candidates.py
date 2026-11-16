from tests.pg_store_support import store_backend  # noqa: F401
from tests.test_dedup_updates import store  # noqa: F401


def test_comparisons_are_bounded_and_report_truncation(store, monkeypatch):  # noqa: F811
    import memory_core.similar as similar
    source = 'Billing ledger location is rack seven'
    for index in range(80):
        store.db.execute(
            'INSERT INTO knowledge(session_id,type,content,project,created_at) VALUES (?,?,?,?,?)',
            ('bounded', 'fact', source + str(index), 'bounded', '2026-11-15T00:00:00Z'))
    store.db.commit()
    calls = []
    original = similar._levenshtein_ratio
    def count(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(similar, '_levenshtein_ratio', count)
    report = similar.find_similar(store.db, source, 'bounded', 0)
    assert report.truncated
    assert 0 < len(calls) <= 2 * similar.CANDIDATES_PER_INDEX
    assert len(report.matches) <= similar.MAX_MATCHES


def test_superseded_and_deleted_candidates_remain_visible(store):  # noqa: F811
    from memory_core.similar import find_similar
    text = 'Billing ledger location is rack seven'
    first, *_ = store.save_knowledge('s1', text, 'fact', project='bounded', skip_quality=True)
    second, *_ = store.save_knowledge('s1', text + '.', 'fact', project='bounded', skip_quality=True)
    store.delete_knowledge(second)
    report = find_similar(store.db, text, 'bounded', 0)
    assert {row.status for row in report.matches} == {'superseded', 'deleted'}
    assert {row.id for row in report.matches} == {first, second}


def test_older_near_duplicate_is_found_among_many_records_sharing_prefix_and_suffix(store):  # noqa: F811
    import memory_core.similar as similar
    target = 'the billing ledger is stored in rack seven of the primary datacenter in production.'
    variant = target.replace('rack seven', 'rack sevem')
    store.db.execute(
        'INSERT INTO knowledge(session_id,type,content,project,created_at) VALUES (?,?,?,?,?)',
        ('older', 'fact', target, 'crowded', '2026-11-15T00:00:00Z'))
    target_id = store.db.execute('SELECT max(id) FROM knowledge').fetchone()[0]
    crowd = 3 * similar.CANDIDATES_PER_INDEX
    for index in range(crowd):
        store.db.execute(
            'INSERT INTO knowledge(session_id,type,content,project,created_at) VALUES (?,?,?,?,?)',
            ('newer', 'fact', f'the unrelated note number {index} about something else entirely in production.',
             'crowded', '2026-11-15T00:00:00Z'))
    store.db.commit()
    report = similar.find_similar(store.db, variant, 'crowded', 0)
    assert [row.id for row in report.matches] == [target_id]
