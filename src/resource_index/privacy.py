from __future__ import annotations

from privacy_filter import PRIVATE_TAG
from secret_redaction import SECRET_PATTERNS


def sanitize(text: str) -> str:
    """Redact before chunking while retaining source line coordinates."""
    parts: list[str] = []
    depth = 0
    cursor = 0
    private_start = 0
    for tag in PRIVATE_TAG.finditer(text):
        if depth == 0:
            parts.append(text[cursor:tag.start()])
            private_start = tag.start()
        depth = max(0, depth - 1) if tag.group(1) else depth + 1
        if depth == 0:
            parts.append('[PRIVATE]' + '\n' * text[private_start:tag.end()].count('\n'))
        cursor = tag.end()
    if depth:
        parts.append('[PRIVATE]' + '\n' * text[private_start:].count('\n'))
    else:
        parts.append(text[cursor:])
    cleaned = ''.join(parts)
    for pattern, replacement in SECRET_PATTERNS:
        def preserve(match, replacement=replacement):
            result = replacement(match) if callable(replacement) else match.expand(replacement)
            return result + '\n' * max(0, match[0].count('\n') - result.count('\n'))
        cleaned = pattern.sub(preserve, cleaned)
    return cleaned.replace('\x00', '')
