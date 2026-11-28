# Configuration

Every setting is an optional environment variable. With none set, the server
needs no LLM and no network after the models have downloaded. The local
dashboard's **Settings** page edits the language model, embeddings, answer size
and log retention without environment variables ([LOCAL_SETTINGS.md](LOCAL_SETTINGS.md)).
Internal-model settings for the team server are in [llm-settings.md](llm-settings.md) and
[team-server.md](team-server.md#configure-internal-models-and-cpu). The source of
truth for every default is [`src/config.py`](../src/config.py).

- [The settings most people touch](#the-settings-most-people-touch)
- [Two rerankers, two switches](#two-rerankers-two-switches)
- [Storage, mode and network](#storage-mode-and-network)
- [Retrieval](#retrieval)
- [Answers and contradictions](#answers-and-contradictions)
- [Writing, rules and consolidation](#writing-rules-and-consolidation)
- [Tool catalogue and telemetry](#tool-catalogue-and-telemetry)
- [Ambient memory (hooks)](#ambient-memory-hooks)
- [LLM providers](#llm-providers)
- [Background enrichment worker](#background-enrichment-worker)

## The settings most people touch

| Variable | Default | What it does |
|---|---|---|
| `TAM_MEMORY_DIR` | `~/.tam` | Where `memory.db` and all state live. The older `CLAUDE_MEMORY_DIR` still works and prints a deprecation warning. |
| `TAM_MODEL_CACHE` | fastembed's default (system temp directory) | Directory for downloaded models (about 370 MB for the two defaults). Set it, for example to `~/.tam/models`, so macOS does not purge the models. |
| `MEMORY_CROSS_RERANK` | `auto` | The local cross-encoder over the fused candidates: `auto`, `on`, `off`. See [below](#two-rerankers-two-switches). |
| `MEMORY_TEXT_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Embedding model for ordinary text records (384 dimensions). |
| `MEMORY_MODE` | `fast` | `ultrafast`, `fast`, `balanced` or `deep`. `fast` uses no LLM in the save and search path. |
| `MEMORY_LLM_ENABLED` | `auto` | `auto`, `true`, `force` or `false`: use an LLM (Ollama or a cloud provider) for optional enrichment. Nothing needs it. |
| `DASHBOARD_PORT` | `37737` | Port of the local dashboard. |
| `TAM_TOOL_PROFILE` | `full` | How many tools `tools/list` returns: `full` (79), `lean` (28) or `minimal` (12). |

## Two rerankers, two switches

TAM has two unrelated rerankers. Do not mix them up.

| | Cross-encoder (default) | Sentence-transformers reranker (legacy, opt-in) |
|---|---|---|
| Switch | `MEMORY_CROSS_RERANK` = `auto` (default), `on`, `off` | `MEMORY_RERANK_ENABLED` = `false` (default); `MEMORY_MODE=deep` sets it to `true` |
| Model | `MEMORY_CROSS_RERANK_MODEL`, default `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` (multilingual) | CrossEncoder or BGE reranker through PyTorch |
| Runtime | ONNX through fastembed, a base dependency: no extra, no PyTorch | Needs the `[rerank]` extra (PyTorch; on Linux about 3 GB of wheels) |
| Effect | Re-scores the 50 fused candidates of every `memory_recall` | Used only when a call asks for `rerank=true`; with `false` that request is ignored |

`MEMORY_CROSS_RERANK=auto` re-ranks once the model has loaded in the background
(the first calls after a start can return the fused order), `on` waits for the
model, `off` keeps the fused order. The cross-encoder model is a one-time
download of about 130 MB. To go back to the English-only model before 15.0.0, set
`MEMORY_CROSS_RERANK_MODEL=Xenova/ms-marco-MiniLM-L-6-v2`; English-only models are
skipped for queries written mostly outside the Latin script.
`jinaai/jina-reranker-v2-base-multilingual` is the other multilingual choice.

## Storage, mode and network

| Variable | Default | What it does |
|---|---|---|
| `TAM_MEMORY_DIR` | `~/.tam` | Memory directory; the database is `<dir>/memory.db`. |
| `TAM_MODEL_CACHE` | see above | Model cache directory; sets `FASTEMBED_CACHE_PATH` when that is unset. |
| `MEMORY_MODE` | `fast` | Hot-path profile. `ultrafast` and `fast`: no LLM calls, no Ollama fallback, no enrichment, the background worker on. `balanced`: the same hot path, with the enrichment worker and the Ollama fallback enabled. `deep`: synchronous quality gate, contradiction check and advanced retrieval, LLM calls in the save and search path, and the legacy reranker. An explicit value of any flag below wins over the mode. |
| `MEMORY_USE_LLM_IN_HOT_PATH` | `false` (`true` in `deep`) | Synchronous LLM stages in `memory_save` and `memory_recall`. |
| `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH` | `false` (`true` in `balanced` and `deep`) | Lets the embedder fall back from FastEmbed to Ollama when FastEmbed is unavailable. |
| `MEMORY_ENRICHMENT_ENABLED` | `false` (`true` in `balanced` and `deep`) | Run the enrichment queues. |
| `MEMORY_RERANK_ENABLED` | `false` (`true` in `deep`) | The legacy reranker, see above. |
| `DASHBOARD_BIND` | `127.0.0.1` | Dashboard listen address (the Docker image uses `0.0.0.0`). |
| `DASHBOARD_ALLOWED_HOSTS` | unset | Extra host names the dashboard answers to, comma-separated. Loopback names always work; any other `Host` gets `421` unless listed (protection against DNS rebinding). |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `127.0.0.1` / `3737` | Address of the HTTP MCP transport (the Docker image uses `0.0.0.0`). |
| `MCP_HTTP_ALLOWED_HOSTS` | unset | Extra host names the HTTP transport answers to, comma-separated. Loopback names and a non-wildcard `MCP_HTTP_HOST` always work; any other `Host` gets `421` and a foreign `Origin` gets `403`. In Docker, list the name clients use to reach the container. |
| `TAM_DAEMON_PORT` | `0` | Opt-in shared warm daemon (`tam daemon`, `tam proxy`): loopback port, `0` = any free port. See [shared-daemon.md](shared-daemon.md). |
| `TAM_DAEMON_IDLE_EXIT_MIN` | `60` | The daemon exits after this many idle minutes; `0` = never. |
| `TAM_DAEMON_START_TIMEOUT_SEC` | `30` | How long `tam proxy` and `tam daemon start` wait for the daemon's health check. |
| `TAM_DAEMON_STOP_TIMEOUT_SEC` | `20` | Graceful-stop window before the daemon is signalled. |

## Retrieval

### Embedding models

| Variable | Default | What it does |
|---|---|---|
| `MEMORY_TEXT_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Model for `embedding_space=text`, every record that is not code, log or config. `BAAI/bge-base-en-v1.5` is the English-only preset used for the English results in [benchmarks](benchmarks.md); it handles other languages poorly. Changing the model on an existing store needs `python src/reembed.py --fastembed`. `FASTEMBED_MODEL` and `MEMORY_EMBED_MODEL`, the older names, win when set. |
| `MEMORY_CODE_EMBED_MODEL` | `jinaai/jina-embeddings-v2-base-code` | Model for `embedding_space=code`. An empty value falls back to the text model; the row still records `space=code`. |
| `MEMORY_LOG_EMBED_MODEL` | empty (text model) | Model for `embedding_space=log`. |
| `MEMORY_CONFIG_EMBED_MODEL` | empty (text model) | Model for `embedding_space=config`. |
| `MEMORY_DEFAULT_EMBEDDING_SPACE` | `text` | Space for unclassified content (`text`, `code`, `log`, `config`). |

### Cross-encoder

`MEMORY_CROSS_RERANK` and `MEMORY_CROSS_RERANK_MODEL` are described [above](#two-rerankers-two-switches).

| Variable | Default | What it does |
|---|---|---|
| `MEMORY_CROSS_RERANK_CONTEXT` | `400` | Characters of the previous and next turn of the same session that the cross-encoder reads with each candidate, so "It was about acceptance" is found for "What was the poetry reading about?". `0` scores each record alone. |
| `MEMORY_CROSS_RERANK_WINDOW` | `50` | Candidates re-ranked (at least 2). |
| `MEMORY_CROSS_RERANK_WEIGHT` | `2.0` | Weight of the cross-encoder rank in the fusion (positive). |
| `MEMORY_CROSS_RERANK_THREADS` | `4` | ONNX threads. |
| `MEMORY_CROSS_RERANK_BATCH_SIZE` | `8` | Candidates scored per batch, grouped by text length. `0` restores the encoder's own batching. |
| `MEMORY_CROSS_RERANK_CACHE_SCORES` | `4096` | Size of the score cache. `0` disables it. |

### Recall tiers

| Variable | Default | What it does |
|---|---|---|
| `MEMORY_RECALL_TIER_WEIGHTS` | `fts=1,semantic=1.2,hyde=1,multi_repr=1,fuzzy=0.5,graph=0.3,episode=0.9,atomic_facts=1,directives=1,multi_query=0,query_entity=0` | Weights of the recall tiers in the rank fusion; name only the tiers to change. `0` switches a tier off. `memory_explain_search` shows the weights in use; `benchmarks/tier_ablation.py` measures them. `query_entity` (records linked to graph entities named in the query) is off by default because it did not help on the held-out sets ([details](benchmarks/pmb-adoption-recall/RESULTS.md#5-query_entity-tier)). |
| `MEMORY_GRAPH_HUB_DEGREE` | `200` | A graph node linked to more records than this is ignored by the graph tier. |
| `MEMORY_RECALL_USER_TURN_BOOST` | `1.3` | For advice-shaped queries ("recommend", "how should I", "посоветуй"), the fused score of the user's own turns is multiplied by this. `1.0` switches it off. |
| `MEMORY_RECALL_INTENT_BOOSTS` | `decision=1.3,lesson=1.3,howto=1.3` | A question about a decision, a lesson or rule, or a fix multiplies the fused score of `decision`, `lesson` plus `convention`, or `solution` records by this factor. Name only the intents to change; `1.0` switches one off. |
| `MEMORY_RECALL_COMPOUND_SLOTS` | `on` | A compound query ("X; Y", "X, and what ...") is split, each part is searched on its own, and each part's best record is guaranteed a place in the top results ([details](benchmarks/pmb-adoption-recall/RESULTS.md#4-compound-queries-guaranteed-slots)). |
| `MEMORY_RECALL_FTS_CROSSLINGUAL_DAMP` | `0.3` | When none of the query's content words occurs in the full-text vocabulary (a question in one language over records in another), the full-text weight is multiplied by this. `1.0` switches it off. |
| `MEMORY_RECALL_SINGLEFLIGHT` | `on` | Concurrent recalls with the same project, normalised query, limit and options share one execution. `off` runs each call on its own. |
| `MEMORY_RECALL_SINGLEFLIGHT_WAIT_SECONDS` | `30` | How long a waiter waits for the shared execution before running its own search. |
| `MEMORY_RECALL_TOUCH_FLUSH_MS` | `250` | Usage statistics (`recall_count`, `last_recalled`) are buffered and written by a background thread this often, so a search never takes the write lock. Minimum `10`. |
| `MEMORY_CONTEXT_RESOLVE_DATES` | `on` | In `memory_recall(mode="context")`, relative date phrases ("last Thursday") are followed by the date they denote, counted from the record's timestamp. English and Russian. `off` returns the records as stored. |

## Answers and contradictions

These settings affect `memory_answer`, the only tool that needs a reasoning LLM.

| Variable | Default | What it does |
|---|---|---|
| `MEMORY_NEGATIVE_RETRIEVAL` | `true` | `memory_answer` runs a contradiction-seeking second search (one inversion call and one batched scoring call over at most 5 x 5 pairs). `false` skips it. |
| `MEMORY_CONTRADICTION_POLICY` | `resolve` | On a hard contradiction, `resolve` passes both sides with their recording dates to the reader, which answers with the latest value and names the one it replaced. `abstain` answers *Not enough information* without reading. |
| `MEMORY_CONTRADICTION_SCORER` | `llm` | Who scores the (supporting, opposing) pairs. `llm` uses the reasoning provider. `jev` sends every pair as one question in a single request to TypeSafe's System One API and needs `TYPESAFE_API_KEY`. If the scorer fails, the pass reports no contradiction and the answer proceeds. |
| `TYPESAFE_API_KEY` | unset | Key for the Jev scorer. Empty keys and keys with control characters are rejected before any request, without echoing the key. |
| `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL` | `https://api.typesafe.ai`, `jev-latest` | Endpoint and model for the Jev scorer (self-hosted servers speaking `/v1/systemone` work too). |

## Writing, rules and consolidation

| Variable | Default | What it does |
|---|---|---|
| `MEMORY_SESSION_NOTES` | `on` | `session_end` also saves its summary, next steps and pitfalls as a `note` record so later recalls find them. |
| `MEMORY_DECAY_EXEMPT_TYPES` | `decision,convention,lesson` | Record types whose recency decay stops at `MEMORY_DECAY_EXEMPT_FLOOR`. An empty value exempts no type. |
| `MEMORY_DECAY_EXEMPT_FLOOR` | `0.85` | Lowest decay factor of the exempt types, `0` to `1`. An invalid value is logged and the default used. |
| `MEMORY_CRITICAL_DAILY_BUDGET` | `5` | `importance=critical` saves per project per UTC day; further ones are stored as `high`, and the reply carries `importance_note`. Conventions and rules are exempt. `0` = unlimited. |
| `MEMORY_HIGH_DAILY_BUDGET` | `0` | The same for `importance=high` (stored as `medium` beyond the budget). `0` = unlimited. |
| `MEMORY_OUTBOX_ENABLED` | `true` | Write-intent journal of every save: an intent is opened before the record is written and closed after it; one left open by a crash is replayed when the store next opens. |
| `MEMORY_OUTBOX_GRACE_SEC` | `60` | Age a pending intent must reach before it is replayed. |
| `MEMORY_OUTBOX_MAX_ATTEMPTS` | `3` | Replays before an intent is marked `failed`. |
| `MEMORY_OUTBOX_LEASE_SEC` | `600` | A reconciler leases the intents it replays for this long; a crashed reconciler's lease expires. |
| `MEMORY_PREDICATE_VOCAB_PATH` | `vocabularies/predicate_aliases.txt` | Vocabulary that groups knowledge-graph predicates stating one relation (`city`, `current_city`, `location` map to `lives_in`); a new object under any form of a group supersedes the current one under another. One group per line, canonical form first, comma-separated; `#` starts a comment. A missing or contradictory file is an error. |
| `MEMORY_PREDICATE_VOCAB_EXTRA` | unset | Further vocabulary files merged into the first, separated by the OS path separator. |
| `MEMORY_CONSOLIDATION_BUDGET_SEC` | `120` | Wall clock the hourly reflection job spends consolidating idle projects (episodes, duplicate merging, decay). `0` switches the sweep off. |
| `MEMORY_CONSOLIDATION_MAX_PROJECTS` | `3` | Idle projects consolidated per reflection run, oldest first. |

## Tool catalogue and telemetry

| Variable | Default | What it does |
|---|---|---|
| `TAM_TOOL_PROFILE` | `full` | The catalogue `tools/list` returns: `full` (every tool, long descriptions), `lean` (28 everyday tools) or `minimal` (12 core tools), the last two with one-line descriptions. Hidden tools still answer when called; an unknown value stops the server at startup. See [LOCAL_SETTINGS.md](LOCAL_SETTINGS.md#agent-interface). |
| `TAM_MCP_INSTRUCTIONS` | `true` | Send a short usage guide in the MCP `instructions` field when a client connects. |
| `TAM_TOOL_TELEMETRY` | `true` | Record one `tool_calls` row per call (tool, duration, outcome, error class, argument and result sizes, never their contents), kept 7 days; `memory_perf_report` shows count, p50, p95 and error rate per tool. |

## Ambient memory (hooks)

Memory that works while the agent never calls a memory tool. The hooks read these
settings on every call, from the environment or, taking precedence, from the local
dashboard (`<memory dir>/settings.json`).

| Variable | Default | What it does |
|---|---|---|
| `TAM_AMBIENT_RECALL` | `true` | UserPromptSubmit adds the rules matching the prompt and a short background block (warm through the [shared daemon](shared-daemon.md), full-text only without it) as `additionalContext`. Nothing already injected in the session is repeated. |
| `TAM_AMBIENT_TOKEN_BUDGET` | `400` | Upper bound per prompt, in tokens (`50` to `5000`). Rules take at most half. |
| `TAM_AMBIENT_LATENCY_MS` | `1500` | Time limit of the prompt recall (`100` to `4000`). The prompt is never blocked or failed. |
| `TAM_RULE_REMINDERS` | `true` | PreToolUse (Bash, Edit, Write, MultiEdit, NotebookEdit): a rule whose text matches the command or the edited path, first of all a prohibition the action seems to break, is shown to the agent. The same rule at most once per 15 minutes per session. Never a permission decision. |
| `TAM_RULE_COMPLIANCE` | `true` | Stop: the turn's logged actions are checked against the rules shown to the session. A broken prohibition is reported to the user and counted in the rule's heuristic `auto_success_count` / `auto_fail_count` only; explicit ratings and auto-suspension never see them. |
| `TAM_TURN_AUTOSAVE` | `true` | Stop (and Codex notify): a turn that edited files or ran commands is saved as a short `fact` (`source='auto'`, tags `auto-saved`, `turn-autosave`). `tam forget-auto` deletes these. |
| `TAM_CORRECTION_CAPTURE` | `true` | UserPromptSubmit: a prompt that corrects the agent ("I already told you", "я же говорил") becomes a draft insight (`category='user_correction'`) that becomes promotable to a rule only after real upvotes. Regex only. |
| `TAM_ACTION_LOG` | `true` | PostToolUse and PostToolUseFailure log each tool call in `agent_actions`: tool, a short secret-redacted summary, exit status. Read-only lookups (Read, Glob, Grep, LS, Todo*) are not logged. |
| `TAM_ACTION_LOG_RETENTION_DAYS` | `30` | Older rows are deleted on every write and by `memory_forget`; the log is also capped at 100,000 rows. |

`tam forget-auto [--project P] [--include-actions] [--dry-run] [--json]` removes the
auto-saved records (with their vectors, full-text rows and raw-log copies) and, with
`--include-actions`, the action log. Codex CLI turns reach the same log through
`hooks/codex-notify.sh` (`notify = [".../hooks/codex-notify.sh"]` in
`~/.codex/config.toml`). On Docker installs (`TAM_DOCKER=1`, set by the launchers
`install-docker.sh` writes) the hooks run these commands inside the container with
`docker exec -i tam-mcp python -m ambient <command>`.

## LLM providers

Ollama and cloud providers are optional.

**Without an LLM:** everything but `memory_answer` works. Raw content is saved and
retrieval uses full-text search, dense embeddings and the cross-encoder.

**With one:** you also get summaries, keywords, question forms, compressed
representations and deep enrichment (entities, intent, topics).

| Variable | Default | What it does |
|---|---|---|
| `MEMORY_LLM_PROVIDER` | `ollama` | `ollama`, `openai`, `anthropic` or an OpenAI-compatible endpoint (see below). |
| `MEMORY_LLM_MODEL` | `qwen2.5-coder:7b` | Model for LLM stages. |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint. |
| `MEMORY_LLM_ENABLED` | `auto` | `auto`, `true`, `force` or `false`. |
| `MEMORY_LLM_PROBE_TTL_SEC` | `60` | Cache time of the Ollama availability probe. |
| `MEMORY_LLM_TIMEOUT_SEC` | `60` | Fallback timeout of LLM requests. |
| `MEMORY_TRIPLE_TIMEOUT_SEC` / `MEMORY_ENRICH_TIMEOUT_SEC` / `MEMORY_REPR_TIMEOUT_SEC` | `30` / `45` / `60` | Timeouts of triple extraction, deep enrichment and representation generation. Setting `MEMORY_LLM_TIMEOUT_SEC` makes it the default for all three. |
| `MEMORY_TRIPLE_MAX_PREDICT` | `2048` | `num_predict` cap for triple extraction. |
| `MEMORY_LLM_FALLBACK_PROVIDERS` | unset | Comma-separated providers tried, in order, when the configured one does not answer its probe (`ollama`, `openai`, `openai-compatible`, `anthropic`). Applies to every LLM phase. |
| `MEMORY_LLM_FALLBACK_MAX_CALLS` | `500` | How many times per process a phase may resolve to a fallback before it stays on the configured provider. |

On CPU-only or WSL hosts where Ollama keeps timing out, lower
`MEMORY_TRIPLE_MAX_PREDICT` before raising timeouts, and use the
[enrichment worker](#background-enrichment-worker).

### Ollama

```bash
brew install ollama     # or: curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5-coder:7b        # default LLM, 4.7 GB
ollama pull nomic-embed-text        # optional, alternative embedder
```

`qwen2.5-coder:32b` (19 GB, needs 32 GB or more of RAM) gives higher quality;
`llama3.1:8b` (4.9 GB) is a general-purpose alternative; `phi3:mini` (2.3 GB) suits
low-RAM machines.

### Cloud providers

OpenAI, Anthropic, or any OpenAI-compatible endpoint (OpenRouter, Together, Groq,
DeepSeek, LM Studio, llama.cpp) can replace Ollama.

```bash
# OpenAI
export MEMORY_LLM_PROVIDER=openai
export MEMORY_LLM_API_KEY=sk-...
export MEMORY_LLM_MODEL=gpt-4o-mini

# Anthropic
export MEMORY_LLM_PROVIDER=anthropic
export MEMORY_LLM_API_KEY=sk-ant-...
export MEMORY_LLM_MODEL=claude-haiku-4-5

# OpenRouter (one endpoint, many models)
export MEMORY_LLM_PROVIDER=openai
export MEMORY_LLM_API_BASE=https://openrouter.ai/api/v1
export MEMORY_LLM_API_KEY=sk-or-...
export MEMORY_LLM_MODEL=anthropic/claude-haiku-4.5
```

Per-phase routing sends bulk work to a cheap model and compression to a better one.
The phases are `triple`, `enrich`, `repr` and `reason`; each reads
`MEMORY_<PHASE>_PROVIDER` and `MEMORY_<PHASE>_MODEL`:

```bash
export MEMORY_TRIPLE_PROVIDER=openai
export MEMORY_TRIPLE_MODEL=gpt-4o-mini
export MEMORY_ENRICH_PROVIDER=anthropic
export MEMORY_ENRICH_MODEL=claude-haiku-4-5
```

Embeddings can also come from a provider. The dimension must match the existing
database, otherwise re-embed (`python src/reembed.py`):

```bash
export MEMORY_EMBED_PROVIDER=openai          # default: fastembed
export MEMORY_EMBED_MODEL=text-embedding-3-small  # 1536 dimensions
# or Cohere:
export MEMORY_EMBED_PROVIDER=cohere
export MEMORY_EMBED_API_KEY=...
```

## Background enrichment worker

With `MEMORY_ASYNC_ENRICHMENT=true` the slow stages (quality gate, contradiction,
entity dedup, episodic, wiki) run in a background thread that consumes
`enrichment_queue`, so `memory_save` returns after the insert and the embedding:

```
sync   : privacy -> canonical_tags -> INSERT -> embed -> enqueue -> return
worker : quality_gate -> entity_dedup_audit -> contradiction -> episodic -> wiki
```

| Variable | Default | What it does |
|---|---|---|
| `MEMORY_ASYNC_ENRICHMENT` | set by `MEMORY_MODE`: `true` in `ultrafast`, `fast` and `balanced`, `false` in `deep` | Run the worker. |
| `MEMORY_ENRICH_TICK_SEC` | `0.1` | Worker tick (clamped to `0.01`-`5`). |
| `MEMORY_ENRICH_BATCH` | `5` | Rows claimed per tick (`1`-`50`). |
| `MEMORY_ENRICH_MAX_ATTEMPTS` | `3` | Retries before a row is marked `failed`. |
| `MEMORY_ENRICH_STALE_AFTER_SEC` | `60` | Seconds before a `processing` row is reclaimed after a worker crash. |

With the worker on, a quality-gate `drop` no longer prevents the insert: the row is
marked `status='quality_dropped'` afterwards and `memory_recall` ignores it. The
verdict stays in `quality_gate_log`. If you need strict gating before the insert
(for example for compliance), use `MEMORY_MODE=deep`. The `write_intents` outbox
covers a crash before the insert.
