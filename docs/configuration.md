# Configuration

All settings are optional environment variables. The default `MEMORY_MODE=fast`
needs no LLM and no network. The local dashboard's **Settings** page edits the
language model, embeddings, answer size and log retention without environment
variables ([LOCAL_SETTINGS.md](LOCAL_SETTINGS.md)). Internal-model settings
and CPU limits for v14 are in [LLM_V14.md](LLM_V14.md) and
[team-server.md](team-server.md#configure-internal-models-and-cpu).

- [Environment variables](#environment-variables)
- [Ollama and cloud providers](#ollama-setup-optional-but-recommended)
- [Performance tuning](#performance-tuning)

## Environment variables

Environment variables (all optional):

### v11.0 — Memory mode + multi-embedding-space

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_MODE` | `fast` | `ultrafast\|fast\|balanced\|deep`. Selects hot-path profile. See [Performance tuning](#performance-tuning). |
| `MEMORY_USE_LLM_IN_HOT_PATH` | `false` | Master switch for sync LLM stages in `save_knowledge` / `Recall.search`. `MEMORY_MODE=deep` flips this to `true`. |
| `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH` | `false` | Re-enables the silent FastEmbed → Ollama fallback ladder when FastEmbed is unavailable. |
| `MEMORY_NEGATIVE_RETRIEVAL` | `true` | `memory_answer` runs the contradiction-seeking second search (one inversion call + one batched scoring call over at most 5×5 pairs). `false` skips it. |
| `MEMORY_CONTRADICTION_POLICY` | `resolve` | What `memory_answer` does when that search finds a hard contradiction. `resolve` hands both sides, with their recording dates, to the reader, which answers with the latest value and names the one it replaced. `abstain` answers *Not enough information* without reading (the 14.1.0 behaviour). |
| `MEMORY_CONTRADICTION_SCORER` | `llm` | Who scores the (supporting, opposing) pairs of that search. `llm` uses the reasoning provider. `jev` sends every pair as one `noul` question in a single request to TypeSafe's System One API (model Jev); needs `TYPESAFE_API_KEY`. If the scorer fails, the pass reports *no contradiction* and the answer proceeds. |
| `TYPESAFE_API_KEY` | _unset_ | Key for `MEMORY_CONTRADICTION_SCORER=jev`. Empty keys and keys with control characters are rejected before any request, without echoing the key. |
| `TYPESAFE_BASE_URL` / `TYPESAFE_DEFAULT_MODEL` | `https://api.typesafe.ai` / `jev-latest` | Endpoint and model for the Jev scorer (self-hosted servers speaking `/v1/systemone` work too). |
| `MEMORY_RERANK_ENABLED` | `false` | Honour caller's `rerank=true`. When `false`, CrossEncoder rerank is hard-disabled even if a tool call requests it. |
| `MEMORY_ENRICHMENT_ENABLED` | `false` | Run the async enrichment worker. Default-ON in `balanced` / `deep`. |
| `MEMORY_TEXT_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Model for `embedding_space=text` (every record that is not code, log or config). For an English-only store, `BAAI/bge-base-en-v1.5` retrieves better (LoCoMo evidence in context 81.8% → 86.2%) but handles other languages poorly. Changing it on an existing store needs `python src/reembed.py --fastembed`. `FASTEMBED_MODEL` and `MEMORY_EMBED_MODEL`, the older names, still win when set. |
| `MEMORY_CROSS_RERANK` | `auto` | Local cross-encoder over the 50 fused candidates of `memory_recall`. `auto` re-ranks once the model has loaded in the background, `on` waits for it, `off` keeps the fused order. |
| `MEMORY_CROSS_RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | English-only models are skipped for queries written mostly outside the Latin script; `jinaai/jina-reranker-v2-base-multilingual` covers other languages. |
| `MEMORY_CROSS_RERANK_CONTEXT` | `400` | Characters of the previous and next turn of the same session the cross-encoder also reads with each candidate, so an answer like "It was about acceptance" is found for "What was the poetry reading about?". `0` scores each record alone. |
| `MEMORY_CONTEXT_RESOLVE_DATES` | `on` | In `memory_recall(mode="context")`, follow relative date phrases with the date they denote, counted from the record's leading timestamp or its `created_at`: "last Thursday [Thu 14 December 2023]", "last week [the week before Sun 17 December 2023]". English and Russian phrases. `off` returns the records as stored. |
| `MEMORY_CODE_EMBED_MODEL` | _empty → falls back to TEXT model_ | Model for `embedding_space=code`. The row still records `space=code` so a future swap is config-only. |
| `MEMORY_LOG_EMBED_MODEL` | _empty → TEXT_ | Model for `embedding_space=log`. |
| `MEMORY_CONFIG_EMBED_MODEL` | _empty → TEXT_ | Model for `embedding_space=config`. |
| `MEMORY_DEFAULT_EMBEDDING_SPACE` | `text` | Space for unclassified content. |
| `MEMORY_RECALL_TIER_WEIGHTS` | `fts=1,semantic=1.2,hyde=1,multi_repr=1,fuzzy=0.5,graph=0.3,episode=0.9,atomic_facts=1,directives=1,multi_query=0` | **14.8.0** — weights of the recall tiers in the rank fusion; name only the tiers to change. `0` switches a tier off (it is not run). `memory_explain_search` shows the weights in use; `benchmarks/tier_ablation.py` measures them. |
| `MEMORY_GRAPH_HUB_DEGREE` | `200` | **14.8.0** — a graph node linked to more records than this is ignored by the graph tier (it would connect everything to everything). |
| `MEMORY_RECALL_USER_TURN_BOOST` | `1.3` | **14.8.0** — for advice-shaped queries ("recommend", "how should I", "посоветуй") the fused score of records that are the user's own turns (`[date] user: ...`) is multiplied by this factor; the preference behind a request is in what the user said, not in the assistant's reply. `1.0` switches it off. |
| `MEMORY_SESSION_NOTES` | `on` | **14.8.0** — `session_end` also saves its summary, next steps and pitfalls as a `note` record so later recalls find them. |
| `MEMORY_LLM_FALLBACK_PROVIDERS` | _unset_ | **14.8.0** — comma-separated providers tried, in order, when the configured one does not answer its availability probe (`ollama`, `openai`, `openai-compatible`, `anthropic`). Applies to every LLM phase. |
| `MEMORY_LLM_FALLBACK_MAX_CALLS` | `500` | **14.8.0** — how many times per process a phase may resolve to a fallback provider before it stays on the configured one. |
| `MEMORY_CONSOLIDATION_BUDGET_SEC` | `120` | **14.8.0** — wall clock the hourly reflection job spends consolidating idle projects (episodes, duplicate merging, decay). `0` switches the sweep off. |
| `MEMORY_CONSOLIDATION_MAX_PROJECTS` | `3` | **14.8.0** — idle projects consolidated per reflection run, oldest first. |

### v10 + earlier

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_DB` | `~/.tam/memory.db` (legacy installs: `~/.claude-memory/memory.db`) | SQLite location |
| `MEMORY_LLM_ENABLED` | `auto` | `auto\|true\|false\|force` — LLM enrichment toggle |
| `MEMORY_LLM_MODEL` | `qwen2.5-coder:7b` | Ollama model for enrichment |
| `MEMORY_LLM_PROBE_TTL_SEC` | `60` | Cache TTL for Ollama availability probe |
| `MEMORY_LLM_TIMEOUT_SEC` | `60` | Global fallback timeout for Ollama requests (s) |
| `MEMORY_TRIPLE_TIMEOUT_SEC` | `30` | Timeout for deep triple extraction (s) |
| `MEMORY_ENRICH_TIMEOUT_SEC` | `45` | Timeout for deep enrichment (s) |
| `MEMORY_REPR_TIMEOUT_SEC` | `60` | Timeout for representation generation (s) |
| `MEMORY_TRIPLE_MAX_PREDICT` | `2048` | `num_predict` cap for triple extraction |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `MEMORY_EMBED_MODE` | `fastembed` | `fastembed\|sentence-transformers\|ollama` |
| `DASHBOARD_PORT` | `37737` | HTTP dashboard port |
| `DASHBOARD_BIND` | `127.0.0.1` | Dashboard listen address (Docker image: `0.0.0.0`) |
| `DASHBOARD_ALLOWED_HOSTS` | — | Extra host names the dashboard answers to, comma-separated. Loopback names always work; any other `Host` (for example the server's LAN name when reached through Docker) gets `421` unless listed here. Protects against DNS rebinding. |
| `MCP_HTTP_ALLOWED_HOSTS` | — | Extra host names the MCP HTTP transport (`MCP_TRANSPORT=http`) answers to, comma-separated. Loopback names and a non-wildcard `MCP_HTTP_HOST` always work; any other `Host` gets `421` and a foreign `Origin` gets `403` (DNS-rebinding protection). In Docker (`0.0.0.0`), list the name clients use to reach the container. |
| `MEMORY_MCP_PORT` | `3737` | HTTP MCP transport port (Docker path) |
| `MEMORY_ASYNC_ENRICHMENT` | `false` | **v10.1** — move quality gate / contradiction / entity dedup / episodic / wiki to a background worker. See [Performance tuning](#performance-tuning) |
| `MEMORY_ENRICH_TICK_SEC` | `0.1` | Worker tick interval (clamp `0.01..5`) |
| `MEMORY_ENRICH_BATCH` | `5` | Rows claimed per tick (clamp `1..50`) |
| `MEMORY_ENRICH_MAX_ATTEMPTS` | `3` | Retries before flipping a row to `failed` |
| `MEMORY_ENRICH_STALE_AFTER_SEC` | `60` | Seconds before a `processing` row is reclaimed (worker crash recovery) |

> CPU-only / WSL hosts: if Ollama keeps timing out, lower `MEMORY_TRIPLE_MAX_PREDICT` before raising timeouts. `install-codex.sh` writes conservative defaults automatically. **For 30-40s save latency on WSL2 → set `MEMORY_ASYNC_ENRICHMENT=true`** — see below.

Full config: see [`src/config.py`](../src/config.py).

## Ollama setup (optional but recommended)

**Without Ollama:** works fully — raw content is saved, retrieval via BM25 + FastEmbed dense embeddings.

**With Ollama:** you also get LLM-generated summaries, keywords, question-forms, compressed representations, and deep enrichment (entities, intent, topics).

```bash
brew install ollama     # or: curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5-coder:7b        # default — best quality/speed on M-series
ollama pull nomic-embed-text        # optional, alternative embedder
```

### Cloud providers (optional)

Use OpenAI, Anthropic, or any OpenAI-compat endpoint (OpenRouter, Together, Groq, DeepSeek, LM Studio, llama.cpp) instead of local Ollama.

**OpenAI:**
```bash
export MEMORY_LLM_PROVIDER=openai
export MEMORY_LLM_API_KEY=sk-...
export MEMORY_LLM_MODEL=gpt-4o-mini
```

**Anthropic:**
```bash
export MEMORY_LLM_PROVIDER=anthropic
export MEMORY_LLM_API_KEY=sk-ant-...
export MEMORY_LLM_MODEL=claude-haiku-4-5
```

**OpenRouter (100+ models via one endpoint):**
```bash
export MEMORY_LLM_PROVIDER=openai
export MEMORY_LLM_API_BASE=https://openrouter.ai/api/v1
export MEMORY_LLM_API_KEY=sk-or-...
export MEMORY_LLM_MODEL=anthropic/claude-haiku-4.5
```

**Per-phase routing** (cheap model for bulk, quality for compression):
```bash
export MEMORY_TRIPLE_PROVIDER=openai
export MEMORY_TRIPLE_MODEL=gpt-4o-mini
export MEMORY_ENRICH_PROVIDER=anthropic
export MEMORY_ENRICH_MODEL=claude-haiku-4-5
```

**Embeddings** (dimension must match existing DB or re-embed required):
```bash
export MEMORY_EMBED_PROVIDER=openai
export MEMORY_EMBED_MODEL=text-embedding-3-small  # 1536d
# or Cohere:
export MEMORY_EMBED_PROVIDER=cohere
export MEMORY_EMBED_API_KEY=...
```

### Model choice

| Model | Size | Use case |
|---|---|---|
| `qwen2.5-coder:7b` | 4.7 GB | **default** — best quality/speed ratio |
| `qwen2.5-coder:32b` | 19 GB | highest quality, needs 32 GB+ RAM |
| `llama3.1:8b` | 4.9 GB | general-purpose alternative |
| `phi3:mini` | 2.3 GB | low-RAM machines |

## Performance tuning

### v11.0 fast-mode hot path (default)

When `MEMORY_MODE=fast` (default):

| metric              |   p50 |   p95 |   p99 |
|---------------------|------:|------:|------:|
| `save_fast`         |  6.2  |  8.9  | 11.4  |
| `save_fast` cached  |  0.3  |  0.4  |  1.4  |
| `search_fast`       |  3.4  |  4.7  |  6.0  |
| `cached_search`     |  3.1  |  3.4  |  3.6  |

`llm_calls=0`, `network_calls=0`. Reproduce: `./scripts/memory-bench`. Regression gate: `./scripts/memory-perf-gate`. Architecture rationale and per-stage audit: [`docs/v11/audit.md`](v11/audit.md). Raw bench artifact: [`docs/v11/benchmark.md`](v11/benchmark.md).

If your numbers do not match the table, run `./scripts/memory-bench --warmup` first — cold FastEmbed import dominates the first call.

### Legacy: v10.5 deep-mode `memory_save` latency

The synchronous v10 hot path runs five LLM-bound stages inline so a `drop` verdict can block the INSERT and a contradiction supersede commits in the same transaction. On macOS with a warm Ollama that's ~340 ms median; on a WSL2 box without GPU/CoreML each LLM round-trip can stretch the same call into 30–40 seconds.

v10.1 ships an opt-in **inbox/outbox worker** that moves the heavy stages out of band:

```
sync   : privacy → canonical_tags → INSERT → embed → enqueue → return
worker : quality_gate → entity_dedup_audit → contradiction → episodic → wiki
```

Enable it in your env:

```bash
export MEMORY_ASYNC_ENRICHMENT=true
# Optional knobs (defaults shown):
export MEMORY_ENRICH_TICK_SEC=0.1
export MEMORY_ENRICH_BATCH=5
export MEMORY_ENRICH_MAX_ATTEMPTS=3
export MEMORY_ENRICH_STALE_AFTER_SEC=60
```

Restart the MCP server. A background daemon thread now consumes `enrichment_queue`; you can watch it on the dashboard panel **⚡ v10.1 enrichment worker**.

### Bench v10.5 (10-record corpus × 2 rounds, with LLM stages on)

`memory_save` latency:

| | min | p50 | **p95** | **p99** | max | mean |
|---|---:|---:|---:|---:|---:|---:|
| **sync** (default) | 17.5 ms | 25.3 ms | **2150.5 ms** | **2179.0 ms** | 2186.1 ms | 348.0 ms |
| **async** (`MEMORY_ASYNC_ENRICHMENT=true`) | 18.1 ms | 22.3 ms | **26.7 ms** | **27.4 ms** | 27.5 ms | 22.7 ms |

`memory_recall` latency: p50 ≈ 3-5 ms in both modes (steady state),
with cold-cache p95 outliers on the first warmup hit.

**p95 collapses 80×** with async (`2150 ms → 27 ms`). On WSL2 with a
slow Ollama, the same shape holds — sync p95 of 30-40 s becomes
async p95 of ~300-1000 ms (LLM moves out of the hot path entirely).

Reproduce: `./.venv/bin/python benchmarks/v10_5_latency.py --rounds 2 --with-llm`.
Full report: [`benchmarks/v10_5_results.md`](../benchmarks/v10_5_results.md).

### Trade-off — soft drop semantic

When async is on, a `quality_gate` `drop` no longer prevents the INSERT (we already committed in the sync path). Instead the row is marked `status='quality_dropped'` after the worker scores it. `memory_recall` ignores that status (`idx_knowledge_status_quality` is added in migration 020). Audit history stays in `quality_gate_log` so nothing is lost.

If you need strict pre-INSERT gating (e.g. compliance), keep the default sync path.

### Crash recovery

Rows stuck in `processing` longer than `MEMORY_ENRICH_STALE_AFTER_SEC` (default 60 s) are flipped back to `pending` automatically — covers worker process kills mid-stage. The pre-existing `write_intents` outbox still covers a crash *before* INSERT.
