# Tools and interfaces

The local server exposes 79 MCP tools. This page lists them, shows typical calls,
and describes the command-line helpers, the TypeScript client and the dashboard.
The team server exposes a smaller remote catalogue; see [team-server.md](team-server.md).

- [The tools you will use most](#the-tools-you-will-use-most)
- [Examples](#examples)
- [All 79 tools](#all-79-tools)
- [Token-efficient recall](#token-efficient-recall)
- [Modes and tool profiles](#modes-and-tool-profiles)
- [CLI: `lookup-memory`](#cli-lookup-memory)
- [Activity reports](#activity-reports-report-and-tam-report)
- [Exploration memos](#exploration-memos-skip-re-reading-unchanged-files)
- [Importing history and moving the store](#importing-history-and-moving-the-store)
- [TypeScript SDK](#typescript-sdk)
- [Dashboard](#dashboard)

## The tools you will use most

Agents with the hooks installed call most of these on their own. You can also ask for
them in plain words ("remember that ...", "what did we decide about ...").

| Tool | Use it to |
|---|---|
| `memory_save` | Store a decision, solution, fact, lesson or convention, with the reason in `context`. |
| `memory_recall` | Search by meaning, keywords and the knowledge graph across sessions and projects. |
| `memory_get` | Fetch the full text of records you picked by id (up to 50 per call). |
| `memory_update` | Change a stored record instead of saving a contradicting copy. |
| `session_init` / `session_end` | Resume with the last summary, next steps and pitfalls; close a session with a new summary. |

## Examples

Outputs are abbreviated and illustrative.

**In a conversation**

```
You:     "remember we picked pgvector over ChromaDB because of multi-tenant RLS"
Claude:  memory_save(type=decision, content="Chose pgvector over ChromaDB",
                     context="WHY: single Postgres, per-tenant RLS")

[3 days later, different session, possibly different project directory:]

You:     "why did we pick pgvector again?"
Claude:  memory_recall(query="vector database choice")
         -> "Chose pgvector over ChromaDB for multi-tenant RLS. Single DB
            instance, row-level security per tenant."
```

**Direct calls**, in any Claude Code, Codex CLI or Cursor session:

```
session_init(project="my-api")
-> {summary: "yesterday: migrated auth middleware to JWT",
    next_steps: ["update OpenAPI spec", "notify frontend team"],
    pitfalls: ["don't revert migration 0042 - dev DB already migrated"]}

memory_save(
  type="decision",
  content="Chose pgvector over ChromaDB for multi-tenant RLS",
  context="WHY: single Postgres instance, per-tenant row-level security",
  project="my-api",
  tags=["database", "multi-tenant"],
)

memory_recall(query="vector database choice", project="my-api", limit=5)

memory_recall(query="what did we decide about caching last month", project="my-api", as_of="2026-09-15")
-> "last month" is counted back from 2026-09-15 instead of today

workflow_predict(task_description="migrate auth middleware to JWT-only")
-> {confidence: 0.82, predicted_steps: [...], similar_past: [...]}

file_context(path="/Users/me/my-api/src/auth/middleware.go")
-> {risk_score: 0.71, warnings: ["last 3 edits caused test failures in ..."], hot_spots: [...]}
```

Text inside `<private>...</private>` is stripped before anything is stored.

## All 79 tools

Every tool carries MCP behaviour annotations. 39 are marked `readOnlyHint`;
`memory_delete`, `memory_forget`, `memory_update`, `kg_invalidate_fact`,
`memory_rebuild_embeddings` and `memory_rebuild_fts` are marked `destructiveHint`.
Clients use these to decide what may run without a confirmation prompt. Tools that
answer in JSON also return it as `structuredContent`. The exact input schemas are in
[`tests/fixtures/tool_contract.json`](../tests/fixtures/tool_contract.json).

**Save and recall (13)**
`memory_save`, `memory_save_fast` (same as `memory_save` without the quality gate or any LLM), `memory_recall`, `memory_recall_iterative` (multi-hop: splits the query and retrieves per sub-question), `memory_search_fast` (recall without rerank or diversity), `memory_get`, `memory_search_by_tag`, `memory_answer` (a cited answer from a configured reasoning LLM), `memory_explain_search` (per-tier breakdown of a search), `memory_temporal_query` (date arithmetic and historical recall), `memory_timeline`, `memory_history`, `memory_context_build`

**Edit and remove (4)**
`memory_update`, `memory_delete` (`hard=true` erases every copy), `memory_forget`, `memory_relate`

**Knowledge graph (10)**
`kg_add_fact`, `kg_invalidate_fact`, `kg_at` (what was true at a moment), `kg_timeline`, `memory_graph`, `memory_graph_index`, `memory_graph_stats`, `memory_concepts`, `memory_associate` (spreading activation), `memory_entity_resolve`

**Sessions and episodes (6)**
`session_init`, `session_end`, `memory_episode_save`, `memory_episode_recall`, `memory_extract_session`, `memory_observe` (a cheap observation, removed after 30 days)

**Workflows and tasks (7)**
`workflow_learn`, `workflow_predict`, `workflow_track`, `classify_task`, `task_create`, `phase_transition`, `task_phases_list`

**Decisions and intents (4)**
`save_decision` (options, criteria matrix, rationale), `save_intent`, `list_intents`, `search_intents`

**Errors, rules and self-improvement (10)**
`file_context` (past errors and hot spots of a file you are about to edit), `learn_error` (repeated failures consolidate into a rule), `self_error_log`, `self_insight`, `self_patterns`, `self_reflect`, `self_rules`, `self_rules_context`, `rule_set_phase`, `memory_self_assess`

**Code and cross-project (5)**
`analogize` (similar records in other projects), `ingest_codebase` (incremental resource index with AST chunks), `memory_index_passages`, `memory_exploration_record`, `memory_exploration_recall` ([exploration memos](#exploration-memos-skip-re-reading-unchanged-files))

**Maintenance and skills (11)**
`memory_consolidate`, `memory_consolidate_status`, `memory_reflect_now`, `memory_rebuild_embeddings`, `memory_rebuild_fts`, `memory_warmup`, `memory_wiki_generate`, `memory_export` (optional `limit` and `cursor` paging with signed cursors; pages import only as one complete, contiguous sequence), `memory_stats`, `memory_skill_get`, `memory_skill_update`

**Reports and evaluation (9)**
`memory_report` ([activity reports](#activity-reports-report-and-tam-report)), `memory_perf_report` (per-tool counts, p50, p95), `benchmark`, `memory_eval_contradictions`, `memory_eval_entity_consistency`, `memory_eval_locomo`, `memory_eval_long_context`, `memory_eval_recall`, `memory_eval_temporal`

`benchmark` runs the packaged 5-scenario fixture (`src/evaluation/fixtures.json`) in an isolated throwaway store, so your memory is neither read nor modified; pass `scenarios_path` to run your own scenarios against the live store. Evaluation errors come back as `{"status": "error", "reason": ...}`.

## Token-efficient recall

When you know the topic but not which records matter, use progressive disclosure:

1. **Index.** `memory_recall(query="auth refactor", mode="index", limit=20)` returns `{id, title, score, type, project, created_at}` per hit, without content.
2. **Timeline.** `memory_recall(query="auth refactor", mode="timeline", limit=5, neighbors=2)` returns the top hits padded with neighbours from the same session, in time order.
3. **Fetch.** `memory_get(ids=[3622, 3606])` returns full content for only the ids you chose (at most 50 per call; `detail="summary"` truncates to 150 characters).

The `mode` values of `memory_recall` are `search` (the default), `index`, `timeline` and `context`. `context` returns excerpts sized for a prompt; with `fill_budget=true` it keeps whole records in rank order until `context_max_chars` is used. Measured effects of recall settings: [benchmarks](benchmarks.md).

## Modes and tool profiles

Two settings change what the server does, not what the tools are called; both are described in [configuration.md](configuration.md).

- `MEMORY_MODE` defaults to `fast`: the save and search path uses no LLM, no Ollama and no network. `balanced` adds background enrichment, `deep` runs the LLM stages synchronously and enables the legacy sentence-transformers reranker.
- `TAM_TOOL_PROFILE` defaults to `full`. `lean` (28 tools) and `minimal` (12) shrink the catalogue a client has to read; hidden tools still answer when called.

Recall also re-ranks with a local cross-encoder by default (`MEMORY_CROSS_RERANK`); see [configuration.md](configuration.md#two-rerankers-two-switches).

## CLI: `lookup-memory`

A shell-friendly search for sub-agents and scripts that should not start the full MCP
server, for example `Bash(lookup-memory "fix slow query")` from inside an agent prompt.
`lookup-memory`, `tam-lookup` and the legacy alias `ctm-lookup` are the same command; the
package installs them.

```bash
lookup-memory "Caroline researched"          # human-readable bullets
lookup-memory --project myproj --limit 5 "auth flow"
lookup-memory --type solution --tag reusable "fix bug"
lookup-memory --json "claude code hooks"     # structured output for piping
```

It opens the same `$TAM_MEMORY_DIR/memory.db` (legacy: `$CLAUDE_MEMORY_DIR/memory.db`) as the running server and ranks with SQLite full-text search (BM25), falling back to `LIKE` on older databases. It needs no Ollama and no model download, and works on macOS, Linux and Windows.

## Activity reports: `/report` and `tam report`

Ask your agent "report for this week on project X" (or type `/report week X`) and it calls `memory_report`: summary numbers with deltas against the previous period, key decisions with their reasons, solutions, errors with recurring patterns, lessons, open next steps from session summaries, most touched files, technologies and a day-by-day timeline. Every item carries the record id for `memory_get`. The report is built from stored records without an LLM; an optional LLM paragraph is opt-in. The same report from a terminal:

```bash
tam report --project billing-api --period week             # Markdown to stdout
tam report --period custom --since 2026-09-01 --until 2026-09-15 --format json --out sept.json
tam report --project billing-api --period month --save     # also <memory dir>/reports/billing-api/month-2026-09-01.md
```

On the team server the same tool reports on your own memory, a department (heads, company viewers, superadmins) or the whole company, and the dashboard has a **Reports** page with a period picker and a Markdown download. Details: [REPORTS.md](REPORTS.md).

## Exploration memos: skip re-reading unchanged files

Agents re-read the same files every session to answer the same structural questions ("how is auth wired?", "where are migrations applied?"). After answering one by reading code, record the conclusion with the files it came from:

```json
memory_exploration_record({
  "question": "How is authentication wired?",
  "conclusion": "check_token() in src/auth/middleware.py runs before every route except the ones listed in src/auth/routes.py.",
  "files": ["src/auth/middleware.py", "src/auth/routes.py"],
  "project": "billing-api",
  "root": "/home/me/code/billing-api"
})
```

TAM stores the SHA-256 and size of each file, with paths relative to `root` (default: the server's working directory). Files outside `root` (absolute paths elsewhere, `../` paths, symlinks leading out) are refused. Limits: 64 files per memo, 32 MB per file, 2,000-character questions, 20,000-character conclusions. Recording the same question again (case and trailing punctuation ignored) replaces the memo.

Before the next exploration, ask first:

```json
memory_exploration_recall({"question": "where is auth checked?", "project": "billing-api", "root": "/home/me/code/billing-api"})
```

Results are ranked by question similarity: an exact question match scores 1.0; otherwise 0.8 x cosine similarity of the question embeddings (same embedding model only) + 0.2 x word overlap, falling back to word overlap alone when no embedding is available. Results below 0.2 are dropped. Every file is re-hashed under the `root` given at recall time, so a moved checkout still verifies:

- `"status": "fresh"`: every file is byte-identical, so the conclusion can be reused without re-reading.
- `"status": "stale"`: `changed_files` and `missing_files` list what to re-read. `unverifiable_files` lists files that now resolve outside the root or exceed the size limit.

`include_stale: false` returns fresh memos only. Memos are kept in the `exploration_memos` and `exploration_memo_files` tables and are not mixed into `memory_recall`, so a stale conclusion never surfaces as a plain fact.

## Importing history and moving the store

`tam import chatgpt|claude|mem0|markdown <path>` brings existing chat history and notes into memory, and `tam export --encrypted` / `tam import-bundle` move the whole store between machines. See [importing.md](importing.md).

## TypeScript SDK

For Node.js, browser or any TypeScript project that is not an MCP-native agent:

```bash
npm i @vbch/total-agent-memory-client
```

```ts
import { connectStdio } from "@vbch/total-agent-memory-client";

const memory = await connectStdio();

await memory.save({
  type: "decision",
  content: "Picked pgvector over ChromaDB for multi-tenant RLS",
  project: "my-api",
});

const hits = await memory.recallFlat({
  query: "vector database choice",
  project: "my-api",
  limit: 5,
});
```

The package also has a LangChain adapter example, a procedural-memory integration and an HTTP transport. Repository: [github.com/vbcherepanov/total-agent-memory-client](https://github.com/vbcherepanov/total-agent-memory-client)

## Dashboard

The local dashboard runs at `http://127.0.0.1:37737` (`DASHBOARD_PORT`). It shows live
statistics and queue depths, a 3D force graph (`/graph/live`), a hive plot
(`/graph/hive`), an adjacency matrix (`/graph/matrix`), record pages
(`/knowledge/<id>`) and the **Settings** page ([LOCAL_SETTINGS.md](LOCAL_SETTINGS.md)).
