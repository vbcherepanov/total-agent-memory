# Upgrading from v7 and v8 (archived)

These two upgrade guides date from before the package was published on PyPI and are kept for reference only. They are moved unchanged from `docs/upgrading.md`, except that links into the current pages were adjusted. For a current install, see [upgrading.md](../upgrading.md).

- [Upgrading from v8.x to v9.0](#upgrading-from-v8x-to-v90)
- [Upgrading from v7.x to v8.0](#upgrading-from-v7x-to-v80)

## Upgrading from v8.x to v9.0

v9 is **backward compatible**. Existing v8 calls and DB schema work unchanged — v9 is an infra release that adds pluggable backends, a public CLI for sub-agents, and LoCoMo benchmark wiring. Nothing is forcibly enabled.

### One-command upgrade

```bash
cd ~/total-agent-memory && ./update.sh   # legacy clones: ~/claude-memory-server
# pulls v9 src, installs new entry-points (tam, tam-lookup, lookup-memory; legacy: ctm-lookup),
# keeps existing memory.db untouched.
```

After upgrade, verify the new CLI is on PATH:

```bash
lookup-memory --limit 1 "any-query-from-your-history"
```

### What's new (no action required)

- **`lookup-memory` / `tam-lookup` / `ctm-lookup` (legacy)** CLI now installed alongside `total-agent-memory` MCP server (registered as `[project.scripts]` so `./install.sh` and `./update.sh` put them on PATH automatically). Sub-agent prompts that reference the legacy `~/claude-memory-server/ollama/lookup_memory.sh` script keep working; new prompts should prefer the package-installed name.
- **Embedding backends** stay on `fastembed` by default. Switch via `V9_EMBED_BACKEND=openai-3-large` (set `MEMORY_EMBED_API_KEY`) — costs ~$0.10/5k rows for re-embed, expected R@5 lift on conversational data.
- **Reranker backend** stays on `ce-marco` by default. `V9_RERANKER_BACKEND=bge-v2-m3` (or `off`) switches at runtime.
- **Subject-aware retrieval** is opt-in via `--subject-aware` in `benchmarks/locomo_bench_llm.py`. Future: surface as MCP tool flag.
- **No migrations.** Schema unchanged from v8.

### What requires manual action

- **Re-embed** (only if switching embedding model, otherwise skip):
  ```bash
  python -m scripts.reembed --backend openai-3-large --confirm
  ```
- **Old bash sub-agent prompts** that hardcode `~/claude-memory-server/ollama/lookup_memory.sh "query"` will keep working. To ride the new package install, replace with `lookup-memory "query"`.

### Breaking changes

None. All v8 MCP tools, env vars, hooks, and DB tables behave identically.

---

## Upgrading from v7.x to v8.0

v8.0 is **backward compatible** — your existing v7 installation keeps working unchanged. All new features are opt-in via MCP tool calls or env vars.

### One-command upgrade

```bash
cd ~/total-agent-memory && ./update.sh   # legacy clones: ~/claude-memory-server
# Applies migrations 011-013 idempotently, restarts LaunchAgents, updates dependencies
```

Then restart Claude Code: `/mcp restart memory`.

### What changes automatically

- **Migrations 011–013** apply on MCP startup (privacy_counters, task_phases, intents). Zero-downtime, idempotent.
- **Existing `memory_save`** calls keep working — they now additionally strip `<private>...</private>` sections if present.
- **Existing `memory_recall`** calls keep working — default mode is still `"search"`. New `mode="index"` is opt-in.
- **Existing `session_end`** calls keep working — `auto_compress=False` by default. Pass `auto_compress=True` to opt in.
- **Existing `self_rules_context`** calls keep working — default returns all rules (no phase filter).

### What requires manual setup

**1. Cloud providers** (only if you want to replace/augment Ollama):
```bash
export MEMORY_LLM_PROVIDER=openai       # or "anthropic"
export MEMORY_LLM_API_KEY=sk-...
export MEMORY_LLM_MODEL=gpt-4o-mini     # or "claude-haiku-4-5"
```
See [Cloud providers](../configuration.md#cloud-providers) for OpenRouter / per-phase routing / Cohere examples.

**2. Install additional hooks** (for UserPromptSubmit capture + citation):
```bash
./install.sh --ide claude-code   # re-run installer; it now registers user-prompt-submit.sh hook
```
The hook is additive — existing hooks keep working.

**3. activeContext.md Obsidian integration** (if you want markdown projection):
```bash
export MEMORY_ACTIVECONTEXT_VAULT=~/Documents/project/Projects   # default
# Disable: export MEMORY_ACTIVECONTEXT_DISABLE=1
```
Each `session_end` writes `<vault>/<project>/activeContext.md`.

### Breaking changes

**None.** All v7 MCP tool signatures are preserved. New parameters are optional with safe defaults.

### Embedding dimension note

If you switch to a cloud embedding provider (`MEMORY_EMBED_PROVIDER=openai/cohere`), the server **will refuse to start** if existing DB embeddings have a different dimension than the new provider returns. This is deliberate — it prevents silent data corruption.

Either:
- Keep `MEMORY_EMBED_PROVIDER=fastembed` (default 384d) and only change the LLM provider, OR
- Re-embed the DB: `python src/tools/reembed.py --provider openai --model text-embedding-3-small`

### New MCP tools in v8.0

Quick reference — see the current list in [All 79 tools](../tools.md#all-79-tools):

| Tool | Purpose |
|---|---|
| `classify_task(description)` | Returns {level 1-4, suggested_phases, estimated_tokens} |
| `task_create(task_id, description)` | Starts state machine in "van" phase |
| `phase_transition(task_id, new_phase, artifacts?)` | Moves task through van/plan/creative/build/reflect/archive |
| `task_phases_list(task_id)` | Chronological phase history |
| `save_decision(title, options, criteria_matrix, selected, rationale, ...)` | Structured decision with per-criterion indexing |
| `memory_get(ids, detail)` | Batched full-content fetch for IDs from `memory_recall(mode="index")` |
| `save_intent` / `list_intents` / `search_intents` | UserPromptSubmit-captured prompts |
| `rule_set_phase(rule_id, phase)` | Tag a rule for phase-scoped loading |

Extended tools:
- `memory_recall(mode="index"|"timeline", decisions_only=False, ...)` — 3-layer token-efficient workflow
- `session_end(auto_compress=True, transcript=None, ...)` — LLM-generated summary
- `self_rules_context(phase="build"|"plan"|...)` — phase filter
- `save_knowledge(...)` — now strips `<private>...</private>` sections automatically

### Rollback plan

v8.0 doesn't remove any v7 functionality. If you hit an issue, you can:

1. Set env var to revert behaviour:
   ```bash
   export MEMORY_LLM_PROVIDER=ollama           # revert to local LLM
   export MEMORY_EMBED_PROVIDER=fastembed      # revert to local embeddings
   export MEMORY_ACTIVECONTEXT_DISABLE=1       # disable markdown projection
   export MEMORY_POST_TOOL_CAPTURE=0           # disable opt-in capture (default anyway)
   ```

2. Migrations 011/012/013 are additive (no `DROP` / `ALTER` on existing tables), so DB downgrade is not destructive — old code continues reading older tables.

3. Worst case: `git checkout v7.0.0 && ./update.sh --skip-migrations`.
