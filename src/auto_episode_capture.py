#!/usr/bin/env python3
"""Auto-capture session episode from transcript.

Called by session-end.sh hook after each session.
Reads transcript, extracts signals, generates episode, saves to DB.

Usage:
    python src/auto_episode_capture.py --session-id SESSION_ID [--db PATH] [--project PROJECT]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from memory_systems.episode_store import EpisodeStore
from memory_systems.self_model import SelfModel
from memory_systems.signals import SignalExtractor

MEMORY_DIR = Path(
    os.environ.get("CLAUDE_MEMORY_DIR", os.path.expanduser("~/.claude-memory"))
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LOG = lambda msg: sys.stderr.write(f"[auto-episode] {msg}\n")


def find_transcript(
    session_id: str, transcript_path: str | None = None
) -> Path | None:
    """Find the JSONL transcript file for a session.

    Searches Claude Code project directories for <session_id>.jsonl files.
    Actual paths look like: ~/.claude/projects/<escaped-cwd>/<uuid>.jsonl

    Args:
        session_id: Session UUID or full path to transcript.
        transcript_path: Direct path to transcript (from hook's transcript_path).
    """
    # Priority 1: explicit transcript_path from caller
    if transcript_path:
        p = Path(transcript_path)
        if p.exists() and p.suffix == ".jsonl":
            return p

    # Priority 2: session_id IS a full path
    candidate = Path(session_id)
    if candidate.exists() and candidate.suffix == ".jsonl":
        return candidate

    claude_dir = Path.home() / ".claude"

    # Priority 3: search ~/.claude/projects/*/<session_id>.jsonl
    for match in claude_dir.glob(f"projects/*/{session_id}.jsonl"):
        if match.is_file():
            return match

    # Priority 4: legacy search in projects/*/sessions/ subdirs
    for sessions_dir in claude_dir.glob("projects/*/sessions"):
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            transcript = session_dir / "transcript.jsonl"
            if transcript.exists() and session_id in session_dir.name:
                return transcript

    # Priority 5: raw logs fallback
    raw_log = MEMORY_DIR / "raw" / f"{session_id}.jsonl"
    if raw_log.exists():
        return raw_log

    return None


def extract_messages_from_transcript(
    transcript_path: Path, max_messages: int = 200
) -> list[dict]:
    """Extract user/assistant messages from JSONL transcript."""
    messages: list[dict] = []
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")

                # New Claude Code format: type=user/assistant, content in entry["message"]
                msg = entry.get("message", {}) if isinstance(entry.get("message"), dict) else {}
                role = msg.get("role", "") or entry.get("role", "")
                content = msg.get("content", "") or entry.get("content", "")

                def _extract_text(c) -> str:
                    if isinstance(c, list):
                        return " ".join(
                            item.get("text", "")
                            for item in c
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    elif isinstance(c, str):
                        return c
                    return ""

                # Extract text content from user messages
                if entry_type == "user" or (role == "user" and entry_type in ("human", "")):
                    text = _extract_text(content)
                    if text.strip():
                        messages.append({"role": "user", "text": text.strip()})

                # Extract text content from assistant messages
                elif entry_type == "assistant" or role == "assistant":
                    text = _extract_text(content)
                    if text.strip():
                        messages.append(
                            {"role": "assistant", "text": text.strip()[:500]}
                        )

                # Track tool usage
                elif entry_type == "tool_use":
                    tool_name = entry.get("name", "") or entry.get("tool", "")
                    if tool_name:
                        messages.append(
                            {"role": "tool", "text": f"{tool_name}"}
                        )

    except Exception as e:
        LOG(f"Error reading transcript: {e}")

    return messages[-max_messages:]


def extract_concepts_from_session(
    messages: list[dict], project: str
) -> list[str]:
    """Extract likely technical concepts from session messages."""
    all_text = " ".join(m.get("text", "") for m in messages)
    text_lower = all_text.lower()

    tech_keywords = {
        "auth", "authentication", "jwt", "oauth", "api", "rest", "graphql",
        "database", "postgresql", "redis", "docker", "kubernetes",
        "go", "golang", "php", "symfony", "python", "vue", "nuxt",
        "testing", "deployment", "ci", "cd", "migration", "refactoring",
        "webhook", "payment", "billing", "notification", "email",
        "error", "bug", "debug", "performance", "security", "cache",
        "frontend", "backend", "fullstack", "microservice",
        "grpc", "protobuf", "websocket", "queue", "rabbitmq",
        "typescript", "react", "nextjs", "tailwind", "css",
        "nginx", "ssl", "dns", "monitoring", "logging",
        "memory", "hook", "session", "transcript", "episode",
    }

    found = []
    for keyword in tech_keywords:
        if keyword in text_lower:
            found.append(keyword)

    # Add project as concept if meaningful
    # Skip generic/home-dir names that add no semantic value
    generic_projects = {"general", "home", "user", "default"}
    if project and project.lower() not in generic_projects:
        found.append(project.lower())

    return found[:10]


def generate_narrative_ollama(
    messages: list[dict], project: str
) -> dict | None:
    """Use Ollama to generate episode narrative."""
    summary_parts = []
    for m in messages[-30:]:
        role = m.get("role", "?")
        text = m.get("text", "")[:300]
        summary_parts.append(f"[{role}] {text}")

    summary_text = "\n".join(summary_parts)

    prompt = (
        "Analyze this coding session and generate a brief episode summary.\n"
        "Return ONLY valid JSON:\n\n"
        "{\n"
        '  "narrative": "2-3 sentence story of what happened",\n'
        '  "outcome": "breakthrough|failure|routine|discovery",\n'
        '  "impact_score": 0.0-1.0,\n'
        '  "key_insight": "the main learning or null",\n'
        '  "approaches_tried": ["approach 1", "approach 2"]\n'
        "}\n\n"
        f"Project: {project}\n"
        f"Session transcript (last messages):\n{summary_text[:3000]}"
    )

    try:
        payload = json.dumps({
            "model": "qwen2.5-coder:32b",
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 500},
        }).encode()

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            response_text = data.get("response", "")

            # Try direct JSON parse
            try:
                result = json.loads(response_text)
                # Validate expected fields
                if "narrative" in result:
                    return result
            except json.JSONDecodeError:
                pass

            # Try to extract JSON block from response
            match = re.search(r"\{[^{}]*\}", response_text, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group())
                    if "narrative" in result:
                        return result
                except json.JSONDecodeError:
                    pass

    except Exception as e:
        LOG(f"Ollama narrative generation failed: {e}")

    return None


def generate_narrative_heuristic(
    messages: list[dict], signals: dict, project: str
) -> dict:
    """Heuristic fallback when Ollama is unavailable."""
    corrections = signals.get("correction_count", 0)
    total = signals.get("total_messages", 0)

    se = SignalExtractor()
    outcome = se.estimate_outcome(signals)
    impact = se.estimate_impact(signals)

    if outcome == "breakthrough":
        narrative = (
            f"Session on {project}: Solved a challenging problem after iteration. "
            f"{corrections} corrections needed but ended successfully."
        )
    elif outcome == "failure":
        narrative = (
            f"Session on {project}: Struggled with the task. "
            f"Had {corrections} corrections and couldn't fully resolve the issue."
        )
    elif outcome == "discovery":
        narrative = (
            f"Session on {project}: Productive session with new discoveries. "
            f"Smooth workflow with minimal corrections."
        )
    else:
        narrative = (
            f"Session on {project}: Routine work session. "
            f"{total} messages exchanged."
        )

    return {
        "narrative": narrative,
        "outcome": outcome,
        "impact_score": impact,
        "key_insight": None,
        "approaches_tried": [],
    }


def capture_episode(
    session_id: str,
    db_path: str,
    project: str = "general",
    transcript_path_arg: str | None = None,
) -> None:
    """Main entry point: capture episode from session transcript."""
    LOG(f"Capturing episode for session {session_id} (project: {project})")

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    # Check if episode already exists for this session
    try:
        existing = db.execute(
            "SELECT id FROM episodes WHERE session_id = ?", (session_id,)
        ).fetchone()
        if existing:
            LOG(f"Episode already exists for session {session_id}, skipping")
            db.close()
            return
    except sqlite3.OperationalError:
        LOG("Episodes table not found, skipping")
        db.close()
        return

    # Find transcript
    transcript_path = find_transcript(session_id, transcript_path_arg)
    if transcript_path is None:
        LOG(f"No transcript found for session {session_id}")
        db.close()
        return

    LOG(f"Found transcript: {transcript_path}")

    # Extract messages
    messages = extract_messages_from_transcript(transcript_path)
    if len(messages) < 3:
        LOG(f"Too few messages ({len(messages)}), skipping episode capture")
        db.close()
        return

    LOG(f"Extracted {len(messages)} messages from transcript")

    # Extract signals from user messages
    se = SignalExtractor()
    signals = se.extract(messages)
    LOG(
        f"Signals: corrections={signals['correction_count']}, "
        f"positive={signals['positive_count']}, "
        f"satisfaction={signals['satisfaction_score']:.2f}"
    )

    # Generate narrative (try Ollama first, fallback to heuristic)
    narrative_data = generate_narrative_ollama(messages, project)
    if not narrative_data:
        LOG("Ollama unavailable, using heuristic narrative")
        narrative_data = generate_narrative_heuristic(messages, signals, project)

    # Extract concepts
    concepts = extract_concepts_from_session(messages, project)

    # Determine tools used from tool messages
    tools_used: set[str] = set()
    for m in messages:
        if m.get("role") == "tool":
            tool_name = m.get("text", "").strip()
            if tool_name:
                tools_used.add(tool_name)

    # Save episode
    es = EpisodeStore(db)
    episode_id = es.save(
        session_id=session_id,
        narrative=narrative_data.get("narrative", "Session completed"),
        outcome=narrative_data.get("outcome", "routine"),
        project=project,
        impact_score=narrative_data.get("impact_score", 0.5),
        concepts=concepts,
        approaches_tried=narrative_data.get("approaches_tried"),
        key_insight=narrative_data.get("key_insight"),
        frustration_signals=signals.get("correction_count", 0),
        user_corrections=signals.get("corrections"),
        tools_used=list(tools_used),
    )

    LOG(
        f"Episode saved: {episode_id} [{narrative_data.get('outcome', '?')}] "
        f"impact={narrative_data.get('impact_score', 0):.1f}"
    )

    # Update self-model competencies based on session outcome
    try:
        sm = SelfModel(db)
        outcome = narrative_data.get("outcome", "routine")
        for concept in concepts:
            sm.update_competency(
                concept,
                outcome,
                frustration_signals=signals.get("correction_count", 0),
            )
    except sqlite3.OperationalError as e:
        LOG(f"Could not update competencies: {e}")

    db.commit()
    db.close()

    LOG(f"Episode capture complete for {session_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Auto-capture episode from session transcript"
    )
    parser.add_argument("--session-id", required=True, help="Session identifier")
    parser.add_argument(
        "--db",
        default=str(MEMORY_DIR / "memory.db"),
        help="Path to memory database",
    )
    parser.add_argument(
        "--project", default="general", help="Project name for the episode"
    )
    parser.add_argument(
        "--transcript", default=None, help="Direct path to transcript JSONL file"
    )
    args = parser.parse_args()

    capture_episode(args.session_id, args.db, args.project, args.transcript)
