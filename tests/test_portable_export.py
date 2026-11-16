"""Encrypted portable bundles: round trip, merge, wrong passphrase, tampering, traversal, manifest checks."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import portable_crypto
import portable_export
from cli_store import open_store
from portable_crypto import BundleCryptoError, DecryptingReader, EncryptingWriter
from portable_export import BundleError

PASSPHRASE = "correct horse battery staple"
FAST_KDF = {"scrypt_n": portable_crypto.SCRYPT_MIN_N}
RECORDS = {
    "alpha": ["Alpha service deploys from the main branch every Tuesday at 10:00 UTC.",
              "Alpha uses PostgreSQL 16 with logical replication to the analytics cluster."],
    "beta": ["Beta frontend is built with Nuxt 4 and served behind Cloudflare."],
}


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setenv("TAM_MEMORY_DIR", str(root))
    return root


def populate(root: Path, records: dict[str, list[str]], *, backdate: str | None = None) -> None:
    with open_store(root) as store:
        store.session_start("seed", project="seed")
        for project, contents in records.items():
            for content in contents:
                record_id, *_ = store.save_knowledge("seed", content, "decision", project=project,
                                                     tags=["seeded"], skip_quality=True)
                if backdate:
                    store.db.execute("UPDATE knowledge SET created_at=?, last_confirmed=? WHERE id=?",
                                     (backdate, backdate, record_id))
                    store.db.commit()


def active(root: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(root / "memory.db") as db:
        return [tuple(row) for row in db.execute(
            "SELECT project, type, content, created_at FROM knowledge WHERE status='active' ORDER BY content")]


def export(root: Path, out: Path, **kwargs) -> dict:
    return portable_export.export_bundle(root, out, PASSPHRASE, **FAST_KDF, **kwargs)


# ─── crypto layer ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("size", [0, 1, 4096, 4097, 3 * 4096 + 5])
def test_stream_round_trip_across_chunk_boundaries(size):
    payload = os.urandom(size)
    sink = io.BytesIO()
    writer = EncryptingWriter(sink, PASSPHRASE, chunk_bytes=portable_crypto.MIN_CHUNK_BYTES, **FAST_KDF)
    writer.write(payload[: size // 2])
    writer.write(payload[size // 2:])
    writer.close()
    reader = DecryptingReader(io.BytesIO(sink.getvalue()), PASSPHRASE)
    assert reader.read() == payload
    reader.finish()


def _sealed(payload: bytes) -> bytes:
    sink = io.BytesIO()
    writer = EncryptingWriter(sink, PASSPHRASE, chunk_bytes=portable_crypto.MIN_CHUNK_BYTES, **FAST_KDF)
    writer.write(payload)
    writer.close()
    return sink.getvalue()


def test_wrong_passphrase_tampering_and_truncation_are_detected():
    sealed = _sealed(os.urandom(10_000))
    with pytest.raises(BundleCryptoError, match="wrong passphrase"):
        DecryptingReader(io.BytesIO(sealed), "not the passphrase").read()
    for position in (len(sealed) // 2, len(sealed) - 1):
        tampered = bytearray(sealed)
        tampered[position] ^= 0x01
        with pytest.raises(BundleCryptoError):
            reader = DecryptingReader(io.BytesIO(bytes(tampered)), PASSPHRASE)
            reader.read()
            reader.finish()
    header_end = len(portable_crypto.MAGIC) + 4
    edited_header = sealed.replace(b'"r":8', b'"r":9', 1)
    assert edited_header != sealed and edited_header[:header_end] == sealed[:header_end]
    with pytest.raises(BundleCryptoError):
        DecryptingReader(io.BytesIO(edited_header), PASSPHRASE).read()
    frame = portable_crypto.MIN_CHUNK_BYTES + portable_crypto.TAG_BYTES + 5
    for cut in (len(sealed) - frame + 3, len(sealed) - 10, 20):
        with pytest.raises(BundleCryptoError, match="truncated|wrong passphrase"):
            reader = DecryptingReader(io.BytesIO(sealed[:cut]), PASSPHRASE)
            reader.read()
            reader.finish()
    with pytest.raises(BundleCryptoError, match="after the final"):
        reader = DecryptingReader(io.BytesIO(sealed + b"x"), PASSPHRASE)
        reader.read()


def test_hostile_kdf_parameters_are_refused():
    sealed = _sealed(b"data")
    start = len(portable_crypto.MAGIC) + 4
    length = int.from_bytes(sealed[len(portable_crypto.MAGIC):start], "big")
    header = json.loads(sealed[start:start + length])
    for kdf in ({"n": 2 ** 20, "r": 32}, {"n": 2 ** 30}, {"n": 3 * 2 ** 14}, {"p": 0}, {"n": "16384"}):
        hostile = json.dumps(dict(header, kdf=dict(header["kdf"], **kdf))).encode()
        crafted = portable_crypto.MAGIC + len(hostile).to_bytes(4, "big") + hostile + sealed[start + length:]
        with pytest.raises(BundleCryptoError, match="scrypt parameters|must be an integer"):
            DecryptingReader(io.BytesIO(crafted), PASSPHRASE)
    oversized = portable_crypto.MAGIC + (portable_crypto.MAX_HEADER_BYTES + 1).to_bytes(4, "big")
    with pytest.raises(BundleCryptoError, match="too large"):
        DecryptingReader(io.BytesIO(oversized), PASSPHRASE)
    with pytest.raises(BundleCryptoError, match="bad magic"):
        DecryptingReader(io.BytesIO(b"PK\x03\x04" + sealed[4:]), PASSPHRASE)


# ─── bundles ────────────────────────────────────────────────────────────────

def test_full_round_trip_replace_into_a_new_machine(memory_dir, tmp_path):
    populate(memory_dir, RECORDS, backdate="2024-02-03T04:05:06.000000Z")
    bundle = tmp_path / "out" / "memory.tambundle"
    summary = export(memory_dir, bundle)
    assert summary["kind"] == "full"
    if sys.platform != "win32":  # NTFS has no POSIX mode bits
        assert bundle.stat().st_mode & 0o777 == 0o600
    assert b"Alpha service" not in bundle.read_bytes()
    assert not list((memory_dir / "backups").glob(".bundle-export-*"))

    target = tmp_path / "target"
    result = portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert result["mode"] == "replace" and result["active_records"] == 3
    assert active(target) == active(memory_dir)
    assert Path(result["safety_backup"]).is_file()
    assert not list((target / "backups").glob(".bundle-import-*"))
    with sqlite3.connect(target / "memory.db") as db:
        assert db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 3


def test_export_includes_wal_committed_data(memory_dir, tmp_path):
    populate(memory_dir, {"alpha": RECORDS["alpha"][:1]})
    writer = sqlite3.connect(memory_dir / "memory.db")
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO knowledge (session_id, type, content, context, project, tags, source, confidence,"
                       " created_at, last_confirmed, recall_count, branch, importance, source_format) VALUES "
                       "('seed', 'fact', 'Only in the WAL so far', '', 'wal', '[]', 'explicit', 1.0, "
                       "'2025-01-01T00:00:00.000000Z', '2025-01-01T00:00:00.000000Z', 0, '', 'medium', 'auto')")
        writer.commit()
        assert (memory_dir / "memory.db-wal").stat().st_size > 0
        bundle = tmp_path / "wal.tambundle"
        export(memory_dir, bundle)
    finally:
        writer.close()
    target = tmp_path / "target"
    portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert ("wal", "fact", "Only in the WAL so far", "2025-01-01T00:00:00.000000Z") in active(target)


def test_replace_keeps_a_safety_backup_of_the_current_store(memory_dir, tmp_path):
    populate(memory_dir, RECORDS)
    bundle = tmp_path / "b.tambundle"
    export(memory_dir, bundle)
    target = tmp_path / "target"
    populate(target, {"local": ["Local-only record that the restore replaces, kept in the backup."]})
    (target / "chroma" / "old-index.bin").write_bytes(b"stale mirror")
    result = portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert [row[0] for row in active(target)] == ["alpha", "alpha", "beta"]
    with sqlite3.connect(result["safety_backup"]) as db:
        assert db.execute("SELECT project FROM knowledge WHERE status='active'").fetchall() == [("local",)]
    assert (Path(result["chroma_backup"]) / "old-index.bin").is_file()
    assert not (target / "chroma" / "old-index.bin").exists()


def test_vectors_from_another_model_are_rebuilt(memory_dir, tmp_path):
    populate(memory_dir, RECORDS)
    with sqlite3.connect(memory_dir / "memory.db") as db:
        db.execute("UPDATE embeddings SET embed_model='some-other-model'")
    bundle = tmp_path / "b.tambundle"
    export(memory_dir, bundle)
    target = tmp_path / "target"
    result = portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert result["vectors_dropped_for_model_change"] == 3
    assert result["reembedded"]["rebuilt"] == 3
    with sqlite3.connect(target / "memory.db") as db:
        models = {row[0] for row in db.execute("SELECT embed_model FROM embeddings")}
    assert "some-other-model" not in models and len(models) == 1


def test_project_bundle_merges_through_the_save_path(memory_dir, tmp_path):
    populate(memory_dir, RECORDS, backdate="2023-07-08T09:10:11.000000Z")
    bundle = tmp_path / "alpha.tambundle"
    summary = export(memory_dir, bundle, project="alpha")
    assert summary["kind"] == "project" and summary["records"] == 2

    target = tmp_path / "target"
    populate(target, {"alpha": [RECORDS["alpha"][0]]})
    result = portable_export.import_bundle(target, bundle, PASSPHRASE)
    assert result["mode"] == "merge" and result["projects"] == ["alpha"]
    assert (result["imported"], result["deduplicated"]) == (1, 1)
    rows = active(target)
    assert [row[2] for row in rows] == RECORDS["alpha"]
    merged = next(row for row in rows if row[2] == RECORDS["alpha"][1])
    assert merged[1] == "decision" and merged[3] == "2023-07-08T09:10:11.000000Z"
    again = portable_export.import_bundle(target, bundle, PASSPHRASE)
    assert (again["imported"], again["already_imported"]) == (0, 2)
    with pytest.raises(BundleError, match="only be merged"):
        portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")


def test_full_bundle_can_be_merged(memory_dir, tmp_path):
    populate(memory_dir, RECORDS)
    bundle = tmp_path / "b.tambundle"
    export(memory_dir, bundle)
    target = tmp_path / "target"
    populate(target, {"gamma": ["Gamma keeps its own record and gains the bundle's records."]})
    result = portable_export.import_bundle(target, bundle, PASSPHRASE, mode="merge")
    assert result["imported"] == 3 and sorted(result["projects"]) == ["alpha", "beta"]
    assert len(active(target)) == 4


@pytest.mark.parametrize("project", [None, "alpha"])
def test_bundle_merge_preserves_archived_deleted_and_superseded_states(memory_dir, tmp_path, project):
    populate(memory_dir, {"alpha": [
        "The historical scheduler ran every morning before the migration.",
        "The current scheduler runs hourly after the migration.",
        "An obsolete diagnostic was archived for future reference.",
        "A mistaken deployment instruction was explicitly deleted.",
    ]})
    with sqlite3.connect(memory_dir / "memory.db") as db:
        ids = [row[0] for row in db.execute("SELECT id FROM knowledge ORDER BY id")]
        db.execute("UPDATE knowledge SET status='superseded', superseded_by=? WHERE id=?", (ids[1], ids[0]))
        db.execute("UPDATE knowledge SET status='archived' WHERE id=?", (ids[2],))
        db.execute("UPDATE knowledge SET status='deleted' WHERE id=?", (ids[3],))
    bundle = tmp_path / "states.tambundle"
    export(memory_dir, bundle, project=project)
    target = tmp_path / "states-target"
    result = portable_export.import_bundle(target, bundle, PASSPHRASE)
    assert result["imported"] == 4
    with sqlite3.connect(target / "memory.db") as db:
        rows = db.execute("SELECT id,status,superseded_by FROM knowledge ORDER BY id").fetchall()
        assert [row[1] for row in rows] == ["superseded", "active", "archived", "deleted"]
        assert rows[0][2] == rows[1][0]
    again = portable_export.import_bundle(target, bundle, PASSPHRASE)
    assert again["already_imported"] == 4 and again["imported"] == 0


def test_snapshot_validation_precedes_destination_writes(tmp_path, monkeypatch):
    from memory_core.export_snapshot import snapshot_lines

    source = tmp_path / "staged"
    source.mkdir()
    row = {"id": 1, "type": "fact", "project": "alpha", "content": "A valid first record.", "status": "active"}
    lines = list(snapshot_lines([row]))
    (source / portable_export.RECORDS_MEMBER).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    staged = portable_export.StagedBundle(source, {"kind": "project"})
    import cli_store

    def forbidden_open(*args, **kwargs):
        raise AssertionError("destination opened before source validation")

    monkeypatch.setattr(cli_store, "open_store", forbidden_open)
    with pytest.raises(BundleError, match="line 3: missing end"):
        portable_export._merge(tmp_path / "destination", staged)


def test_wrong_passphrase_leaves_the_live_store_untouched(memory_dir, tmp_path):
    populate(memory_dir, RECORDS)
    bundle = tmp_path / "b.tambundle"
    export(memory_dir, bundle)
    target = tmp_path / "target"
    populate(target, {"local": ["Local-only record that must survive a failed import attempt."]})
    before = active(target)
    with pytest.raises(BundleCryptoError, match="wrong passphrase"):
        portable_export.import_bundle(target, bundle, "wrong passphrase!!", mode="replace")
    assert active(target) == before
    assert not list((target / "backups").glob("pre-import-*"))


def test_tampered_bundle_is_refused(memory_dir, tmp_path):
    populate(memory_dir, RECORDS)
    bundle = tmp_path / "b.tambundle"
    export(memory_dir, bundle)
    data = bytearray(bundle.read_bytes())
    data[len(data) // 2] ^= 0x40
    bundle.write_bytes(bytes(data))
    target = tmp_path / "target"
    with pytest.raises(BundleCryptoError):
        portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert not (target / "memory.db").exists()


def _craft(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with path.open("wb") as sink:
        writer = EncryptingWriter(sink, PASSPHRASE, **FAST_KDF)
        with tarfile.open(fileobj=writer, mode="w|gz") as archive:
            for info, payload in members:
                archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
        writer.close()


def _regular(name: str, payload: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    return info, payload


@pytest.mark.parametrize("hostile", ["../escape.txt", "/tmp/absolute.txt", "nested/memory.db", "symlink", "hardlink",
                                     "directory", "fifo"])
def test_traversal_links_and_special_members_are_refused(tmp_path, hostile):
    bundle = tmp_path / "evil.tambundle"
    manifest = _regular("manifest.json", b"{}")
    if hostile in ("symlink", "hardlink", "directory", "fifo"):
        info = tarfile.TarInfo("memory.db")
        info.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "directory": tarfile.DIRTYPE,
                     "fifo": tarfile.FIFOTYPE}[hostile]
        info.linkname = "../../etc/passwd"
        member = (info, None)
    else:
        member = _regular(hostile, b"pwned")
    _craft(bundle, [manifest, member])
    target = tmp_path / "target"
    with pytest.raises(BundleError, match="refusing"):
        portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert not (tmp_path / "escape.txt").exists()
    assert not (target / "memory.db").exists()
    assert not list((target / "backups").glob(".bundle-import-*"))


def test_manifest_checksum_and_member_set_are_enforced(memory_dir, tmp_path):
    populate(memory_dir, RECORDS)
    database = tmp_path / "copy.db"
    portable_export.snapshot_database(memory_dir / "memory.db", database)
    payload = database.read_bytes()
    good = {"format": "tam-bundle", "version": 1, "kind": "full", "project": None,
            "members": [{"name": "memory.db", "size": len(payload),
                         "sha256": portable_export._sha256(database)}]}
    cases = {
        "checksum": dict(good, members=[dict(good["members"][0], sha256="0" * 64)]),
        "do not match": dict(good, kind="project", project="alpha"),
        "kind": dict(good, kind="everything"),
        "manifest version": dict(good, version=99),
    }
    for message, manifest in cases.items():
        bundle = tmp_path / f"{message.replace(' ', '-')}.tambundle"
        _craft(bundle, [_regular("manifest.json", json.dumps(manifest).encode()), _regular("memory.db", payload)])
        with pytest.raises(BundleError, match=message):
            portable_export.import_bundle(tmp_path / "target", bundle, PASSPHRASE, mode="replace")
    duplicate = tmp_path / "dup.tambundle"
    _craft(duplicate, [_regular("manifest.json", json.dumps(good).encode()), _regular("memory.db", payload),
                       _regular("memory.db", payload)])
    with pytest.raises(BundleError, match="duplicate"):
        portable_export.import_bundle(tmp_path / "target", duplicate, PASSPHRASE, mode="replace")


def test_corrupt_or_newer_database_is_refused_before_touching_the_store(memory_dir, tmp_path):
    populate(memory_dir, RECORDS)
    database = tmp_path / "copy.db"
    portable_export.snapshot_database(memory_dir / "memory.db", database)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO migrations (version, description, applied_at) VALUES ('999', 'future', 'x')")
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + os.urandom(8192))
    for name, source, message in (("newer", database, "newer than this TAM"), ("corrupt", corrupt, "database")):
        payload = source.read_bytes()
        manifest = {"format": "tam-bundle", "version": 1, "kind": "full", "project": None,
                    "members": [{"name": "memory.db", "size": len(payload), "sha256": portable_export._sha256(source)}]}
        bundle = tmp_path / f"{name}.tambundle"
        _craft(bundle, [_regular("manifest.json", json.dumps(manifest).encode()), _regular("memory.db", payload)])
        target = tmp_path / f"target-{name}"
        with pytest.raises(BundleError, match=message):
            portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
        assert not (target / "memory.db").exists()


def test_export_refuses_overwrite_and_missing_store(memory_dir, tmp_path):
    with pytest.raises(BundleError, match="no memory database"):
        export(memory_dir, tmp_path / "x.tambundle")
    populate(memory_dir, RECORDS)
    existing = tmp_path / "exists.tambundle"
    existing.write_text("keep me", encoding="utf-8")
    with pytest.raises(BundleError, match="already exists"):
        export(memory_dir, existing)
    assert existing.read_text(encoding="utf-8") == "keep me"
    export(memory_dir, existing, force=True)
    assert existing.read_bytes().startswith(portable_crypto.MAGIC)


def test_cli_round_trip_with_env_passphrase_and_key_file(memory_dir, tmp_path, monkeypatch, capsys):
    populate(memory_dir, RECORDS)
    monkeypatch.setattr(portable_crypto, "SCRYPT_N", portable_crypto.SCRYPT_MIN_N)
    monkeypatch.setenv("TAM_BUNDLE_PASSPHRASE", "short")
    bundle = tmp_path / "cli.tambundle"
    assert portable_export.main_export(["--encrypted", str(bundle), "--memory-dir", str(memory_dir)]) == 2
    assert "at least" in capsys.readouterr().err
    monkeypatch.setenv("TAM_BUNDLE_PASSPHRASE", PASSPHRASE)
    assert portable_export.main_export(["--encrypted", str(bundle), "--memory-dir", str(memory_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "full"
    monkeypatch.delenv("TAM_BUNDLE_PASSPHRASE")
    key_file = tmp_path / "key"
    key_file.write_text(PASSPHRASE + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    target = tmp_path / "cli-target"
    assert portable_export.main_import([str(bundle), "--mode", "replace", "--memory-dir", str(target),
                                        "--key-file", str(key_file)]) == 0
    assert json.loads(capsys.readouterr().out)["active_records"] == 3
    key_file.write_text("a different passphrase", encoding="utf-8")
    assert portable_export.main_import([str(bundle), "--memory-dir", str(target), "--key-file", str(key_file)]) == 2
    assert "wrong passphrase" in capsys.readouterr().err


def test_entry_point_routes_bundle_commands():
    import total_agent_memory.server as entry

    assert entry._portable_cli("export") is portable_export.main_export
    assert entry._portable_cli("import-bundle") is portable_export.main_import


def test_replace_restores_over_a_live_store_that_fails_its_integrity_check(memory_dir, tmp_path, monkeypatch):
    populate(memory_dir, RECORDS)
    bundle = tmp_path / "b.tambundle"
    export(memory_dir, bundle)
    target = tmp_path / "target"
    populate(target, {"local": ["Record in the store that is about to be declared corrupt."]})
    real_snapshot = portable_export.snapshot_database

    def corrupt_live(source, destination):
        if Path(source).name == "memory.db" and Path(destination).parent.name == "backups":
            raise BundleError("database integrity check failed: [('page 7 is never used',)]")
        return real_snapshot(source, destination)

    monkeypatch.setattr(portable_export, "snapshot_database", corrupt_live)
    result = portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert [row[0] for row in active(target)] == ["alpha", "alpha", "beta"]
    assert Path(result["safety_backup"]).is_file()
    assert any("integrity check" in warning for warning in result["warnings"])


def test_two_replace_imports_in_one_second_keep_distinct_safety_backups(memory_dir, tmp_path, monkeypatch):
    populate(memory_dir, RECORDS)
    bundle = tmp_path / "b.tambundle"
    export(memory_dir, bundle)
    target = tmp_path / "target"
    populate(target, {"local": ["Original data that the first backup must keep."]})
    monkeypatch.setattr(portable_export, "utc_now", lambda: "2026-01-01T00:00:00.000000Z")
    first = portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    second = portable_export.import_bundle(target, bundle, PASSPHRASE, mode="replace")
    assert first["safety_backup"] != second["safety_backup"]
    with sqlite3.connect(first["safety_backup"]) as db:
        assert db.execute("SELECT project FROM knowledge WHERE status='active'").fetchall() == [("local",)]
    if os.name == "posix":  # Windows protects the profile with ACLs, not mode bits
        assert oct(os.stat(first["safety_backup"]).st_mode & 0o777) == oct(0o600)


def test_deeply_nested_record_line_is_a_bundle_error(tmp_path):
    path = tmp_path / "records.jsonl"
    depth = 200_000
    path.write_text("[" * depth + "]" * depth + "\n", encoding="utf-8")
    with pytest.raises(BundleError, match="nested too deeply"):
        list(portable_export._jsonl_records(path))
