# Tools and interfaces

The local server exposes 79 MCP tools. This page groups them, shows typical
calls, and describes the command-line helpers, the TypeScript client and the
dashboard. The team server exposes a smaller remote catalogue; see
[team-server.md](team-server.md).

- [Capabilities](#capabilities)
- [Usage examples](#usage-examples)
- [MCP tools reference](#mcp-tools-reference-79-tools)
- [CLI: `lookup-memory` for sub-agents](#cli-lookup-memory-for-sub-agents)
- [Activity reports](#activity-reports-report-and-tam-report)
- [TypeScript SDK](#typescript-sdk)
- [Dashboard](#dashboard-localhost37737)

## Capabilities

### Coding-agent features

| Capability | Tool | One-liner |
|---|---|---|
| 🧠 **Procedural memory** | `workflow_predict` / `workflow_track` | "How did I solve this last time?" — predicts steps with confidence |
| 🔗 **Cross-project analogy** | `analogize` | "Was there something like this in another repo?" — Jaccard + Dempster-Shafer |
| ⚠️ **Pre-edit risk warnings** | `file_context` | Surfaces past errors / hot spots on the file you're about to edit |
| 🛡 **Self-improving rules** | `learn_error` + `self_rules_context` | Bash failures → patterns → auto-consolidated behavioral rules at N≥3 |
| 🕰 **Temporal facts** | `kg_add_fact` / `kg_at` | Append-only KG with `valid_from`/`valid_to` — query what was true at any point |
| 🎯 **Task workflow phases** | `classify_task` / `phase_transition` | Automatic L1-L4 complexity classification, state machine across van/plan/creative/build/reflect/archive |
| 🧩 **Structured decisions** | `save_decision` | Options + criteria matrix + rationale + discarded → searchable decision records with per-criterion embeddings |
| 💸 **Token-efficient retrieval** | `memory_recall(mode="index")` + `memory_get` | 3-layer workflow: compact IDs → timeline → batched full fetch. ~83% token saving on typical queries |

### Other features

- **Hybrid retrieval** (BM25 + dense + fuzzy + graph, RRF fusion; optional CrossEncoder rerank and MMR) — 95.1% R@5 on LongMemEval with the default profile ([benchmarks](benchmarks.md#longmemeval--xiaowu0162longmemeval-cleaned))
- **Multi-representation embeddings** — each record embedded as raw + summary + keywords + questions + compressed
- **AST codebase ingest** — tree-sitter across 9 languages (Python, TS/JS, Go, Rust, Java, C/C++, Ruby, C#)
- **Auto-reflection pipeline** — `memory_save` → LaunchAgent file-watch → graph edges appear ~30 s later
- **rtk-style content filters** — strip noise from pytest / cargo / git / docker logs while preserving URLs, paths, code
- **3D WebGL knowledge graph viewer** — 3,500+ nodes, 120,000+ edges, click-to-focus, filters
- **Hive plot & adjacency matrix** — alternate graph views sorted by node type
- **A2A protocol** — memory shared between multiple agents (backend + frontend + mobile in a team)
- **`design-explore` skill** — drop-in Claude Code skill that walks L3-L4 tasks through options → criteria matrix → `save_decision` before code (see `examples/skills/design-explore/SKILL.md`)
- **`<private>...</private>` inline redaction** in any saved content
- **Cloud LLM/embed providers** with per-phase routing (OpenAI / Anthropic / OpenRouter / Together / Groq / Cohere / any OpenAI-compat)
- **`activeContext.md` Obsidian projection** for human-readable session state
- **Phase-scoped rules** (`self_rules_context(phase="build")`) — ~70% token reduction

## Usage examples

Outputs below are abbreviated and illustrative.

### In a conversation

```
You:     "remember we picked pgvector over ChromaDB because of multi-tenant RLS"
Claude:  ✓ memory_save(type=decision, content="Chose pgvector over ChromaDB",
                       context="WHY: single Postgres, per-tenant RLS")

[3 days later, different session, possibly different project directory:]

You:     "why did we pick pgvector again?"
Claude:  ✓ memory_recall(query="vector database choice")
         → "Chose pgvector over ChromaDB for multi-tenant RLS. Single DB
            instance, row-level security per tenant."
```

It's not just retrieval. It's procedural too:

```
You:     "migrate auth middleware to JWT-only session tokens"
Claude:  ✓ workflow_predict(task_description="migrate auth middleware...")
         → confidence 0.82, predicted steps:
             1. read src/auth/middleware.go + tests
             2. update session fixtures in tests/
             3. run migration 0042
             4. regenerate OpenAPI spec
           similar past: wf#118 (success), wf#93 (success)
```

### Direct calls

> **v11 default is `MEMORY_MODE=fast`.** No LLM, no Ollama, no network in the save/search/recall hot path. To restore v10.5 synchronous-LLM behaviour set `export MEMORY_MODE=deep`. Mode switching: [`LAUNCH.md` § Tuning](../LAUNCH.md#tuning-v110).

Once installed, in any Claude Code / Codex CLI / Cursor session:

**1. Resume where you left off** (auto on session start, but you can also invoke)

```
session_init(project="my-api")
→ {summary: "yesterday: migrated auth middleware to JWT",
   next_steps: ["update OpenAPI spec", "notify frontend team"],
   pitfalls: ["don't revert migration 0042 — dev DB already migrated"]}
```

**2. Save a decision (agent does this automatically after hooks are registered)**

```
memory_save(
  type="decision",
  content="Chose pgvector over ChromaDB for multi-tenant RLS",
  context="WHY: single Postgres instance, per-tenant row-level security",
  project="my-api",
  tags=["database", "multi-tenant"],
)
```

**3. Recall across sessions / projects**

```
memory_recall(query="vector database choice", project="my-api", limit=5)
→ RRF-fused results from 6 retrieval tiers

memory_recall(query="what did we decide about caching last month", project="my-api", as_of="2026-09-15")
→ "last month" is counted back from 2026-09-15 instead of today (replays, benchmarks, "as of" questions)
```

**4. Predict approach before starting a task**

```
workflow_predict(task_description="migrate auth middleware to JWT-only")
→ {confidence: 0.82, predicted_steps: [...], similar_past: [...]}
```

**5. Check a file's risk before editing** (auto via hook, also manual)

```
file_context(path="/Users/me/my-api/src/auth/middleware.go")
→ {risk_score: 0.71, warnings: ["last 3 edits caused test failures in ..."], hot_spots: [...]}
```

**6. Get full stats**

```
memory_stats()
→ {sessions: 515, knowledge: {active: 1859, ...}, storage_mb: 119.5, ...}
```

## MCP tools reference (79 tools)

### Tool categories

**Core retrieval (9):** `memory_save`, `memory_recall`, `memory_get`, `memory_update`, `memory_delete`, `memory_history`, `memory_extract_session`, `memory_relate`, `memory_search_by_tag`

**Knowledge graph (8):** `kg_add_fact`, `kg_invalidate_fact`, `kg_at`, `kg_timeline`, `memory_graph`, `memory_graph_index`, `memory_graph_stats`, `memory_concepts`

**Episodic / session (6):** `memory_episode_save`, `memory_episode_recall`, `session_init`, `session_end`, `memory_timeline`, `memory_history`

**Procedural / workflows (4):** `workflow_learn`, `workflow_predict`, `workflow_track`, `classify_task`

**Task phases (4, v8.0):** `task_create`, `phase_transition`, `task_phases_list`, `complete_task`

**Decisions (1, v8.0):** `save_decision`

**Intents (3, v8.0):** `save_intent`, `list_intents`, `search_intents`

**Self-improvement (5):** `self_rules`, `self_rules_context`, `self_insight`, `self_patterns`, `self_error_log`, `rule_set_phase` (v8.0)

**Pre-edit guard / error learning (3):** `file_context`, `learn_error`, `self_error_log`

**Analogy / cross-project (2):** `analogize`, `ingest_codebase`

**Reflection / consolidation (4):** `memory_reflect_now`, `memory_consolidate`, `memory_forget`, `memory_observe`

**Stats / export (5):** `memory_stats`, `memory_export` (optional `limit`/`cursor` paging with signed cursors; pages import only as one complete, contiguous sequence), `memory_self_assess`, `memory_context_build`, `benchmark`

`benchmark` runs the packaged 5-scenario fixture (`src/evaluation/fixtures.json`) in an isolated throwaway store, so your memory is neither read nor modified; pass `scenarios_path` to run your own scenarios against the live store. Evaluation errors come back as `{"status": "error", "reason": ...}`.

**Reports (1):** `memory_report` — day / week / month / all-time / custom activity report, see [docs/REPORTS.md](REPORTS.md)

**Skills (3):** `memory_skill_get`, `memory_skill_update`, `file_context`

**Exploration memos (2):** `memory_exploration_record`, `memory_exploration_recall` — conclusions about a codebase keyed to the SHA-256 of the files they came from, see [Exploration memos](#exploration-memos-skip-re-reading-unchanged-files)

Total: **79 tools.** Each is documented below with input schema and example.

Every tool carries MCP behaviour annotations — 39 are marked `readOnlyHint`,
and `memory_delete` / `memory_forget` / `memory_update` / `kg_invalidate_fact`
plus the two rebuild tools are marked `destructiveHint`. Clients use these to
decide what may run without a confirmation prompt. Tools that answer in JSON
also return it as `structuredContent`, so you do not have to parse the text.

### Token-efficient 3-layer workflow

When you only know the topic but not which records matter, use progressive disclosure:

1. **Index** — `memory_recall(query="auth refactor", mode="index", limit=20)` → ~2 KB of `{id, title, score, type, project, created_at}` per hit. No content, no cognitive expansion.
2. **Timeline** — `memory_recall(query="auth refactor", mode="timeline", limit=5, neighbors=2)` → top-K hits padded with ±neighbours from the same session, sorted chronologically.
3. **Fetch** — `memory_get(ids=[3622, 3606])` → full content for ONLY the IDs you chose (max 50 per call, `detail="summary"` truncates to 150 chars).

**Typical saving:** 80-90 %% fewer tokens vs `memory_recall(detail="full", limit=20)` when you end up using 2-3 of the 20 hits.

<details>
<summary><b>Core memory (15)</b></summary>

`memory_recall` · `memory_get` · `memory_save` · `memory_update` · `memory_delete` · `memory_search_by_tag` · `memory_history` · `memory_timeline` · `memory_stats` · `memory_consolidate` · `memory_export` · `memory_forget` · `memory_relate` · `memory_extract_session` · `memory_observe`

</details>

<details>
<summary><b>Knowledge graph (6)</b></summary>

`memory_graph` · `memory_graph_index` · `memory_graph_stats` · `memory_concepts` · `memory_associate` · `memory_context_build`

</details>

<details>
<summary><b>Episodic memory & skills (4)</b></summary>

`memory_episode_save` · `memory_episode_recall` · `memory_skill_get` · `memory_skill_update`

</details>

<details>
<summary><b>Reflection & self-improvement (7)</b></summary>

`memory_reflect_now` · `memory_self_assess` · `self_error_log` · `self_insight` · `self_patterns` · `self_reflect` · `self_rules` · `self_rules_context`

</details>

<details>
<summary><b>Temporal knowledge graph (4)</b></summary>

`kg_add_fact` · `kg_invalidate_fact` · `kg_at` · `kg_timeline`

</details>

<details>
<summary><b>Procedural memory (3)</b></summary>

`workflow_learn` · `workflow_predict` · `workflow_track`

</details>

<details>
<summary><b>Pre-flight guards & automation (8)</b></summary>

`file_context` (pre-edit risk scoring) · `learn_error` (auto-consolidating error capture) · `session_init` / `session_end` · `ingest_codebase` (AST, 9 languages) · `analogize` (cross-project analogy) · `benchmark` (regression gate: packaged 5-scenario fixture in an isolated store)

</details>

Full JSON schemas: `python -m total_agent_memory.cli tools --json` or open the dashboard at `localhost:37737/tools`.

## CLI: `lookup-memory` for sub-agents

**New in v9.** Bash-friendly memory search for sub-agent workflows where launching the full MCP server would be overkill (e.g. `Bash(lookup-memory "fix slow Wave query")` from inside a Claude Code agent prompt).

Two equivalent commands ship with the package (registered as `[project.scripts]` entries — installed automatically by `./install.sh` or `./update.sh`):

```bash
lookup-memory "Caroline researched"          # human-readable bullets
tam-lookup "Caroline researched"             # short canonical alias
ctm-lookup "Caroline researched"             # legacy alias (v11.x and earlier)

lookup-memory --project myproj --limit 5 "auth flow"
lookup-memory --type solution --tag reusable "fix bug"
lookup-memory --json "claude code hooks"     # structured stdout for piping
```

**How it works:** opens the same `$TAM_MEMORY_DIR/memory.db` (legacy: `$CLAUDE_MEMORY_DIR/memory.db`) the running MCP server uses → BM25 ranking via FTS5 → falls back to LIKE on older DBs. **Zero deps beyond the package.** No Ollama, no rag_chat.py, no ChromaDB required for the CLI path. Works on macOS, Linux, Windows.

```text
$ lookup-memory --project locomo_0 --limit 2 "adoption"
1. [synthesized_fact|locomo_0] Caroline is researching adoption agencies.
2. [synthesized_fact|locomo_0] Melanie congratulates Caroline on her adoption.
```

**Why three names?** `lookup-memory` matches the legacy bash script that older docs and sub-agent prompts reference (`~/claude-memory-server/ollama/lookup_memory.sh`, legacy install path). `tam-lookup` is the new project-prefixed canonical form (v12+). `ctm-lookup` is the v11.x prefixed name, kept as a legacy alias. All three call into `total_agent_memory.lookup:main` (v11.x and earlier: `claude_total_memory.lookup:main`, still importable via deprecation shim).

**Migration note:** v7/v8 docs that pointed at `~/claude-memory-server/ollama/lookup_memory.sh` should be updated — the bash version still works for users with a manual install, but `./install.sh` / `./update.sh` clients on v9+ now get `lookup-memory` (and `tam-lookup`) on PATH directly via the package's `[project.scripts]` entry.

## Activity reports: `/report` and `tam report`

Ask your agent "report for this week on project X" (or type `/report week X`) and it calls `memory_report`: summary numbers with deltas against the previous period, key decisions with their WHY, solutions, errors with recurring patterns, lessons, open next steps from session summaries, most touched files, technologies and a day-by-day timeline. Every item carries the record ID for `memory_get`. The report is built from stored records without an LLM; an optional LLM paragraph is opt-in. The same report from a terminal:

```bash
tam report --project billing-api --period week             # Markdown to stdout
tam report --period custom --since 2026-09-01 --until 2026-09-15 --format json --out sept.json
tam report --project billing-api --period month --save     # also <memory dir>/reports/billing-api/month-2026-09-01.md
```

On the team server the same tool reports on your own memory, a department (heads, company viewers, superadmins) or the whole company, and the dashboard has a **Reports** page with a period picker and a Markdown download. Details: [docs/REPORTS.md](REPORTS.md).

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

TAM stores the SHA-256 and size of each file, with paths relative to `root` (default: the server's working directory). Files outside `root` — absolute paths elsewhere, `../` paths, symlinks leading out — are refused. Limits: 64 files per memo, 32 MB per file, 2,000-character questions, 20,000-character conclusions. Recording the same question again (case and trailing punctuation ignored) replaces the memo.

Before the next exploration, ask first:

```json
memory_exploration_recall({"question": "where is auth checked?", "project": "billing-api", "root": "/home/me/code/billing-api"})
```

Results are ranked by question similarity: an exact question match scores 1.0; otherwise 0.8 × cosine similarity of the question embeddings (same embedding model only) + 0.2 × word overlap, falling back to word overlap alone when no embedding is available. Results below 0.2 are dropped. Every file is re-hashed under the `root` given at recall time, so a moved checkout still verifies:

- `"status": "fresh"`: every file is byte-identical, so the conclusion can be reused without re-reading.
- `"status": "stale"`: `changed_files` and `missing_files` list what to re-read. `unverifiable_files` lists files that now resolve outside the root or exceed the size limit.

`include_stale: false` returns fresh memos only. Memos are kept in the `exploration_memos` / `exploration_memo_files` tables (migration 042) and are not mixed into `memory_recall`, so a stale conclusion never surfaces as a plain fact.

## Importing history and moving the store

`tam import chatgpt|claude|mem0|markdown <path>` brings existing chat history and notes into memory, and `tam export --encrypted` / `tam import-bundle` move the whole store between machines. See [docs/importing.md](importing.md).

## TypeScript SDK

For Node.js / browser / any TS project that isn't an MCP-native agent:

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

Also ships LangChain adapter example, procedural-memory integration, and HTTP transport (for team / serverless setups).

Package repo: [github.com/vbcherepanov/total-agent-memory-client](https://github.com/vbcherepanov/total-agent-memory-client)

## Dashboard (localhost:37737)

- **`/`** — live stats, queue depths, token savings from filters, representation coverage
- **`/graph/live`** — 3D WebGL force-graph (Three.js), 3,500+ nodes / 120,000+ edges, click-to-focus, type filters, search
- **`/graph/hive`** — D3 hive plot, nodes on radial axes by type
- **`/graph/matrix`** — canvas adjacency matrix sorted by type
- **`/knowledge`** — paginated knowledge browser, tag filters
- **`/sessions`** — last 50 sessions with summaries + next steps
- **`/errors`** — consolidated error patterns
- **`/rules`** — active behavioral rules + fire counts
- **SSE-pill in header** — live reconnect indicator

Screenshots → the dashboard is at `http://localhost:37737` once installed.
