# Hooks reference

Hooks fire automatically on agent-runtime events and call MCP tools
without you having to remember to. They are **optional** — the
protocol works without them, but with them everything happens at the
right moment by default.

Each hook ships in two forms:

- `hooks/<name>.sh` — bash, for macOS / Linux / WSL2 / cygwin
- `hooks/<name>.ps1` — PowerShell 5.1+ / 7+ for native Windows

The installer wires these hooks for Claude Code only; the Claude Code
plugin brings the same set. Codex CLI gets one optional script,
`codex-notify` (section 9), which you register yourself.

---

## How a hook works

All hooks read **stdin** (JSON event payload from the IDE), do their
work, and either exit 0 (continue) or print a message + exit non-zero
(block / warn). They never read or write user files directly — they
shell out to the MCP server (or to `lookup_memory.sh`).

Common environment:

| Variable | Purpose |
|---|---|
| `CLAUDE_MEMORY_DIR` | DB location, default `~/.claude-memory` |
| `MEMORY_PROJECT` | Override auto-detected project name |
| `MEMORY_HOOK_TIMEOUT_SEC` | Max time a hook may run (default 5) |
| `MEMORY_HOOK_DEBUG` | If `1`, hook logs to `~/.claude-memory/hooks.log` |

If a hook hits the timeout it is killed — never blocks the IDE
indefinitely.

---

## 1. `session-start.{sh,ps1}`

**Fires:** every time a new Claude Code session starts (on `claude`
launch).

**Reads:** event JSON containing `cwd`, sometimes `git_branch`.

**Does:**
1. Calls `session_init(project=<basename of cwd>)` via MCP.
2. If a recovery file exists in `~/.claude/recovery/pending-*.md`
   (left by `on-stop`), prints its content as `additionalContext` so
   the agent sees it on the first turn.
3. Generates the A2A self-label (`<basename>-<4hex>`) at
   `/tmp/a2a-label-<PID>` if A2A protocol is in use.
4. Reindexes self-improvement rules to the graph.

**Skip if:** you don't want auto-resume; e.g. throwaway shell.

**IDE wiring:** see `ide-setup.md` per platform.

---

## 2. `pre-edit.{sh,ps1}`

**Fires:** **before** the agent calls `Edit` / `Write` (Claude Code
hook event `PreToolUse`).

**Reads:** event JSON with `tool_name` and `tool_input.file_path`.

**Does:**
1. Calls `file_context(path=<file_path>)` via MCP.
2. If `risk_score > 0.5`, prints the warnings as `additionalContext`
   so the agent sees "this file has 3 prior bugs in this region" on
   the same turn.
3. Exits 0 — never blocks. The agent decides.

**Skip if:** you want zero pre-edit overhead. Without this hook you
still get the data when the agent calls `file_context()` manually.

**Cost:** ~80 ms when warm; one DB query.

---

## 2a. `rule-reminder.{sh,ps1}`

**Fires:** before `Bash`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`
(`PreToolUse`).

**Reads:** `tool_input.command`, or `tool_input.file_path` plus the new
text (`new_string` / `content` / `edits[].new_string`).

**Does:** matches that text against the active SOUL rules
(`src/ambient/reminders.py`). A prohibition the action seems to break
("never use sqlite" vs `sqlite3 app.db`) or a rule sharing two or more
words with the action (one of them rare among the rules) is sent to the
agent as `additionalContext` and recorded as a rule surface, so the Stop
hook can check it. The same rule is reminded at most once per 15 minutes
per session. Never a permission decision.

**Skip:** `TAM_RULE_REMINDERS=false`.

**Cost:** one python start (~70 ms) and a scan of the active rules.

---

## 3. `post-tool-use.{sh,ps1}`

**Fires:** after every tool call (`PostToolUse`) and after every failed
one (`PostToolUseFailure`).

**Reads:** `tool_name`, `tool_input`, `tool_response`, `hook_event_name`.

**Does:**
1. Logs the call in `agent_actions` (`TAM_ACTION_LOG`, default on):
   tool, a short secret-redacted summary (the command, or the path plus
   the start of the new text; MCP tools keep argument names only), status
   and exit code. Read-only lookups (Read, Glob, Grep, LS, Todo*) are
   skipped. Kept `TAM_ACTION_LOG_RETENTION_DAYS` (30) and at most 100 000
   rows. Synchronous, so the Stop hook sees the turn's last call.
2. Counts edits vs `memory_save` calls and nudges the agent to save
   (`MEMORY_NUDGE_*`). Failed calls are not counted.
3. Opt-in, `MEMORY_POST_TOOL_CAPTURE=1`: queues an observation for the
   extractor.

**Cost:** two python starts per call (~100 ms).

---

## 4. `user-prompt-submit.{sh,ps1}`

**Fires:** every time the user sends a prompt (Claude Code hook event
`UserPromptSubmit`).

**Reads:** `prompt`, `cwd`, `session_id`.

**Does:**
1. A detached worker stores the prompt in `intents`
   (`list_intents()` / `search_intents()`).
2. Ambient recall (`src/ambient/prompt_context.py`, `TAM_AMBIENT_RECALL`):
   rules sharing two or more words with the prompt, then a short block of
   relevant records, go to the agent as `additionalContext`. The records
   come from the shared daemon when one runs (full hybrid recall) and
   from FTS5 otherwise. Bounded by `TAM_AMBIENT_TOKEN_BUDGET` (400
   tokens) and `TAM_AMBIENT_LATENCY_MS` (1500 ms); anything already
   injected into the session is skipped. Slash commands and prompts under
   8 characters get nothing. A failure is logged to `hooks.log`; the
   prompt is never blocked.
3. Correction capture (`TAM_CORRECTION_CAPTURE`): a prompt that corrects
   the agent ("I already told you", "сколько раз", ...) becomes a draft
   insight (`src/correction_capture.py`), once per prompt per session.

**Cost:** ~100 ms cold, plus the daemon's recall time when warm.

---

## 5. `on-stop.{sh,ps1}`

**Fires:** at the end of every agent turn (Claude Code hook event
`Stop`).

**Reads:** `session_id`, `cwd`, `last_assistant_message` (when sent).

**Does:**
1. Refreshes one recovery snapshot per session (rate-limited).
2. Rule compliance (`TAM_RULE_COMPLIANCE`): the actions logged since the
   previous Stop are checked against the rules shown to the session
   (prompt context, rule reminders). A "never X" rule whose X appears in
   an action is *violated* and reported to the user in `systemMessage`;
   enough overlap counts as *followed*. Each new verdict goes to the
   rule's heuristic counters `auto_success_count` / `auto_fail_count`
   only (shown by `self_rules_context`, `self_rules(action='list')` and
   `self_patterns(view='rule_effectiveness')`). Explicit ratings,
   `success_rate`, `success_lower_bound` and auto-suspension never see
   them. Showing a rule never changes `fire_count`.
   With `TAM_DOCKER=1` (Docker install) the ambient commands run inside
   the container: `docker exec -i tam-mcp python -m ambient <command>`.
3. Turn summary (`TAM_TURN_AUTOSAVE`): a turn that edited files or ran
   commands is saved as a short auto-saved `fact` (request, tools, files,
   commands, outcome; secrets redacted). `tam forget-auto` removes them.
4. Once per session, a reminder when there were edits but no
   `memory_save`; prunes per-session state older than 7 days.

**Cost:** one python start plus a few SQLite queries.

---

## 6. `auto-capture.{sh,ps1}`

**Fires:** periodic (every N tool calls or on context-pressure
warning).

**Reads:** session id, current context fill %.

**Does:** writes a checkpoint to `extract-queue/` for later
reflection processing. Different from `on-stop` — this is
*proactive* checkpointing during a healthy session.

**Skip:** unless you run very long sessions (3+ hours).

---

## 7. `on-bash-error.{sh,ps1}`

**Fires:** Claude Code `PostToolUse` and `PostToolUseFailure` on Bash —
the command exited non-zero, error worth recording.

**Reads:** command, exit code, stderr.

**Does:** calls `learn_error(file, error, root_cause, fix, pattern)`
with placeholder `root_cause`/`fix` if not provided — agent fills
them in later via `memory_update`.

**Skip:** never (cheap and high-signal).

---

## 8. `memory-trigger.{sh,ps1}`

**Fires:** custom trigger — usually invoked via slash-command or
keyboard shortcut, not by IDE event.

**Does:** runs `memory_recall` with a user-provided query, prints
result. Convenience for "give me the recipe for X" without leaving
the editor.

---

## 9. `codex-notify.{sh,ps1}`

**Fires:** Codex `notify` program, after each agent turn — only if you
add it to `~/.codex/config.toml` yourself
(`notify = ["/path/to/hooks/codex-notify.sh"]`); the installer does not
register it.

**Reads:** the notification JSON Codex passes as the last argument
(`type`, `thread-id`, `cwd`, `input-messages`,
`last-assistant-message`); a JSON on stdin also works.

**Does:** each `agent-turn-complete` becomes one `codex_turn` row in the
agent action log (client `codex`, session = thread id) and, with
`TAM_TURN_AUTOSAVE`, one auto-saved turn summary. Then a desktop
notification (`TAM_HOOK_NOTIFY=0` turns it off).

---

## 10. `session-end.{sh,ps1}`

**Fires:** clean session end (user typed `/quit`, IDE closed
normally).

**Does:** calls `session_end(session_id, summary, next_steps)` with
LLM-generated summary if `auto_compress=true` was set; otherwise
expects the agent to have called `session_end` already and is a
no-op.

---

## What the installer registers

| Hook | Claude Code | Codex CLI |
|---|:-:|:-:|
| `session-start` | ✅ | ❌ |
| `session-end` | ✅ | ❌ |
| `on-stop` | ✅ | ❌ |
| `user-prompt-submit` | ✅ | ❌ |
| `pre-edit` | ✅ | ❌ |
| `rule-reminder` | ✅ | ❌ |
| `inject-provenance` | ✅ | ❌ |
| `post-tool-use` | ✅ | ❌ |
| `on-bash-error` | ✅ | ❌ |
| `memory-trigger` | ✅ | ❌ |
| `auto-capture` | ✅ | ❌ |
| `codex-notify` | ❌ | manual ¹ |

¹ Not registered by the installer. Add it to `~/.codex/config.toml`
yourself: `notify = ["/path/to/total-agent-memory/hooks/codex-notify.sh"]`.

For Cursor, Cline, Continue, Aider, Windsurf, Gemini CLI and OpenCode the
installer registers no hooks: the agent follows the protocol from the
skill or rules file and calls `session_init`, `memory_recall` and
`memory_save` itself.

---

## Where the hooks are registered

| Client | Hooks live in |
|---|---|
| Claude Code (`install.sh --ide claude-code`) | `~/.claude/settings.json` → `"hooks"` map, scripts in `~/.claude/hooks/` |
| Claude Code (plugin) | the plugin's own `hooks/hooks.json` |
| Codex CLI | `notify` in `~/.codex/config.toml`, set by hand |

Manual setup blueprints are in `templates/`.

---

## Debugging hooks

```bash
# Watch every hook fire in real time:
tail -f ~/.claude-memory/hooks.log

# Run a hook manually with a fake event:
echo '{"cwd":"/tmp/x","tool_name":"Edit","tool_input":{"file_path":"/tmp/x/a.py"}}' \
  | MEMORY_HOOK_DEBUG=1 ~/.claude/hooks/pre-edit.sh

# Disable a single hook without uninstalling:
chmod -x ~/.claude/hooks/<name>.sh   # makes it skipped
```

If `~/.claude-memory/hooks.log` is silent and you expected fires:
the IDE's hook config is not picking the file up. Re-run
`install.sh --ide <name>` (idempotent) and restart the IDE.
