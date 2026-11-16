from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from resource_index.extraction import ResourceExtractor
from resource_index.models import IndexErrorBase, IndexLimits, WatchPolicy
from resource_index.repository import IndexRepository
from resource_index.service import DEFAULT_EXCLUDED, ResourceIndex
from resource_index.watcher import BUSY_TIMEOUT_SECONDS
from tam_db.contracts import CompatConnection


class IndexAction(StrEnum):
    INDEX = 'index'
    PREVIEW = 'preview'
    SEARCH = 'search'
    STATUS = 'status'


@dataclass(frozen=True)
class IndexRequest:
    path: Path
    action: IndexAction = IndexAction.INDEX
    project: str = 'general'
    query: str = ''
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = DEFAULT_EXCLUDED
    sample_limit: int = 5
    limit: int = 10
    offset: int = 0
    watch: bool = True


def execute_isolated(database: Path, request: IndexRequest) -> dict:
    """Run ``execute`` on its own SQLite connection, safe to call from any worker thread.

    The personal Store's connection belongs to the Store owner thread, so work that must
    leave that thread (slow extraction) uses a side connection, as the resource watcher does.
    """
    def open_sqlite() -> sqlite3.Connection:
        db = sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS)
        db.row_factory = sqlite3.Row
        return db

    return execute_on_connection(open_sqlite, request)


def execute_on_connection(open_connection: Callable[[], CompatConnection], request: IndexRequest) -> dict:
    """Run ``execute`` on a connection opened for this call and always closed after it."""
    db = open_connection()
    try:
        result = execute(db, request)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def execute(db: CompatConnection, request: IndexRequest) -> dict:
    limits = IndexLimits.from_env()
    service = ResourceIndex(IndexRepository(db), ResourceExtractor(limits), limits)
    match request.action:
        case IndexAction.INDEX | IndexAction.PREVIEW:
            result = service.index(request.path, request.project, include=request.include,
                                   exclude=request.exclude, preview=request.action == IndexAction.PREVIEW,
                                   sample_limit=request.sample_limit,
                                   watch_policy=WatchPolicy(request.include, request.exclude, request.watch))
            return asdict(result)
        case IndexAction.SEARCH:
            hits = service.search(request.path, request.project, request.query, request.limit)
            return {'path': str(request.path), 'project': request.project,
                    'count': len(hits), 'results': [asdict(hit) for hit in hits]}
        case IndexAction.STATUS:
            from resource_index.watcher import WatchRepository, WatchSettings
            result = asdict(service.status(request.path, request.project, request.limit, request.offset))
            result['watch'] = WatchRepository(db).status(service.repository.root(request.project, str(request.path)).id)
            result['watch']['worker_enabled'] = WatchSettings.from_env().enabled
            return result
        case _:
            raise IndexErrorBase('Unknown resource index action')
