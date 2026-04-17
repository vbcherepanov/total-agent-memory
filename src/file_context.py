"""
File-context guard — v7.0 Phase C.

Before editing a file, surface warnings from past errors, lessons, and
knowledge related to that file. Scans:
  - errors table (self_error_log history)
  - knowledge table (solutions / lessons / conventions tagged with the path)

Matching strategy:
  1. Exact path in `tags` JSON array (strongest signal).
  2. Path substring in description/context/content (medium).
  3. Basename substring match (weak, only when exact & substring miss).

Returns a structured dossier:
  {
    file: str,
    risk_score: float 0..1,
    summary: str,
    warnings: [
      {source: 'error'|'knowledge', severity: str, content: str, ...},
      ...
    ],
    related_rules: [...],   # from self_rules table if available
  }
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[file-context] {msg}\n")


# Severity weights for risk scoring
SEVERITY_WEIGHTS = {
    "critical": 1.0,
    "high": 0.75,
    "medium": 0.5,
    "low": 0.25,
    "info": 0.1,
}


def _norm_path(p: str) -> str:
    """Normalize path for matching: collapse separators, expand ~."""
    if not p:
        return ""
    p = os.path.expanduser(p)
    # Keep as-is (don't abspath — memory may store relative paths)
    return p.replace("\\", "/").strip()


def _parse_tags(tags_raw: Any) -> list[str]:
    if tags_raw is None:
        return []
    if isinstance(tags_raw, list):
        return [str(t) for t in tags_raw]
    if isinstance(tags_raw, str):
        try:
            parsed = json.loads(tags_raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _path_matches_tags(path: str, tags: list[str]) -> bool:
    nrm = _norm_path(path)
    base = os.path.basename(nrm)
    for t in tags:
        t_norm = _norm_path(t)
        if t_norm == nrm:
            return True
        # Tag like "file:path/to.py"
        if t_norm.startswith("file:") and t_norm[5:] == nrm:
            return True
        if t_norm == base:
            return True
    return False


def _path_appears_in_text(path: str, *texts: str | None) -> bool:
    nrm = _norm_path(path)
    base = os.path.basename(nrm)
    for text in texts:
        if not text:
            continue
        tn = text.replace("\\", "/")
        if nrm in tn:
            return True
        # Basename match only when basename is distinctive (len > 3)
        if len(base) > 3 and re.search(rf"\b{re.escape(base)}\b", tn):
            return True
    return False


class FileContextGuard:
    """Surface warnings relevant to a specific file path."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def get_file_warnings(
        self,
        path: str,
        *,
        project: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return structured warnings for editing `path`.

        `project` filters both errors and knowledge.
        `limit` caps the number of items per source.
        """
        if not path:
            return {
                "file": "",
                "risk_score": 0.0,
                "summary": "path is empty",
                "warnings": [],
                "related_rules": [],
            }

        norm = _norm_path(path)
        warnings: list[dict[str, Any]] = []

        warnings.extend(self._scan_errors(norm, project, limit))
        warnings.extend(self._scan_knowledge(norm, project, limit))

        risk = self._compute_risk(warnings)
        summary = self._make_summary(norm, warnings, risk)
        rules = self._scan_rules(norm, limit)

        return {
            "file": norm,
            "risk_score": round(risk, 3),
            "summary": summary,
            "warnings": warnings[: 2 * limit],
            "related_rules": rules,
        }

    # ──────────────────────────────────────────────
    # Scanners
    # ──────────────────────────────────────────────

    def _scan_errors(
        self,
        path: str,
        project: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not self._table_exists("errors"):
            return []

        conditions: list[str] = []
        params: list[Any] = []
        if project:
            conditions.append("project = ?")
            params.append(project)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            f"""SELECT id, category, severity, description, context, fix,
                       tags, status, created_at, resolved_at
                FROM errors {where}
                ORDER BY created_at DESC LIMIT ?""",
            [*params, limit * 10],
        ).fetchall()

        matches: list[dict[str, Any]] = []
        for r in rows:
            tags = _parse_tags(r["tags"])
            is_match = (
                _path_matches_tags(path, tags)
                or _path_appears_in_text(path, r["description"], r["context"], r["fix"])
            )
            if not is_match:
                continue
            matches.append({
                "source": "error",
                "severity": r["severity"] or "medium",
                "category": r["category"],
                "content": r["description"],
                "context": r["context"],
                "fix": r["fix"],
                "status": r["status"],
                "created_at": r["created_at"],
                "resolved_at": r["resolved_at"],
                "error_id": r["id"],
            })
            if len(matches) >= limit:
                break
        return matches

    def _scan_knowledge(
        self,
        path: str,
        project: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not self._table_exists("knowledge"):
            return []

        conditions = ["status = 'active'"]
        params: list[Any] = []
        if project:
            conditions.append("project = ?")
            params.append(project)

        where = f"WHERE {' AND '.join(conditions)}"

        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            f"""SELECT id, type, content, context, tags, confidence,
                       created_at, recall_count
                FROM knowledge {where}
                ORDER BY confidence DESC, created_at DESC LIMIT ?""",
            [*params, limit * 10],
        ).fetchall()

        matches: list[dict[str, Any]] = []
        for r in rows:
            tags = _parse_tags(r["tags"])
            is_match = (
                _path_matches_tags(path, tags)
                or _path_appears_in_text(path, r["content"], r["context"])
            )
            if not is_match:
                continue
            # Map knowledge type → severity
            sev = {
                "solution": "medium",
                "lesson": "high",
                "convention": "medium",
                "fact": "low",
                "decision": "medium",
            }.get((r["type"] or "").lower(), "low")
            matches.append({
                "source": "knowledge",
                "severity": sev,
                "type": r["type"],
                "content": r["content"],
                "context": r["context"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
                "recall_count": r["recall_count"],
                "knowledge_id": r["id"],
            })
            if len(matches) >= limit:
                break
        return matches

    def _scan_rules(self, path: str, limit: int) -> list[dict[str, Any]]:
        if not self._table_exists("rules"):
            return []
        cur = self.db.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            """SELECT id, content, context, category, priority, success_rate
               FROM rules WHERE status = 'active'
               ORDER BY priority DESC, success_rate DESC LIMIT ?""",
            [limit * 5],
        ).fetchall()
        matches: list[dict[str, Any]] = []
        for r in rows:
            if _path_appears_in_text(path, r["content"], r["context"]):
                matches.append({
                    "rule_id": r["id"],
                    "content": r["content"],
                    "context": r["context"],
                    "category": r["category"],
                    "priority": r["priority"],
                    "success_rate": r["success_rate"],
                })
                if len(matches) >= limit:
                    break
        return matches

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _table_exists(self, name: str) -> bool:
        cur = self.db.cursor()
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _compute_risk(self, warnings: list[dict[str, Any]]) -> float:
        if not warnings:
            return 0.0
        # Sum severity weights with diminishing returns (sqrt for scaling)
        total = 0.0
        for w in warnings:
            sev = (w.get("severity") or "low").lower()
            total += SEVERITY_WEIGHTS.get(sev, 0.25)
            # Errors get extra weight
            if w.get("source") == "error":
                total += 0.2
            # Unresolved open errors heavier
            if w.get("status") == "open":
                total += 0.15
        # Squash to [0, 1] via 1 - exp(-x/3)
        import math
        return min(1.0, 1.0 - math.exp(-total / 3.0))

    def _make_summary(
        self,
        path: str,
        warnings: list[dict[str, Any]],
        risk: float,
    ) -> str:
        if not warnings:
            return f"No prior context for {path}. Proceed normally."

        err_cnt = sum(1 for w in warnings if w.get("source") == "error")
        open_cnt = sum(1 for w in warnings if w.get("status") == "open")
        kn_cnt = sum(1 for w in warnings if w.get("source") == "knowledge")

        parts = [f"⚠️  {path}: risk={risk:.2f}"]
        if err_cnt:
            parts.append(f"{err_cnt} past error(s)")
            if open_cnt:
                parts.append(f"{open_cnt} unresolved")
        if kn_cnt:
            parts.append(f"{kn_cnt} related lesson(s)/conventions")
        return " · ".join(parts)
