# `total-agent-memory` — v7.0 rule block

Append this to your `~/.claude/rules/memory.md` (or `~/.claude/CLAUDE.md`).
It tells the agent **when** to call the new v7 tools — without this block
they will sit idle even though the MCP server exposes them.

---

## Memory-first (before any task)

1. `memory_recall(query="<topic>", project="<project>")` — search for prior
   reusable solutions, conventions, and decisions before writing new code.
2. Use matching results as your starting point, don't start from scratch.
3. Before delegating to a sub-agent, run `memory_recall` yourself and pass
   the recipes into the sub-agent's prompt.

## Proactive save (after any meaningful action)

Call `memory_save` **immediately**, don't batch at end-of-session:

- Architectural decision → `type="decision"`, `context` explains the WHY.
- Solved a bug → `type="solution"`, add `tags=["reusable", "<tech>"]` if it
  can be reused.
- Config / setup / install fact → `type="fact"`.
- Error → `self_error_log` (or `learn_error` with a root cause, see below).

## v7.0 Tool Triggers

**Pre-flight (before an action):**
- **Before `Edit` / `Write`** → `file_context(path=<abs>)`. If
  `risk_score > 0.3`, read the returned `warnings` (past errors, hot spots)
  and factor them into the edit.
- **At session start** → `session_init(project=<name>)` **FIRST**, before
  `memory_recall`. Returns previous session's `summary + next_steps +
  pitfalls` and marks them consumed so they don't repeat next turn.

**Capture (on event):**
- **Bash exits non-zero** with a reproducible root cause → `learn_error(file,
  error, root_cause, fix, pattern)` instead of `self_error_log`. After N≥3
  same patterns the server auto-consolidates into a rule.
- **Stack / dependency / config change** → `kg_add_fact(subject, predicate,
  object)` instead of a plain note. Old facts auto-invalidate via `valid_to`.
- **Big task starts** → `workflow_predict(task_description)`. If
  `confidence < 0.3`, pause and ask the user about the approach.
- **Big task finishes** → `workflow_track(workflow_id, outcome)` so the
  predictor learns.

**Query (on demand):**
- **"what stack did we use on date X"** → `kg_at(timestamp)` rather than a
  generic `memory_recall`.
- **"have we seen this in another project?"** → `analogize(query,
  exclude_project=<current>)` — Jaccard similarity with Dempster-Shafer
  fusion of conflicting evidence.
- **Indexing a foreign repo** → `ingest_codebase(path, languages)` (tree-sitter,
  9 languages) instead of reading file-by-file.

**Session boundary:**
- **"save" / "checkpoint"** → `session_end(summary, next_steps, pitfalls)`.
- **Weekly / after retrieval pipeline changes** → `benchmark(scenarios)` for
  R@1 / R@5 / R@10 and p50 / p95 latency regression checks.

## Save ↔ Restore — asymmetric rule

**SAVE → both MCP and your notes system (e.g. Obsidian) at once** so future
you has the human-readable narrative and future-agent has the machine-queryable
graph.

**RESTORE → MCP only.** `memory_recall` / `session_init` /
`self_rules_context(project)` are the single source of truth for the agent at
session start. Don't read human notes to restore context — if something is
missing in MCP, save it there rather than bouncing through notes.
