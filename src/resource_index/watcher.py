from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from filelock import FileLock, Timeout

from memory_core.telemetry import counters, op_timer
from resource_index.extraction import ResourceExtractor
from resource_index.models import IndexErrorBase, IndexLimits, ScanIncomplete
from resource_index.repository import IndexRepository
from resource_index.service import ResourceIndex
from tam_db.contracts import CompatConnection

LOGGER = logging.getLogger(__name__)
BUSY_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class WatchSettings:
    enabled: bool = True
    interval_seconds: float = 5
    reconcile_seconds: float = 300

    def __post_init__(self) -> None:
        import math
        for name in ('interval_seconds', 'reconcile_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise IndexErrorBase(f'TAM_INDEX_WATCH_{name.upper()} must be positive and finite')

    @classmethod
    def from_env(cls) -> WatchSettings:
        enabled = os.environ.get('TAM_INDEX_WATCH_ENABLED', 'true').lower()
        if enabled not in {'true', 'false'}:
            raise IndexErrorBase('TAM_INDEX_WATCH_ENABLED must be true or false')
        defaults = cls()
        return cls(enabled == 'true', float(os.environ.get('TAM_INDEX_WATCH_INTERVAL_SECONDS', defaults.interval_seconds)),
                   float(os.environ.get('TAM_INDEX_WATCH_RECONCILE_SECONDS', defaults.reconcile_seconds)))


@dataclass(frozen=True)
class WatchRoot:
    root_id: str
    revision: int
    project: str
    path: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    signature: str
    checked_at: float
    error: str


class WatchRepository:
    def __init__(self, db: CompatConnection):
        self.db = db

    def register(self, root_id: str, include: tuple[str, ...], exclude: tuple[str, ...], enabled: bool) -> None:
        self.db.execute('INSERT INTO resource_watches(root_id,include_json,exclude_json,enabled) VALUES(?,?,?,?) '
                        'ON CONFLICT(root_id) DO UPDATE SET include_json=excluded.include_json,'
                        "exclude_json=excluded.exclude_json,enabled=excluded.enabled,signature='',checked_at=0,error=''",
                        (root_id, json.dumps(include), json.dumps(exclude), int(enabled)))

    def roots(self) -> tuple[WatchRoot, ...]:
        rows = self.db.execute('SELECT r.id,r.revision,r.project,r.path,w.include_json,w.exclude_json,w.signature,w.checked_at,w.error '
                               'FROM resource_watches w JOIN resource_roots r ON r.id=w.root_id WHERE w.enabled=1')
        return tuple(WatchRoot(row[0], int(row[1]), row[2], row[3], self._patterns(row[4]), self._patterns(row[5]),
                               row[6], float(row[7]), row[8]) for row in rows)

    @staticmethod
    def _patterns(raw: str) -> tuple[str, ...]:
        value = json.loads(raw)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise IndexErrorBase('Invalid persisted resource watch patterns')
        return tuple(value)

    def updated(self, root: WatchRoot, signature: str, checked_at: float, error: str = '') -> None:
        self.db.execute('UPDATE resource_watches SET signature=?,checked_at=?,error=? '
                        'WHERE root_id=? AND include_json=? AND exclude_json=? AND enabled=1',
                        (signature, checked_at, error, root.root_id, json.dumps(root.include), json.dumps(root.exclude)))

    def status(self, root_id: str) -> dict[str, bool | float | str]:
        row = self.db.execute('SELECT enabled,checked_at,error FROM resource_watches WHERE root_id=?', (root_id,)).fetchone()
        return {'enabled': bool(row and row[0]), 'checked_at': float(row[1]) if row else 0,
                'error': row[2] if row else ''}


class ResourceWatcher:
    def __init__(self, database: Path, allowed_path: Callable[[str], Path], settings: WatchSettings,
                 limits: IndexLimits):
        self.database = database
        self.allowed_path = allowed_path
        self.settings = settings
        self.limits = limits
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='tam-resource-watch', daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                counters.bump('resource_watch_failures')
                LOGGER.error(json.dumps({'event': 'resource_watch_failure', 'error_type': type(exc).__name__}))
            self._stop.wait(self.settings.interval_seconds)

    @op_timer('resource_watch_ms')
    def tick(self) -> int:
        counters.bump('resource_watch_calls')
        lock = FileLock(str(self.database) + '.resource-watch.lock', timeout=0)
        try:
            lock.acquire()
        except Timeout:
            counters.bump('resource_watch_contended')
            return 0
        try:
            db = sqlite3.connect(self.database, timeout=BUSY_TIMEOUT_SECONDS)
            try:
                service = ResourceIndex(IndexRepository(db), ResourceExtractor(self.limits, self._stop.is_set), self.limits)
                repository = WatchRepository(db)
                return sum(self._refresh(service, repository, root) for root in repository.roots()
                           if not self._stop.is_set())
            finally:
                db.close()
        finally:
            lock.release()

    def _refresh(self, service: ResourceIndex, repository: WatchRepository, root: WatchRoot) -> int:
        try:
            path = self.allowed_path(root.path)
            if str(path) != root.path or not path.exists():
                raise ScanIncomplete('Watched root is missing or changed')
            before = self._signature(service, path, root.exclude)
            now = time.time()
            if before == root.signature and not root.error and now - root.checked_at < self.settings.reconcile_seconds:
                return 0
            service.index(path, root.project, include=root.include, exclude=root.exclude,
                          sample_limit=0, cancelled=self._stop.is_set, expected_revision=root.revision)
            # Store the pre-scan signature so a concurrent file edit is retried next poll.
            repository.updated(root, before, now)
            repository.db.commit()
            counters.bump('resource_watch_updates')
            return 1
        except Exception as exc:
            repository.db.rollback()
            if self._stop.is_set():
                return 0
            error = type(exc).__name__
            if root.error != error:
                repository.updated(root, root.signature, root.checked_at, error)
                repository.db.commit()
            counters.bump('resource_watch_failures')
            LOGGER.error(json.dumps({'event': 'resource_watch_root_failure', 'root_id': root.root_id, 'error_type': error}))
            return 0

    def _signature(self, service: ResourceIndex, root: Path, exclude: tuple[str, ...]) -> str:
        rows: list[str] = []
        for count, (path, relative, ignored) in enumerate(service._walk(root, exclude), 1):
            if self._stop.is_set() or count > self.limits.max_files:
                raise ScanIncomplete('Watch scan cancelled or file limit exceeded')
            info = path.lstat()
            rows.append(json.dumps((relative, ignored, info.st_mode, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns)))
        return hashlib.sha256('\n'.join(sorted(rows)).encode('utf-8')).hexdigest()
