"""
Procedural memory — v7.0 Phase B.

Learns workflows from successful patterns and tracks outcomes so that
future executions can be predicted (success probability, expected duration).

Workflow lifecycle:
    learn_workflow(name, steps, context) → workflow_id
    predict_outcome(workflow_id | trigger)  → {success_probability, avg_duration, similar_runs}
    track_outcome(workflow_id, outcome, duration_ms, ...) → updates counters
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[procedural] {msg}\n")

VALID_OUTCOMES = {"success", "failure", "partial", "aborted"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex


class ProceduralMemory:
    """Learned workflows + outcome tracking."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    # ──────────────────────────────────────────────
    # Workflow CRUD
    # ──────────────────────────────────────────────

    def learn_workflow(
        self,
        name: str,
        steps: list[str],
        *,
        description: str | None = None,
        trigger_pattern: str | None = None,
        context: dict[str, Any] | None = None,
        project: str = "general",
        source: str = "learned",
    ) -> str:
        """Record a new workflow. If name+project already exists, update it."""
        if not name:
            raise ValueError("name is required")
        if not steps or not isinstance(steps, list):
            raise ValueError("steps must be a non-empty list")

        now = _now()
        existing = self.db.execute(
            "SELECT id FROM workflows WHERE name = ? AND project = ?",
            (name, project),
        ).fetchone()

        if existing:
            wf_id = existing[0]
            self.db.execute(
                """UPDATE workflows
                   SET steps = ?, description = COALESCE(?, description),
                       trigger_pattern = COALESCE(?, trigger_pattern),
                       context = COALESCE(?, context),
                       source = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    json.dumps(steps),
                    description,
                    trigger_pattern,
                    json.dumps(context) if context is not None else None,
                    source,
                    now,
                    wf_id,
                ),
            )
        else:
            wf_id = _new_id()
            self.db.execute(
                """INSERT INTO workflows
                   (id, name, description, trigger_pattern, steps, context,
                    project, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    wf_id, name, description, trigger_pattern,
                    json.dumps(steps),
                    json.dumps(context) if context is not None else None,
                    project, source, now, now,
                ),
            )
        self.db.commit()
        return wf_id

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        row = cur.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["steps"] = json.loads(d["steps"]) if d["steps"] else []
        if d.get("context"):
            d["context"] = json.loads(d["context"])
        return d

    def list_workflows(
        self,
        *,
        project: str | None = None,
        status: str | None = "active",
        order_by: str = "success_rate",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if project is not None:
            conditions.append("project = ?")
            params.append(project)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Whitelist order_by to prevent injection
        order_col = {
            "success_rate": "success_rate DESC",
            "times_run": "times_run DESC",
            "recent": "last_run_at DESC",
            "created": "created_at DESC",
        }.get(order_by, "success_rate DESC")

        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            f"SELECT * FROM workflows {where} ORDER BY {order_col} LIMIT ?",
            [*params, limit],
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["steps"] = json.loads(d["steps"]) if d["steps"] else []
            if d.get("context"):
                d["context"] = json.loads(d["context"])
            result.append(d)
        return result

    # ──────────────────────────────────────────────
    # Outcome tracking
    # ──────────────────────────────────────────────

    def track_outcome(
        self,
        workflow_id: str,
        outcome: str,
        *,
        duration_ms: int | None = None,
        session_id: str | None = None,
        context: dict[str, Any] | None = None,
        error_details: str | None = None,
        notes: str | None = None,
        started_at: str | None = None,
    ) -> str:
        """Record an execution outcome and refresh workflow aggregates."""
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"outcome must be one of {VALID_OUTCOMES}")

        wf = self.get_workflow(workflow_id)
        if not wf:
            raise ValueError(f"workflow {workflow_id} not found")

        now = _now()
        run_id = _new_id()

        try:
            self.db.execute(
                """INSERT INTO workflow_runs
                   (id, workflow_id, session_id, outcome, duration_ms, context,
                    error_details, notes, started_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, workflow_id, session_id, outcome, duration_ms,
                    json.dumps(context) if context is not None else None,
                    error_details, notes,
                    started_at or now,
                    now,
                ),
            )

            # Recompute aggregates from workflow_runs (source of truth)
            stats_row = self.db.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS ok,
                       SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) AS fail,
                       AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) AS avg_dur
                   FROM workflow_runs WHERE workflow_id = ?""",
                (workflow_id,),
            ).fetchone()
            total, ok, fail, avg_dur = stats_row
            success_rate = (ok / total) if total else 0.0
            avg_dur_int = int(avg_dur) if avg_dur is not None else None

            self.db.execute(
                """UPDATE workflows
                   SET times_run = ?, success_count = ?, failure_count = ?,
                       success_rate = ?, avg_duration_ms = ?,
                       last_run_at = ?, updated_at = ?
                   WHERE id = ?""",
                (total, ok or 0, fail or 0, success_rate, avg_dur_int,
                 now, now, workflow_id),
            )
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise

        return run_id

    # ──────────────────────────────────────────────
    # Prediction
    # ──────────────────────────────────────────────

    def predict_outcome(
        self,
        workflow_id: str | None = None,
        *,
        trigger: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Return predicted {success_probability, avg_duration_ms, confidence, workflow}.

        Confidence scales with number of prior runs (Laplace-smoothed so we
        don't predict 100% on a single run).
        """
        wf = None
        if workflow_id:
            wf = self.get_workflow(workflow_id)
        elif trigger:
            # Match trigger against name / trigger_pattern substring
            conditions = ["status = 'active'",
                          "(name LIKE ? OR trigger_pattern LIKE ? OR description LIKE ?)"]
            params: list[Any] = [f"%{trigger}%", f"%{trigger}%", f"%{trigger}%"]
            if project:
                conditions.append("project = ?")
                params.append(project)
            cur = self.db.cursor()
            cur.row_factory = sqlite3.Row
            row = cur.execute(
                f"""SELECT * FROM workflows
                    WHERE {' AND '.join(conditions)}
                    ORDER BY success_rate DESC, times_run DESC
                    LIMIT 1""",
                params,
            ).fetchone()
            if row:
                wf = dict(row)
                wf["steps"] = json.loads(wf["steps"]) if wf["steps"] else []

        if not wf:
            return {
                "found": False,
                "success_probability": None,
                "avg_duration_ms": None,
                "confidence": 0.0,
                "workflow": None,
            }

        # Laplace smoothing: (ok + 1) / (total + 2) — avoids 100%/0% on tiny n
        ok = wf.get("success_count", 0) or 0
        total = wf.get("times_run", 0) or 0
        smoothed = (ok + 1) / (total + 2)
        # Confidence: saturates at ~0.95 after ~20 runs
        confidence = min(0.95, total / (total + 5)) if total else 0.0

        return {
            "found": True,
            "workflow_id": wf["id"],
            "success_probability": round(smoothed, 4),
            "raw_success_rate": wf.get("success_rate", 0.0),
            "avg_duration_ms": wf.get("avg_duration_ms"),
            "times_run": total,
            "confidence": round(confidence, 4),
            "workflow": {
                "name": wf["name"],
                "steps": wf.get("steps", []),
                "description": wf.get("description"),
            },
        }

    def recent_runs(
        self,
        workflow_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            """SELECT * FROM workflow_runs
               WHERE workflow_id = ?
               ORDER BY started_at DESC, rowid DESC LIMIT ?""",
            (workflow_id, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("context"):
                d["context"] = json.loads(d["context"])
            result.append(d)
        return result

    def deprecate_workflow(self, workflow_id: str) -> bool:
        cur = self.db.execute(
            "UPDATE workflows SET status = 'deprecated', updated_at = ? WHERE id = ?",
            (_now(), workflow_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def stats(self, *, project: str | None = None) -> dict[str, Any]:
        cur = self.db.cursor()
        params: list[Any] = []
        proj_clause = ""
        if project:
            proj_clause = "WHERE project = ?"
            params = [project]
        total_wf = cur.execute(
            f"SELECT COUNT(*) FROM workflows {proj_clause}", params
        ).fetchone()[0]
        active_wf = cur.execute(
            f"""SELECT COUNT(*) FROM workflows
                WHERE status = 'active' {' AND project = ?' if project else ''}""",
            params,
        ).fetchone()[0]
        total_runs = cur.execute(
            f"""SELECT COUNT(*) FROM workflow_runs wr
                {'JOIN workflows w ON w.id = wr.workflow_id WHERE w.project = ?' if project else ''}""",
            params,
        ).fetchone()[0]
        return {
            "total_workflows": total_wf,
            "active_workflows": active_wf,
            "total_runs": total_runs,
        }
