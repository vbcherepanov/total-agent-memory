"""Canonical topic vocabulary normalisation — v10 P0.2.

Beever Atlas's `ClassifierAgent` enriches every fact with one of 22 canonical
topic tags from a controlled vocabulary. Without that, free-form tags drift
over time: an MCP-memory query for `database` misses `azure-sql`,
`db-optimization`, `db-perf`, and a half-dozen variants the user invented in
different sessions.

This module loads `vocabularies/canonical_topics.txt`, exposes a
`normalise_tags()` function that maps incoming free-form tags to the
nearest canonical, and is wired into `save_knowledge`. Originals are
preserved alongside canonicals (combined into the stored tag list) so
retrieval that knew a specific synonym still works.

Resolution strategy (in order):
  1. Exact match against canonical or any alias (case-insensitive).
  2. Substring containment in either direction (free-form contains
     canonical, or vice versa).
  3. Embedding cosine ≥ `MEMORY_TAG_SIM_THRESHOLD` (default 0.65) when an
     embedding model is available — uses the same FastEmbed instance the
     server already loaded.
  4. Levenshtein ratio ≥ `MEMORY_TAG_LEVENSHTEIN_THRESHOLD`
     (default 0.78) as a final cheap fallback.
  5. None → tag is left as-is (it might be a brand-new concept worth
     adding to the vocabulary later).

All outputs are deduplicated and lowercased.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

LOG = lambda msg: sys.stderr.write(f"[canonical-tags] {msg}\n")

_DEFAULT_VOCAB_PATH = (
    Path(__file__).resolve().parent.parent / "vocabularies" / "canonical_topics.txt"
)
_DEFAULT_SIM_THRESHOLD = 0.65
_DEFAULT_LEV_THRESHOLD = 0.78


# ──────────────────────────────────────────────
# Vocabulary load
# ──────────────────────────────────────────────


@dataclass
class CanonicalTopic:
    canonical: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def all_forms(self) -> tuple[str, ...]:
        return (self.canonical,) + self.aliases


@dataclass
class Vocabulary:
    topics: tuple[CanonicalTopic, ...]
    by_form: dict[str, str]                  # alias OR canonical → canonical
    canonical_set: frozenset[str]

    @property
    def canonicals(self) -> tuple[str, ...]:
        return tuple(t.canonical for t in self.topics)


_vocab_cache: Vocabulary | None = None


def _vocab_path() -> Path:
    override = os.environ.get("MEMORY_TAG_VOCAB_PATH")
    if override:
        return Path(override)
    return _DEFAULT_VOCAB_PATH


def load_vocabulary(path: Path | None = None) -> Vocabulary:
    """Parse the canonical-tags file. Memoised on the resolved path."""
    global _vocab_cache
    if path is None and _vocab_cache is not None:
        return _vocab_cache

    target = path or _vocab_path()
    if not target.exists():
        LOG(f"vocabulary not found at {target}; using empty vocab")
        empty = Vocabulary(topics=(), by_form={}, canonical_set=frozenset())
        if path is None:
            _vocab_cache = empty
        return empty

    topics: list[CanonicalTopic] = []
    seen_canonicals: set[str] = set()
    by_form: dict[str, str] = {}

    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip().lower() for p in line.split(",") if p.strip()]
        if not parts:
            continue
        canonical, *aliases = parts
        if canonical in seen_canonicals:
            raise ValueError(
                f"duplicate canonical '{canonical}' in vocabulary {target}"
            )
        seen_canonicals.add(canonical)
        topic = CanonicalTopic(canonical=canonical, aliases=tuple(aliases))
        topics.append(topic)
        for form in topic.all_forms():
            # First definition wins on alias collisions — log conflicts so
            # the curator notices, but don't blow up at runtime.
            if form in by_form and by_form[form] != canonical:
                LOG(
                    f"alias '{form}' already maps to '{by_form[form]}'; "
                    f"ignoring duplicate under '{canonical}'"
                )
                continue
            by_form[form] = canonical

    vocab = Vocabulary(
        topics=tuple(topics),
        by_form=by_form,
        canonical_set=frozenset(seen_canonicals),
    )
    if path is None:
        _vocab_cache = vocab
    return vocab


def reset_vocabulary_cache() -> None:
    """Test helper — clear the memoised vocab."""
    global _vocab_cache
    _vocab_cache = None


# ──────────────────────────────────────────────
# Resolution helpers
# ──────────────────────────────────────────────


def _normalise(tag: str) -> str:
    return (tag or "").strip().lower()


def _exact_or_alias(tag: str, vocab: Vocabulary) -> str | None:
    return vocab.by_form.get(tag)


def _substring_match(tag: str, vocab: Vocabulary) -> str | None:
    """Containment in either direction (cheap, no model needed)."""
    if len(tag) < 3:
        return None
    for form, canonical in vocab.by_form.items():
        if len(form) < 3:
            continue
        if form in tag or tag in form:
            return canonical
    return None


def _levenshtein_ratio(a: str, b: str, *, max_distance: int | None = None) -> float:
    """Pure-Python ratio in [0,1]. We don't have python-Levenshtein in deps."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    m, n = len(a), len(b)
    if abs(m - n) / max(m, n) > 0.5:
        # Length too different — short-circuit cheap reject.
        return 0.0
    if max_distance is not None:
        if type(max_distance) is not int or max_distance < 0:
            raise ValueError('max_distance must be a non-negative integer')
        if abs(m - n) > max_distance:
            return 0.0
        denominator = max(m, n)
        start = 0
        while start < min(m, n) and a[start] == b[start]:
            start += 1
        a, b = a[start:], b[start:]
        end = 0
        while end < min(len(a), len(b)) and a[-end - 1] == b[-end - 1]:
            end += 1
        if end:
            a, b = a[:-end], b[:-end]
        m, n = len(a), len(b)
        if not m or not n:
            distance = max(m, n)
            return 1.0 - distance / denominator if distance <= max_distance else 0.0
        balance = Counter(a)
        balance.subtract(b)
        # One substitution changes two counts; insert/delete changes one and the length.
        if sum(abs(count) for count in balance.values()) + abs(m - n) > 2 * max_distance:
            return 0.0
        unreachable = max_distance + 1
        previous = {j: j for j in range(min(n, max_distance) + 1)}
        for i in range(1, m + 1):
            current = {0: i} if i <= max_distance else {}
            for j in range(max(1, i - max_distance), min(n, i + max_distance) + 1):
                current[j] = min(previous.get(j, unreachable) + 1,
                                 current.get(j - 1, unreachable) + 1,
                                 previous.get(j - 1, unreachable) + (a[i - 1] != b[j - 1]))
            if min(current.values(), default=unreachable) > max_distance:
                return 0.0
            previous = current
        distance = previous.get(n, unreachable)
        return 1.0 - distance / denominator if distance <= max_distance else 0.0
    # Iterative DP — O(m*n) but tags are tiny.
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,         # deletion
                curr[j - 1] + 1,     # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    distance = prev[n]
    return 1.0 - (distance / max(m, n))


def _levenshtein_match(tag: str, vocab: Vocabulary, threshold: float) -> str | None:
    if not tag:
        return None
    best_form = None
    best_ratio = 0.0
    for form in vocab.by_form:
        ratio = _levenshtein_ratio(tag, form)
        if ratio > best_ratio:
            best_ratio = ratio
            best_form = form
    if best_form and best_ratio >= threshold:
        return vocab.by_form[best_form]
    return None


# ──────────────────────────────────────────────
# Embedding-based match (optional, lazy)
# ──────────────────────────────────────────────


_canonical_embedding_cache: dict[str, list[float]] | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _embed_provider():
    """Return the embed_provider module; None when unavailable."""
    try:
        import embed_provider  # type: ignore
        return embed_provider
    except Exception:
        return None


def _embed(texts: list[str]) -> list[list[float]] | None:
    ep = _embed_provider()
    if ep is None:
        return None
    try:
        provider = ep.get_provider()
    except Exception:
        return None
    if provider is None or not getattr(provider, "available", lambda: True)():
        return None
    try:
        out = provider.embed(texts)
    except Exception as exc:
        LOG(f"embed failed: {exc}")
        return None
    if not out or not isinstance(out, list):
        return None
    return out


def _build_canonical_embeddings(vocab: Vocabulary) -> dict[str, list[float]] | None:
    global _canonical_embedding_cache
    if _canonical_embedding_cache is not None:
        return _canonical_embedding_cache
    if not vocab.canonicals:
        return None
    embeddings = _embed(list(vocab.canonicals))
    if embeddings is None or len(embeddings) != len(vocab.canonicals):
        return None
    _canonical_embedding_cache = dict(zip(vocab.canonicals, embeddings))
    return _canonical_embedding_cache


def _embedding_match(tag: str, vocab: Vocabulary, threshold: float) -> str | None:
    canonical_embs = _build_canonical_embeddings(vocab)
    if not canonical_embs:
        return None
    tag_emb = _embed([tag])
    if not tag_emb or not tag_emb[0]:
        return None
    target = tag_emb[0]
    best_canonical = None
    best_score = 0.0
    for canonical, emb in canonical_embs.items():
        score = _cosine(target, emb)
        if score > best_score:
            best_score = score
            best_canonical = canonical
    if best_canonical and best_score >= threshold:
        return best_canonical
    return None


def reset_embedding_cache() -> None:
    """Test helper — clear cached canonical embeddings."""
    global _canonical_embedding_cache
    _canonical_embedding_cache = None


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


def _sim_threshold() -> float:
    raw = os.environ.get("MEMORY_TAG_SIM_THRESHOLD")
    if not raw:
        return _DEFAULT_SIM_THRESHOLD
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return _DEFAULT_SIM_THRESHOLD


def _lev_threshold() -> float:
    raw = os.environ.get("MEMORY_TAG_LEVENSHTEIN_THRESHOLD")
    if not raw:
        return _DEFAULT_LEV_THRESHOLD
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return _DEFAULT_LEV_THRESHOLD


def resolve_tag(tag: str, vocab: Vocabulary | None = None) -> str | None:
    """Map a single free-form tag to its canonical form.

    Returns the canonical string when matched, or None when the tag does
    not resolve to anything in the vocabulary (caller should keep it
    as-is).
    """
    if not tag:
        return None
    vocab = vocab or load_vocabulary()
    if not vocab.topics:
        return None
    norm = _normalise(tag)
    if not norm:
        return None

    matched = _exact_or_alias(norm, vocab)
    if matched:
        return matched

    matched = _substring_match(norm, vocab)
    if matched:
        return matched

    matched = _embedding_match(norm, vocab, _sim_threshold())
    if matched:
        return matched

    matched = _levenshtein_match(norm, vocab, _lev_threshold())
    if matched:
        return matched

    return None


def normalise_tags(tags: Iterable[str] | None) -> list[str]:
    """Return a deduped, lowercased tag list with canonicals mapped.

    Originals are kept alongside canonicals: if a tag matches a canonical
    it is replaced; if it does not, it is preserved as-is. The returned
    list preserves insertion order.
    """
    if not tags:
        return []
    vocab = load_vocabulary()
    result: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        v = _normalise(value)
        if not v or v in seen:
            return
        seen.add(v)
        result.append(v)

    for raw in tags:
        if not isinstance(raw, str):
            continue
        original = _normalise(raw)
        if not original:
            continue
        canonical = resolve_tag(original, vocab)
        if canonical:
            # Both the canonical AND the original — recall by a known
            # synonym ("azure-sql") still works after canonicalisation.
            _add(canonical)
            if canonical != original:
                _add(original)
        else:
            _add(original)
    return result
