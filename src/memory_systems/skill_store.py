"""
Procedural Memory Store — manages learned skills and their usage tracking.

Skills represent reusable procedures with trigger patterns, step sequences,
anti-patterns, and effectiveness metrics. Usage is tracked per invocation
to refine success rates and identify mastery.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[memory-skills] {msg}\n")

_VALID_STATUSES = {"draft", "active", "mastered", "deprecated"}

_SKILL_COLS = (
    "id", "name", "trigger_pattern", "steps", "anti_patterns",
    "times_used", "success_rate", "avg_steps_to_solve",
    "version", "learned_from", "last_refined_at", "projects",
    "stack", "related_skills", "status", "created_at",
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
    """Convert a skill DB row to a plain dict."""
    d: dict[str, Any] = {}
    for i, col in enumerate(_SKILL_COLS):
        if i >= len(row):
            break
        val = row[i]
        if col in (
            "steps", "anti_patterns", "learned_from",
            "projects", "stack", "related_skills",
        ):
            d[col] = _json_loads(val, [])
        elif col in ("times_used", "version"):
            d[col] = int(val) if val is not None else 0
        elif col in ("success_rate",):
            d[col] = float(val) if val is not None else 0.0
        elif col in ("avg_steps_to_solve",):
            d[col] = float(val) if val is not None else None
        else:
            d[col] = val
    return d


# Tokenizer: split on non-alphanumeric, lowercase, filter short words
_TOKEN_RE = re.compile(r"[a-zA-Z0-9\u0400-\u04FF]+")


def _tokenize(text: str) -> set[str]:
    """Extract lowercase word tokens from text, ignoring words < 3 chars."""
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if len(t) >= 3
    }


class SkillStore:
    """Manages procedural memory: skills, their usage, and refinement."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create(
        self,
        name: str,
        trigger_pattern: str,
        steps: list[str],
        stack: list[str] | None = None,
        projects: list[str] | None = None,
        learned_from: list[str] | None = None,
        anti_patterns: list[str] | None = None,
    ) -> str:
        """Create a new skill. Returns skill_id."""
        skill_id = uuid.uuid4().hex
        now = _now_iso()

        self.db.execute(
            """INSERT INTO skills (
                id, name, trigger_pattern, steps, anti_patterns,
                times_used, success_rate, avg_steps_to_solve,
                version, learned_from, last_refined_at, projects,
                stack, related_skills, status, created_at
            ) VALUES (
                ?, ?, ?, ?, ?,
                0, 0.0, NULL,
                1, ?, NULL, ?,
                ?, ?, 'draft', ?
            )""",
            (
                skill_id, name, trigger_pattern,
                _json_dumps(steps),
                _json_dumps(anti_patterns or []),
                _json_dumps(learned_from or []),
                _json_dumps(projects or []),
                _json_dumps(stack or []),
                _json_dumps([]),  # related_skills starts empty
                now,
            ),
        )
        self.db.commit()
        LOG(f"created skill '{name}' id={skill_id}")
        return skill_id

    def get(self, skill_id: str) -> dict[str, Any] | None:
        """Get a skill by ID."""
        cur = self.db.execute(
            f"SELECT {','.join(_SKILL_COLS)} FROM skills WHERE id = ?",
            (skill_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        """Get a skill by exact name."""
        cur = self.db.execute(
            f"SELECT {','.join(_SKILL_COLS)} FROM skills WHERE name = ?",
            (name,),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None

    def match_trigger(self, text: str, limit: int = 3) -> list[dict[str, Any]]:
        """
        Find skills whose trigger_pattern matches the given text.
        Uses word overlap + substring + stem matching for robust scoring.
        Returns best matches sorted by relevance score (descending).
        """
        text_tokens = _tokenize(text)
        if not text_tokens:
            return []

        # Fetch all non-deprecated skills
        cur = self.db.execute(
            f"SELECT {','.join(_SKILL_COLS)} FROM skills WHERE status != 'deprecated'"
        )
        rows = cur.fetchall()

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            skill = _row_to_dict(row)
            trigger_tokens = _tokenize(skill["trigger_pattern"])
            if not trigger_tokens:
                continue

            # Count matches: exact + substring + stem overlap
            match_count = 0
            matched_trigger_tokens: set[str] = set()
            matched_text_tokens: set[str] = set()

            for tt in text_tokens:
                for trig in trigger_tokens:
                    if trig in matched_trigger_tokens:
                        continue

                    matched = False
                    # Exact match
                    if tt == trig:
                        matched = True
                    # Substring: one contains the other (min 3 chars for the shorter)
                    elif len(tt) >= 3 and len(trig) >= 3:
                        if tt in trig or trig in tt:
                            matched = True
                        # Stem matching: shared prefix >= 4 chars
                        # Handles: "compile" <-> "compilation", "implement" <-> "implementation"
                        elif len(tt) >= 4 and len(trig) >= 4:
                            prefix_len = 0
                            for c1, c2 in zip(tt, trig):
                                if c1 == c2:
                                    prefix_len += 1
                                else:
                                    break
                            # Shared prefix must be >= 4 chars and >= 60% of shorter word
                            min_len = min(len(tt), len(trig))
                            if prefix_len >= 4 and prefix_len >= min_len * 0.6:
                                matched = True

                    if matched:
                        match_count += 1
                        matched_trigger_tokens.add(trig)
                        matched_text_tokens.add(tt)
                        break

            if match_count == 0:
                continue

            # Score: coverage of trigger tokens (how well text matches the trigger)
            trigger_coverage = match_count / len(trigger_tokens)
            # Also factor in coverage of text tokens (how specific the match is)
            text_coverage = match_count / len(text_tokens)
            # Combined score: weighted toward trigger coverage
            score = trigger_coverage * 0.7 + text_coverage * 0.3

            # Boost by success rate (small factor so relevance dominates)
            score += skill.get("success_rate", 0.0) * 0.05

            skill["_score"] = round(score, 4)
            scored.append((score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:limit]]

    def record_use(
        self,
        skill_id: str,
        success: bool,
        episode_id: str | None = None,
        steps_used: int | None = None,
        notes: str | None = None,
    ) -> str:
        """Record a skill use and update skill metrics. Returns use_id."""
        use_id = uuid.uuid4().hex
        now = _now_iso()

        # Insert usage record
        inserted = self.db.execute(
            """INSERT INTO skill_uses (id, skill_id, episode_id, success, steps_used, notes, used_at)
            SELECT ?, id, ?, ?, ?, ?, ? FROM skills WHERE id = ?""",
            (use_id, episode_id, int(success), steps_used, notes, now, skill_id),
        )
        if inserted.rowcount == 0:
            raise ValueError(f"Skill not found: {skill_id}")

        # Recalculate metrics from all uses
        cur = self.db.execute(
            """SELECT
                COUNT(*),
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),
                AVG(CASE WHEN steps_used IS NOT NULL THEN steps_used END)
            FROM skill_uses WHERE skill_id = ?""",
            (skill_id,),
        )
        row = cur.fetchone()
        total = row[0] or 0
        successes = row[1] or 0
        avg_steps = row[2]

        new_rate = successes / total if total > 0 else 0.0

        # Auto-promote status based on usage
        status_update = ""
        if total >= 10 and new_rate >= 0.8:
            status_update = ", status = 'mastered'"
        elif total >= 3:
            status_update = ", status = 'active'"

        self.db.execute(
            f"""UPDATE skills
            SET times_used = ?,
                success_rate = ?,
                avg_steps_to_solve = ?
                {status_update}
            WHERE id = ?""",
            (total, round(new_rate, 4), avg_steps, skill_id),
        )
        self.db.commit()
        LOG(f"recorded use for skill {skill_id}: success={success}, total={total}, rate={new_rate:.2f}")
        return use_id

    def refine(
        self,
        skill_id: str,
        new_steps: list[str] | None = None,
        new_anti_pattern: str | None = None,
    ) -> None:
        """Add steps or anti-patterns, increment version."""
        skill = self.get(skill_id)
        if skill is None:
            raise ValueError(f"Skill {skill_id} not found")

        now = _now_iso()

        if new_steps is not None:
            # Replace steps entirely
            self.db.execute(
                """UPDATE skills
                SET steps = ?, version = version + 1, last_refined_at = ?
                WHERE id = ?""",
                (_json_dumps(new_steps), now, skill_id),
            )

        if new_anti_pattern is not None:
            existing = skill.get("anti_patterns") or []
            # Avoid duplicates
            if new_anti_pattern not in existing:
                existing.append(new_anti_pattern)
                self.db.execute(
                    """UPDATE skills
                    SET anti_patterns = ?, version = version + 1, last_refined_at = ?
                    WHERE id = ?""",
                    (_json_dumps(existing), now, skill_id),
                )

        self.db.commit()
        LOG(f"refined skill {skill_id}")

    def get_all(
        self,
        status: str | None = None,
        stack: str | None = None,
    ) -> list[dict[str, Any]]:
        """List skills, optionally filtered by status or stack technology."""
        conditions: list[str] = []
        params: list[Any] = []

        if status:
            if status not in _VALID_STATUSES:
                raise ValueError(f"Invalid status '{status}', must be one of {_VALID_STATUSES}")
            conditions.append("status = ?")
            params.append(status)

        # Stack is stored as JSON array, use LIKE for simple containment
        if stack:
            conditions.append("stack LIKE ?")
            params.append(f'%"{stack}"%')

        where = " AND ".join(conditions) if conditions else "1=1"
        cur = self.db.execute(
            f"SELECT {','.join(_SKILL_COLS)} FROM skills WHERE {where} ORDER BY times_used DESC",
            params,
        )
        return [_row_to_dict(r) for r in cur.fetchall()]

    def deprecate(self, skill_id: str) -> None:
        """Mark skill as deprecated."""
        self.db.execute(
            "UPDATE skills SET status = 'deprecated' WHERE id = ?",
            (skill_id,),
        )
        self.db.commit()
        LOG(f"deprecated skill {skill_id}")

    def stats(self) -> dict[str, Any]:
        """Skill statistics: total, by status, avg success rate, most used."""
        # Status counts
        cur = self.db.execute(
            "SELECT status, COUNT(*) FROM skills GROUP BY status"
        )
        status_counts = {row[0]: row[1] for row in cur.fetchall()}

        # Overall stats
        cur = self.db.execute(
            """SELECT
                COUNT(*),
                COALESCE(AVG(success_rate), 0),
                COALESCE(SUM(times_used), 0)
            FROM skills WHERE status != 'deprecated'"""
        )
        row = cur.fetchone()
        total = row[0] if row else 0

        # Most used skill
        cur = self.db.execute(
            f"SELECT {','.join(_SKILL_COLS)} FROM skills ORDER BY times_used DESC LIMIT 1"
        )
        most_used_row = cur.fetchone()
        most_used = _row_to_dict(most_used_row) if most_used_row else None

        return {
            "total": total,
            "by_status": status_counts,
            "avg_success_rate": round(row[1], 3) if row else 0.0,
            "total_uses": row[2] if row else 0,
            "most_used": most_used,
        }
