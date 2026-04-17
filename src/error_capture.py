"""
First-class error capture — v7.0 Phase D.

Wrapper over the existing `errors` table that enforces structured capture
and auto-consolidates repeated patterns into prevention rules.

Capture schema (required fields):
    file         — path of the file where the error occurred (or "global")
    error        — the error message / exception text
    root_cause   — short explanation of WHY it happened
    fix          — concrete fix applied
    pattern      — generalized pattern key (e.g. "sqlite-locked-during-ddl")

After each capture, if N>=threshold errors share the same pattern key AND
no active rule exists for it, a prevention rule is synthesized into the
`rules` table and linked to each source error via `insight_id` proxy.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[error-capture] {msg}\n")

DEFAULT_CONSOLIDATE_THRESHOLD = 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ErrorCapture:
    """Structured error logging + auto-consolidation into rules."""

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        consolidate_threshold: int = DEFAULT_CONSOLIDATE_THRESHOLD,
    ) -> None:
        self.db = db
        self.threshold = consolidate_threshold

    # ──────────────────────────────────────────────
    # Capture
    # ──────────────────────────────────────────────

    def learn_error(
        self,
        *,
        file: str,
        error: str,
        root_cause: str,
        fix: str,
        pattern: str,
        severity: str = "medium",
        category: str = "bug",
        project: str = "general",
        session_id: str = "learn_error",
        extra_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store a structured error and auto-consolidate if threshold reached.

        Returns {error_id, consolidated: bool, rule_id: Optional[int]}.
        """
        for field_name, value in [
            ("file", file), ("error", error), ("root_cause", root_cause),
            ("fix", fix), ("pattern", pattern),
        ]:
            if not value:
                raise ValueError(f"{field_name} is required")

        tags = [f"file:{file}", f"pattern:{pattern}", "learn_error"]
        if extra_tags:
            tags.extend(extra_tags)

        # Context carries root_cause + pattern so retrieval works via FTS
        context = f"root_cause: {root_cause} | pattern: {pattern}"
        description = error
        now = _now()

        cur = self.db.cursor()
        cur.execute(
            """INSERT INTO errors
               (session_id, category, severity, description, context, fix,
                project, tags, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (session_id, category, severity, description, context, fix,
             project, json.dumps(tags), now),
        )
        error_id = cur.lastrowid
        self.db.commit()

        # Try consolidation
        rule_id = self._try_consolidate(pattern, project=project, category=category)

        return {
            "error_id": error_id,
            "pattern": pattern,
            "consolidated": rule_id is not None,
            "rule_id": rule_id,
        }

    # ──────────────────────────────────────────────
    # Consolidation
    # ──────────────────────────────────────────────

    def _try_consolidate(
        self,
        pattern: str,
        *,
        project: str,
        category: str,
    ) -> int | None:
        """If >=threshold errors share `pattern` and no active rule exists,
        synthesize a rule and return its id."""
        cur = self.db.cursor()
        tag_needle = f'"pattern:{pattern}"'

        # Count occurrences (errors with this pattern tag)
        count = cur.execute(
            """SELECT COUNT(*) FROM errors
               WHERE tags LIKE ? AND project = ?""",
            (f"%{tag_needle}%", project),
        ).fetchone()[0]

        if count < self.threshold:
            return None

        # Do we already have an active rule for this pattern?
        rule_marker = f"[pattern:{pattern}]"
        existing = cur.execute(
            """SELECT id FROM rules
               WHERE context LIKE ? AND status = 'active'
               LIMIT 1""",
            (f"%{rule_marker}%",),
        ).fetchone()
        if existing:
            return existing[0]

        # Synthesize rule content from most recent sample
        sample = cur.execute(
            """SELECT description, fix, context FROM errors
               WHERE tags LIKE ? AND project = ?
               ORDER BY created_at DESC LIMIT 1""",
            (f"%{tag_needle}%", project),
        ).fetchone()
        if not sample:
            return None

        desc, fix, ctx = sample
        rule_content = (
            f"Prevention rule for recurring pattern '{pattern}' "
            f"(observed {count} times). Fix: {fix}"
        )
        rule_context = f"{rule_marker} original_error: {desc} | {ctx}"

        now = _now()
        try:
            cur.execute(
                """INSERT INTO rules (session_id, content, context, category,
                                      scope, priority, project, tags, status,
                                      created_at, updated_at)
                   VALUES ('auto_consolidate', ?, ?, ?, 'project', 7, ?, ?,
                           'active', ?, ?)""",
                (rule_content, rule_context, category, project,
                 json.dumps(["auto-consolidated", f"pattern:{pattern}"]),
                 now, now),
            )
            rule_id = cur.lastrowid
            # Link source errors via insight_id field (reused as rule pointer)
            cur.execute(
                """UPDATE errors SET insight_id = ?
                   WHERE tags LIKE ? AND project = ? AND insight_id IS NULL""",
                (rule_id, f"%{tag_needle}%", project),
            )
            self.db.commit()
            return rule_id
        except sqlite3.Error as e:
            self.db.rollback()
            LOG(f"consolidate failed: {e}")
            return None

    # ──────────────────────────────────────────────
    # Queries
    # ──────────────────────────────────────────────

    def pattern_frequency(
        self,
        *,
        project: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return [{pattern, count, last_seen}] sorted by count desc."""
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        conditions: list[str] = []
        params: list[Any] = []
        if project:
            conditions.append("project = ?")
            params.append(project)
        # tags JSON contains "pattern:..."
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = cur.execute(
            f"SELECT tags, created_at FROM errors {where}", params
        ).fetchall()

        freq: dict[str, dict[str, Any]] = {}
        for r in rows:
            try:
                tags = json.loads(r["tags"]) if r["tags"] else []
            except (json.JSONDecodeError, ValueError):
                continue
            for t in tags:
                if isinstance(t, str) and t.startswith("pattern:"):
                    key = t.split(":", 1)[1]
                    slot = freq.setdefault(key, {"pattern": key, "count": 0,
                                                  "last_seen": r["created_at"]})
                    slot["count"] += 1
                    if r["created_at"] > slot["last_seen"]:
                        slot["last_seen"] = r["created_at"]

        ordered = sorted(freq.values(), key=lambda x: x["count"], reverse=True)
        return ordered[:limit]

    def rules_for_pattern(self, pattern: str) -> list[dict[str, Any]]:
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            """SELECT id, content, context, priority, success_rate, status
               FROM rules WHERE context LIKE ? ORDER BY priority DESC""",
            (f"%[pattern:{pattern}]%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve(self, error_id: int, *, note: str | None = None) -> bool:
        now = _now()
        cur = self.db.execute(
            """UPDATE errors SET status = 'resolved', resolved_at = ?
               WHERE id = ? AND status != 'resolved'""",
            (now, error_id),
        )
        self.db.commit()
        return cur.rowcount > 0
