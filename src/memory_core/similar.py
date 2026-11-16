from __future__ import annotations

from dataclasses import dataclass

from canonical_tags import _levenshtein_ratio
from memory_core.dedup import normalize
from memory_core.telemetry import counters, op_timer
from tam_db.contracts import CompatConnection

CANDIDATES_PER_INDEX = 32
MAX_KEY_CHARS = 256
MAX_DISTANCE = 8
MIN_SCORE = 0.85
MAX_MATCHES = 5
LONG_KEY_CHARS = 24
PREFIX_EXPRESSION = 'substr(lower(trim(content)), 1, 3)'
SUFFIX_EXPRESSION = 'substr(lower(trim(content)), length(trim(content)) - 2, 3)'

LONG_PREFIX_EXPRESSION = f'substr(lower(trim(content)), 1, {LONG_KEY_CHARS})'
LONG_SUFFIX_EXPRESSION = (f'CASE WHEN length(trim(content)) >= {LONG_KEY_CHARS} THEN '
                          f'substr(lower(trim(content)), length(trim(content)) - {LONG_KEY_CHARS - 1}, {LONG_KEY_CHARS}) END')


@dataclass(frozen=True)
class SimilarMatch:
    id: int
    score: float
    status: str


@dataclass(frozen=True)
class SimilarityReport:
    matches: tuple[SimilarMatch, ...]
    truncated: bool

    def payload(self) -> dict[str, object]:
        return {'similar': [{'id': row.id, 'score': round(row.score, 4)} for row in self.matches],
                'revived': bool(self.matches and self.matches[0].status == 'deleted'),
                'similar_search_truncated': self.truncated}


def _bind(expression: str) -> str:
    return expression.replace('content', '?')


def _values(expression: str, content: str) -> list[str]:
    return [content] * expression.count('content')


def _long_key_candidates(db: CompatConnection, content: str, project: str, exclude_id: int,
                         long_key: str) -> list[dict[str, object]]:
    """Near-duplicates behind newer rows that share only the 3-character key.

    The 24-character prefix and suffix are indexed too, so a one-character variant that keeps either
    end of the text is reached by an index seek however many records share the short key.
    """
    return db.execute(
        f'SELECT id,status,substr(content,1,{MAX_KEY_CHARS + 1}) AS content_key '
        f'FROM knowledge WHERE project=? AND {long_key}={_bind(long_key)} AND id<>? ORDER BY id DESC LIMIT ?',
        [project, *_values(long_key, content), exclude_id, CANDIDATES_PER_INDEX],
    ).fetchall()


def find_similar(db: CompatConnection, content: str, project: str, exclude_id: int) -> SimilarityReport:
    key = normalize(content[:MAX_KEY_CHARS])
    if not key:
        return SimilarityReport((), False)
    candidates: dict[int, tuple[str, str]] = {}
    truncated = len(content) > MAX_KEY_CHARS
    with op_timer('save_similarity_ms'):
        for narrow, wide in ((PREFIX_EXPRESSION, LONG_PREFIX_EXPRESSION), (SUFFIX_EXPRESSION, LONG_SUFFIX_EXPRESSION)):
            for row in _long_key_candidates(db, content, project, exclude_id, wide):
                candidates[row['id']] = (row['status'], row['content_key'])
            rows = db.execute(
                f'SELECT id,status,substr(content,1,{MAX_KEY_CHARS + 1}) AS content_key '
                f'FROM knowledge WHERE project=? AND {narrow}={_bind(narrow)} '
                'AND id<>? ORDER BY id DESC LIMIT ?',
                [project, *_values(narrow, content), exclude_id, CANDIDATES_PER_INDEX + 1],
            ).fetchall()
            truncated = truncated or len(rows) > CANDIDATES_PER_INDEX
            for row in rows[:CANDIDATES_PER_INDEX]:
                candidates[row['id']] = (row['status'], row['content_key'])
        matches = []
        for identity, (status, text) in candidates.items():
            truncated = truncated or len(text) > MAX_KEY_CHARS
            score = _levenshtein_ratio(key, normalize(text[:MAX_KEY_CHARS]), max_distance=MAX_DISTANCE)
            if score >= MIN_SCORE:
                matches.append(SimilarMatch(identity, score, status))
        counters.bump('save_similarity_candidates', len(candidates))
        counters.bump('save_similarity_matches', len(matches))
        matches.sort(key=lambda row: (-row.score, -row.id))
    return SimilarityReport(tuple(matches[:MAX_MATCHES]), truncated or len(matches) > MAX_MATCHES)
