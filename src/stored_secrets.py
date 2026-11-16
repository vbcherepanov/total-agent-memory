"""Find and redact credentials that earlier versions stored before redaction covered every write.

`scan` only reads. `redact` first copies the database to ``backups/pre-redact-<time>.db``
(0600), then rewrites every text cell and raw call log line that `redact_secrets`
changes. FTS indexes follow through their triggers and are rebuilt at the end.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from secret_redaction import redact_secrets, redact_value

# FTS5 shadow tables hold index data, not rows; the virtual tables are rebuilt instead.
SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")
BACKUP_PREFIX = "pre-redact-"
# Schema bookkeeping: rewriting it would break migrations, and it never holds user text.
SYSTEM_TABLES = frozenset(("migrations", "schema_migrations", "schema_version"))
# Chroma's own database: only the tables that hold document text. The rest (collections,
# migrations with their hashes, ...) must stay byte-identical or Chroma refuses to start.
CHROMA_TABLES = frozenset(("embeddings_queue", "embedding_metadata", "embedding_fulltext_search"))


class _Text(bytes):
    """TEXT cells arrive as raw bytes: older rows hold invalid UTF-8 that a str factory refuses."""


SqlValue = str | bytes | int | float | None


@dataclass(frozen=True)
class RowChange:
    key_columns: tuple[str, ...]
    key_values: tuple[SqlValue, ...]
    changes: dict[str, bytes]


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _key_columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    columns = list(db.execute(f'PRAGMA table_info({_identifier(table)})'))
    primary = sorted((row for row in columns if row[5]), key=lambda row: row[5])
    # WITHOUT ROWID keys are always NOT NULL; ordinary nullable keys are not unique for NULL.
    if primary and all(row[3] for row in primary):
        return tuple(_name(row[1]) for row in primary)
    names = {_name(row[1]).casefold() for row in columns}
    for alias in ('rowid', '_rowid_', 'oid'):
        if alias not in names:
            return (alias,)
    raise ValueError(f'Table {table!r} has no unambiguous row key')


def _name(value) -> str:
    return value.decode() if isinstance(value, bytes) else value or ""


def _connect(target: str, **kwargs) -> sqlite3.Connection:
    db = sqlite3.connect(target, **kwargs)
    db.text_factory = _Text
    return db


def _fts_kinds(db: sqlite3.Connection) -> dict[str, str]:
    """FTS5 tables by storage: 'external' (content=table), 'contentless' (content='') or 'own'."""
    kinds = {}
    for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' "
                                "AND sql LIKE 'CREATE VIRTUAL TABLE%USING fts5%'"):
        text = _name(sql).replace(" ", "").lower()
        kinds[_name(name)] = "contentless" if "content=''" in text or 'content=""' in text \
            else "external" if "content=" in text else "own"
    return kinds


def _tables(db: sqlite3.Connection, only: frozenset[str] | None = None) -> list[str]:
    """Row tables plus FTS5 tables that keep their own copy of the text; `only` restricts them."""
    virtual = {_name(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE "
                                                   "'CREATE VIRTUAL TABLE%'")}
    names = [_name(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    shadows = {name for name in names for base in virtual if name.startswith(base) and name[len(base):] in SHADOW_SUFFIXES}
    own_fts = [name for name, kind in _fts_kinds(db).items() if kind == "own"]
    tables = [name for name in names if name not in virtual and name not in shadows] + own_fts
    return [name for name in tables if name not in SYSTEM_TABLES and (only is None or name in only)]


def _text_columns(db: sqlite3.Connection, table: str) -> list[str]:
    columns = [(_name(row[1]), _name(row[2]).upper()) for row in db.execute(f'PRAGMA table_info("{table}")')]
    return [name for name, kind in columns if kind in ("TEXT", "", "JSON") or "CHAR" in kind or "CLOB" in kind]


def _redact_cell(value: str) -> tuple[str, bool]:
    """JSON cells are redacted value by value, so a pattern never swallows a quote or bracket."""
    if value.startswith(("[", "{")):
        try:
            parsed = json.loads(value)
        except ValueError:
            return redact_secrets(value)
        cleaned, changed = redact_value(parsed)
        return (json.dumps(cleaned, ensure_ascii=False), True) if changed else (value, False)
    return redact_secrets(value)


def _dirty_rows(db: sqlite3.Connection, table: str) -> Iterator[RowChange]:
    columns = _text_columns(db, table)
    if not columns:
        return
    keys = _key_columns(db, table)
    select = ", ".join(_identifier(column) for column in (*keys, *columns))
    for row in db.execute(f'SELECT {select} FROM {_identifier(table)}'):
        changes = {}
        for column, value in zip(columns, row[len(keys):], strict=True):
            if isinstance(value, _Text):
                cleaned, changed = _redact_cell(value.decode("utf-8", "surrogateescape"))
                if changed:
                    changes[column] = cleaned.encode("utf-8", "surrogateescape")
        if changes:
            yield RowChange(keys, tuple(row[:len(keys)]), changes)


def _raw_line(line: str) -> str:
    try:
        cleaned, _ = redact_value(json.loads(line))
        return json.dumps(cleaned, ensure_ascii=False)
    except ValueError:
        return redact_secrets(line)[0]


def _dirty_logs(raw_dir: Path):
    if not raw_dir.is_dir():
        return
    for path in sorted(raw_dir.glob("*.jsonl")):
        original = path.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines()
        cleaned = [_raw_line(line) if line.strip() else line for line in lines]
        if any(redact_secrets(line)[1] for line in lines):
            yield path, "\n".join(cleaned) + ("\n" if original.endswith("\n") else "")


def _stores(root: Path) -> list[tuple[str, Path, frozenset[str] | None]]:
    """The SQLite files that hold record text: memory.db and, when Chroma is used, its own database."""
    stores: list[tuple[str, Path, frozenset[str] | None]] = [("", root / "memory.db", None)]
    chroma = root / "chroma" / "chroma.sqlite3"
    if chroma.is_file():
        stores.append(("chroma:", chroma, CHROMA_TABLES))
    return stores


def scan(root: Path) -> dict:
    """Rows per table and raw log files that hold something `redact_secrets` would replace."""
    tables = {}
    for prefix, path, only in _stores(root):
        db = _connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table in _tables(db, only):
                count = sum(1 for _ in _dirty_rows(db, table))
                if count:
                    tables[prefix + table] = count
        finally:
            db.close()
    return {"tables": tables, "rows": sum(tables.values()), "raw_logs": sum(1 for _ in _dirty_logs(root / "raw"))}


def _redact_database(path: Path, backup: Path, only: frozenset[str] | None) -> dict[str, int]:
    db = _connect(str(path), timeout=30)
    try:
        target = sqlite3.connect(backup)
        try:
            db.backup(target)
        finally:
            target.close()
        os.chmod(backup, 0o600)
        db.execute("PRAGMA secure_delete=ON")
        tables = {}
        with db:
            for table in _tables(db, only):
                updates = list(_dirty_rows(db, table))
                for update in updates:
                    assignments = ", ".join(f'{_identifier(column)}=CAST(? AS TEXT)' for column in update.changes)
                    where = ' AND '.join(_identifier(column) + ' IS ' +
                                         ('CAST(? AS TEXT)' if isinstance(value, _Text) else '?')
                                         for column, value in zip(update.key_columns, update.key_values, strict=True))
                    db.execute(f'UPDATE {_identifier(table)} SET {assignments} WHERE {where}',
                               (*update.changes.values(), *update.key_values))
                if updates:
                    tables[table] = len(updates)
            for fts, kind in _fts_kinds(db).items():
                if kind == "external":
                    db.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
                db.execute(f"INSERT INTO {fts}({fts}) VALUES('optimize')")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    return tables


def redact(root: Path) -> dict:
    """Back up each store, then redact every stored credential; returns what changed and the backups."""
    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    tables, backups = {}, []
    for prefix, path, only in _stores(root):
        backup = backup_dir / f"{BACKUP_PREFIX}{stamp}{'-' + prefix.rstrip(':') if prefix else ''}.db"
        tables.update({prefix + table: count for table, count in _redact_database(path, backup, only).items()})
        backups.append(str(backup))
    raw_dir = root / "raw"
    logs = 0
    for path, cleaned in list(_dirty_logs(raw_dir)):
        fd, temp = tempfile.mkstemp(dir=raw_dir, prefix=".redact-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cleaned)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        logs += 1
    return {"tables": tables, "rows": sum(tables.values()), "raw_logs": logs, "backup": backups[0],
            "backups": backups,
            "older_backups": sorted(p.name for p in backup_dir.iterdir()
                                    if p.is_file() and not p.name.startswith(BACKUP_PREFIX))}


def main(argv: list[str]) -> int:
    """`tam redact-existing [--apply]`: report stored credentials, or back up and redact them."""
    import argparse

    from paths import memory_dir

    parser = argparse.ArgumentParser(prog="tam redact-existing",
                                     description="Find credentials stored before 14.6.0 and redact them.")
    parser.add_argument("--apply", action="store_true",
                        help="back up memory.db, then redact; without it nothing is changed")
    parser.add_argument("--memory-dir", type=Path, help="memory directory (default: TAM_MEMORY_DIR or ~/.tam)")
    args = parser.parse_args(argv)
    root = (args.memory_dir or memory_dir()).expanduser()
    db_path = root / "memory.db"
    if not db_path.is_file():
        print(f"No memory.db in {root}", file=sys.stderr)
        return 1
    if not args.apply:
        found = scan(root)
        print(json.dumps(found, indent=2))
        if found["rows"] or found["raw_logs"]:
            print("Run `tam redact-existing --apply` to back up and redact them.", file=sys.stderr)
        return 0
    result = redact(root)
    print(json.dumps(result, indent=2))
    print(f"The backups {', '.join(result['backups'])} still hold the old values: delete them once you have "
          "checked the store.", file=sys.stderr)
    if result["older_backups"]:
        print(f"{len(result['older_backups'])} older backups in {root / 'backups'} predate this run and may hold "
              "credentials too.", file=sys.stderr)
    return 0
