#!/usr/bin/env bash
# ===========================================
# Memory nudge library — track tool-use vs memory_save ratio
# per session and emit reminders into hook stdout so Claude
# sees them as context on the next turn.
#
# Problem: smaller models (Sonnet, Haiku) consistently skip
# memory_save during long sessions even when priority-10 rules
# instruct them to save. SessionStart reminders are one-shot
# and fade out by the time meaningful work happens. Without
# proactive in-session nudges, the rule fires "~1 of 30 sessions"
# (reported by client 2026-05-14).
#
# This library:
#   * persists per-session counters in
#     $CLAUDE_MEMORY_DIR/state/nudge-<session>.json
#   * incremented from PostToolUse on writes / saves
#   * exposes nudge_check() which echoes a one-line reminder
#     to stdout when the writes-without-saves ratio crosses
#     thresholds, throttled to avoid spam
#
# Source from any hook AFTER common.sh:
#     source "$(dirname "$0")/lib/memory-nudge.sh"
# ===========================================

NUDGE_STATE_DIR="${CLAUDE_MEMORY_DIR:-$HOME/.claude-memory}/state"
mkdir -p "$NUDGE_STATE_DIR" 2>/dev/null

# nudge_path SESSION_ID -> echoes the counter file path.
nudge_path() {
    local sid="${1:-unknown}"
    # Strip anything that looks unsafe in a filename
    sid=$(printf '%s' "$sid" | tr -c 'A-Za-z0-9._-' '_')
    echo "${NUDGE_STATE_DIR}/nudge-${sid}.json"
}

# nudge_init SESSION_ID — make sure file exists with zero counts.
nudge_init() {
    local sid="$1"
    local path
    path=$(nudge_path "$sid")
    if [ ! -f "$path" ]; then
        cat > "$path" <<EOF
{"session_id":"${sid}","started_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","writes":0,"edits":0,"bashes":0,"memory_saves":0,"last_nudge_writes":0,"nudge_count":0}
EOF
    fi
}

# nudge_increment SESSION_ID FIELD
# FIELD in {writes, edits, bashes, memory_saves}
nudge_increment() {
    local sid="$1"
    local field="$2"
    local path
    path=$(nudge_path "$sid")
    [ ! -f "$path" ] && nudge_init "$sid"

    "${HOOK_PYTHON:-python3}" - "$path" "$field" <<'PY' 2>/dev/null
import json, sys
path, field = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        d = json.load(f)
except Exception:
    d = {"writes": 0, "edits": 0, "bashes": 0, "memory_saves": 0,
         "last_nudge_writes": 0, "nudge_count": 0}
if field not in d:
    d[field] = 0
d[field] += 1
with open(path, "w") as f:
    json.dump(d, f)
PY
}

# nudge_check SESSION_ID PROJECT
# Echoes a one-line reminder to stdout (Claude sees it) when the
# significant-edits-without-save ratio crosses thresholds.
# Throttled by `last_nudge_writes`: re-fires only when the count
# has grown by NUDGE_STEP (default 3) since the previous nudge.
nudge_check() {
    local sid="$1"
    local project="${2:-unknown}"
    local path
    path=$(nudge_path "$sid")
    [ ! -f "$path" ] && return 0

    "${HOOK_PYTHON:-python3}" - "$path" "$project" <<'PY' 2>/dev/null
import json, os, sys
path, project = sys.argv[1], sys.argv[2]

SOFT_THRESHOLD = int(os.environ.get("MEMORY_NUDGE_SOFT", "3"))
HARD_THRESHOLD = int(os.environ.get("MEMORY_NUDGE_HARD", "7"))
STEP = int(os.environ.get("MEMORY_NUDGE_STEP", "3"))

try:
    with open(path) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)

writes = int(d.get("writes", 0)) + int(d.get("edits", 0))
saves = int(d.get("memory_saves", 0))
last = int(d.get("last_nudge_writes", 0))

if saves > 0 and writes - last < STEP * 2:
    # User did save recently; back off.
    sys.exit(0)
if writes < SOFT_THRESHOLD:
    sys.exit(0)
if writes - last < STEP:
    # Throttle: wait for STEP more writes before re-nudging.
    sys.exit(0)

if writes >= HARD_THRESHOLD and saves == 0:
    msg = (
        f"MEMORY_NUDGE [hard]: {writes} significant edits this session, 0 memory_save calls. "
        f"Save decisions/solutions NOW while context is fresh: "
        f"memory_save(content=..., type='decision'|'solution', project='{project}', "
        f"tags=['reusable', ...]). Skipping saves is the #1 cause of session amnesia."
    )
elif saves == 0:
    msg = (
        f"MEMORY_NUDGE [soft]: {writes} edits without memory_save. "
        f"When the next decision or fix is finalized, run "
        f"memory_save(project='{project}'). Don't batch for end of session."
    )
else:
    msg = (
        f"MEMORY_NUDGE: {writes} writes vs {saves} saves. "
        f"If a non-trivial new fact landed, memory_save now while it's still hot."
    )

print(msg, flush=True)

d["last_nudge_writes"] = writes
d["nudge_count"] = int(d.get("nudge_count", 0)) + 1
with open(path, "w") as f:
    json.dump(d, f)
PY
}

# nudge_summary SESSION_ID — emit a final-status line for Stop hook.
# Always prints (no throttle), suppressed only when nothing relevant
# happened in the session.
nudge_summary() {
    local sid="$1"
    local project="${2:-unknown}"
    local path
    path=$(nudge_path "$sid")
    [ ! -f "$path" ] && return 0

    "${HOOK_PYTHON:-python3}" - "$path" "$project" <<'PY' 2>/dev/null
import json, sys
path, project = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)

writes = int(d.get("writes", 0)) + int(d.get("edits", 0))
saves = int(d.get("memory_saves", 0))
nudges = int(d.get("nudge_count", 0))

if writes == 0 and saves == 0:
    sys.exit(0)

if saves == 0 and writes >= 3:
    print(
        f"MEMORY_FINAL_WARNING: session ending with {writes} significant edits "
        f"and 0 memory_save calls (received {nudges} nudges). Before stop, save "
        f"the most important decision/fix from this session: "
        f"memory_save(project='{project}').",
        flush=True,
    )
elif writes >= 5 and saves < writes // 5:
    print(
        f"MEMORY_FINAL_NOTE: {writes} edits vs {saves} saves. "
        f"Coverage ratio low — consider one more memory_save if anything "
        f"reusable wasn't captured.",
        flush=True,
    )
else:
    print(f"MEMORY_FINAL_OK: {writes} edits, {saves} saves recorded.",
          flush=True)
PY
}
