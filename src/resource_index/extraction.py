from __future__ import annotations

import codecs
import gzip
import io
import json
import logging
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree

from ast_ingest import ASTIngester, lang_for_path
from memory_core.telemetry import counters, op_timer
from resource_index.models import Extraction, FileStatus, Fragment, IndexLimits
from resource_index.privacy import sanitize
from resource_index.policy import sensitive

LOGGER = logging.getLogger(__name__)
OFFICE_SUFFIXES = frozenset({'.docx', '.docm', '.xlsx', '.xlsm', '.pptx', '.pptm', '.odt', '.ods', '.odp', '.epub'})
BINARY_SIGNATURES = (b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'RIFF', b'\x7fELF', b'MZ',
                     b'SQLite format 3\x00', b'\xd0\xcf\x11\xe0', b'\x00\x00\x00')
CONTROL_FRACTION = 0.01
TEXT_PROBE_BYTES = 64 * 1024


class ExpansionLimit(ValueError):
    """A compressed resource exceeds its extraction budget."""


def decode_text(data: bytes) -> tuple[str, str] | None:
    if data.startswith(BINARY_SIGNATURES):
        return None
    for bom, encoding in ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
                          (codecs.BOM_UTF16_LE, 'utf-16'), (codecs.BOM_UTF16_BE, 'utf-16'),
                          (codecs.BOM_UTF8, 'utf-8-sig')):
        if data.startswith(bom):
            return data.decode(encoding), encoding
    if b'\x00' in data:
        return None
    try:
        text, encoding = data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes
        match = from_bytes(data[:TEXT_PROBE_BYTES]).best()
        if match is None:
            return None
        encoding = match.encoding
        text = data.decode(encoding)
    controls = sum(ord(c) < 32 and c not in '\n\r\t\f' for c in text)
    if controls > len(text) * CONTROL_FRACTION:
        return None
    return text, encoding


def _xml_text(data: bytes) -> str:
    # Reject entity declarations before invoking the standard library parser.
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.IGNORECASE):
        raise ValueError('XML entity declarations are not allowed')
    node = ElementTree.fromstring(data)
    return '\n'.join(text.strip() for text in node.itertext() if text.strip())


class ResourceExtractor:
    def __init__(self, limits: IndexLimits, cancelled: Callable[[], bool] | None = None):
        from resource_index.media import MediaSettings
        self.limits = limits
        self.cancelled = cancelled
        self.media_settings = MediaSettings.from_env()
        self.ast = ASTIngester(max_file_bytes=limits.max_file_bytes)

    @op_timer('resource_extract_ms')
    def extract(self, path: str, data: bytes) -> Extraction:
        counters.bump('resource_extract_calls')
        try:
            return self._extract(path, data)
        except ExpansionLimit as exc:
            counters.bump('resource_extract_limits')
            return Extraction('', status=FileStatus.TOO_LARGE, detail=str(exc))
        except Exception as exc:
            counters.bump('resource_extract_errors')
            LOGGER.warning(json.dumps({'event': 'resource_extract_error', 'path': path,
                                       'error_type': type(exc).__name__}))
            return Extraction('', status=FileStatus.ERROR, detail=f'extraction_failed:{type(exc).__name__}')

    def _extract(self, path: str, data: bytes) -> Extraction:
        from resource_index.media import AV_SUFFIXES, IMAGE_SUFFIXES, extract_media, unavailable
        suffix = Path(path).suffix.lower()
        if data.startswith(b'%PDF-'):
            if unavailable('pdf') is None:
                return extract_media('pdf', suffix, data, self.limits, self.media_settings, self.cancelled)
            return self._pdf(data)
        if suffix in IMAGE_SUFFIXES:
            return extract_media('image', suffix, data, self.limits, self.media_settings, self.cancelled)
        if suffix in AV_SUFFIXES:
            return extract_media('av', suffix, data, self.limits, self.media_settings, self.cancelled)
        if suffix in {'.rtf', '.xls'}:
            return extract_media(suffix[1:], suffix, data, self.limits, self.media_settings, self.cancelled)
        if zipfile.is_zipfile(io.BytesIO(data)):
            return self._zip(path, data)
        if suffix in {'.tar', '.tgz', '.tbz', '.tbz2', '.txz'} or path.lower().endswith(('.tar.gz', '.tar.bz2', '.tar.xz')):
            return self._tar(data)
        if data.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
                expanded = archive.read(self.limits.max_expanded_bytes + 1)
            self._bound(len(expanded))
            decoded = decode_text(expanded)
            return Extraction(decoded[0], encoding=decoded[1], detail='gzip_text') if decoded else Extraction(
                '', status=FileStatus.METADATA, detail='gzip_binary_payload')
        decoded = decode_text(data)
        if decoded is None:
            return Extraction('', status=FileStatus.METADATA, detail='binary_content_no_text_extractor')
        text, encoding = decoded
        text = sanitize(text)
        language = lang_for_path(path)
        if language is None:
            from tree_sitter_language_pack import detect_language_from_content
            language = detect_language_from_content(text)
        if language is None:
            return Extraction(text, encoding=encoding, detail='text_fallback_unknown_language')
        try:
            chunks = self.ast.parse_source(path, language, text.encode('utf-8'))
        except Exception as exc:
            counters.bump('resource_ast_fallbacks')
            LOGGER.warning(json.dumps({'event': 'resource_ast_fallback', 'language': language,
                                       'error_type': type(exc).__name__}))
            return Extraction(text, language, encoding, detail=f'text_fallback:{type(exc).__name__}')
        fragments = tuple(Fragment(c.kind, c.name, c.content, c.start_line, c.end_line, c.language) for c in chunks)
        states = {str(c.extras.get('parse_status', 'parsed')) for c in chunks}
        return Extraction(text, language, encoding, detail=','.join(sorted(states)), fragments=fragments)

    def _bound(self, size: int) -> None:
        if size > self.limits.max_expanded_bytes:
            raise ExpansionLimit('expanded_content_exceeds_limit')

    def _pdf(self, data: bytes) -> Extraction:
        from resource_index.pdf_process import extract_pdf
        return extract_pdf(data, self.limits, self.cancelled)

    def _zip(self, path: str, data: bytes) -> Extraction:
        office = Path(path).suffix.lower() in OFFICE_SUFFIXES
        parts: list[str] = []
        omitted = 0
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > self.limits.max_archive_members:
                raise ExpansionLimit('archive_member_count_exceeds_limit')
            self._bound(sum(item.file_size for item in members))
            for item in members:
                if item.is_dir():
                    continue
                parts.append(item.filename)
                if item.flag_bits & 1 or sensitive(item.filename):
                    omitted += 1
                    continue
                payload = archive.read(item)
                if office and item.filename.endswith(('.xml', '.xhtml', '.html')):
                    parts.append(_xml_text(payload))
                elif not office:
                    decoded = decode_text(payload)
                    if decoded:
                        parts.append(decoded[0])
                    else:
                        omitted += 1
        return Extraction('\n'.join(parts), detail=f'archive_text;unextracted_members={omitted}')

    def _tar(self, data: bytes) -> Extraction:
        parts: list[str] = []
        total = 0
        omitted = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
            for count, member in enumerate(archive, 1):
                if count > self.limits.max_archive_members:
                    raise ExpansionLimit('archive_member_count_exceeds_limit')
                total += member.size
                self._bound(total)
                parts.append(member.name)
                if member.isfile() and not sensitive(member.name):
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError('Archive member has no stream')
                    with stream:
                        decoded = decode_text(stream.read(self.limits.max_expanded_bytes + 1))
                    if decoded:
                        parts.append(decoded[0])
                    else:
                        omitted += 1
                else:
                    omitted += 1
        return Extraction('\n'.join(parts), detail=f'archive_text;unextracted_members={omitted}')
