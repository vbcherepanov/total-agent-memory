#!/usr/bin/env python3
"""
Auto-save session context to MCP Memory on session end.
Called by session-end.sh hook to preserve context
even when Claude didn't manually save.

Saves a lightweight session summary as a 'fact' record
with tags=['session-autosave', 'context-recovery'].
"""

import argparse
import sqlite3
import os
import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import memory_dir

MEMORY_DIR = str(memory_dir())
DB_PATH = os.path.join(MEMORY_DIR, "memory.db")


def save_session_context(project: str, cwd: str, reason: str,
                         user_context: str, assistant_context: str) -> None:
    """Save session context to memory.db as a fact."""
    if not os.path.exists(DB_PATH):
        print(f"Memory DB not found at {DB_PATH}", file=sys.stderr)
        return

    # Build content summary
    user_summary = user_context.strip()[:500] if user_context else ""
    assistant_summary = assistant_context.strip()[:500] if assistant_context else ""

    if not user_summary and not assistant_summary:
        return  # Nothing to save

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session_id = f"auto_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    content = f"Session auto-save ({reason}).\n"
    if user_summary:
        content += f"User was working on: {user_summary}\n"
    if assistant_summary:
        content += f"Assistant context: {assistant_summary}\n"

    context_field = f"Project: {project}, CWD: {cwd}, Ended: {timestamp}"
    tags = json.dumps(["session-autosave", "context-recovery", project])

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check for recent duplicate (same project, within 5 minutes)
        cursor.execute("""
            SELECT id FROM knowledge
            WHERE project = ? AND status = 'active'
              AND tags LIKE '%session-autosave%'
              AND created_at > datetime('now', '-5 minutes')
            LIMIT 1
        """, (project,))

        if cursor.fetchone():
            conn.close()
            return  # Skip duplicate

        cursor.execute("""
            INSERT INTO knowledge (session_id, type, content, context, project, tags, status, confidence, source, created_at, last_confirmed, recall_count)
            VALUES (?, 'fact', ?, ?, ?, ?, 'active', 0.6, 'auto', ?, ?, 0)
        """, (session_id, content, context_field, project, tags, timestamp, timestamp))

        conn.commit()
        record_id = cursor.lastrowid
        conn.close()

        print(f"Auto-saved session context: id={record_id}, project={project}")

    except Exception as e:
        print(f"Error saving session context: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Auto-save session context to memory")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--cwd", required=True, help="Working directory")
    parser.add_argument("--reason", required=True, help="Session end reason")
    parser.add_argument("--user-context", default="", help="Last user messages")
    parser.add_argument("--assistant-context", default="", help="Last assistant context")

    args = parser.parse_args()

    save_session_context(
        project=args.project,
        cwd=args.cwd,
        reason=args.reason,
        user_context=args.user_context,
        assistant_context=args.assistant_context,
    )


if __name__ == "__main__":
    main()
