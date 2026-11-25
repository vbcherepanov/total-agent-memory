"""Encrypted, portable memory bundles: `tam export --encrypted` and `tam import-bundle`.

A bundle is a gzip'd tar of flat, regular members — ``manifest.json`` plus
either ``memory.db`` (full export) or ``knowledge.jsonl`` (``--project``
export) — sealed by ``portable_crypto`` (AES-256-GCM, scrypt-derived key).

Export takes a consistent snapshot with SQLite's online backup API, so data
committed to the WAL is included and concurrent writers are never torn. The
embeddings table (TAM's authoritative vector store) travels inside that
snapshot; the Chroma directory is only a derived mirror of it and is not
bundled (its files cannot be snapshotted consistently). On import the mirror
is rebuilt from the restored table, and vectors are re-embedded through the
``memory_rebuild_embeddings`` path when the bundle was made with another
embedding model.

Import decrypts into a private staging directory, refuses any member that is
not one of the expected names or not a regular file (no path traversal,
links or devices), checks every member against the manifest's SHA-256 and the
database with ``PRAGMA integrity_check`` before the live store is touched.
``replace`` keeps a safety backup (``backups/pre-import-*.db`` and the old
Chroma directory) and restores the snapshot with the backup API; ``merge``
feeds every active record through the normal save path (redaction,
de-duplication, embedding), preserving original timestamps. Nothing is ever
unpickled. Stop running TAM servers before ``replace``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from erasure import chroma_write_lock
from importers.records import ImportRecord, parse_timestamp
from memory_core.timestamps import utc_now
from memory_core.export_snapshot import ExportFormatError, read_pages, read_snapshot, record_state, snapshot_header, snapshot_lines
from portable_crypto import BundleCryptoError, DecryptingReader, EncryptingWriter

LOGGER = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
DATABASE_MEMBER = "memory.db"
RECORDS_MEMBER = "knowledge.jsonl"
DATA_MEMBERS = frozenset({DATABASE_MEMBER, RECORDS_MEMBER})
ALLOWED_MEMBERS = DATA_MEMBERS | {MANIFEST_NAME}
BUNDLE_FORMAT = "tam-bundle"
MANIFEST_VERSION = 1
KIND_FULL = "full"
KIND_PROJECT = "project"
MODE_MERGE = "merge"
MODE_REPLACE = "replace"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMBER_BYTES = 1 << 36
MAX_RECORD_LINE_BYTES = 16 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
SQLITE_TIMEOUT_SECONDS = 30
# Pages copied per backup step, so a live writer is never blocked for the whole copy.
BACKUP_PAGES_PER_STEP = 4096
CHROMA_BATCH = 256
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
DEFAULT_PASSPHRASE_ENV = "TAM_BUNDLE_PASSPHRASE"
MIN_PASSPHRASE_CHARS = 12
KNOWLEDGE_TYPES = frozenset({"decision", "fact", "solution", "lesson", "convention", "incident"})
SOURCE_FORMATS = frozenset({"auto", "conversation"})
REQUIRED_TABLES = ("knowledge", "migrations", "sessions")
BUNDLE_IMPORT_KIND = "bundle"
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
RECORD_COLUMNS = ("id", "type", "content", "context", "project", "tags", "created_at", "last_confirmed",
                  "importance", "branch", "source_format", "status", "superseded_by")


class BundleError(ValueError):
    """The bundle or the request is invalid; the live store was not modified."""


# ─── snapshot helpers ───────────────────────────────────────────────────────

def _private_dir(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))


def snapshot_database(source: Path, target: Path) -> None:
    """Consistent copy of ``source`` (WAL included) via the online backup API, as one rollback-journal file."""
    with closing(sqlite3.connect(str(source), timeout=SQLITE_TIMEOUT_SECONDS)) as live, \
            closing(sqlite3.connect(str(target))) as copy:
        live.backup(copy, pages=BACKUP_PAGES_PER_STEP)
        copy.execute("PRAGMA journal_mode=DELETE")
        copy.commit()
    os.chmod(target, PRIVATE_FILE_MODE)
    check_database(target)


def check_database(path: Path) -> str | None:
    """Raise unless ``path`` passes integrity_check and has TAM's core tables; returns its schema version."""
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            verdict = db.execute("PRAGMA integrity_check").fetchall()
            if [tuple(row) for row in verdict] != [("ok",)]:
                raise BundleError(f"database integrity check failed: {verdict[:3]}")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [name for name in REQUIRED_TABLES if name not in tables]
            if missing:
                raise BundleError(f"database is not a TAM store (missing tables: {missing})")
            row = db.execute("SELECT MAX(version) FROM migrations").fetchone()
    except sqlite3.DatabaseError as error:
        raise BundleError(f"bundled database is unreadable: {error}") from error
    return row[0] if row else None


def shipped_schema_version() -> str:
    versions = [path.stem.split("_", 1)[0] for path in MIGRATIONS_DIR.glob("*.sql")]
    if not versions:
        raise RuntimeError(f"bundled SQL migrations are missing: {MIGRATIONS_DIR}")
    return max(versions)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _export_records(snapshot: Path, project: str, target: Path) -> int:
    count = 0
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as db, \
            target.open("w", encoding="utf-8") as out:
        db.row_factory = sqlite3.Row
        # Column names come from the RECORD_COLUMNS constant; the project is a bound parameter.
        rows = [_record_metadata(db, dict(row)) for row in db.execute(f"SELECT {', '.join(RECORD_COLUMNS)} FROM knowledge "
                                               "WHERE project=? ORDER BY id", (project,))]
        count = len(rows)
        for line in snapshot_lines(rows):
            out.write(line + "\n")
    os.chmod(target, PRIVATE_FILE_MODE)
    return count


# ─── passphrase ─────────────────────────────────────────────────────────────

def obtain_passphrase(*, env_name: str, key_file: Path | None, confirm: bool) -> str:
    """Passphrase from a key file, else the environment variable, else an interactive prompt."""
    if key_file is not None:
        mode = key_file.stat().st_mode
        if os.name == "posix" and stat.S_IMODE(mode) & 0o077:
            LOGGER.warning(json.dumps({"event": "bundle_key_file_permissions", "path": str(key_file),
                                       "mode": oct(stat.S_IMODE(mode))}))
            sys.stderr.write(f"warning: {key_file} is readable by other users; chmod 600 it\n")
        passphrase = key_file.read_text(encoding="utf-8").strip()
    elif os.environ.get(env_name):
        passphrase = os.environ[env_name]
    elif sys.stdin.isatty():
        passphrase = getpass.getpass("Bundle passphrase: ")
        if confirm and getpass.getpass("Repeat passphrase: ") != passphrase:
            raise BundleError("passphrases do not match")
    else:
        raise BundleError(f"no passphrase: set {env_name}, pass --key-file, or run in a terminal")
    if not passphrase:
        raise BundleError("empty passphrase")
    if confirm and len(passphrase) < MIN_PASSPHRASE_CHARS:
        raise BundleError(f"passphrase must have at least {MIN_PASSPHRASE_CHARS} characters")
    return passphrase


# ─── export ─────────────────────────────────────────────────────────────────

def _write_member(archive: tarfile.TarFile, name: str, path: Path) -> None:
    info = tarfile.TarInfo(name)
    info.size = path.stat().st_size
    info.mode = PRIVATE_FILE_MODE
    info.mtime = int(time.time())
    info.type = tarfile.REGTYPE
    with path.open("rb") as handle:
        archive.addfile(info, handle)


def write_bundle(out: Path, passphrase: str, members: list[tuple[str, Path]], **crypto: int) -> None:
    """Seal ``members`` (name, staged path) into ``out`` atomically with owner-only permissions."""
    partial = out.with_name(f".{out.name}.{os.getpid()}.partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as sink:
            writer = EncryptingWriter(sink, passphrase, **crypto)
            with tarfile.open(fileobj=writer, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:  # type: ignore[call-overload]
                for name, path in members:
                    _write_member(archive, name, path)
            writer.close()
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(partial, out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def export_bundle(memory_dir: Path, out: Path, passphrase: str, *, project: str | None = None,
                  force: bool = False, **crypto: int) -> dict[str, Any]:
    database = memory_dir / "memory.db"
    if not database.is_file():
        raise BundleError(f"no memory database at {database}")
    if out.exists() and not force:
        raise BundleError(f"{out} already exists (use --force to overwrite)")
    if project is not None and not project.strip():
        raise BundleError("project must be non-empty")
    staging = _private_dir(memory_dir / "backups", ".bundle-export-")
    try:
        snapshot = staging / DATABASE_MEMBER
        snapshot_database(database, snapshot)
        schema = check_database(snapshot)
        records: int | None = None
        header = snapshot_header(row for row in _database_records(snapshot)
                                 if project is None or row.get("project") == project)
        if project is None:
            data = [(DATABASE_MEMBER, snapshot)]
        else:
            jsonl = staging / RECORDS_MEMBER
            records = _export_records(snapshot, project, jsonl)
            snapshot.unlink()
            data = [(RECORDS_MEMBER, jsonl)]
        from version import VERSION

        manifest = {
            "format": BUNDLE_FORMAT, "version": MANIFEST_VERSION,
            "kind": KIND_FULL if project is None else KIND_PROJECT, "project": project,
            "created_at": utc_now(), "tam_version": VERSION, "schema_version": schema,
            "records": records,
            "header": header,
            "end": {"complete": True, "next_cursor": None},
            "members": [{"name": name, "size": path.stat().st_size, "sha256": _sha256(path)} for name, path in data],
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        write_bundle(out, passphrase, [(MANIFEST_NAME, manifest_path), *data], **crypto)
    finally:
        shutil.rmtree(staging, ignore_errors=False)
    return {"exported": True, "file": str(out), "kind": manifest["kind"], "project": project,
            "records": records, "schema_version": schema, "bytes": out.stat().st_size,
            "members": manifest["members"]}


# ─── import: decrypt, extract, validate ─────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StagedBundle:
    directory: Path
    manifest: dict[str, Any]

    @property
    def kind(self) -> str:
        return str(self.manifest["kind"])

    def member(self, name: str) -> Path:
        return self.directory / name


def _copy_member(source: IO[bytes], target: Path, limit: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "wb") as out:
        while chunk := source.read(COPY_CHUNK_BYTES):
            size += len(chunk)
            if size > limit:
                raise BundleError(f"bundle member {target.name} exceeds {limit} bytes")
            digest.update(chunk)
            out.write(chunk)
    return size, digest.hexdigest()


def extract_bundle(bundle: Path, passphrase: str, staging: Path) -> StagedBundle:
    """Decrypt and unpack ``bundle`` into ``staging``, then validate it against its manifest."""
    if not bundle.is_file():
        raise BundleError(f"{bundle} is not a file")
    extracted: dict[str, tuple[int, str]] = {}
    with bundle.open("rb") as source:
        reader = DecryptingReader(source, passphrase)
        try:
            with tarfile.open(fileobj=reader, mode="r|gz") as archive:  # type: ignore[call-overload]
                for member in archive:
                    name = member.name
                    if name not in ALLOWED_MEMBERS:
                        raise BundleError(f"refusing unexpected bundle member {name!r}")
                    if not member.isreg():
                        raise BundleError(f"refusing bundle member {name!r}: not a regular file")
                    if name in extracted:
                        raise BundleError(f"duplicate bundle member {name!r}")
                    limit = MAX_MANIFEST_BYTES if name == MANIFEST_NAME else MAX_MEMBER_BYTES
                    if member.size > limit:
                        raise BundleError(f"bundle member {name!r} is too large ({member.size} bytes)")
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise BundleError(f"bundle member {name!r} has no data")
                    extracted[name] = _copy_member(handle, staging / name, limit)
        except (tarfile.TarError, EOFError, OSError) as error:
            raise BundleError(f"bundle archive is malformed: {error}") from error
        reader.finish()
    manifest = _validate_manifest(staging, extracted)
    return StagedBundle(staging, manifest)


def _validate_manifest(staging: Path, extracted: dict[str, tuple[int, str]]) -> dict[str, Any]:
    if MANIFEST_NAME not in extracted:
        raise BundleError("bundle has no manifest")
    try:
        manifest = json.loads((staging / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise BundleError("bundle manifest is not valid JSON") from error
    if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
        raise BundleError("bundle manifest has the wrong format")
    if manifest.get("version") != MANIFEST_VERSION:
        raise BundleError(f"unsupported bundle manifest version {manifest.get('version')!r}")
    kind = manifest.get("kind")
    expected_member = {KIND_FULL: DATABASE_MEMBER, KIND_PROJECT: RECORDS_MEMBER}.get(kind)
    if expected_member is None:
        raise BundleError(f"unknown bundle kind {kind!r}")
    members = manifest.get("members")
    if not isinstance(members, list):
        raise BundleError("bundle manifest has no member list")
    listed: dict[str, dict[str, Any]] = {}
    for entry in members:
        if not isinstance(entry, dict) or entry.get("name") not in DATA_MEMBERS or entry["name"] in listed:
            raise BundleError(f"bundle manifest lists an invalid member: {entry!r}")
        listed[entry["name"]] = entry
    data_extracted = {name for name in extracted if name != MANIFEST_NAME}
    if set(listed) != data_extracted or set(listed) != {expected_member}:
        raise BundleError(f"bundle members {sorted(data_extracted)} do not match the manifest {sorted(listed)}")
    for name, entry in listed.items():
        size, digest = extracted[name]
        if entry.get("size") != size or entry.get("sha256") != digest:
            raise BundleError(f"bundle member {name} does not match its manifest checksum")
    if kind == KIND_PROJECT and not (isinstance(manifest.get("project"), str) and manifest["project"].strip()):
        raise BundleError("project bundle manifest has no project")
    return manifest


# ─── import: apply ──────────────────────────────────────────────────────────

def _record_metadata(db: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    if row.get("type") == "incident" and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_incidents'").fetchone():
        from memory_core.incidents import IncidentService

        row["incident"] = IncidentService(db).get(row["id"]).model_dump(by_alias=True)
    return row

def _database_records(path: Path) -> Iterator[dict[str, Any]]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute("PRAGMA table_info(knowledge)")}
        # Only names from the RECORD_COLUMNS constant that the bundled table really has.
        selected = [name for name in RECORD_COLUMNS if name in columns]
        query = f"SELECT {', '.join(selected)} FROM knowledge ORDER BY id"
        for row in db.execute(query):
            yield _record_metadata(db, dict(row))


def _jsonl_records(*paths: Path) -> Iterator[dict[str, Any]]:
    """Records of one export file, or of several page files read as one snapshot in the order given."""
    with ExitStack() as stack:
        handles = [stack.enter_context(path.open("rb")) for path in paths]
        try:
            yield from read_snapshot(read_pages(handles), max_line_bytes=MAX_RECORD_LINE_BYTES)
        except ExportFormatError as error:
            raise BundleError(str(error)) from error


def _as_import_record(row: dict[str, Any]) -> ImportRecord:
    if "incident" in row:
        from memory_core.incidents import IncidentSpec

        if row.get("type") != "incident":
            raise BundleError("incident metadata requires type=incident")
        IncidentSpec.model_validate(row["incident"])
    content = row.get("content")
    project = row.get("project")
    if not isinstance(content, str) or not isinstance(project, str) or not project:
        raise BundleError(f"bundle record {row.get('id')!r} has no content or project")
    if type(row.get("id")) is not int or row["id"] < 1:
        raise BundleError("bundle record requires a positive integer id")
    if row.get("status", "active") not in ("active", "archived", "superseded", "deleted"):
        raise BundleError(f"bundle record {row['id']} has an invalid status")
    if row.get("superseded_by") is not None and type(row["superseded_by"]) is not int:
        raise BundleError(f"bundle record {row['id']} has an invalid successor id")
    raw_tags = row.get("tags")
    if isinstance(raw_tags, str):
        try:
            raw_tags = json.loads(raw_tags or "[]")
        except ValueError:
            raw_tags = []
    tags = tuple(tag for tag in raw_tags if isinstance(tag, str)) if isinstance(raw_tags, list) else ()
    knowledge_type = row.get("type") if row.get("type") in KNOWLEDGE_TYPES else "fact"
    source_format = row.get("source_format") if row.get("source_format") in SOURCE_FORMATS else "auto"
    context = row.get("context") if isinstance(row.get("context"), str) else ""
    return ImportRecord(text=content, occurred_at=parse_timestamp(row.get("created_at")),
                        source_ref=f"bundle:{row.get('id')}", context=context, tags=tags,
                        source_format=source_format, knowledge_type=knowledge_type,
                        preserved_identity=(str(row["id"]) + ":" + str(row.get("status", "active"))
                                            if record_state(row) != "active" else None))


def _merge(memory_dir: Path, staged: StagedBundle) -> dict[str, Any]:
    from cli_store import open_store
    from importers.service import ImportService

    rows = (_database_records(staged.member(DATABASE_MEMBER)) if staged.kind == KIND_FULL
            else _jsonl_records(staged.member(RECORDS_MEMBER)))
    # Validate the entire authenticated source before opening the destination store.
    records = [(row, _as_import_record(row)) for row in rows]
    services: dict[str, ImportService] = {}
    identities: dict[int, int] = {}
    inactive: list[tuple[dict[str, Any], int]] = []
    with open_store(memory_dir) as store:
        try:
            for row, record in records:
                project = str(row["project"])
                service = services.get(project)
                if service is None:
                    service = ImportService(kind=BUNDLE_IMPORT_KIND, project=project, store=store,
                                            ledger_db=store.db, dry_run=False, min_chars=1, max_chars=None)
                    services[project] = service
                service.add(record)
                if service.last_record_id is not None:
                    identities[int(row["id"])] = service.last_record_id
                    if row.get("type") == "incident":
                        from memory_core.incidents import IncidentService, IncidentSpec

                        IncidentService(store.db).save(service.last_record_id,
                                                       IncidentSpec.model_validate(row.get("incident", {})), record.text)
                    if record_state(row) != "active":
                        inactive.append((row, service.last_record_id))
                        store.db.execute("UPDATE knowledge SET status=? WHERE id=?",
                                         ("archived" if record_state(row) == "archived" else "deleted",
                                          service.last_record_id))
                        store.db.commit()
            for row, target_id in inactive:
                successor = row.get("superseded_by")
                status = row.get("status", "deleted")
                if status == "active" and successor is not None:
                    status = "superseded"
                store.db.execute("UPDATE knowledge SET status=?, superseded_by=? WHERE id=?",
                                 (status, identities.get(successor), target_id))
            store.db.commit()
        finally:
            for service in services.values():
                service.finish()
    totals = {"imported": 0, "deduplicated": 0, "already_imported": 0, "rejected": 0, "redacted": 0}
    for service in services.values():
        for key in totals:
            totals[key] += getattr(service.stats, key)
    return {"mode": MODE_MERGE, "projects": sorted(services), **totals}


def _vector_revision(db: sqlite3.Connection) -> int:
    try:
        row = db.execute("SELECT revision FROM vector_index_revision WHERE singleton=1").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def _bump_vector_revision(db: sqlite3.Connection, previous: int) -> None:
    """Move the vector revision past both stores' values, so a process holding a vector cache reloads it."""
    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vector_index_revision'"
                           ).fetchone()
    if has_table is None:
        return
    db.execute("UPDATE vector_index_revision SET revision = ? WHERE singleton = 1",
               (max(previous, _vector_revision(db)) + 1,))
    db.commit()


def _rebuild_embeddings(store: Any) -> dict[str, Any]:
    """Re-embed through the memory_rebuild_embeddings tool path, bound to ``store``."""
    import server

    previous = server.store
    server.store = store
    try:
        return json.loads(asyncio.run(server._do("memory_rebuild_embeddings", {})))
    finally:
        server.store = previous


def _mirror_chroma(store: Any, chroma_dir: Path) -> int:
    """Repopulate the Chroma mirror from the embeddings table (only used when binary search is not active)."""
    import struct

    rows = store.db.execute(
        "SELECT e.knowledge_id, e.float32_vector, COALESCE(e.embedding_space, 'text'), k.content, k.type, "
        "k.project, k.session_id, k.created_at FROM embeddings e JOIN knowledge k ON k.id = e.knowledge_id "
        "WHERE k.status = 'active' ORDER BY e.knowledge_id").fetchall()
    batches: dict[str, list[Any]] = {}
    mirrored = 0
    for row in rows:
        batches.setdefault(row[2], []).append(row)
    for space, items in batches.items():
        collection = store._chroma_collection_for(space)
        if collection is None:
            continue
        for start in range(0, len(items), CHROMA_BATCH):
            chunk = items[start:start + CHROMA_BATCH]
            with chroma_write_lock(chroma_dir):
                collection.upsert(
                    ids=[str(row[0]) for row in chunk],
                    embeddings=[list(struct.unpack(f"{len(row[1]) // 4}f", row[1])) for row in chunk],
                    documents=[row[3] for row in chunk],
                    metadatas=[{"type": row[4], "project": row[5], "status": "active", "session_id": row[6] or "",
                                "created_at": row[7], "embedding_space": space} for row in chunk],
                )
            mirrored += len(chunk)
    return mirrored


def _drop_foreign_vectors(staged_db: Path, active_model: str) -> int:
    """Delete text-space vectors made by another model from the staged copy; returns how many were dropped."""
    with closing(sqlite3.connect(str(staged_db))) as db:
        dropped = db.execute("DELETE FROM embeddings WHERE COALESCE(embedding_space, 'text') = 'text' "
                             "AND embed_model IS NOT ?", (active_model,)).rowcount
        db.commit()
    return int(dropped)


SQLITE_SIDE_FILES = ("-wal", "-shm")
BACKUP_TOKEN_BYTES = 4


def _reserve_backup_path(backups: Path, stamp: str) -> tuple[Path, str]:
    """Create (O_EXCL) a unique, owner-only safety-backup file; two imports in one second never share it."""
    while True:
        name = f"pre-import-{stamp}-{secrets.token_hex(BACKUP_TOKEN_BYTES)}"
        path = backups / f"{name}.db"
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE))
        except FileExistsError:
            continue
        return path, name


def _copy_raw_database(live: Path, target: Path) -> None:
    """Byte copy of memory.db and its -wal/-shm side files, for a store SQLite cannot back up."""
    shutil.copyfile(live, target)
    os.chmod(target, PRIVATE_FILE_MODE)
    for suffix in SQLITE_SIDE_FILES:
        side = live.with_name(live.name + suffix)
        if side.is_file():
            copied = target.with_name(target.name + suffix)
            shutil.copyfile(side, copied)
            os.chmod(copied, PRIVATE_FILE_MODE)


def _replace(memory_dir: Path, staged: StagedBundle) -> dict[str, Any]:
    from cli_store import open_store

    if staged.kind != KIND_FULL:
        raise BundleError("a --project bundle can only be merged (use --mode merge)")
    live = memory_dir / "memory.db"
    stamp = utc_now()[:19].replace(":", "").replace("-", "")
    backups = memory_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    with open_store(memory_dir) as store:
        active_model = str(store._active_embed_model_name())
    dropped = _drop_foreign_vectors(staged.member(DATABASE_MEMBER), active_model)
    safety, backup_name = _reserve_backup_path(backups, stamp)
    warnings: list[str] = []
    try:
        snapshot_database(live, safety)
    except (BundleError, sqlite3.DatabaseError) as error:
        # Replacing a corrupt store is exactly when --mode replace is needed: keep the raw files instead.
        LOGGER.warning(json.dumps({"event": "bundle_safety_snapshot_failed", "error": str(error)}))
        _copy_raw_database(live, safety)
        warnings.append(f"live database failed its integrity check ({error}); raw files kept at {safety}")
    with closing(sqlite3.connect(str(staged.member(DATABASE_MEMBER)))) as source, \
            closing(sqlite3.connect(str(live), timeout=SQLITE_TIMEOUT_SECONDS)) as target:
        previous_revision = _vector_revision(target)
        source.backup(target, pages=BACKUP_PAGES_PER_STEP)
        _bump_vector_revision(target, previous_revision)
    chroma = memory_dir / "chroma"
    chroma_backup = None
    if chroma.is_dir() and any(chroma.iterdir()):
        chroma_backup = backups / f"{backup_name}-chroma"
        chroma.rename(chroma_backup)
    chroma.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=True)
    with open_store(memory_dir) as store:
        missing = store.db.execute(
            "SELECT COUNT(*) FROM knowledge k WHERE k.status='active' AND NOT EXISTS "
            "(SELECT 1 FROM embeddings e WHERE e.knowledge_id = k.id)").fetchone()[0]
        rebuilt = _rebuild_embeddings(store) if missing else None
        mirrored = 0
        if store._chroma_client is not None and not store._check_binary_search():
            try:
                mirrored = _mirror_chroma(store, chroma)
            except Exception as error:  # noqa: BLE001 — chromadb raises its own hierarchy; the store is intact
                LOGGER.warning(json.dumps({"event": "bundle_chroma_mirror_failed", "error": str(error)}))
                warnings.append(f"Chroma mirror not rebuilt ({error}); run memory_rebuild_embeddings")
        active = store.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0]
    return {"mode": MODE_REPLACE, "safety_backup": str(safety),
            "chroma_backup": str(chroma_backup) if chroma_backup else None,
            "active_records": active, "vectors_dropped_for_model_change": dropped,
            "reembedded": rebuilt, "chroma_mirrored": mirrored, "warnings": warnings}


def import_bundle(memory_dir: Path, bundle: Path, passphrase: str, *, mode: str = MODE_MERGE) -> dict[str, Any]:
    if mode not in (MODE_MERGE, MODE_REPLACE):
        raise BundleError(f"mode must be {MODE_MERGE} or {MODE_REPLACE}")
    memory_dir.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    staging = _private_dir(memory_dir / "backups", ".bundle-import-")
    try:
        staged = extract_bundle(bundle, passphrase, staging)
        if staged.kind == KIND_FULL:
            schema = check_database(staged.member(DATABASE_MEMBER))
            shipped = shipped_schema_version()
            if schema is not None and schema > shipped:
                raise BundleError(f"bundle schema {schema} is newer than this TAM ({shipped}); upgrade TAM first")
        result = _replace(memory_dir, staged) if mode == MODE_REPLACE else _merge(memory_dir, staged)
        return {"imported": True, "bundle": str(bundle), "kind": staged.kind,
                "created_at": staged.manifest.get("created_at"), "tam_version": staged.manifest.get("tam_version"),
                **result}
    finally:
        shutil.rmtree(staging, ignore_errors=False)


# ─── CLI ────────────────────────────────────────────────────────────────────

def _passphrase_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--passphrase-env", default=DEFAULT_PASSPHRASE_ENV,
                        help=f"environment variable holding the passphrase (default: {DEFAULT_PASSPHRASE_ENV})")
    parser.add_argument("--key-file", type=Path, help="file whose content is the passphrase")
    parser.add_argument("--memory-dir", type=Path, help="memory directory (default: TAM_MEMORY_DIR or ~/.tam)")


def export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tam export",
                                     description="Write an encrypted, portable snapshot of the memory store.")
    parser.add_argument("--encrypted", type=Path, required=True, metavar="OUT", help="bundle file to write")
    parser.add_argument("--project", help="only this project's active records (merge-only bundle)")
    parser.add_argument("--force", action="store_true", help="overwrite OUT if it exists")
    _passphrase_arguments(parser)
    return parser


def import_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tam import-bundle",
                                     description="Restore or merge an encrypted bundle written by `tam export`.")
    parser.add_argument("bundle", type=Path, help="bundle file")
    parser.add_argument("--mode", choices=(MODE_MERGE, MODE_REPLACE), default=MODE_MERGE,
                        help="merge: add records through the normal save path (default); replace: back up the "
                             "current store and restore the bundle's database (stop TAM servers first)")
    _passphrase_arguments(parser)
    return parser


def _run(command: str, action: Any) -> int:
    try:
        result = action()
    except (BundleError, BundleCryptoError, FileNotFoundError) as error:
        sys.stderr.write(f"tam {command}: {error}\n")
        return EXIT_USAGE
    except (OSError, sqlite3.Error) as error:
        LOGGER.error(json.dumps({"event": f"bundle_{command}_failed", "error": str(error)}))
        sys.stderr.write(f"tam {command}: failed: {error}\n")
        return EXIT_FAILURE
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return EXIT_OK


def main_export(argv: list[str] | None = None) -> int:
    args = export_parser().parse_args(argv)
    from cli_store import pin_memory_dir

    root = pin_memory_dir(args.memory_dir)
    return _run("export", lambda: export_bundle(
        root, args.encrypted.expanduser(),
        obtain_passphrase(env_name=args.passphrase_env, key_file=args.key_file, confirm=True),
        project=args.project, force=args.force))


def main_import(argv: list[str] | None = None) -> int:
    args = import_parser().parse_args(argv)
    from cli_store import pin_memory_dir

    root = pin_memory_dir(args.memory_dir)
    return _run("import-bundle", lambda: import_bundle(
        root, args.bundle.expanduser(),
        obtain_passphrase(env_name=args.passphrase_env, key_file=args.key_file, confirm=False),
        mode=args.mode))
