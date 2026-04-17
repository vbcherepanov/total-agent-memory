"""
Temporal Knowledge Graph — v7.0 Phase A.

Tracks fact assertions with validity intervals so that facts can evolve
over time without destroying history. Enables point-in-time queries:

    "What was our auth stack on 2026-03-15?"

Design:
- Append-only `fact_assertions` log. `valid_to IS NULL` means currently valid.
- `graph_edges` still stores the *current* projected state (unchanged).
- Invalidation closes an assertion (sets valid_to) and optionally links to
  the superseding assertion; graph_edges is updated in lockstep.
- Subjects/objects can reference graph_nodes.id or be free-form strings.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[temporal-kg] {msg}\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex


class TemporalKG:
    """Append-only temporal fact store with point-in-time queries."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    # ──────────────────────────────────────────────
    # Write path
    # ──────────────────────────────────────────────

    def add_fact(
        self,
        subject: str,
        predicate: str,
        object: str,
        *,
        subject_name: str | None = None,
        object_name: str | None = None,
        confidence: float = 1.0,
        context: str | None = None,
        source: str = "auto",
        project: str = "general",
        valid_from: str | None = None,
        invalidate_previous: bool = True,
    ) -> str:
        """Record a new fact assertion.

        If `invalidate_previous` is True (default) and a currently-valid
        assertion with same (subject, predicate) but different object exists,
        it is closed with valid_to = now and superseded_by = new assertion.

        Returns the new assertion id.
        """
        if not subject or not predicate or not object:
            raise ValueError("subject, predicate, object must all be non-empty")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError("confidence must be in [0, 1]")

        now = valid_from or _now()
        new_id = _new_id()

        cur = self.db.cursor()
        try:
            # Dedup: if a currently-valid assertion with EXACT same (s,p,o) exists,
            # just return its id (idempotent).
            existing = cur.execute(
                """SELECT id FROM fact_assertions
                   WHERE subject = ? AND predicate = ? AND object = ?
                   AND valid_to IS NULL AND project = ?
                   LIMIT 1""",
                (subject, predicate, object, project),
            ).fetchone()
            if existing:
                return existing[0]

            # Insert new assertion
            cur.execute(
                """INSERT INTO fact_assertions
                   (id, subject, predicate, object, subject_name, object_name,
                    confidence, context, source, project, valid_from, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id, subject, predicate, object, subject_name, object_name,
                 confidence, context, source, project, now, now),
            )

            if invalidate_previous:
                # Close any currently-valid assertion with same (s,p) but different object
                to_close = cur.execute(
                    """SELECT id FROM fact_assertions
                       WHERE subject = ? AND predicate = ? AND object != ?
                       AND valid_to IS NULL AND project = ?""",
                    (subject, predicate, object, project),
                ).fetchall()
                for (old_id,) in to_close:
                    cur.execute(
                        """UPDATE fact_assertions
                           SET valid_to = ?, superseded_by = ?,
                               invalidation_reason = 'replaced_by_newer_assertion'
                           WHERE id = ?""",
                        (now, new_id, old_id),
                    )

            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise

        return new_id

    def invalidate_fact(
        self,
        subject: str,
        predicate: str,
        object: str,
        *,
        project: str = "general",
        reason: str = "manually_invalidated",
        at: str | None = None,
    ) -> int:
        """Close all currently-valid assertions matching (s,p,o).

        Returns the number of assertions closed.
        """
        now = at or _now()
        cur = self.db.cursor()
        try:
            cur.execute(
                """UPDATE fact_assertions
                   SET valid_to = ?, invalidation_reason = ?
                   WHERE subject = ? AND predicate = ? AND object = ?
                   AND valid_to IS NULL AND project = ?""",
                (now, reason, subject, predicate, object, project),
            )
            closed = cur.rowcount
            self.db.commit()
            return closed
        except sqlite3.Error:
            self.db.rollback()
            raise

    def invalidate_assertion(self, assertion_id: str, *, reason: str = "manual", at: str | None = None) -> bool:
        """Close a specific assertion by id. Returns True if it was open."""
        now = at or _now()
        cur = self.db.cursor()
        cur.execute(
            """UPDATE fact_assertions
               SET valid_to = ?, invalidation_reason = ?
               WHERE id = ? AND valid_to IS NULL""",
            (now, reason, assertion_id),
        )
        changed = cur.rowcount > 0
        self.db.commit()
        return changed

    # ──────────────────────────────────────────────
    # Read path
    # ──────────────────────────────────────────────

    def get_current(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        *,
        project: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return currently-valid assertions matching filters (valid_to IS NULL)."""
        return self.query_at(None, subject=subject, predicate=predicate,
                             object=object, project=project, limit=limit)

    def query_at(
        self,
        timestamp: str | None,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return assertions that were valid at `timestamp`.

        timestamp=None → "now" (returns currently-valid assertions).
        timestamp=ISO 8601 string → returns assertions where
            valid_from <= timestamp AND (valid_to IS NULL OR valid_to > timestamp).
        """
        conditions: list[str] = []
        params: list[Any] = []

        if timestamp is None:
            conditions.append("valid_to IS NULL")
        else:
            conditions.append("valid_from <= ?")
            conditions.append("(valid_to IS NULL OR valid_to > ?)")
            params.extend([timestamp, timestamp])

        if subject is not None:
            conditions.append("subject = ?")
            params.append(subject)
        if predicate is not None:
            conditions.append("predicate = ?")
            params.append(predicate)
        if object is not None:
            conditions.append("object = ?")
            params.append(object)
        if project is not None:
            conditions.append("project = ?")
            params.append(project)

        where = " AND ".join(conditions)
        sql = f"""SELECT * FROM fact_assertions
                  WHERE {where}
                  ORDER BY valid_from DESC
                  LIMIT ?"""
        params.append(limit)

        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def timeline(
        self,
        subject: str,
        *,
        predicate: str | None = None,
        project: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Full chronological history of assertions for a subject."""
        conditions = ["subject = ?"]
        params: list[Any] = [subject]
        if predicate is not None:
            conditions.append("predicate = ?")
            params.append(predicate)
        if project is not None:
            conditions.append("project = ?")
            params.append(project)

        where = " AND ".join(conditions)
        sql = f"""SELECT * FROM fact_assertions
                  WHERE {where}
                  ORDER BY valid_from ASC
                  LIMIT ?"""
        params.append(limit)

        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def diff(
        self,
        t1: str,
        t2: str,
        *,
        project: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return {added, removed, changed} between two timestamps.

        added: valid at t2 but not at t1 (new facts).
        removed: valid at t1 but not at t2 (retracted facts).
        changed: same (subject, predicate) but different object.
        """
        at_t1 = {(r["subject"], r["predicate"], r["object"]): r
                 for r in self.query_at(t1, project=project, limit=100000)}
        at_t2 = {(r["subject"], r["predicate"], r["object"]): r
                 for r in self.query_at(t2, project=project, limit=100000)}

        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []

        # Keys by (subject, predicate) for change detection
        sp_t1: dict[tuple[str, str], str] = {}
        for (s, p, o) in at_t1:
            sp_t1[(s, p)] = o
        sp_t2: dict[tuple[str, str], str] = {}
        for (s, p, o) in at_t2:
            sp_t2[(s, p)] = o

        for key, row in at_t2.items():
            if key not in at_t1:
                s, p, _ = key
                if (s, p) in sp_t1 and sp_t1[(s, p)] != sp_t2[(s, p)]:
                    changed.append({
                        "subject": s, "predicate": p,
                        "from_object": sp_t1[(s, p)],
                        "to_object": sp_t2[(s, p)],
                        "assertion": row,
                    })
                else:
                    added.append(row)

        for key, row in at_t1.items():
            if key not in at_t2:
                s, p, _ = key
                # If (s, p) has a new object in t2 → already captured as 'changed'
                if (s, p) in sp_t2 and sp_t2[(s, p)] != sp_t1[(s, p)]:
                    continue
                removed.append(row)

        return {"added": added, "removed": removed, "changed": changed}

    def stats(self, *, project: str | None = None) -> dict[str, int]:
        """Basic counts for monitoring / dashboard."""
        cur = self.db.cursor()
        proj_clause = "WHERE project = ?" if project else ""
        params: list[Any] = [project] if project else []

        total = cur.execute(
            f"SELECT COUNT(*) FROM fact_assertions {proj_clause}", params
        ).fetchone()[0]

        current = cur.execute(
            f"""SELECT COUNT(*) FROM fact_assertions
                WHERE valid_to IS NULL {' AND project = ?' if project else ''}""",
            params,
        ).fetchone()[0]

        closed = total - current

        distinct_subjects = cur.execute(
            f"SELECT COUNT(DISTINCT subject) FROM fact_assertions {proj_clause}", params
        ).fetchone()[0]

        return {
            "total_assertions": total,
            "currently_valid": current,
            "closed": closed,
            "distinct_subjects": distinct_subjects,
        }
