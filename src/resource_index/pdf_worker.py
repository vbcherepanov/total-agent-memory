"""Bounded PDF extraction in a disposable process; never imports the TAM server."""
from __future__ import annotations

import io
import json
import sys


def extract(data: bytes, max_expanded: int) -> dict[str, str]:
    from pypdf import PdfReader, filters
    filters.ZLIB_MAX_OUTPUT_LENGTH = max_expanded
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        return {'text': '', 'status': 'metadata_only', 'detail': 'encrypted_pdf'}
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        text = page.extract_text() or ''
        total += len(text.encode('utf-8'))
        if total > max_expanded:
            return {'text': '', 'status': 'too_large', 'detail': 'pdf_text_exceeds_limit'}
        parts.append(text)
    text = '\n'.join(parts)
    if text.strip():
        return {'text': text, 'status': 'indexed', 'detail': 'pdf_text'}
    return {'text': '', 'status': 'metadata_only', 'detail': 'pdf_has_no_text_layer_requires_ocr'}


def main() -> None:
    max_input, max_expanded, memory_bytes = map(int, sys.argv[1:])
    if sys.platform == 'linux':
        import resource
        limit = resource.RLIMIT_AS
        _, hard = resource.getrlimit(limit)
        effective = memory_bytes if hard == resource.RLIM_INFINITY else min(memory_bytes, hard)
        resource.setrlimit(limit, (effective, hard))
    try:
        data = sys.stdin.buffer.read(max_input + 1)
        if len(data) > max_input:
            result = {'text': '', 'status': 'too_large', 'detail': 'pdf_input_exceeds_limit'}
        else:
            result = extract(data, max_expanded)
    except MemoryError:
        result = {'text': '', 'status': 'too_large', 'detail': 'pdf_memory_limit'}
    except Exception as exc:
        result = {'text': '', 'status': 'error', 'detail': f'pdf_extraction_failed:{type(exc).__name__}'}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
