from __future__ import annotations

import fnmatch
import hashlib
import json
import mimetypes
import os
import stat
from importlib.metadata import PackageNotFoundError, version
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterator

from memory_core.telemetry import counters, op_timer
from resource_index.extraction import ResourceExtractor
from resource_index.models import (
    ChunkSample, FileStatus, FileSummary, Fragment, IndexConflict, IndexErrorBase, IndexLimits,
    IndexOverview, IndexResult, ResourceFile, ScanIncomplete, SearchHit, WatchPolicy,
)
from resource_index.repository import IndexRepository, identity, terms
from resource_index.privacy import sanitize
from resource_index.policy import sensitive

PIPELINE_VERSION = 'resource-index-2'
DEFAULT_EXCLUDED = ('.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__')
MAX_LABEL_CHARS = 512
MAX_QUERY_CHARS = 4096


def extraction_versions() -> tuple[str, ...]:
    from resource_index.media import unavailable
    packages = ('tree-sitter-language-pack', 'pypdf', 'charset-normalizer',
                'Pillow', 'pypdfium2', 'faster-whisper', 'striprtf', 'xlrd')
    versions: list[str] = []
    for package in packages:
        try:
            versions.append(package + ':' + version(package))
        except PackageNotFoundError:
            versions.append(package + ':unavailable')
    return (*versions, *(kind + ':' + (unavailable(kind) or 'ready') for kind in ('image', 'pdf', 'av', 'rtf', 'xls')))


def fragments(text: str, size: int, *, kind: str = 'text', name: str = '', first_line: int = 1,
              language: str = '') -> Iterator[Fragment]:
    start = 0
    line = first_line
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            newline = text.rfind('\n', start, end)
            if newline >= start:
                end = newline + 1
        part = text[start:end]
        end_line = line + part.count('\n') - int(part.endswith('\n'))
        yield Fragment(kind, sanitize(name)[:MAX_LABEL_CHARS], part, line, max(line, end_line), language)
        line += part.count('\n')
        start = end


class ResourceIndex:
    def __init__(self, repository: IndexRepository, extractor: ResourceExtractor, limits: IndexLimits):
        self.repository = repository
        self.extractor = extractor
        self.limits = limits

    @op_timer('resource_index_ms')
    def index(self, path: Path, project: str, *, include: tuple[str, ...] = (),
              exclude: tuple[str, ...] = DEFAULT_EXCLUDED, preview: bool = False,
              sample_limit: int = 5, cancelled: Callable[[], bool] | None = None,
              watch_policy: WatchPolicy | None = None, expected_revision: int | None = None) -> IndexResult:
        counters.bump('resource_index_calls')
        self._validate(project, sample_limit)
        path = path.resolve()
        if not path.exists():
            raise IndexErrorBase('Index root does not exist')
        root = self.repository.root(project, str(path))
        if expected_revision is not None and root.revision != expected_revision:
            raise IndexConflict('Index policy changed before scan; retry this request')
        previous = {} if preview else self.repository.fingerprints(root.id)
        fingerprint = identity(PIPELINE_VERSION, json.dumps(asdict(self.limits), sort_keys=True),
                               json.dumps(asdict(self.extractor.media_settings), sort_keys=True),
                               json.dumps(sorted(include)), json.dumps(sorted(exclude)),
                               *extraction_versions())
        changed: list[ResourceFile] = []
        unchanged: set[str] = set()
        scanned_bytes = 0
        for count, (file_path, relative, excluded) in enumerate(self._walk(path, exclude), 1):
            if cancelled is not None and cancelled():
                raise ScanIncomplete('Scan cancelled; index was not changed')
            if count > self.limits.max_files:
                raise ScanIncomplete('File count exceeds TAM_INDEX_MAX_FILES; index was not changed')
            if include and file_path.suffix.lower() not in include:
                excluded = 'extension_filter'
            item = self._read(file_path, relative, fingerprint, excluded, previous.get(relative))
            resource, bytes_read = item
            scanned_bytes += bytes_read
            if scanned_bytes > self.limits.max_scan_bytes:
                raise ScanIncomplete('Scan exceeds TAM_INDEX_MAX_SCAN_BYTES; index was not changed')
            if resource is None:
                unchanged.add(relative)
                continue
            changed.append(resource)
        if cancelled is not None and cancelled():
            raise ScanIncomplete('Scan cancelled; index was not changed')
        if preview:
            overview = self._preview_overview(changed)
            sample = tuple(ChunkSample(f.path, c.language, c.kind, c.name, c.content, c.start_line, c.end_line)
                           for f in changed for c in f.fragments)[:sample_limit]
            revision, removed = root.revision, 0
        else:
            revision, removed = self.repository.replace(root, project, str(path), changed, unchanged, watch_policy)
            overview = self.repository.overview(root.id, self.limits.max_results)
            sample = self.repository.sample(root.id, sample_limit)
        counters.bump('resource_index_changed_files', len(changed))
        counters.bump('resource_index_unchanged_files', len(unchanged))
        return IndexResult(root.id, str(path), project, revision, not preview, overview.total_files,
                           overview.total_chunks, len(changed), len(unchanged), removed,
                           overview.by_status, overview.by_kind, sample, overview.files, overview.files_truncated)

    @op_timer('resource_search_ms')
    def search(self, path: Path, project: str, query: str, limit: int = 10) -> tuple[SearchHit, ...]:
        counters.bump('resource_search_calls')
        self._validate(project, limit)
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise IndexErrorBase(f'query must be a string of at most {MAX_QUERY_CHARS} characters')
        query_terms = terms(query)
        if len(query_terms) > self.limits.max_query_terms:
            raise IndexErrorBase('Query has too many distinct terms')
        root = self.repository.root(project, str(path))
        return self.repository.search(root.id, query_terms, limit)

    def status(self, path: Path, project: str, limit: int = 100, offset: int = 0) -> IndexOverview:
        self._validate(project, limit)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise IndexErrorBase('offset must be a nonnegative integer')
        return self.repository.overview(self.repository.root(project, str(path)).id, limit, offset)

    def _validate(self, project: str, limit: int) -> None:
        if not isinstance(project, str) or not project.strip() or not project.isprintable() or len(project) > MAX_LABEL_CHARS:
            raise IndexErrorBase('project must be a nonempty bounded string')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= self.limits.max_results:
            raise IndexErrorBase(f'limit must be between 0 and {self.limits.max_results}')

    def _walk(self, root: Path, excluded: tuple[str, ...]) -> Iterator[tuple[Path, str, str]]:
        if not root.is_dir():
            yield root, root.name, ''
            return
        pending = [root]
        try:
            while pending:
                directory = pending.pop()
                if directory.is_symlink() or not directory.resolve().is_relative_to(root):
                    raise ScanIncomplete('Directory changed during traversal; index was not changed')
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        relative = path.relative_to(root).as_posix()
                        if any(fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(relative, pattern)
                               for pattern in excluded):
                            yield path, relative, 'excluded_by_policy'
                        elif entry.is_symlink():
                            yield path, relative, 'symlink_not_followed'
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(path)
                        else:
                            yield path, relative, ''
        except OSError as exc:
            raise ScanIncomplete(f'Directory traversal failed: {type(exc).__name__}; index was not changed') from exc

    def _read(self, path: Path, relative: str, fingerprint: str, excluded: str,
              previous: tuple[str, str, str, str] | None) -> tuple[ResourceFile | None, int]:
        media = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        bytes_read = 0
        def record(status: FileStatus, detail: str, size: int = 0, digest: str = '',
                   language: str = '', encoding: str = '', chunks: tuple[Fragment, ...] = ()) -> tuple[ResourceFile, int]:
            # A searchable metadata fragment also represents non-text and excluded resources.
            metadata = Fragment('metadata', sanitize(relative), sanitize(relative + '\n' + media + '\n' + detail), 0, 0)
            return ResourceFile(relative, digest, size, language, media, encoding, status, detail,
                                fingerprint, (metadata, *chunks)), bytes_read
        if excluded:
            return record(FileStatus.EXCLUDED, excluded)
        if sensitive(path.name):
            return record(FileStatus.EXCLUDED, 'sensitive_file')
        try:
            if path.resolve() != path.absolute():
                return record(FileStatus.EXCLUDED, 'symlink_not_followed')
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                return record(FileStatus.EXCLUDED, 'not_a_regular_file')
            if before.st_size > self.limits.max_file_bytes:
                return record(FileStatus.TOO_LARGE, 'file_exceeds_limit', before.st_size)
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or not stat.S_ISREG(opened.st_mode):
                    return record(FileStatus.ERROR, 'file_changed_during_open')
                data = stream.read(self.limits.max_file_bytes + 1)
                bytes_read = len(data)
                after = os.fstat(stream.fileno())
            if len(data) > self.limits.max_file_bytes:
                return record(FileStatus.TOO_LARGE, 'file_exceeds_limit', len(data))
            if (opened.st_mtime_ns, opened.st_size) != (after.st_mtime_ns, after.st_size):
                return record(FileStatus.ERROR, 'file_changed_during_read')
        except OSError as exc:
            return record(FileStatus.ERROR, f'read_failed:{type(exc).__name__}')
        digest = hashlib.sha256(data).hexdigest()
        if (previous and previous[:2] == (digest, fingerprint) and previous[2] == FileStatus.INDEXED.value
                and 'parser_unavailable' not in previous[3] and not previous[3].startswith('text_fallback:')):
            return None, bytes_read
        extracted = self.extractor.extract(relative, data)
        text = sanitize(extracted.text)
        chunks = list(fragments(text, self.limits.chunk_chars, language=extracted.language))
        for symbol in extracted.fragments:
            if symbol.kind == 'file':
                continue
            chunks.extend(fragments(sanitize(symbol.content), self.limits.chunk_chars,
                                    kind=symbol.kind, name=symbol.name, first_line=symbol.start_line,
                                    language=symbol.language))
        return record(extracted.status, extracted.detail, len(data), digest, extracted.language,
                      extracted.encoding, tuple(chunks))

    def _preview_overview(self, files: list[ResourceFile]) -> IndexOverview:
        return IndexOverview(len(files), sum(len(f.fragments) for f in files),
                             dict(Counter(f.status.value for f in files)),
                             dict(Counter(c.kind for f in files for c in f.fragments)),
                             tuple(FileSummary(f.path, f.status.value, f.detail, f.language, f.size_bytes, f.digest)
                                   for f in files[:self.limits.max_results]), len(files) > self.limits.max_results)
