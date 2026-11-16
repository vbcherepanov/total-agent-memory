from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone

from resource_index.models import ChunkSample, FileSummary, IndexConflict, IndexOverview, ResourceFile, RootState, SearchHit, WatchPolicy
from tam_db.contracts import CompatConnection

CAMEL_BOUNDARY = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')
WORDS = re.compile(r'[^\W_]+', re.UNICODE)
TOKEN_CHARS = 128


def terms(text: str) -> set[str]:
    normalized = unicodedata.normalize('NFKC', text)
    split = CAMEL_BOUNDARY.sub(' ', normalized)
    words = {word.casefold() for word in WORDS.findall(normalized + ' ' + split)}
    return {word if len(word) <= TOKEN_CHARS else '#' + identity(word) for word in words}


def identity(*parts: str) -> str:
    return hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()


class IndexRepository:
    def __init__(self, db: CompatConnection):
        self.db = db

    def root(self, project: str, path: str) -> RootState:
        root_id = identity(project, path)
        row = self.db.execute('SELECT revision FROM resource_roots WHERE id=?', (root_id,)).fetchone()
        return RootState(root_id, int(row[0]) if row else 0)

    def fingerprints(self, root_id: str) -> dict[str, tuple[str, str, str, str]]:
        return {row[0]: (row[1], row[2], row[3], row[4]) for row in self.db.execute(
            'SELECT path,digest,fingerprint,status,detail FROM resource_files WHERE root_id=?', (root_id,))}

    def replace(self, root: RootState, project: str, path: str, files: list[ResourceFile],
                unchanged: set[str], watch_policy: WatchPolicy | None = None) -> tuple[int, int]:
        present = {item.path for item in files} | unchanged
        prior = set(self.fingerprints(root.id))
        removed = prior - present
        # A savepoint preserves a caller's transaction and is atomic on both backends.
        self.db.execute('SAVEPOINT resource_index_write')
        try:
            self.db.execute('INSERT INTO resource_roots(id,project,path,revision,updated_at) VALUES(?,?,?,0,?) '
                            'ON CONFLICT(id) DO NOTHING',
                            (root.id, project, path, datetime.now(timezone.utc).isoformat()))
            cursor = self.db.execute('UPDATE resource_roots SET revision=revision+1,updated_at=? '
                                     'WHERE id=? AND revision=?',
                                     (datetime.now(timezone.utc).isoformat(), root.id, root.revision))
            if cursor.rowcount != 1:
                raise IndexConflict('Index changed during scan; retry this request')
            for relative in removed | {item.path for item in files}:
                self._delete_file(identity(root.id, relative))
            for item in files:
                self._insert(root.id, item)
            if watch_policy is not None:
                from resource_index.watcher import WatchRepository
                WatchRepository(self.db).register(root.id, watch_policy.include, watch_policy.exclude, watch_policy.enabled)
            self.db.execute('RELEASE SAVEPOINT resource_index_write')
        except BaseException:
            self.db.execute('ROLLBACK TO SAVEPOINT resource_index_write')
            self.db.execute('RELEASE SAVEPOINT resource_index_write')
            raise
        return root.revision + 1, len(removed)

    def _delete_file(self, file_id: str) -> None:
        # Existing TAM databases intentionally disable foreign-key enforcement.
        self.db.execute('DELETE FROM resource_terms WHERE chunk_id IN '
                        '(SELECT id FROM resource_chunks WHERE file_id=?)', (file_id,))
        self.db.execute('DELETE FROM resource_chunks WHERE file_id=?', (file_id,))
        self.db.execute('DELETE FROM resource_files WHERE id=?', (file_id,))

    def _insert(self, root_id: str, item: ResourceFile) -> None:
        file_id = identity(root_id, item.path)
        self.db.execute('INSERT INTO resource_files '
                        '(id,root_id,path,digest,size_bytes,language,media_type,encoding,status,detail,fingerprint) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (file_id, root_id, item.path, item.digest, item.size_bytes, item.language,
                         item.media_type, item.encoding, item.status.value, item.detail, item.fingerprint))
        for ordinal, fragment in enumerate(item.fragments):
            chunk_id = identity(file_id, str(ordinal))
            self.db.execute('INSERT INTO resource_chunks '
                            '(id,file_id,ordinal,kind,language,name,content,start_line,end_line) VALUES(?,?,?,?,?,?,?,?,?)',
                            (chunk_id, file_id, ordinal, fragment.kind, fragment.language, fragment.name, fragment.content,
                             fragment.start_line, fragment.end_line))
            self.db.executemany('INSERT INTO resource_terms(term,chunk_id) VALUES(?,?)',
                                ((term, chunk_id) for term in sorted(terms(
                                    item.path + ' ' + fragment.name + ' ' + fragment.content))))

    def overview(self, root_id: str, limit: int, offset: int = 0) -> IndexOverview:
        statuses = Counter({r[0]: int(r[1]) for r in self.db.execute(
            'SELECT status,COUNT(*) FROM resource_files WHERE root_id=? GROUP BY status', (root_id,))})
        kinds = {r[0]: int(r[1]) for r in self.db.execute(
            'SELECT c.kind,COUNT(*) FROM resource_chunks c JOIN resource_files f ON f.id=c.file_id '
            'WHERE f.root_id=? GROUP BY c.kind', (root_id,))}
        files = tuple(FileSummary(*row)
                      for row in self.db.execute('SELECT path,status,detail,language,size_bytes,digest '
                          'FROM resource_files WHERE root_id=? ORDER BY path LIMIT ? OFFSET ?',
                          (root_id, limit, offset)))
        return IndexOverview(sum(statuses.values()), sum(kinds.values()), dict(statuses), kinds,
                             files, offset + len(files) < sum(statuses.values()))

    def sample(self, root_id: str, limit: int) -> tuple[ChunkSample, ...]:
        return tuple(ChunkSample(*row) for row in self.db.execute(
            'SELECT f.path,c.language,c.kind,c.name,c.content,c.start_line,c.end_line '
            'FROM resource_chunks c JOIN resource_files f ON f.id=c.file_id WHERE f.root_id=? '
            'ORDER BY f.path,c.ordinal LIMIT ?', (root_id, limit)))

    def search(self, root_id: str, query_terms: set[str], limit: int) -> tuple[SearchHit, ...]:
        if not query_terms:
            return ()
        placeholders = ','.join('?' for _ in query_terms)
        rows = self.db.execute(
            'SELECT f.path,c.language,f.status,c.kind,c.name,c.content,c.start_line,c.end_line,COUNT(*) AS score '
            'FROM resource_terms t JOIN resource_chunks c ON c.id=t.chunk_id '
            'JOIN resource_files f ON f.id=c.file_id '
            f'WHERE f.root_id=? AND t.term IN ({placeholders}) '
            'GROUP BY f.path,c.language,f.status,c.id,c.kind,c.name,c.content,c.start_line,c.end_line '
            'HAVING COUNT(*)=? ORDER BY score DESC,f.path,c.start_line,c.id LIMIT ?',
            (root_id, *sorted(query_terms), len(query_terms), limit))
        return tuple(SearchHit(*row) for row in rows)
