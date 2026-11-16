"""
Episodic Memory Store — records and retrieves work episodes.

Each episode captures: what happened, approaches tried, outcome,
key insights, frustration signals, and user corrections.
Supports filtering by project, outcome, concepts, impact, and time range.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[memory-episodes] {msg}\n")

_VALID_OUTCOMES = {"breakthrough", "failure", "routine", "discovery"}

_EPISODE_COLS = (
    "id", "session_id", "project", "timestamp", "narrative",
    "approaches_tried", "key_insight", "outcome", "impact_score",
    "frustration_signals", "user_corrections", "concepts",
    "entities", "tools_used", "duration_minutes", "similar_to",
    "led_to", "contradicts", "created_at", "embedding_id",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_dict(row: sqlite3.Row | tuple) -> dict[str, Any]:
    """Convert a DB row to a plain dict with JSON fields deserialized."""
    d: dict[str, Any] = {}
    for i, col in enumerate(_EPISODE_COLS):
        if i >= len(row):
            break
        val = row[i]
        if col in (
            "approaches_tried", "user_corrections", "concepts",
            "entities", "tools_used", "similar_to",
        ):
            d[col] = _json_loads(val, [])
        elif col in ("impact_score",):
            d[col] = float(val) if val is not None else 0.5
        elif col in ("frustration_signals",):
            d[col] = int(val) if val is not None else 0
        elif col in ("duration_minutes",):
            d[col] = int(val) if val is not None else None
        else:
            d[col] = val
    return d


class EpisodeStore:
    """Manages episodic memory: saving, retrieving, and searching episodes."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def save(
        self,
        session_id: str,
        narrative: str,
        outcome: str,
        project: str = "general",
        impact_score: float = 0.5,
        concepts: list[str] | None = None,
        entities: list[str] | None = None,
        approaches_tried: list[str] | None = None,
        key_insight: str | None = None,
        frustration_signals: int = 0,
        user_corrections: list[str] | None = None,
        tools_used: list[str] | None = None,
        duration_minutes: int | None = None,
    ) -> str:
        """Save an episode. Returns episode_id."""
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"Invalid outcome '{outcome}', must be one of {_VALID_OUTCOMES}"
            )
        impact_score = max(0.0, min(1.0, impact_score))

        episode_id = uuid.uuid4().hex
        now = _now_iso()

        self.db.execute(
            """INSERT INTO episodes (
                id, session_id, project, timestamp, narrative,
                approaches_tried, key_insight, outcome, impact_score,
                frustration_signals, user_corrections, concepts,
                entities, tools_used, duration_minutes, similar_to,
                led_to, contradicts, created_at, embedding_id
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?
            )""",
            (
                episode_id, session_id, project, now, narrative,
                _json_dumps(approaches_tried or []),
                key_insight, outcome, impact_score,
                frustration_signals,
                _json_dumps(user_corrections or []),
                _json_dumps(concepts or []),
                _json_dumps(entities or []),
                _json_dumps(tools_used or []),
                duration_minutes,
                _json_dumps([]),  # similar_to starts empty
                None, None, now, None,
            ),
        )
        self.db.commit()
        LOG(f"saved episode {episode_id} [{outcome}] project={project}")
        return episode_id

    def get(self, episode_id: str) -> dict[str, Any] | None:
        """Get a single episode by ID."""
        cur = self.db.execute(
            f"SELECT {','.join(_EPISODE_COLS)} FROM episodes WHERE id = ?",
            (episode_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    def find_similar(
        self,
        query: str | None = None,
        project: str | None = None,
        outcome: str | None = None,
        min_impact: float = 0.0,
        concepts: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Find episodes matching criteria. If concepts provided, rank by overlap."""
        conditions: list[str] = []
        params: list[Any] = []

        if project:
            conditions.append("project = ?")
            params.append(project)
        if outcome:
            if outcome not in _VALID_OUTCOMES:
                raise ValueError(f"Invalid outcome '{outcome}'")
            conditions.append("outcome = ?")
            params.append(outcome)
        if min_impact > 0.0:
            conditions.append("impact_score >= ?")
            params.append(min_impact)
        if query:
            for term in dict.fromkeys(query.split()):
                escaped = term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                conditions.append("lower(narrative) LIKE lower(?) ESCAPE '!'")
                params.append(f"%{escaped}%")

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT {','.join(_EPISODE_COLS)} FROM episodes WHERE {where}"
        # If concepts provided, fetch more rows for client-side ranking
        fetch_limit = limit * 5 if concepts else limit
        sql += f" ORDER BY timestamp DESC LIMIT {fetch_limit}"

        cur = self.db.execute(sql, params)
        rows = [_row_to_dict(r) for r in cur.fetchall()]

        if concepts and rows:
            concept_set = {c.lower() for c in concepts}
            for row in rows:
                row_concepts = {c.lower() for c in (row.get("concepts") or [])}
                row["_concept_overlap"] = len(concept_set & row_concepts)
            rows.sort(key=lambda r: (-r["_concept_overlap"], r["timestamp"]))
            # Clean up internal sort key
            for row in rows:
                row.pop("_concept_overlap", None)

        return rows[:limit]

    def find_failures(
        self,
        concepts: list[str] | None = None,
        min_impact: float = 0.5,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Find failure episodes, optionally filtered by concepts."""
        return self.find_similar(
            outcome="failure",
            min_impact=min_impact,
            concepts=concepts,
            limit=limit,
        )

    def get_recent(
        self,
        days: int = 7,
        project: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get recent episodes within the given time window."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        conditions = ["timestamp >= ?"]
        params: list[Any] = [cutoff]
        if project:
            conditions.append("project = ?")
            params.append(project)

        where = " AND ".join(conditions)
        sql = (
            f"SELECT {','.join(_EPISODE_COLS)} FROM episodes "
            f"WHERE {where} ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)

        cur = self.db.execute(sql, params)
        return [_row_to_dict(r) for r in cur.fetchall()]

    def update_similar(self, episode_id: str, similar_ids: list[str]) -> None:
        """Update similar_to field for an episode."""
        self.db.execute(
            "UPDATE episodes SET similar_to = ? WHERE id = ?",
            (_json_dumps(similar_ids), episode_id),
        )
        self.db.commit()

    def stats(self, project: str | None = None) -> dict[str, Any]:
        """Episode statistics: counts by outcome, avg impact, etc."""
        where = "WHERE project = ?" if project else ""
        params: tuple[Any, ...] = (project,) if project else ()

        # Outcome counts
        cur = self.db.execute(
            f"SELECT outcome, COUNT(*) FROM episodes {where} GROUP BY outcome",
            params,
        )
        outcome_counts = {row[0]: row[1] for row in cur.fetchall()}

        # Total and averages
        cur = self.db.execute(
            f"""SELECT
                COUNT(*),
                COALESCE(AVG(impact_score), 0),
                COALESCE(AVG(frustration_signals), 0),
                COALESCE(AVG(duration_minutes), 0)
            FROM episodes {where}""",
            params,
        )
        row = cur.fetchone()
        total = row[0] if row else 0

        return {
            "total": total,
            "by_outcome": outcome_counts,
            "avg_impact": round(row[1], 3) if row else 0.0,
            "avg_frustration": round(row[2], 3) if row else 0.0,
            "avg_duration_minutes": round(row[3], 1) if row else 0.0,
            "project": project,
        }
