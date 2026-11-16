from __future__ import annotations

import io
import sqlite3
import urllib.error

import pytest

from ingestion import file_watcher, gateway


TEST_URL = "https://example.test/article"


@pytest.fixture
def db(db, tmp_path):
    connection = sqlite3.connect(tmp_path / "ingestion.db")
    db.backup(connection)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


class Response(io.BytesIO):
    def __init__(self, body=b"A durable article about deployment recovery.", content_type="text/plain"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


@pytest.fixture
def inbox_processor(db, tmp_path, monkeypatch):
    monkeypatch.setattr(file_watcher, "MEMORY_DIR", tmp_path / "memory")
    path = tmp_path / "inbox" / "article.url"
    path.parent.mkdir()
    path.write_text(f"[InternetShortcut]\nURL={TEST_URL}\n", encoding="utf-8")
    db_path = db.execute("PRAGMA database_list").fetchone()[2]
    processor = file_watcher.FileProcessor(db_path, path.parent)
    return processor, path


@pytest.mark.parametrize("error", [TimeoutError("timeout"), urllib.error.HTTPError(TEST_URL, 503, "unavailable", {}, None)])
def test_fetch_failure_keeps_file_for_retry(inbox_processor, monkeypatch, error):
    processor, path = inbox_processor

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fail)
    assert processor.process_file(path) is False
    assert path.exists()
    assert list(processor.processed_dir.iterdir()) == []


def test_failed_fetch_retries_and_stores_once(inbox_processor, db, monkeypatch):
    processor, path = inbox_processor
    attempts = []

    def fetch(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise urllib.error.URLError("temporary failure")
        return Response()

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fetch)
    assert processor.process_file(path) is False
    assert processor.process_file(path) is True
    assert db.execute("SELECT count(*) FROM ingest_queue WHERE source='url'").fetchone()[0] == 1
    assert not path.exists()


def test_move_failure_reuses_durable_receipt(inbox_processor, db, monkeypatch):
    processor, path = inbox_processor
    attempts = []

    def fetch(*args, **kwargs):
        attempts.append(1)
        return Response()

    def fail_move(*args, **kwargs):
        raise OSError("destination unavailable")

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fetch)
    with monkeypatch.context() as patch:
        patch.setattr(file_watcher.shutil, "move", fail_move)
        assert processor.process_file(path) is False
    assert processor.process_file(path) is True
    assert len(attempts) == 1
    assert db.execute("SELECT count(*) FROM ingest_queue WHERE source='url'").fetchone()[0] == 1


def test_file_changed_during_download_is_not_moved(inbox_processor, monkeypatch):
    processor, path = inbox_processor

    def fetch(*args, **kwargs):
        path.write_text('https://example.test/new-article', encoding='utf-8')
        return Response()

    monkeypatch.setattr(gateway.urllib.request, 'urlopen', fetch)
    assert processor.process_file(path) is False
    assert path.exists()
    assert list(processor.processed_dir.iterdir()) == []


@pytest.mark.parametrize("url", ["not a URL", "file:///etc/passwd", "https:///missing-host"])
def test_invalid_url_remains_in_inbox(inbox_processor, monkeypatch, url):
    processor, path = inbox_processor
    path.write_text(url, encoding="utf-8")

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid URL must not be fetched")

    monkeypatch.setattr(gateway.urllib.request, "urlopen", unexpected)
    assert processor.process_file(path) is False
    assert path.exists()


@pytest.mark.parametrize("body,media_type", [(b"x" * 33, "text/plain"), (b"binary", "application/octet-stream"), (b"", "text/plain")])
def test_rejected_content_creates_no_receipt(db, monkeypatch, body, media_type):
    monkeypatch.setattr(gateway, "MAX_URL_BYTES", 32)
    monkeypatch.setattr(gateway.urllib.request, "urlopen", lambda *args, **kwargs: Response(body, media_type))
    with pytest.raises(ValueError):
        gateway.IngestGateway(db).ingest_url(TEST_URL)
    assert db.execute("SELECT count(*) FROM ingest_queue").fetchone()[0] == 0


def test_failed_receipt_is_not_reported_as_success(db, monkeypatch):
    service = gateway.IngestGateway(db)
    monkeypatch.setattr(gateway.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    receipt = service.ingest_url(TEST_URL, idempotency_key="same-drop")
    db.execute("UPDATE ingest_queue SET status='error' WHERE id=?", (receipt,))
    db.commit()
    with pytest.raises(ValueError, match="receipt"):
        service.ingest_url(TEST_URL, idempotency_key="same-drop")
