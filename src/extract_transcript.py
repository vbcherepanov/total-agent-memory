#!/usr/bin/env python3
"""
Extract and compress Claude Code transcript for knowledge extraction.

Two-level auto-save:
1. INSTANT: Save session summary directly to memory.db (no LLM needed)
2. DETAILED: Save compact transcript to extract-queue for Claude to analyze next session

Usage:
    python3 extract_transcript.py --transcript /path/to/transcript.jsonl \
                                  --session-id <uuid> \
                                  --output-dir ~/.claude-memory/extract-queue \
                                  --db ~/.claude-memory/memory.db
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_OUTPUT_KB = 200
MAX_USER_TEXT = 2000
MAX_ASSISTANT_TEXT = 500
MAX_TOOL_INPUT_TEXT = 200
HEAD_MESSAGES = 20
TAIL_MESSAGES = 30

SENSITIVE_PATTERNS = [
    re.compile(r'(?:api[_-]?key|password|secret|token|credential|auth)\s*[:=]\s*["\']?[\w\-\.]{8,}', re.IGNORECASE),
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    re.compile(r'ghp_[a-zA-Z0-9]{36}'),
    re.compile(r'gho_[a-zA-Z0-9]{36}'),
    re.compile(r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----'),
    re.compile(r'Bearer\s+[a-zA-Z0-9\-._~+/]+=*', re.IGNORECASE),
]


def sanitize(text: str) -> str:
    """Remove sensitive data from text."""
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text


def extract_content_text(content) -> str:
    """Extract text from message content (string or array)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                parts.append(c["text"].strip())
        return "\n".join(parts)
    return ""


def extract(transcript_path: str, session_id: str, output_dir: str) -> dict:
    """Extract transcript. Returns output dict with conversation and metadata."""
    conversation = []
    memory_saves = []
    user_texts = []
    assistant_texts = []
    tools_used = set()
    files_written = []
    stats = {
        "total_messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
    }

    project_dir = ""
    project_name = ""
    git_branch = ""
    first_ts = None
    last_ts = None

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = obj.get("timestamp", "")

            if not project_dir and obj.get("cwd"):
                project_dir = obj["cwd"]
                project_name = os.path.basename(project_dir)
            if not git_branch and obj.get("gitBranch"):
                git_branch = obj["gitBranch"]
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts

            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                stats["user_messages"] += 1
                stats["total_messages"] += 1

                text = extract_content_text(content)
                if text:
                    clean_text = sanitize(text[:MAX_USER_TEXT])
                    conversation.append({
                        "role": "user",
                        "ts": ts,
                        "text": clean_text,
                    })
                    user_texts.append(clean_text)

            elif role == "assistant":
                stats["assistant_messages"] += 1
                stats["total_messages"] += 1

                if isinstance(content, str) and content.strip():
                    clean_text = sanitize(content[:MAX_ASSISTANT_TEXT])
                    conversation.append({
                        "role": "assistant",
                        "ts": ts,
                        "text": clean_text,
                    })
                    assistant_texts.append(clean_text)
                elif isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type", "")

                        if ctype == "text" and c.get("text", "").strip():
                            clean_text = sanitize(c["text"][:MAX_ASSISTANT_TEXT])
                            conversation.append({
                                "role": "assistant",
                                "ts": ts,
                                "text": clean_text,
                            })
                            assistant_texts.append(clean_text)

                        elif ctype == "tool_use":
                            stats["tool_calls"] += 1
                            tool_name = c.get("name", "")
                            tool_input = c.get("input", {})
                            tools_used.add(tool_name.split("__")[-1] if "__" in tool_name else tool_name)

                            if "memory_save" in tool_name:
                                content_preview = str(tool_input.get("content", ""))[:200]
                                ktype = tool_input.get("type", "")
                                project = tool_input.get("project", "")
                                memory_saves.append(
                                    f"memory_save(type={ktype}, project={project}, "
                                    f"content={content_preview})"
                                )

                            if tool_name in ("Write", "Edit") or tool_name.endswith("Write") or tool_name.endswith("Edit"):
                                fp = tool_input.get("file_path", "")
                                if fp:
                                    files_written.append(os.path.basename(fp))

                            input_str = json.dumps(tool_input, ensure_ascii=False)
                            conversation.append({
                                "role": "tool_call",
                                "ts": ts,
                                "tool": tool_name,
                                "input_summary": sanitize(input_str[:MAX_TOOL_INPUT_TEXT]),
                            })

    if not conversation:
        return {}

    output = {
        "version": 1,
        "session_id": session_id,
        "project_dir": project_dir,
        "project_name": project_name,
        "git_branch": git_branch,
        "started_at": first_ts,
        "ended_at": last_ts,
        "stats": stats,
        "conversation": conversation,
        "memory_saves_in_session": memory_saves,
        "user_texts": user_texts,
        "assistant_texts": assistant_texts,
        "tools_used": sorted(tools_used),
        "files_written": files_written[:20],
        "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "pending",
    }

    max_bytes = MAX_OUTPUT_KB * 1024
    output_str = json.dumps(output, ensure_ascii=False)

    if len(output_str) > max_bytes and len(conversation) > HEAD_MESSAGES + TAIL_MESSAGES:
        head = conversation[:HEAD_MESSAGES]
        tail = conversation[-TAIL_MESSAGES:]
        dropped = len(conversation) - HEAD_MESSAGES - TAIL_MESSAGES
        output["conversation"] = (
            head
            + [{"role": "system", "text": f"[... {dropped} messages omitted ...]"}]
            + tail
        )
        output["stats"]["trimmed"] = True
        output["stats"]["dropped_messages"] = dropped
        output_str = json.dumps(output, ensure_ascii=False)

    if len(output_str) > max_bytes:
        for item in output["conversation"]:
            if item.get("role") == "assistant" and "text" in item:
                item["text"] = item["text"][:200]
            if item.get("role") == "tool_call" and "input_summary" in item:
                item["input_summary"] = item["input_summary"][:100]
        output_str = json.dumps(output, ensure_ascii=False)

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_path = Path(output_dir) / f"pending-{session_id}.json"

    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_text(output_str, encoding="utf-8")
    tmp_path.rename(output_path)

    return output


def build_session_summary(data: dict) -> str:
    """Build a concise session summary from extracted data."""
    project = data.get("project_name", "unknown")
    branch = data.get("git_branch", "")
    stats = data.get("stats", {})
    user_texts = data.get("user_texts", [])
    assistant_texts = data.get("assistant_texts", [])
    tools = data.get("tools_used", [])
    files = data.get("files_written", [])
    memory_saves = data.get("memory_saves_in_session", [])

    parts = [f"Session on project '{project}'"]
    if branch:
        parts[0] += f" (branch: {branch})"
    parts[0] += f": {stats.get('user_messages', 0)} user msgs, {stats.get('tool_calls', 0)} tool calls."

    if user_texts:
        first_task = user_texts[0][:300]
        parts.append(f"Task: {first_task}")

    if assistant_texts:
        last_result = assistant_texts[-1][:300]
        parts.append(f"Last output: {last_result}")

    if tools:
        tool_list = ", ".join(tools[:10])
        parts.append(f"Tools: {tool_list}")

    if files:
        file_list = ", ".join(sorted(set(files))[:10])
        parts.append(f"Files modified: {file_list}")

    if memory_saves:
        parts.append(f"Explicit memory_save calls: {len(memory_saves)}")

    return " | ".join(parts)


def auto_save_to_db(db_path: str, session_id: str, data: dict):
    """Save session summary directly to memory.db as a fact."""
    summary = build_session_summary(data)
    if not summary or len(summary) < 20:
        return None

    project = data.get("project_name", "general")
    now = datetime.now(tz=timezone.utc).isoformat()

    try:
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")

        existing = db.execute(
            "SELECT id FROM knowledge WHERE session_id=? AND source='auto-extract'",
            (f"auto_{session_id}",)
        ).fetchone()
        if existing:
            db.close()
            return existing[0]

        cur = db.execute("""
            INSERT INTO knowledge (session_id, type, content, context, project, tags,
                                   source, confidence, created_at, last_confirmed, recall_count)
            VALUES (?, 'fact', ?, ?, ?, ?, 'auto-extract', 0.8, ?, ?, 0)
        """, (
            f"auto_{session_id}",
            sanitize(summary),
            f"Auto-extracted session summary. Started: {data.get('started_at', '')}. "
            f"Ended: {data.get('ended_at', '')}.",
            project,
            json.dumps(["session-summary", "auto-extract", project]),
            now,
            now,
        ))
        db.commit()
        rid = cur.lastrowid
        db.close()
        return rid
    except Exception as e:
        print(f"Auto-save to DB failed: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description="Extract Claude Code transcript for knowledge extraction")
    parser.add_argument("--transcript", required=True, help="Path to JSONL transcript file")
    parser.add_argument("--session-id", required=True, help="Session UUID")
    parser.add_argument("--output-dir", required=True, help="Output directory for pending extraction")
    parser.add_argument("--db", default=os.path.expanduser("~/.claude-memory/memory.db"),
                        help="Path to memory.db for auto-save")
    args = parser.parse_args()

    if not os.path.isfile(args.transcript):
        print(f"Transcript not found: {args.transcript}", file=sys.stderr)
        sys.exit(1)

    data = extract(args.transcript, args.session_id, args.output_dir)
    if not data:
        print("Empty session, nothing to extract", file=sys.stderr)
        return

    size = os.path.getsize(Path(args.output_dir) / f"pending-{args.session_id}.json")
    print(f"Extracted {size} bytes to {args.output_dir}/pending-{args.session_id}.json", file=sys.stderr)

    if os.path.isfile(args.db):
        rid = auto_save_to_db(args.db, args.session_id, data)
        if rid:
            print(f"Auto-saved session summary to memory.db (id={rid})", file=sys.stderr)


if __name__ == "__main__":
    main()
