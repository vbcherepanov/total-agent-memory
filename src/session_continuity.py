"""
Session continuity — v7.0 Phase G.

Provides `session_end` to capture a structured summary and `session_init`
to load the most recent unconsumed summary into a new session. This replaces
shell-hook-based recovery with first-class MCP tools.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[session-continuity] {msg}\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex


class SessionContinuity:
    """End-of-session summary capture + start-of-session resume."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    # ──────────────────────────────────────────────
    # End of session
    # ──────────────────────────────────────────────

    def session_end(
        self,
        session_id: str,
        summary: str,
        *,
        highlights: list[str] | None = None,
        pitfalls: list[str] | None = None,
        next_steps: list[str] | None = None,
        open_questions: list[str] | None = None,
        context_blob: str | None = None,
        project: str = "general",
        branch: str | None = None,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            raise ValueError("session_id required")
        if not summary:
            raise ValueError("summary required")

        sid = _new_id()
        now = _now()
        self.db.execute(
            """INSERT INTO session_summaries
               (id, session_id, project, branch, summary, highlights, pitfalls,
                next_steps, open_questions, context_blob, started_at,
                ended_at, consumed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                sid, session_id, project, branch, summary,
                json.dumps(highlights or []),
                json.dumps(pitfalls or []),
                json.dumps(next_steps or []),
                json.dumps(open_questions or []),
                context_blob, started_at, now, now,
            ),
        )
        self.db.commit()
        return {
            "id": sid,
            "session_id": session_id,
            "project": project,
            "ended_at": now,
            "summary_len": len(summary),
            "next_steps_count": len(next_steps or []),
        }

    # ──────────────────────────────────────────────
    # Start of session (resume)
    # ──────────────────────────────────────────────

    def session_init(
        self,
        *,
        project: str = "general",
        mark_consumed: bool = True,
        include_pitfalls: bool = True,
    ) -> dict[str, Any] | None:
        """Fetch the most recent unconsumed summary for `project`.

        Returns None if nothing to resume. When `mark_consumed=True`, sets the
        consumed flag so the same summary is not replayed twice.
        """
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        row = cur.execute(
            """SELECT * FROM session_summaries
               WHERE project = ? AND consumed = 0
               ORDER BY ended_at DESC, rowid DESC LIMIT 1""",
            (project,),
        ).fetchone()

        if not row:
            return None

        d = dict(row)
        for k in ("highlights", "pitfalls", "next_steps", "open_questions"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    d[k] = []
            else:
                d[k] = []

        if not include_pitfalls:
            d["pitfalls"] = []

        if mark_consumed:
            self.db.execute(
                "UPDATE session_summaries SET consumed = 1 WHERE id = ?",
                (d["id"],),
            )
            self.db.commit()

        return d

    # ──────────────────────────────────────────────
    # Listing / stats
    # ──────────────────────────────────────────────

    def list_summaries(
        self,
        *,
        project: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        if project:
            rows = cur.execute(
                """SELECT id, session_id, project, summary, consumed, ended_at
                   FROM session_summaries WHERE project = ?
                   ORDER BY ended_at DESC, rowid DESC LIMIT ?""",
                (project, limit),
            ).fetchall()
        else:
            rows = cur.execute(
                """SELECT id, session_id, project, summary, consumed, ended_at
                   FROM session_summaries
                   ORDER BY ended_at DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, *, project: str | None = None) -> dict[str, int]:
        cur = self.db.cursor()
        if project:
            total = cur.execute(
                "SELECT COUNT(*) FROM session_summaries WHERE project = ?",
                (project,),
            ).fetchone()[0]
            pending = cur.execute(
                """SELECT COUNT(*) FROM session_summaries
                   WHERE project = ? AND consumed = 0""",
                (project,),
            ).fetchone()[0]
        else:
            total = cur.execute(
                "SELECT COUNT(*) FROM session_summaries"
            ).fetchone()[0]
            pending = cur.execute(
                "SELECT COUNT(*) FROM session_summaries WHERE consumed = 0"
            ).fetchone()[0]
        return {"total_summaries": total, "pending": pending,
                "consumed": total - pending}

    def mark_unconsumed(self, summary_id: str) -> bool:
        cur = self.db.execute(
            "UPDATE session_summaries SET consumed = 0 WHERE id = ?",
            (summary_id,),
        )
        self.db.commit()
        return cur.rowcount > 0
