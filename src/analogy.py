"""
Analogical reasoning — v7.0 Phase H.

Given a target problem described as a set of features/concepts, find past
episodes/solutions whose feature sets align (Jaccard similarity) even if
the surface text differs. The goal is *cross-domain transfer* — "we solved
a similar problem before in another project".

Source pool: `knowledge` rows, filtered by type ∈ {solution, lesson,
decision} by default. Features are extracted from tags + a simple token
set of content (lowercase words length ≥ 4).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from typing import Any, Iterable

LOG = lambda msg: sys.stderr.write(f"[analogy] {msg}\n")


_STOP = frozenset({
    "this", "that", "from", "with", "have", "when", "what", "where",
    "which", "about", "there", "their", "they", "then", "than", "been",
    "were", "would", "could", "should", "some", "more", "like", "into",
    "also", "very", "just", "over", "only", "such", "these", "those",
    "html", "http", "https", "using", "used", "uses",
})


def _tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text.lower())
    return {t for t in tokens if t not in _STOP}


def _parse_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _extract_features(row: dict[str, Any]) -> set[str]:
    feats = set()
    feats.update(t.lower() for t in _parse_tags(row.get("tags")))
    feats.update(_tokenize(row.get("content", "")))
    feats.update(_tokenize(row.get("context", "")))
    return feats


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class AnalogyEngine:
    """Find analogous past solutions/lessons across projects."""

    DEFAULT_TYPES = ("solution", "lesson", "decision")

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def find_analogies(
        self,
        *,
        features: Iterable[str] | None = None,
        text: str | None = None,
        exclude_project: str | None = None,
        only_types: Iterable[str] | None = None,
        min_score: float = 0.1,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Rank knowledge rows by feature-set Jaccard similarity.

        `features` is used directly; if not provided, features are derived
        from `text`. `exclude_project` omits the asker's own project so we
        get cross-domain analogies.
        """
        target: set[str] = set(f.lower() for f in (features or []))
        if text:
            target |= _tokenize(text)
        if not target:
            return []

        types = tuple(only_types or self.DEFAULT_TYPES)
        placeholders = ",".join(["?"] * len(types))

        conditions = [f"type IN ({placeholders})", "status = 'active'"]
        params: list[Any] = [*types]
        if exclude_project:
            conditions.append("project != ?")
            params.append(exclude_project)

        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            f"""SELECT id, type, content, context, project, tags, confidence,
                       created_at
                FROM knowledge WHERE {' AND '.join(conditions)}
                ORDER BY confidence DESC LIMIT 2000""",
            params,
        ).fetchall()

        scored: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            d = dict(r)
            feats = _extract_features(d)
            s = _jaccard(target, feats)
            if s >= min_score:
                scored.append((s, d))
        scored.sort(key=lambda x: x[0], reverse=True)

        out: list[dict[str, Any]] = []
        for s, d in scored[:limit]:
            d["analogy_score"] = round(s, 4)
            d["shared_features"] = sorted(target & _extract_features(d))[:20]
            out.append(d)
        return out

    def transfer_lessons(
        self,
        *,
        target_project: str,
        text: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Convenience wrapper: find analogies from OTHER projects and
        structure them as "lessons we can apply here".
        """
        analogies = self.find_analogies(
            text=text,
            exclude_project=target_project,
            only_types=("solution", "lesson"),
            limit=limit,
        )
        return {
            "target_project": target_project,
            "count": len(analogies),
            "analogies": analogies,
        }
