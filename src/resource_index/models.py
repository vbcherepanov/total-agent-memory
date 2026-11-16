from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class IndexErrorBase(ValueError):
    """A resource index request cannot be completed."""


class ScanIncomplete(IndexErrorBase):
    """The scan could not enumerate a complete, bounded snapshot."""


class IndexConflict(IndexErrorBase):
    """Another writer changed this root during the scan."""


class FileStatus(StrEnum):
    INDEXED = "indexed"
    METADATA = "metadata_only"
    EXCLUDED = "excluded"
    TOO_LARGE = "too_large"
    ERROR = "error"


@dataclass(frozen=True)
class IndexLimits:
    max_file_bytes: int = 8 * 1024 * 1024
    max_files: int = 50_000
    max_scan_bytes: int = 256 * 1024 * 1024
    chunk_chars: int = 4000
    max_archive_members: int = 1000
    max_expanded_bytes: int = 16 * 1024 * 1024
    max_query_terms: int = 32
    max_results: int = 100
    pdf_timeout_seconds: int = 30
    pdf_memory_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise IndexErrorBase(f"{name} must be a positive integer")

    @classmethod
    def from_env(cls) -> IndexLimits:
        defaults = cls()
        return cls(**{name: int(os.environ.get('TAM_INDEX_' + name.upper(), getattr(defaults, name)))
                      for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Fragment:
    kind: str
    name: str
    content: str
    start_line: int
    end_line: int
    language: str = ""


@dataclass(frozen=True)
class Extraction:
    text: str
    language: str = ""
    encoding: str = ""
    status: FileStatus = FileStatus.INDEXED
    detail: str = ""
    fragments: tuple[Fragment, ...] = ()


@dataclass(frozen=True)
class ResourceFile:
    path: str
    digest: str
    size_bytes: int
    language: str
    media_type: str
    encoding: str
    status: FileStatus
    detail: str
    fingerprint: str
    fragments: tuple[Fragment, ...]


@dataclass(frozen=True)
class RootState:
    id: str
    revision: int


@dataclass(frozen=True)
class WatchPolicy:
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise IndexErrorBase('watch must be a boolean')
        if not all(isinstance(item, str) for item in (*self.include, *self.exclude)):
            raise IndexErrorBase('Watch patterns must be strings')


@dataclass(frozen=True)
class FileSummary:
    path: str
    status: str
    detail: str
    language: str
    size_bytes: int
    digest: str


@dataclass(frozen=True)
class ChunkSample:
    file: str
    language: str
    kind: str
    name: str
    content: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class IndexOverview:
    total_files: int
    total_chunks: int
    by_status: dict[str, int]
    by_kind: dict[str, int]
    files: tuple[FileSummary, ...]
    files_truncated: bool


@dataclass(frozen=True)
class IndexResult:
    root_id: str
    path: str
    project: str
    revision: int
    persisted: bool
    total_files: int
    total_chunks: int
    changed_files: int
    unchanged_files: int
    removed_files: int
    by_status: dict[str, int]
    by_kind: dict[str, int]
    sample: tuple[ChunkSample, ...]
    files: tuple[FileSummary, ...]
    files_truncated: bool


@dataclass(frozen=True)
class SearchHit:
    path: str
    language: str
    status: str
    kind: str
    name: str
    content: str
    start_line: int
    end_line: int
    matched_terms: int
