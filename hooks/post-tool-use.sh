#!/usr/bin/env bash
# ===========================================
# PostToolUse Hook — observation capture + memory_save nudges
#
# Runs AFTER a tool call. Two responsibilities:
#
#  1. (opt-in) when MEMORY_POST_TOOL_CAPTURE=1, enqueue a deferred
#     observation for the extractor.
#  2. (always-on) bump per-session counters (writes / edits / saves)
#     and, when the writes-without-saves ratio crosses a threshold,
#     echo a nudge line to stdout so Claude sees it on the next turn.
#     This addresses the "model never calls memory_save on its own"
#     pattern (reported 2026-05-14 by client running Sonnet).
#
# Env:
#   MEMORY_POST_TOOL_CAPTURE  — "1" to enable observation capture
#   MEMORY_NUDGE_DISABLE      — "1" to disable nudges entirely
#   MEMORY_NUDGE_SOFT/HARD/STEP — tune thresholds (see memory-nudge.sh)
#   CLAUDE_MEMORY_INSTALL_DIR — install root (auto-resolved)
#   CLAUDE_MEMORY_DIR         — memory storage (~/.claude-memory)
#
# Hook: PostToolUse (matcher: "*")
# ===========================================

CLAUDE_MEMORY_INSTALL_DIR="${CLAUDE_MEMORY_INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)}"
CLAUDE_MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}"

HOOK_PYTHON="${CLAUDE_MEMORY_INSTALL_DIR}/.venv/bin/python"
if [ ! -x "$HOOK_PYTHON" ]; then
    HOOK_PYTHON="python3"
fi

SRC_DIR="${CLAUDE_MEMORY_INSTALL_DIR}/src"

# Cache stdin so the background process can read it after this shell exits.
TMP_INPUT="$(mktemp -t cmm-pthook.XXXXXX)"
cat > "$TMP_INPUT"

# ---------- Nudge counter + emission (synchronous, in-shell) ----------
# Runs BEFORE the opt-in capture exit so it fires unconditionally. The
# python below is short and reads only the cached temp file — no DB I/O,
# no network, well under 50ms typical.
if [ "${MEMORY_NUDGE_DISABLE:-0}" != "1" ]; then
    NUDGE_LINE=$(
        "$HOOK_PYTHON" - "$TMP_INPUT" "$CLAUDE_MEMORY_DIR" <<'PY' 2>/dev/null
import json, os, sys, time, pathlib

tmp_path = sys.argv[1]
memory_dir = sys.argv[2]
try:
    data = json.loads(pathlib.Path(tmp_path).read_text() or "{}")
except Exception:
    sys.exit(0)

tool = (data.get("tool_name") or "").strip()
if not tool:
    sys.exit(0)

sid_raw = data.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "unknown"
sid = "".join(c if c.isalnum() or c in "._-" else "_" for c in sid_raw)
project_cwd = data.get("cwd") or os.getcwd()
project = os.path.basename(project_cwd) or "unknown"

state_dir = pathlib.Path(memory_dir) / "state"
state_dir.mkdir(parents=True, exist_ok=True)
state_path = state_dir / f"nudge-{sid}.json"

try:
    state = json.loads(state_path.read_text())
except Exception:
    state = {"session_id": sid_raw,
             "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "writes": 0, "edits": 0, "bashes": 0,
             "memory_saves": 0,
             "last_nudge_writes": 0, "nudge_count": 0}

# Classify the tool. memory_save fires for any MCP tool whose name
# contains "memory_save" or the dedicated save_decision/save_intent
# entrypoints — covers all three save flavours.
field = None
t_lower = tool.lower()
if "memory_save" in t_lower or t_lower.endswith("save_decision") or t_lower.endswith("save_intent"):
    field = "memory_saves"
elif tool in ("Edit", "MultiEdit") or t_lower.endswith("__edit"):
    field = "edits"
elif tool == "Write" or t_lower.endswith("__write"):
    field = "writes"
elif tool == "Bash" or t_lower.endswith("__bash"):
    field = "bashes"

if field:
    state[field] = int(state.get(field, 0)) + 1
    state_path.write_text(json.dumps(state))

writes_total = int(state.get("writes", 0)) + int(state.get("edits", 0))
saves = int(state.get("memory_saves", 0))
last = int(state.get("last_nudge_writes", 0))

SOFT = int(os.environ.get("MEMORY_NUDGE_SOFT", "3"))
HARD = int(os.environ.get("MEMORY_NUDGE_HARD", "7"))
STEP = int(os.environ.get("MEMORY_NUDGE_STEP", "3"))

# Don't nudge on save/bash events themselves — only after write/edit.
if field not in ("edits", "writes"):
    sys.exit(0)

# Recently saved → back off until divergence grows again.
if saves > 0 and (writes_total - last) < STEP * 2:
    sys.exit(0)
if writes_total < SOFT:
    sys.exit(0)
# Hard threshold escalation always fires (after one prior soft nudge):
# the throttle should never prevent the urgent message.
escalating = writes_total >= HARD and saves == 0 and last < HARD
if not escalating and writes_total - last < STEP:
    sys.exit(0)

if writes_total >= HARD and saves == 0:
    msg = (
        f"MEMORY_NUDGE [hard]: {writes_total} significant edits this session, "
        f"0 memory_save calls. Save decisions/solutions NOW while context is "
        f"fresh: memory_save(content=..., type='decision'|'solution', "
        f"project='{project}', tags=['reusable', ...]). "
        f"Skipping saves is the #1 cause of session amnesia."
    )
elif saves == 0:
    msg = (
        f"MEMORY_NUDGE [soft]: {writes_total} edits without memory_save. "
        f"When the next decision/fix is finalized, call "
        f"memory_save(project='{project}'). Don't batch for end of session."
    )
else:
    msg = (
        f"MEMORY_NUDGE: {writes_total} writes vs {saves} saves. "
        f"If a non-trivial new fact landed, memory_save now while it's hot."
    )

print(msg)
state["last_nudge_writes"] = writes_total
state["nudge_count"] = int(state.get("nudge_count", 0)) + 1
state_path.write_text(json.dumps(state))
PY
    )
    if [ -n "$NUDGE_LINE" ]; then
        echo "$NUDGE_LINE"
    fi
fi

# Opt-in guard — observation capture only runs when explicitly enabled.
if [ "${MEMORY_POST_TOOL_CAPTURE:-0}" != "1" ]; then
    rm -f "$TMP_INPUT" 2>/dev/null
    exit 0
fi

(
    "$HOOK_PYTHON" -c '
import json, os, sys
from pathlib import Path

src_dir = sys.argv[1]
memory_dir = sys.argv[2]
tmp = sys.argv[3]

if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

os.environ.setdefault("CLAUDE_MEMORY_DIR", memory_dir)

try:
    raw = Path(tmp).read_text()
except Exception:
    raw = ""
finally:
    try:
        os.unlink(tmp)
    except Exception:
        pass

if not raw:
    sys.exit(0)

try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name") or ""
if not tool_name:
    sys.exit(0)

# tool_response shape varies across tools; merge stdout+stderr+content.
tool_response = data.get("tool_response") or {}
if isinstance(tool_response, str):
    combined = tool_response
else:
    parts = []
    for key in ("stdout", "stderr", "output", "content"):
        val = tool_response.get(key) if isinstance(tool_response, dict) else None
        if val:
            parts.append(val if isinstance(val, str) else json.dumps(val))
    combined = "\n".join(parts)

combined = (combined or "").strip()
if not combined:
    sys.exit(0)

session_id = data.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "unknown"
cwd = data.get("cwd") or os.getcwd()
project = os.path.basename(cwd) or "unknown"

try:
    from auto_extract_active import capture_tool_observation
    queue_dir = Path(memory_dir) / "extract-queue"
    capture_tool_observation(
        tool_name, combined, session_id, project, queue_dir=queue_dir,
    )
except Exception:
    pass
' "$SRC_DIR" "$CLAUDE_MEMORY_DIR" "$TMP_INPUT" >/dev/null 2>&1
) &

disown 2>/dev/null || true
exit 0
