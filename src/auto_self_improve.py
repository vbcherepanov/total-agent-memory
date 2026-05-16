#!/usr/bin/env python3
"""Lightweight CLI to log errors/insights directly to the memory SQLite DB.

Called by bash hooks (memory-trigger.sh) so they don't rely on Claude
reading shell output.  No heavy imports (no chromadb, no sentence-transformers).

Usage:
    python auto_self_improve.py error --description "..." --category "..." [--severity ...] [--project ...]
    python auto_self_improve.py fix   --description "..." --category "..." --fix "..." [--project ...]
    python auto_self_improve.py check-patterns [--project ...] [--days 30]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import memory_dir

DB_PATH = memory_dir() / "memory.db"
SESSION_ID = f"hook-{datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')}"


def get_db() -> sqlite3.Connection:
    """Open DB with row_factory and WAL mode for safe concurrent writes."""
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def log_error(
    db: sqlite3.Connection,
    description: str,
    category: str,
    severity: str = "medium",
    fix: str = "",
    context: str = "",
    project: str = "general",
    tags: list[str] | None = None,
) -> int:
    """Insert an error record. Returns the new error id."""
    now = datetime.now(tz=UTC).isoformat() + "Z"
    status = "resolved" if fix else "open"
    cur = db.execute(
        """
        INSERT INTO errors
            (session_id, category, severity, description, context,
             fix, project, tags, status, resolved_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SESSION_ID,
            category,
            severity,
            description,
            context,
            fix,
            project,
            json.dumps(tags or []),
            status,
            now if fix else None,
            now,
        ),
    )
    db.commit()
    return cur.lastrowid  # type: ignore[return-value]


def log_fix(
    db: sqlite3.Connection,
    description: str,
    category: str,
    fix: str,
    project: str = "general",
) -> dict:
    """Find the most recent open error in the same category/project and resolve it.

    If none found, insert a new resolved error record.
    """
    now = datetime.now(tz=UTC).isoformat() + "Z"

    # Try to resolve the latest open error in same category+project
    row = db.execute(
        """
        SELECT id FROM errors
        WHERE category=? AND project=? AND status='open'
        ORDER BY created_at DESC LIMIT 1
        """,
        (category, project),
    ).fetchone()

    if row:
        error_id = row["id"]
        db.execute(
            """
            UPDATE errors
            SET fix=?, status='resolved', resolved_at=?
            WHERE id=?
            """,
            (fix, now, error_id),
        )
        db.commit()
        return {"action": "resolved", "error_id": error_id}

    # No open error found -- insert a new resolved record
    new_id = log_error(
        db,
        description=description,
        category=category,
        fix=fix,
        project=project,
        tags=["auto-fix"],
    )
    return {"action": "created_resolved", "error_id": new_id}


def check_patterns(
    db: sqlite3.Connection,
    project: str = "general",
    days: int = 30,
) -> list[dict]:
    """Detect error categories with 3+ occurrences and auto-create insights."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat() + "Z"
    now = datetime.now(tz=UTC).isoformat() + "Z"

    rows = db.execute(
        """
        SELECT category, COUNT(*) as cnt, GROUP_CONCAT(id) as ids
        FROM errors
        WHERE project=? AND status NOT IN ('insight_extracted')
          AND created_at > ?
        GROUP BY category
        HAVING cnt >= 3
        """,
        (project, cutoff),
    ).fetchall()

    created_insights: list[dict] = []

    for row in rows:
        category = row["category"]
        error_ids = [int(x) for x in (row["ids"] or "").split(",") if x]

        # Skip if an active insight already exists for this category+project
        existing = db.execute(
            """
            SELECT id FROM insights
            WHERE category=? AND project=? AND status='active'
            LIMIT 1
            """,
            (category, project),
        ).fetchone()

        if existing:
            # Auto-upvote the existing insight
            db.execute(
                """
                UPDATE insights
                SET importance = importance + 1,
                    confidence = MIN(1.0, confidence + 0.05),
                    fire_count = fire_count + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, existing["id"]),
            )
            db.commit()
            created_insights.append(
                {
                    "action": "upvoted",
                    "insight_id": existing["id"],
                    "category": category,
                    "error_count": row["cnt"],
                }
            )
            continue

        # Gather descriptions for the insight content
        placeholders = ",".join("?" * len(error_ids[:10]))
        descs = db.execute(
            f"""
            SELECT description, fix FROM errors
            WHERE id IN ({placeholders})
            ORDER BY created_at DESC
            """,
            error_ids[:10],
        ).fetchall()

        summary_parts = []
        for d in descs[:5]:
            line = d["description"][:150]
            if d["fix"]:
                line += f" | fix: {d['fix'][:100]}"
            summary_parts.append(line)
        summary = "; ".join(summary_parts)

        content = (
            f"Repeated pattern ({row['cnt']}x): {category} errors in {project}. "
            f"Examples: {summary}"
        )

        cur = db.execute(
            """
            INSERT INTO insights
                (session_id, content, context, category, importance, confidence,
                 source_error_ids, project, tags, status, created_at, updated_at)
            VALUES (?,?,?,?,3,0.6,?,?,?,'active',?,?)
            """,
            (
                SESSION_ID,
                content[:1000],
                f"Auto-detected pattern from {row['cnt']} errors",
                category,
                json.dumps(error_ids[:10]),
                project,
                json.dumps(["auto-pattern", category]),
                now,
                now,
            ),
        )
        db.commit()
        insight_id = cur.lastrowid

        # Mark source errors
        for eid in error_ids[:10]:
            db.execute(
                "UPDATE errors SET status='insight_extracted', insight_id=? WHERE id=?",
                (insight_id, eid),
            )
        db.commit()

        created_insights.append(
            {
                "action": "created",
                "insight_id": insight_id,
                "category": category,
                "error_count": row["cnt"],
                "content": content[:200],
            }
        )

    return created_insights


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Direct DB writer for self-improvement pipeline"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- error ---
    p_err = sub.add_parser("error", help="Log an error to memory DB")
    p_err.add_argument("--description", required=True)
    p_err.add_argument("--category", required=True)
    p_err.add_argument("--severity", default="medium", choices=["low", "medium", "high", "critical"])
    p_err.add_argument("--fix", default="")
    p_err.add_argument("--context", default="")
    p_err.add_argument("--project", default="general")

    # --- fix ---
    p_fix = sub.add_parser("fix", help="Resolve a recent error with a fix")
    p_fix.add_argument("--description", required=True)
    p_fix.add_argument("--category", required=True)
    p_fix.add_argument("--fix", required=True)
    p_fix.add_argument("--project", default="general")

    # --- check-patterns ---
    p_pat = sub.add_parser("check-patterns", help="Detect 3+ error patterns, auto-create insights")
    p_pat.add_argument("--project", default="general")
    p_pat.add_argument("--days", type=int, default=30)

    args = parser.parse_args()
    db = get_db()

    try:
        if args.command == "error":
            eid = log_error(
                db,
                description=args.description,
                category=args.category,
                severity=args.severity,
                fix=args.fix,
                context=args.context,
                project=args.project,
            )
            print(json.dumps({"ok": True, "error_id": eid}))

        elif args.command == "fix":
            result = log_fix(
                db,
                description=args.description,
                category=args.category,
                fix=args.fix,
                project=args.project,
            )
            print(json.dumps({"ok": True, **result}))

        elif args.command == "check-patterns":
            patterns = check_patterns(db, project=args.project, days=args.days)
            print(json.dumps({"ok": True, "patterns": patterns, "count": len(patterns)}))

    finally:
        db.close()


if __name__ == "__main__":
    main()
