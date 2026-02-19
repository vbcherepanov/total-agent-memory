#!/usr/bin/env python3
"""
Auto-extract knowledge from ACTIVE Claude Code sessions.

Runs periodically (via launchd every 3 min) and:
1. Finds all .jsonl transcripts modified in the last 10 minutes
2. Skips transcripts already processed recently (marker file)
3. Runs extract logic and saves knowledge to memory.db

This ensures knowledge is saved DURING sessions, not just at the end.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Import from extract_transcript.py (same directory)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_transcript import extract, auto_save_knowledge, sanitize

PROJECTS_DIR = Path.home() / ".claude" / "projects"
MEMORY_DB = Path.home() / ".claude-memory" / "memory.db"
MARKER_DIR = Path.home() / ".claude-memory" / "extract-markers"
EXTRACT_QUEUE = Path.home() / ".claude-memory" / "extract-queue"
LOG_FILE = Path.home() / ".claude-memory" / "auto-extract.log"

# Only process transcripts modified in the last 10 minutes
MAX_AGE_SECONDS = 600
# Don't re-process a transcript within 3 minutes
MIN_INTERVAL_SECONDS = 180


def log(msg: str):
    """Append to log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def find_active_transcripts() -> list[Path]:
    """Find .jsonl transcripts modified recently (active sessions)."""
    now = time.time()
    active = []

    if not PROJECTS_DIR.exists():
        return active

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        # Skip subagent transcripts
        if "subagents" in str(jsonl):
            continue
        # Skip small files (< 1KB = likely empty)
        if jsonl.stat().st_size < 1024:
            continue
        # Only recent files
        age = now - jsonl.stat().st_mtime
        if age < MAX_AGE_SECONDS:
            active.append(jsonl)

    return active


def should_process(transcript: Path) -> bool:
    """Check if transcript needs processing (not processed recently)."""
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker = MARKER_DIR / f"{transcript.stem}.marker"

    if marker.exists():
        marker_age = time.time() - marker.stat().st_mtime
        if marker_age < MIN_INTERVAL_SECONDS:
            return False

    return True


def mark_processed(transcript: Path):
    """Update marker timestamp for processed transcript."""
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker = MARKER_DIR / f"{transcript.stem}.marker"
    marker.write_text(datetime.now(tz=timezone.utc).isoformat())


def process_transcript(transcript: Path):
    """Extract knowledge from transcript and save to memory.db."""
    session_id = transcript.stem
    db_path = str(MEMORY_DB)

    if not MEMORY_DB.exists():
        log(f"SKIP {session_id}: memory.db not found")
        return

    try:
        # Use "live-" prefix to distinguish from end-of-session extracts
        live_session_id = f"live-{session_id}"

        data = extract(str(transcript), live_session_id, str(EXTRACT_QUEUE))
        if not data:
            return

        # Rename pending to done immediately (auto-processed)
        pending = EXTRACT_QUEUE / f"pending-{live_session_id}.json"
        done = EXTRACT_QUEUE / f"done-{live_session_id}.json"
        if pending.exists():
            pending.rename(done)

        # Save knowledge records
        saved_ids = auto_save_knowledge(db_path, live_session_id, data)

        project = data.get("project_name", "unknown")
        stats = data.get("stats", {})
        log(
            f"OK {project}: {len(saved_ids)} records saved "
            f"({stats.get('user_messages', 0)} user msgs, "
            f"{stats.get('tool_calls', 0)} tool calls)"
        )

    except Exception as e:
        log(f"ERROR {session_id}: {e}")


def cleanup_old_markers():
    """Remove markers older than 1 hour."""
    if not MARKER_DIR.exists():
        return
    now = time.time()
    for marker in MARKER_DIR.glob("*.marker"):
        if now - marker.stat().st_mtime > 3600:
            marker.unlink()


def main():
    transcripts = find_active_transcripts()

    if not transcripts:
        return

    processed = 0
    for t in transcripts:
        if should_process(t):
            process_transcript(t)
            mark_processed(t)
            processed += 1

    if processed > 0:
        log(f"Processed {processed}/{len(transcripts)} active transcripts")

    # Periodic cleanup
    cleanup_old_markers()


if __name__ == "__main__":
    main()
