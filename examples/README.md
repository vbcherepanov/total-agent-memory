# Wiring `total-agent-memory` into Claude Code

The MCP server alone gives you 46 tools. But **the agent needs triggers to know when to call them** — otherwise half the value sits idle.

This directory contains drop-in examples:

| What | Where it lives on your machine | What it does |
|---|---|---|
| [`rules/memory.md`](rules/memory.md) | append to `~/.claude/rules/memory.md` (or `CLAUDE.md`) | Tells the agent *when* to call v7 tools (`file_context`, `learn_error`, `session_init`, `kg_*`, `workflow_*`, `analogize`, `ingest_codebase`, `benchmark`) |
| [`hooks/pre-edit.sh`](hooks/pre-edit.sh) | `~/.claude/hooks/pre-edit.sh` | Before every `Write`/`Edit` — emits a reminder to call `file_context(path)`, so the agent sees prior errors / risk_score for that file |
| [`hooks/on-bash-error.sh`](hooks/on-bash-error.sh) | `~/.claude/hooks/on-bash-error.sh` | After a non-zero bash exit — emits a reminder to call `learn_error(...)`. After N≥3 same patterns the server auto-consolidates them into a rule |
| [`hooks/session-start-addon.sh`](hooks/session-start-addon.sh) | append to `~/.claude/hooks/session-start.sh` | At session start — emits a reminder to call `session_init(project)` FIRST, before `memory_recall`. Returns previous session's `summary + next_steps + pitfalls` |
| [`settings/hooks.fragment.json`](settings/hooks.fragment.json) | merge into `~/.claude/settings.json` under `"hooks"` | Registers the hooks above with Claude Code |

---

## 30-second setup

```bash
# 1. Rules — tell the agent WHEN to call v7 tools
cat examples/rules/memory.md >> ~/.claude/rules/memory.md
# (If you don't have ~/.claude/rules/memory.md yet, just copy it:)
# cp examples/rules/memory.md ~/.claude/rules/memory.md

# 2. Hooks — give the agent automatic reminders
mkdir -p ~/.claude/hooks
cp examples/hooks/pre-edit.sh       ~/.claude/hooks/
cp examples/hooks/on-bash-error.sh  ~/.claude/hooks/
chmod +x ~/.claude/hooks/pre-edit.sh ~/.claude/hooks/on-bash-error.sh

# If you already have ~/.claude/hooks/session-start.sh:
cat examples/hooks/session-start-addon.sh >> ~/.claude/hooks/session-start.sh
# Otherwise:
# cp examples/hooks/session-start-addon.sh ~/.claude/hooks/session-start.sh
# chmod +x ~/.claude/hooks/session-start.sh

# 3. Register hooks in settings.json
#    Merge the keys from examples/settings/hooks.fragment.json
#    into the "hooks" object of ~/.claude/settings.json.
#    (Claude Code loads settings at startup — restart after editing.)
```

Restart Claude Code → open a project → notice the reminders being emitted when you edit a file or a bash command fails.

---

## Why this matters

Without the rules+hooks layer, here's what an agent does with a stock `total-agent-memory` install:

- ❌ Edits a file without knowing it caused a bug 3 weeks ago
- ❌ Fails the same `alembic upgrade` command 5 times instead of learning the pattern
- ❌ Starts every session with `memory_recall(...)` even though `session_init` would give it richer context
- ❌ Ignores `workflow_predict` on a big task, misses the "this has 20% success historically" signal
- ❌ Never calls `kg_at(timestamp)` when asked about historical state, just dumps current facts

Adding the rules+hooks closes all of that.

---

## Adapting to your workflow

Everything here is a starting point — edit freely:

- **Different trigger thresholds?** Edit `rules/memory.md` — change `risk_score > 0.5` to your own bar.
- **Don't want pre-edit noise?** Remove the `Write|Edit` entry from `settings/hooks.fragment.json` and `pre-edit.sh` won't fire.
- **Different project layout?** Hooks read `.cwd` and `.tool_input.file_path` from Claude Code's JSON — customize in `lib/common.sh` (see your existing hooks).
- **Multiple teammates?** Commit these files to your team repo and everyone runs `bash examples/install-hooks.sh` (write your own tiny wrapper).

---

## Troubleshooting

- **Hooks don't fire?** Check `~/.claude/settings.json` has the registration and hooks are `chmod +x`. Run a hook manually: `echo '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/x"}}' | ~/.claude/hooks/pre-edit.sh`.
- **Rules ignored?** They're loaded from `~/.claude/CLAUDE.md` / `~/.claude/rules/*.md` per Claude Code's docs. Confirm your global CLAUDE.md includes `@rules/memory.md` (or paste the content directly).
- **Tools not found?** Check `/mcp` in Claude Code — the `memory` server should show `Connected`. If `file_context` / `learn_error` / `session_init` aren't listed, you're on an older MCP server version. Run `./update.sh`.

---

## MCP tool surface (v7.0)

Your cheat sheet — all 46 tools, grouped:

**Core memory (14):** `memory_recall` · `memory_save` · `memory_update` · `memory_delete` · `memory_search_by_tag` · `memory_history` · `memory_timeline` · `memory_stats` · `memory_consolidate` · `memory_export` · `memory_forget` · `memory_relate` · `memory_extract_session` · `memory_observe`

**Knowledge graph (6):** `memory_graph` · `memory_graph_index` · `memory_graph_stats` · `memory_concepts` · `memory_associate` · `memory_context_build`

**Episodic & skills (4):** `memory_episode_save` · `memory_episode_recall` · `memory_skill_get` · `memory_skill_update`

**Self-improvement (7):** `memory_reflect_now` · `memory_self_assess` · `self_error_log` · `self_insight` · `self_patterns` · `self_reflect` · `self_rules` · `self_rules_context`

**Temporal KG (4):** `kg_add_fact` · `kg_invalidate_fact` · `kg_at` · `kg_timeline`

**Procedural memory (3):** `workflow_learn` · `workflow_predict` · `workflow_track`

**Pre-flight & automation (8):** `file_context` · `learn_error` · `session_init` · `session_end` · `ingest_codebase` · `analogize` · `benchmark`
