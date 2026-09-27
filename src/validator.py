"""Content Validator — safety net for LLM text transformations.

Verifies that transformed text (summary, merge, compress) preserves
critical elements from the original:
- code blocks (byte-for-byte fenced content)
- URLs
- absolute/tilde file paths
- markdown heading count
- bullet count (within tolerance)
- inline code

Ported/inspired by juliusbrussee/caveman validator, adapted for
claude-total-memory reflection/consolidate operations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


# ──────────────────────────────────────────────
# Regex patterns (module-level, compiled once)
# ──────────────────────────────────────────────

# Fenced code blocks. Non-greedy; language tag optional.
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# URLs. Strip trailing sentence-ending punctuation so "…com." still matches.
_URL_RE = re.compile(r"https?://[^\s<>`'\"()]+")
_URL_TRAILING_PUNCT = ".,;:!?"

# Absolute paths (/Users/..., /etc/...) and tilde paths (~/...).
# A slash may only start a path at the beginning of the text/line or after
# whitespace/quote/bracket/`=`/`:`/`,`/`|`/`<`/`>`/`;`/backtick/`*`, so prose
# like "EU/Russia" is ignored while "db=/var/x", "|/etc/hosts|" and
# "**/etc/hosts**" are still matched.
_PATH_BOUNDARY = r"""(?:(?<=^)|(?<=[\s'"(\[{=:,|<>;`*]))"""

# Extensions that make a single-segment absolute path (/README.md) count.
_CODE_EXT = r"(?:py|sql|md|json|ya?ml|toml|sh|ts|js|txt|ini|cfg|go|rs|tsx|jsx)"

_SEG = r"[A-Za-z0-9_.\-]+"
# Absolute body: either >=2 segments or a trailing slash (single `(seg/)+`
# repetition with an empty final segment covers both), or a single segment
# with a recognized extension (/README.md). This is what excludes bare
# single-segment slash tokens like slash-commands ("/compact", "/jira-task").
_ABS_MULTI_OR_TRAILING = rf"(?:{_SEG}/)+(?:{_SEG})?"
_ABS_EXT_SINGLE = rf"{_SEG}\.{_CODE_EXT}\b"
_ABS_BODY = rf"(?:{_ABS_MULTI_OR_TRAILING}|{_ABS_EXT_SINGLE})"

# Tilde paths count from one segment (~/.bashrc).
_TILDE_BODY = rf"{_SEG}(?:/{_SEG})*/?"

_PATH_RE = re.compile(
    _PATH_BOUNDARY + rf"(?:~/{_TILDE_BODY}|/{_ABS_BODY})",
    re.MULTILINE,
)

# Markdown headings (line-starting #..######).
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S", re.MULTILINE)

# Bullet-style list items: -, *, +, or "1." (with space after).
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\S", re.MULTILINE)

# Inline `code` — single backtick pairs on the same line, non-greedy.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

BULLET_TOLERANCE = 0.15  # ±15%


# ──────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Outcome of validating transformed text against its original."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        """Human-readable feedback for an LLM retry prompt."""
        parts: list[str] = []
        if self.errors:
            parts.append("ERRORS (must fix):")
            parts.extend(f"  - {e}" for e in self.errors)
        if self.warnings:
            parts.append("WARNINGS:")
            parts.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(parts) if parts else "OK"


# ──────────────────────────────────────────────
# Extractors (pure helpers)
# ──────────────────────────────────────────────


def _extract_code_blocks(text: str) -> list[str]:
    """Return code-block bodies (excluding fences and language tag)."""
    return [m.group(1) for m in _CODE_BLOCK_RE.finditer(text)]


def _extract_urls(text: str) -> set[str]:
    urls: set[str] = set()
    for raw in _URL_RE.findall(text):
        # Strip trailing sentence punctuation that regex caught (e.g. "com.")
        while raw and raw[-1] in _URL_TRAILING_PUNCT:
            raw = raw[:-1]
        if raw:
            urls.add(raw)
    return urls


def _extract_paths(text: str) -> set[str]:
    """Extract file paths, ignoring things already captured as URLs."""
    urls = _URL_RE.findall(text)
    # Temporarily strip URLs so we don't double-match inside them.
    cleaned = text
    for u in urls:
        cleaned = cleaned.replace(u, " ")
    paths: set[str] = set()
    for m in _PATH_RE.findall(cleaned):
        # Strip trailing punctuation (comma/period/etc.) that regex may include.
        while m and m[-1] in _URL_TRAILING_PUNCT:
            m = m[:-1]
        if m and ("/" in m):
            paths.add(m)
    return paths


def _count_headings(text: str) -> int:
    return len(_HEADING_RE.findall(text))


def _count_bullets(text: str) -> int:
    return len(_BULLET_RE.findall(text))


def _extract_inline_code(text: str) -> set[str]:
    # Exclude inline code that is actually inside a fenced block.
    fences_stripped = _CODE_BLOCK_RE.sub(" ", text)
    return {m.group(1).strip() for m in _INLINE_CODE_RE.finditer(fences_stripped) if m.group(1).strip()}


# ──────────────────────────────────────────────
# Validator
# ──────────────────────────────────────────────


class ContentValidator:
    """Check that a transformed text preserves critical elements of the original."""

    def __init__(self, bullet_tolerance: float = BULLET_TOLERANCE) -> None:
        self.bullet_tolerance = bullet_tolerance

    def validate(self, original: str, transformed: str) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        original = original or ""
        transformed = transformed or ""

        # ── Code blocks: each original block must appear verbatim in transformed
        orig_blocks = _extract_code_blocks(original)
        trans_blocks = _extract_code_blocks(transformed)
        for idx, block in enumerate(orig_blocks):
            if block not in trans_blocks:
                snippet = block.strip().splitlines()[0][:60] if block.strip() else "(empty)"
                errors.append(
                    f"code block #{idx + 1} altered or removed (first line: {snippet!r})"
                )

        # ── URLs
        orig_urls = _extract_urls(original)
        trans_urls = _extract_urls(transformed)
        for u in orig_urls - trans_urls:
            errors.append(f"url lost: {u}")

        # ── Paths
        # Kept if the path text survives anywhere, so formatting added around
        # it in the transformed text cannot fail the check.
        for p in _extract_paths(original):
            if p.rstrip("/") not in transformed:
                errors.append(f"path lost: {p}")

        # ── Inline code
        orig_inline = _extract_inline_code(original)
        trans_inline = _extract_inline_code(transformed)
        for code in orig_inline - trans_inline:
            errors.append(f"inline code lost: `{code}`")

        # ── Heading count (exact match — headings are structural)
        oh = _count_headings(original)
        th = _count_headings(transformed)
        if oh != th:
            errors.append(f"heading count changed: {oh} -> {th}")

        # ── Bullet count within tolerance
        ob = _count_bullets(original)
        tb = _count_bullets(transformed)
        if ob > 0:
            drop = (ob - tb) / ob  # positive = lost bullets
            if drop > self.bullet_tolerance:
                msg = (
                    f"bullet count dropped {drop * 100:.0f}% "
                    f"(tolerance {self.bullet_tolerance * 100:.0f}%): {ob} -> {tb}"
                )
                # Large drops are errors, small-but-over-tolerance are warnings
                if drop > max(0.3, self.bullet_tolerance * 2):
                    errors.append(msg)
                else:
                    warnings.append(msg)
            elif tb > ob and (tb - ob) / ob > self.bullet_tolerance:
                warnings.append(f"bullet count grew unexpectedly: {ob} -> {tb}")

        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


# ──────────────────────────────────────────────
# Retry helper
# ──────────────────────────────────────────────


TransformFn = Callable[[str, "str | None"], str]


def validate_and_retry(
    original: str,
    transform: TransformFn,
    max_retries: int = 2,
    validator: ContentValidator | None = None,
) -> tuple[str, ValidationResult]:
    """Run `transform(original, feedback)` then validate, retrying on failure.

    `transform` is called with (original_text, feedback_or_None). On the first
    call feedback is None; on retry it is the previous validator report so the
    LLM can self-correct.

    Returns the final transformed text and the final ValidationResult.
    The caller decides what to do when `result.ok` is False after max_retries.
    """
    v = validator or ContentValidator()
    feedback: str | None = None
    last_out = ""
    last_result = ValidationResult(ok=False, errors=["no attempt made"])

    attempts = max(1, max_retries + 1)  # initial try + N retries
    for _ in range(attempts):
        last_out = transform(original, feedback)
        last_result = v.validate(original, last_out)
        if last_result.ok:
            return last_out, last_result
        feedback = last_result.feedback()

    return last_out, last_result
