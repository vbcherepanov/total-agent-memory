from __future__ import annotations

import gzip
import io
import sqlite3
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from resource_index.extraction import ResourceExtractor, decode_text
from resource_index.models import IndexConflict, IndexLimits, ScanIncomplete
from resource_index.repository import IndexRepository
from resource_index.service import ResourceIndex

SCHEMA = Path(__file__).parents[1] / 'migrations' / '050_resource_index.sql'


@pytest.fixture(params=['original', 'compact'])
def index(tmp_path, request):
    db = sqlite3.connect(tmp_path / 'index.db')
    db.execute('PRAGMA foreign_keys=OFF')
    db.executescript(SCHEMA.read_text(encoding='utf-8'))
    if request.param == 'compact':
        db.executescript((SCHEMA.parent / '051_resource_terms_compact.sql').read_text(encoding='utf-8'))
    limits = IndexLimits()
    service = ResourceIndex(IndexRepository(db), ResourceExtractor(limits), limits)
    root = tmp_path / 'project'
    root.mkdir()
    yield service, root, db
    db.close()


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def test_persistent_index_changes_deletes_and_project_isolation(index):
    service, root, db = index
    source = write(root, 'unusual.unknown_xyz', 'OldSentinel данные договоров')
    first = service.index(root, 'one')
    assert first.persisted and first.total_files == 1
    assert service.search(root, 'one', 'данные договоров')
    assert not service.search(root, 'two', 'данные договоров')
    second = service.index(root, 'one')
    assert second.changed_files == 0 and second.unchanged_files == 1
    source.write_text('NewSentinel данные договоров', encoding='utf-8')
    service.index(root, 'one')
    assert not service.search(root, 'one', 'OldSentinel')
    assert service.search(root, 'one', 'NewSentinel')
    source.unlink()
    removed = service.index(root, 'one')
    assert removed.removed_files == 1 and removed.total_files == 0
    assert db.execute('SELECT COUNT(*) FROM resource_terms').fetchone()[0] == 0


def test_index_survives_connection_restart(index):
    service, root, db = index
    write(root, 'notes', 'persistent unique needle')
    service.index(root, 'one')
    database = db.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(database) as reopened:
        repo = IndexRepository(reopened)
        from resource_index.repository import terms
        assert repo.search(repo.root('one', str(root)).id, terms('unique needle'), 5)


def test_preview_does_not_persist(index):
    service, root, db = index
    write(root, 'README', 'preview sentinel')
    result = service.index(root, 'one', preview=True)
    assert result.total_chunks > 0 and not result.persisted
    assert db.execute('SELECT COUNT(*) FROM resource_roots').fetchone()[0] == 0


@pytest.mark.parametrize('name,text,language', [
    ('main.c', 'int main(void) { return 0; }', 'c'),
    ('main.swift', 'func greet() -> String { return "hello" }', 'swift'),
    ('main.kt', 'fun greet() = "hello"', 'kotlin'),
    ('main.dart', 'String greet() => "hello";', 'dart'),
    ('query.sql', 'SELECT message FROM events;', 'sql'),
    ('config.yaml', 'message: hello', 'yaml'),
    ('config.toml', 'message = "hello"', 'toml'),
    ('page.html', '<h1>hello</h1>', 'html'),
    ('README.md', '# hello\nWorld', 'markdown'),
    ('Dockerfile', 'FROM scratch\nCOPY . /app', 'dockerfile'),
    ('script', '#!/bin/bash\necho hello', 'bash'),
    ('program.bsl', 'Процедура Привет()\nКонецПроцедуры', 'bsl'),
])
def test_registry_languages_are_indexed(index, name, text, language):
    service, root, _ = index
    write(root, name, text)
    result = service.index(root, 'one')
    assert result.by_status == {'indexed': 1}
    assert result.files[0].language == language
    assert service.search(root, 'one', Path(name).stem)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16', 'utf-32', 'cp1251'])
def test_text_encodings(encoding):
    text = 'Документ содержит описание работы системы и поиска информации.\n' * 10
    decoded = decode_text(text.encode(encoding))
    assert decoded is not None and decoded[0] == text


def test_binary_and_arbitrary_extension_are_visible(index):
    service, root, _ = index
    write(root, 'new_language.never_seen', 'UnchartedLanguageSymbol')
    (root / 'photo.unknown_binary').write_bytes(b'\x89PNG\r\n\x1a\n\x00\xff')
    result = service.index(root, 'one')
    assert result.total_files == 2
    assert result.by_status == {'indexed': 1, 'metadata_only': 1}
    assert service.search(root, 'one', 'UnchartedLanguageSymbol')
    assert service.search(root, 'one', 'photo')[0].status == 'metadata_only'


def test_secrets_private_blocks_and_sensitive_files_are_not_stored(index):
    service, root, db = index
    secret = 'sk-' + 'aB1cD2' * 8
    write(root, 'code.py', f'<private>\ndef PrivateHiddenSentinel(): return 42\n</private>\ndef public(): return "{secret}"')
    write(root, '.env', 'SENSITIVE_UNREDACTABLE_SENTINEL')
    service.index(root, 'one')
    stored = '\n'.join(row[0] for row in db.execute('SELECT content FROM resource_chunks'))
    assert secret not in stored and 'PrivateHiddenSentinel' not in stored
    assert 'SENSITIVE_UNREDACTABLE_SENTINEL' not in stored
    assert service.status(root, 'one').by_status['excluded'] == 1


def test_symlink_and_dependency_directory_reported_without_following(index, make_symlink):
    service, root, _ = index
    outside = write(root.parent, 'outside.txt', 'OutsideSentinel')
    make_symlink(root / 'link.txt', outside)
    write(root, 'node_modules/nested.txt', 'DependencySentinel')
    result = service.index(root, 'one')
    assert result.by_status == {'excluded': 2}
    assert not service.search(root, 'one', 'OutsideSentinel')
    assert not service.search(root, 'one', 'DependencySentinel')
    service.index(root, 'one', exclude=())
    assert service.search(root, 'one', 'DependencySentinel')


def test_budget_failure_preserves_previous_complete_index(index):
    service, root, db = index
    write(root, 'a.txt', 'OriginalSentinel')
    service.index(root, 'one')
    write(root, 'b.txt', 'AnotherSentinel')
    limits = replace(service.limits, max_files=1)
    limited = ResourceIndex(service.repository, ResourceExtractor(limits), limits)
    with pytest.raises(ScanIncomplete):
        limited.index(root, 'one')
    assert service.search(root, 'one', 'OriginalSentinel')
    assert not service.search(root, 'one', 'AnotherSentinel')
    assert not db.in_transaction


def test_file_limit_is_explicit(index):
    service, root, _ = index
    write(root, 'big.txt', 'x' * 100)
    limits = replace(service.limits, max_file_bytes=10)
    limited = ResourceIndex(service.repository, ResourceExtractor(limits), limits)
    assert limited.index(root, 'one').by_status == {'too_large': 1}


def test_stale_writer_rejected_and_transaction_rolled_back(index):
    service, root, db = index
    stale = service.repository.root('one', str(root))
    write(root, 'a.txt', 'WinnerSentinel')
    service.index(root, 'one')
    with pytest.raises(IndexConflict):
        service.repository.replace(stale, 'one', str(root), [], set())
    assert service.search(root, 'one', 'WinnerSentinel')
    assert not db.in_transaction


def test_insert_error_rolls_back_whole_replacement(index, monkeypatch):
    service, root, db = index
    write(root, 'a.txt', 'OriginalSentinel')
    service.index(root, 'one')
    write(root, 'a.txt', 'ReplacementSentinel')
    def fail(*args):
        raise RuntimeError('injected storage failure')
    monkeypatch.setattr(service.repository, '_insert', fail)
    with pytest.raises(RuntimeError, match='injected'):
        service.index(root, 'one')
    assert service.search(root, 'one', 'OriginalSentinel')
    assert not service.search(root, 'one', 'ReplacementSentinel')
    assert not db.in_transaction


def test_chunking_preserves_all_text_and_line_spans(index):
    service, root, _ = index
    content = ('SomeLongLine' * 100 + '\n') * 20
    write(root, 'content.unknown_xyz', content)
    service.index(root, 'one')
    rows = service.repository.db.execute("SELECT content,start_line,end_line FROM resource_chunks WHERE kind='text' ORDER BY ordinal").fetchall()
    assert ''.join(row[0] for row in rows) == content
    assert all(len(row[0]) <= service.limits.chunk_chars for row in rows)
    assert rows[0][1] == 1 and rows[-1][2] == 20


def test_office_archive_and_gzip_text(index):
    service, root, _ = index
    with zipfile.ZipFile(root / 'document.docx', 'w') as z:
        z.writestr('word/document.xml', '<document><p>OfficeSentinel</p></document>')
    with zipfile.ZipFile(root / 'bundle.zip', 'w') as z:
        z.writestr('../../escape.txt', 'ArchiveSentinel')
        z.writestr('binary.dat', b'\x00\xff\x80')
    (root / 'logs.gz').write_bytes(gzip.compress(b'GzipSentinel'))
    result = service.index(root, 'one')
    assert result.by_status == {'indexed': 3}
    for query in ['OfficeSentinel', 'ArchiveSentinel', 'GzipSentinel']:
        assert service.search(root, 'one', query)
    assert not (root.parent.parent / 'escape.txt').exists()


def test_tar_links_not_followed(index):
    service, root, _ = index
    with tarfile.open(root / 'archive.tar', 'w') as archive:
        item = tarfile.TarInfo('doc.txt')
        data = b'TarSentinel'
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
        link = tarfile.TarInfo('outside')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        archive.addfile(link)
    service.index(root, 'one')
    assert service.search(root, 'one', 'TarSentinel')
    assert 'unextracted_members=1' in service.status(root, 'one').files[0].detail


def test_zip_bomb_and_xml_entity_are_rejected(index):
    service, root, _ = index
    with zipfile.ZipFile(root / 'bomb.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('big.txt', 'a' * 10000)
    with zipfile.ZipFile(root / 'entity.docx', 'w') as archive:
        archive.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><x>&leak;</x>')
    limits = replace(service.limits, max_expanded_bytes=1000)
    limited = ResourceIndex(service.repository, ResourceExtractor(limits), limits)
    result = limited.index(root, 'one')
    assert result.by_status == {'too_large': 1, 'error': 1}


def test_pdf_text_and_encrypted_status(index):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    service, root, _ = index
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 20 100 Td (PdfSentinel) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    with (root / 'document.pdf').open('wb') as output:
        writer.write(output)
    writer.encrypt('password')
    with (root / 'locked.pdf').open('wb') as output:
        writer.write(output)
    result = service.index(root, 'one')
    assert service.search(root, 'one', 'PdfSentinel')
    assert result.by_status == {'indexed': 1, 'metadata_only': 1}


def test_registry_unavailable_preserves_text(index, monkeypatch):
    import ast_ingest.ingester as ast
    service, root, _ = index
    write(root, 'program.swift', 'func CachedGrammarSentinel() {}')
    def unavailable(language):
        raise RuntimeError('offline grammar')
    monkeypatch.setattr(ast, '_get_parser', unavailable)
    result = service.index(root, 'one')
    assert result.by_status == {'indexed': 1}
    assert 'parser_unavailable' in result.files[0].detail
    assert service.search(root, 'one', 'CachedGrammarSentinel')


def test_status_pagination_and_query_validation(index):
    service, root, _ = index
    for i in range(3):
        write(root, f'file{i}.txt', f'Value{i}')
    service.index(root, 'one')
    first = service.status(root, 'one', limit=2)
    last = service.status(root, 'one', limit=2, offset=2)
    assert len(first.files) == 2 and first.files_truncated
    assert len(last.files) == 1 and not last.files_truncated
    with pytest.raises(ValueError):
        service.search(root, 'one', ' '.join(f'word{i}' for i in range(100)))
    with pytest.raises(ValueError):
        service.index(root, '')


def test_private_redaction_preserves_symbol_lines(index):
    service, root, db = index
    write(root, 'module.py', '<private>\nsecret = 1\n</private>\ndef public():\n    return 42\n')
    service.index(root, 'one')
    row = db.execute("SELECT start_line,end_line,content FROM resource_chunks WHERE name='public'").fetchone()
    assert row[:2] == (4, 5)
    assert 'secret' not in row[2]


def test_archive_sensitive_members_do_not_leak(index):
    service, root, _ = index
    with zipfile.ZipFile(root / 'bundle.zip', 'w') as archive:
        archive.writestr('nested/.env', 'ArchivePrivateSentinel')
        archive.writestr('nested/.env.example', 'PublicExampleSentinel')
    service.index(root, 'one')
    assert not service.search(root, 'one', 'ArchivePrivateSentinel')
    assert service.search(root, 'one', 'PublicExampleSentinel')


def test_read_error_removes_stale_searchable_content(index, monkeypatch):
    import resource_index.service as module
    service, root, _ = index
    write(root, 'notes.unknown', 'OldReadableSentinel')
    service.index(root, 'one')
    original = module.os.open
    def denied(path, flags, *args, **kwargs):
        if Path(path).name == 'notes.unknown':
            raise PermissionError('test denial')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(module.os, 'open', denied)
    result = service.index(root, 'one')
    assert result.by_status == {'error': 1}
    assert not service.search(root, 'one', 'OldReadableSentinel')


def test_traversal_error_keeps_previous_index(index, monkeypatch):
    import resource_index.service as module
    service, root, _ = index
    write(root, 'notes.unknown', 'StillReadableSentinel')
    service.index(root, 'one')
    def denied(*args):
        raise PermissionError('test denial')
    monkeypatch.setattr(module.os, 'scandir', denied)
    with pytest.raises(ScanIncomplete):
        service.index(root, 'one')
    assert service.search(root, 'one', 'StillReadableSentinel')


def test_caller_transaction_is_not_committed(index):
    service, root, db = index
    write(root, 'notes.unknown', 'TransientSentinel')
    db.execute('BEGIN')
    service.index(root, 'one')
    assert db.in_transaction
    db.rollback()
    assert not service.search(root, 'one', 'TransientSentinel')


def test_long_terms_do_not_collide(index):
    service, root, _ = index
    word = 'a' * 200
    write(root, 'notes.unknown', word + 'b')
    service.index(root, 'one')
    assert service.search(root, 'one', word + 'b')
    assert not service.search(root, 'one', word + 'c')


def test_scan_budget_counts_unchanged_file_reads(index):
    service, root, _ = index
    write(root, 'notes.unknown', 'x' * 200)
    service.index(root, 'one')
    limits = replace(service.limits, max_scan_bytes=100)
    limited = ResourceIndex(service.repository, service.extractor, limits)
    with pytest.raises(ScanIncomplete):
        limited.index(root, 'one')
    assert service.status(root, 'one').total_files == 1


def test_pdf_worker_timeout_is_explicit():
    import sys
    from resource_index.pdf_process import run_worker
    result = run_worker([sys.executable, '-c', 'import time; time.sleep(10)'], b'',
                        replace(IndexLimits(), pdf_timeout_seconds=1))
    assert result.status.value == 'error' and result.detail == 'pdf_extraction_timeout'


def test_pdf_worker_failure_is_explicit():
    import sys
    from resource_index.pdf_process import run_worker
    result = run_worker([sys.executable, '-c', 'raise SystemExit(7)'], b'', IndexLimits())
    assert result.status.value == 'error' and result.detail == 'pdf_worker_failed'


def test_pdf_worker_memory_watchdog():
    import sys
    from resource_index.pdf_process import run_worker
    result = run_worker([sys.executable, '-c', 'import time; data=bytearray(32*1024*1024); time.sleep(10)'], b'',
                        replace(IndexLimits(), pdf_memory_bytes=4*1024*1024))
    assert result.status.value == 'too_large' and result.detail == 'pdf_memory_limit'
