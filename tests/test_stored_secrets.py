"""Credentials stored by earlier versions: scan reads only, redact backs up and cleans rows, FTS and raw logs."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import stored_secrets

# Split in the source so secret scanners do not take it for a real key.
STRIPE = "sk_" + "live_" + "51HxYzAbCdEfGhIjKlMnOpQrStUvWx"
ANTHROPIC = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.fixture
def old_install(tmp_path, monkeypatch):
    """A store written the way 14.5 did: secrets in rows, JSON tags and the raw log."""
    for sub in ("raw", "blobs", "chroma", "backups"):
        (tmp_path / sub).mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    store.db.execute("INSERT INTO knowledge (session_id, type, content, context, project, tags, created_at, "
                     "last_confirmed) VALUES ('s','fact',?,?, 'p', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                     (f"Stripe key is {STRIPE} for billing", "set password=Hunter2Secret! yesterday",
                      json.dumps(["billing", f"token={ANTHROPIC}"])))
    store.db.execute("INSERT INTO knowledge (session_id, type, content, created_at, last_confirmed) "
                     "VALUES ('s','fact','Plain note about CI caching', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    store.db.commit()
    store.db.close()
    (tmp_path / "raw" / "s.jsonl").write_text(
        json.dumps({"type": "tool_call", "args": {"content": f"use {ANTHROPIC}"}}) + "\n"
        + json.dumps({"type": "tool_call", "args": {"content": "harmless"}}) + "\n", encoding="utf-8")
    (tmp_path / "raw" / "clean.jsonl").write_text(json.dumps({"args": {"q": "nothing"}}) + "\n", encoding="utf-8")
    return tmp_path


def test_scan_counts_without_changing_anything(old_install):
    before = (old_install / "memory.db").read_bytes()
    result = stored_secrets.scan(old_install)
    assert result["tables"]["knowledge"] == 1
    assert result["raw_logs"] == 1
    assert (old_install / "memory.db").read_bytes() == before
    assert ANTHROPIC in (old_install / "raw" / "s.jsonl").read_text(encoding="utf-8")


def test_without_rowid_composite_keys_are_scanned_and_redacted(old_install):
    with sqlite3.connect(old_install / 'memory.db') as db:
        db.execute('INSERT INTO resource_terms(term,chunk_id) VALUES(?,?)', (ANTHROPIC, 'first'))
        db.execute('INSERT INTO resource_terms(term,chunk_id) VALUES(?,?)', (ANTHROPIC, 'second'))
        db.execute('INSERT INTO resource_terms(term,chunk_id) VALUES(?,?)', ('plain', 'second'))
    assert stored_secrets.scan(old_install)['tables']['resource_terms'] == 2
    result = stored_secrets.redact(old_install)
    assert result['tables']['resource_terms'] == 2
    with sqlite3.connect(old_install / 'memory.db') as db:
        assert db.execute('SELECT term,chunk_id FROM resource_terms ORDER BY chunk_id,term').fetchall() == [
            ('[REDACTED]', 'first'), ('[REDACTED]', 'second'), ('plain', 'second')]
    assert stored_secrets.scan(old_install)['rows'] == 0


def test_nullable_composite_keys_use_distinct_rowids(old_install):
    with sqlite3.connect(old_install / 'memory.db') as db:
        db.execute('CREATE TABLE nullable_keys(a TEXT,b TEXT,content TEXT,PRIMARY KEY(a,b))')
        db.executemany('INSERT INTO nullable_keys VALUES(NULL,NULL,?)', [(ANTHROPIC,), ('safe',)])
    stored_secrets.redact(old_install)
    with sqlite3.connect(old_install / 'memory.db') as db:
        assert db.execute('SELECT content FROM nullable_keys ORDER BY rowid').fetchall() == [('[REDACTED]',), ('safe',)]


def test_without_rowid_preserves_invalid_utf8_in_text_keys(old_install):
    raw = b'\xff prefix ' + ANTHROPIC.encode()
    with sqlite3.connect(old_install / 'memory.db') as db:
        db.execute('INSERT INTO resource_terms VALUES(CAST(? AS TEXT),?)', (raw, 'legacy'))
    stored_secrets.redact(old_install)
    with sqlite3.connect(old_install / 'memory.db') as db:
        db.text_factory = bytes
        assert db.execute('SELECT term,typeof(term) FROM resource_terms WHERE chunk_id=?', ('legacy',)).fetchone() == (
            b'\xff prefix [REDACTED]', b'text')


def test_redaction_key_collision_rolls_back_and_retains_backup(old_install):
    with sqlite3.connect(old_install / 'memory.db') as db:
        db.executemany('INSERT INTO resource_terms VALUES(?,?)', [(ANTHROPIC, 'same'), (STRIPE, 'same')])
    with pytest.raises(sqlite3.IntegrityError):
        stored_secrets.redact(old_install)
    with sqlite3.connect(old_install / 'memory.db') as db:
        assert {row[0] for row in db.execute('SELECT term FROM resource_terms')} == {ANTHROPIC, STRIPE}
        assert ANTHROPIC in db.execute("SELECT tags FROM knowledge WHERE project='p'").fetchone()[0]
    assert list((old_install / 'backups').glob('pre-redact-*.db'))


def test_redact_cleans_rows_json_fts_and_logs_after_a_private_backup(old_install):
    result = stored_secrets.redact(old_install)
    assert result["tables"]["knowledge"] == 1 and result["raw_logs"] == 1
    backup = Path(result["backup"])
    if sys.platform != "win32":  # NTFS has no POSIX modes; the profile ACL protects the file
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert STRIPE.encode() in backup.read_bytes()

    db = sqlite3.connect(old_install / "memory.db")
    content, context, tags = db.execute("SELECT content, context, tags FROM knowledge WHERE project='p'").fetchone()
    assert content == "Stripe key is [REDACTED] for billing"
    assert "Hunter2Secret" not in context
    assert json.loads(tags) == ["billing", "[REDACTED]"]
    assert db.execute("SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'Hunter2Secret'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'billing'").fetchone()[0] == 1
    assert db.execute("SELECT content FROM knowledge WHERE content LIKE 'Plain%'").fetchone()[0] == \
        "Plain note about CI caching"
    db.close()

    log = (old_install / "raw" / "s.jsonl").read_text(encoding="utf-8")
    assert ANTHROPIC not in log and "harmless" in log
    assert json.loads(log.splitlines()[0])["args"]["content"] == "use [REDACTED]"
    for path in old_install.glob("memory.db*"):
        assert STRIPE.encode() not in path.read_bytes(), path.name
    assert stored_secrets.scan(old_install) == {"tables": {}, "rows": 0, "raw_logs": 0}


def test_invalid_utf8_cells_are_redacted_and_their_other_bytes_kept(old_install):
    broken = b"\xff\xfe legacy bytes; token=" + ANTHROPIC.encode() + b" tail \xc3"
    db = sqlite3.connect(old_install / "memory.db")
    db.execute("INSERT INTO knowledge (session_id, type, content, project, created_at, last_confirmed) "
               "VALUES ('s','fact', CAST(? AS TEXT), 'legacy', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
               (broken,))
    db.commit()
    db.close()
    assert stored_secrets.scan(old_install)["tables"]["knowledge"] == 2
    stored_secrets.redact(old_install)
    db = sqlite3.connect(old_install / "memory.db")
    db.text_factory = bytes
    stored, kind = db.execute("SELECT content, typeof(content) FROM knowledge WHERE project='legacy'").fetchone()
    assert kind == b"text"
    assert stored == b"\xff\xfe legacy bytes; [REDACTED] tail \xc3"


def test_cli_scans_by_default_and_redacts_only_with_apply(old_install, capsys):
    assert stored_secrets.main(["--memory-dir", str(old_install)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 1
    assert not list((old_install / "backups").glob("pre-redact-*"))
    assert stored_secrets.main(["--memory-dir", str(old_install), "--apply"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["rows"] == 1
    assert "still hold the old values" in captured.err
    assert stored_secrets.main(["--memory-dir", str(old_install)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 0


def test_cli_without_a_store_fails(tmp_path, capsys):
    assert stored_secrets.main(["--memory-dir", str(tmp_path)]) == 1
    assert "No memory.db" in capsys.readouterr().err


def test_chroma_copies_of_documents_are_found_and_redacted(old_install):
    chromadb = pytest.importorskip("chromadb")
    client = chromadb.PersistentClient(path=str(old_install / "chroma"))
    collection = client.get_or_create_collection("knowledge")
    collection.upsert(ids=["1"], embeddings=[[0.1] * 8], documents=[f"Stripe key is {STRIPE} for billing"])
    del client
    found = stored_secrets.scan(old_install)
    assert any(table.startswith("chroma:") for table in found["tables"]), found
    result = stored_secrets.redact(old_install)
    assert len(result["backups"]) == 2 and result["backups"][1].endswith("-chroma.db")
    for path in (old_install / "chroma").glob("chroma.sqlite3*"):
        assert STRIPE.lower().encode() not in path.read_bytes().lower(), path.name
    for path in old_install.glob("memory.db*"):
        assert STRIPE.lower().encode() not in path.read_bytes().lower(), path.name
    assert not any(table.startswith("chroma:") for table in stored_secrets.scan(old_install)["tables"])


def test_chroma_opens_and_serves_after_redaction(old_install):
    chromadb = pytest.importorskip("chromadb")
    client = chromadb.PersistentClient(path=str(old_install / "chroma"))
    client.get_or_create_collection("knowledge").upsert(
        ids=["7"], embeddings=[[0.2] * 8], documents=[f"deploy token={ANTHROPIC} rotated"])
    del client
    stored_secrets.redact(old_install)
    reopened = chromadb.PersistentClient(path=str(old_install / "chroma")).get_or_create_collection("knowledge")
    assert reopened.count() == 1
    assert reopened.get(ids=["7"])["documents"] == ["deploy [REDACTED] rotated"]
    assert reopened.query(query_embeddings=[[0.2] * 8], n_results=1)["ids"] == [["7"]]
