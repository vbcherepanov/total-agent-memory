# Changelog

All notable changes to total-agent-memory are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions use [Semantic Versioning](https://semver.org/).

## [Unreleased]

- The WAL no longer grows without bound, and writes wait longer before failing with "database is locked". With 8-10 processes sharing one store (an MCP server per open session plus the dashboard), a real installation held a 334 MB WAL next to a 171 MB database and `memory_save` gave up after 5 s. `Store` now sets `journal_size_limit` to 48 MB, raises `busy_timeout` from 5000 to 15000 ms, and tries a `wal_checkpoint(TRUNCATE)` with a 200 ms timeout when it opens; a busy checkpoint is logged and startup continues.

## [14.5.1] - 2026-09-24

- Recall no longer ranks a record above its own later update. The cross-encoder that 14.5.0 turned on is trained on web search and knows the real-world value, so it lifted "The company that produced Windows Vista is Microsoft" above the later record "… is Raytheon" that replaced it. After re-ranking, records that give different values for the same statement (same opening words, a different trailing value — the rule `memory_save(supersede=true)` uses) keep the places the encoder gave them, filled newest first; every other record keeps its place. On MemoryAgentBench FactConsolidation single-hop 6k, the newest value now ranks first for 98 of 100 questions (45 in 14.5.0; the gold value is among the top 10 for all 100 in both). The development splits do not move beyond noise: LoCoMo 92.73 vs 92.99 (2 answers better, 3 worse, 337 of 385 identical), LongMemEval 92.0 vs 92.0 (all 100 answers identical). MemoryAgentBench results and what changed in the adapter: `docs/benchmarks/memoryagentbench/FINDINGS.md`.

## [14.5.0] - 2026-09-23

- The cross-encoder also reads each candidate together with the turns before and after it in its session, and both verdicts join the fused rank. A conversational turn often answers a question only next to its neighbour — "What was it about?" / "Self-acceptance and trans stories" — and the turn alone shares no word with the question. On the LoCoMo development conversations, every evidence turn of a question reaches the context for 81.8% of questions (80.3% before; multi-hop 41.9% → 45.9%), recall@10 rises from 80.3% to 82.3%, and recall@1 stays near 48% (`benchmarks/retrieval_eval.py`). `MEMORY_CROSS_RERANK_CONTEXT` sets how many characters of each neighbour it reads (400; `0` scores the record alone).
- `memory_recall(mode="context")` follows relative date phrases with the date they denote, counted from the time the record was said — its leading timestamp (`[1:56 pm on 8 May, 2023]`, `[2023/05/20 (Sat) 02:21]`, `[2023-05-20]`) or its `created_at`: "last Thursday [Thu 14 December 2023]", "yesterday [Tue 2 January 2024]". Week-level phrases stay relative to the message ("last week [the week before Sun 17 December 2023]"), because "last week" means the previous calendar week to some speakers and the past seven days to others; resolving it to a calendar week cost 5.5 points on LoCoMo's temporal questions. English and Russian phrases; `MEMORY_CONTEXT_RESOLVE_DATES=off` returns the records as stored.
- Context mode shares its character budget by search rank. It split the budget evenly, so with forty long records each kept about a thousand characters around the query's words: an assistant's list of a hundred prompt parameters reached the reader as items 1–9 and 49–57 when the question asked for the 27th, although its round ranked first. The first hit now gets four shares, the tenth 1.3, and session neighbours half a share. On the LongMemEval development questions the rounds that hold the answer reach the reader whole for 63.5% of questions instead of 52.1%, and for every question about something the assistant said instead of 38%.
- The answer guidance of context mode tells the reader to answer requests for advice from the person's recorded preferences, possessions and plans instead of replying "Not enough information", and to list the distinct items before counting or totalling them.
- `MEMORY_TEXT_EMBED_MODEL` now sets the model of ordinary records. It reached only code, log and config records; the text space kept `FASTEMBED_MODEL`, and the embedding provider checked `MEMORY_EMBED_MODEL`, so changing the documented variable mixed two models in one store and failed the dimension check at startup. `FASTEMBED_MODEL` and `MEMORY_EMBED_MODEL` still win when set.
- fastembed models whose weights sit in a separate `model.onnx_data` file load again. onnxruntime 1.2x refuses that file when it is reached through the HuggingFace cache symlink ("External data path escapes model directory"), which ruled out every model above 2 GB (`intfloat/multilingual-e5-large`, `jinaai/jina-embeddings-v3`) — and the store then saved records without any vector. Such a snapshot is now copied once into `<fastembed cache>/materialized/` and loaded from there.
- Recall ranks a cross-encoder's verdict alongside the fused lexical and vector ranks. The vector tier embeds short conversational turns poorly — "Jon: Thanks!" sits near any question about Jon — and RRF let that pull a lexical first place down to 27th. A local ONNX cross-encoder (fastembed, `Xenova/ms-marco-MiniLM-L-6-v2`, 80 MB, no torch) now scores the 50 fused candidates and its rank joins the RRF score. Evidence recall@10 on LoCoMo rises from 64.4% to 75.5%, and QA accuracy on the held-out conversations from 85.6% to 87.0% (1,155 questions, gpt-4.1-mini answering, the grading prompt Zep and Mem0 publish; +1.40 points, 95% CI [-0.09; +2.88]). The model loads in the background and the first searches keep the fused order; `MEMORY_CROSS_RERANK=off` disables it, `on` waits for the model, and `MEMORY_CROSS_RERANK_MODEL` takes a multilingual model. English-only models are skipped for queries written mostly outside the Latin script.
- `recall_count` no longer boosts ranking. It counts how often a record was *returned*, not whether it helped, so records that matched common words climbed with every query: an agent's own queries cost 4.7 points of evidence recall@10 within a single pass over LoCoMo. The counter and `last_recalled` stay for the dashboard and consolidation.
- Add `benchmarks/crossgrade_mem0.py`: grades TAM's answers and the per-question answers Mem0 publishes for its platform (github.com/mem0ai/memory-benchmarks) with the same judge model and two judge prompts — the one the public numbers used and Mem0's current one — on the same held-out questions, with paired differences, bootstrap intervals and sign tests. Add `benchmarks/retrieval_eval.py` (evidence recall@k and context coverage without an LLM). The QA harnesses answer with reasoning models (`--answer-model gpt-5-2025-08-07`: `max_completion_tokens`, default temperature, one retry with twice the budget when the reasoning leaves no answer). Results and how to reproduce them: `docs/benchmarks/head-to-head-v14/RESULTS.md`.
- Add `benchmarks/locomo_qa.py` and `benchmarks/longmemeval_qa.py`: QA accuracy through the product's own `memory_recall`, graded with the published prompts of each benchmark (LoCoMo: the lenient Zep/Mem0 grader; LongMemEval: the task-specific prompts of the official evaluator), with a development split for tuning and a held-out split for reporting. Held-out results, the protocol and the comparison with published claims: `docs/benchmarks/qa-v14/RESULTS.md`.

## [14.4.0] - 2026-09-22

Not published on its own; these changes ship in 14.5.0.


- `memory_save(supersede=true)` (also `memory_save_fast`) retires active records of the same project and type that the new record gives a new value for: same opening words, a different trailing value ("X's citizenship is Argentina" → "... is Armenia", "billing runs on PostgreSQL 16" → "18"). The retired ids come back as `superseded`. Opt-in per record: the rule misfires on multi-valued relations ("likes jazz" would retire "likes rock") and on logs with a shared header (138 of 5,128 records on a real store). On MemoryAgentBench FactConsolidation single-hop with gpt-4o-mini, saving facts with it scores 99 / 93 (6k / 262k) against 82 / 85 without.
- The vector cache no longer reloads after every write. Migration 036 logs which record each change touched, and a cached pool is patched with those rows when a search next reads it, instead of every pool being re-read: at 1M records, an unscoped recall right after a save took 4.2 s and now takes 0.37 s (p50 of 20 save/recall pairs). Pools that no search reads are not touched, so write-then-recall on many small projects costs the same as in 14.3.1 (LongMemEval, 100 questions: 22.9 vs 23.1 ms per query).
- The concept extractor's 60-second name refresh read every graph node, including the event node every save adds, which stalled one save a minute at 1M records. Migration 037 indexes only names that can match text; the refresh takes 2.6 ms, and save p99 at 1M drops from 1,432 to 140 ms. Paired 10k / 100k / 1M results: `docs/benchmarks/scale-v14/RESULTS.md`.
- `benchmarks/locomo_bench_llm.py` runs again: it replaced the store's connection after warm-up, which left the vector cache holding a closed one.

## [14.3.1] - 2026-09-21

- Fix `memory_save` dropping updates. Dedup treated a record as a repeat when it scored above a similarity threshold, so "Lionel Messi's country of citizenship is Argentina" followed by "... is Armenia" (similarity 0.96) kept only Argentina, and "billing runs on PostgreSQL 16" → "18" kept 16. A record is now a repeat only when it has the same words in the same order, ignoring case, punctuation and ё/е; anything else is stored. On MemoryAgentBench FactConsolidation (455 facts), 14.3.0 lost 36 of them.
- A repeat now replaces the stored record instead of refreshing it, so a statement carries the time it was last made. After "A", "B", "A" the current value is A again, and `memory_answer` (which orders by recording date) sees that; the old row stays as `superseded`. Team stores keep confirming the existing record, which preserves its author (`save_knowledge(..., repeat="confirm")`).
- Project-scoped search no longer slows down with the size of the whole store. Migration 035 adds the project to the full-text index as a token column, so a scoped query scores only that project's matches. The graph-seed lookup behind every recall uses the `name_norm` index instead of scanning all nodes, and `available_solutions` uses the full-text index instead of `LIKE '%word%'`. Scoped recall p50 / p95: 781 / 1,901 ms → 25 / 35 ms at 100k records, 6–64 s → 105 / 147 ms at 1M. Migration 035 rebuilds the index once (about 30 s per million records).
- The graph enrichment of `memory_recall` no longer reads the whole graph. Spreading activation loaded every edge on each call and now reads only the edges of the current frontier; the memories linked to activated nodes are summed in SQL instead of shipping every link to Python (hub tags link thousands of records). On a real 7k-record store with 181k edges, one recall's enrichment went from 2.4 s to 0.45 s with identical results.
- The full-text index is no longer rewritten on every recall: its update trigger fired on any change to a record, including the recall counter bumped for each result. It now fires only when content, context, tags or project change, and a missing delete trigger was added.
- `MCP_HTTP_WORKERS=N` runs N HTTP server processes on one port. One process runs one tool call at a time; at 100k records four workers serve 117 calls/s to 16 clients instead of 37. Sessions are stateless in that mode; POSIX only.
- The dedup lookup in `memory_save` matches every word of the new record instead of any of its first twelve: save at 100k records 149 → 43 ms p50.
- Add `benchmarks/scale_bench.py` and `docs/benchmarks/scale-v14/RESULTS.md` (10k / 100k / 1M records, 200 tenants), MemoryAgentBench FactConsolidation results in `docs/benchmarks/memoryagentbench/`, and a stdio contract test for external MCP clients.

## [14.3.0] - 2026-09-21

- `MEMORY_CONTRADICTION_SCORER=jev` scores the contradiction pairs of `memory_answer` with TypeSafe's Jev (System One API): one `noul` question per pair, one request per pass. Same accuracy as the Claude Haiku 4.5 scorer within noise (LongMemEval knowledge-update 35/78 vs 36/78); the median contradiction pass drops from 3.1 s to 1.9 s, and Jev billed $0.038 for all 78 questions. Needs `TYPESAFE_API_KEY`; `TYPESAFE_BASE_URL` and `TYPESAFE_DEFAULT_MODEL` are optional. The default scorer is unchanged.
- The Jev client rejects empty keys and keys with control characters without echoing them, retries 408/429/5xx and connection errors with backoff, and honours `retry-after-ms` / `Retry-After` up to 60 s.
- `benchmarks/knowledge_update_eval.py` records per-question contradiction-pass time and Jev token usage.

## [14.2.0] - 2026-09-21

- `memory_answer` answers with the latest value of a fact that changed over time ("Mary loves red", later "Mary no longer likes red; she has fallen for green" → *green, previously red*). The reader and verifier now see each record's recording date, and a value stays current until a later record changes it. LongMemEval knowledge-update 12/78 → 35/78 with Claude Haiku 4.5; see `docs/benchmarks/knowledge-update-v14/RESULTS.md`.
- A hard contradiction no longer refuses before reading: both sides go to the reader with their dates. `MEMORY_CONTRADICTION_POLICY=abstain` restores the 14.1.0 refusal. The contradiction scorer sees the question, so a conflict about someone else no longer blocks the answer.
- Russian word forms: Cyrillic terms are stemmed in the lexical recall tier and in claim grounding ("Маша" finds "Маше"). New dependency `snowballstemmer`.
- One stored timestamp format, `2026-09-21T08:21:37.622445Z` (UTC). Migration 034 rewrites older `+00:00`, fraction-less and zone-less values; zone-less values were local time and are converted with that zone's DST rules. Relative-time SQL filters compare against the same format.
- Answers are written in the language of the question.
- Add `benchmarks/knowledge_update_eval.py` and `scripts/smoke_knowledge_update.py`.

## [14.1.0] - 2026-09-21

- `memory_answer` now runs negative retrieval: an inverted, contradiction-seeking second search whose (positive, negative) pairs are scored in one batched call. A score ≥ 0.60 abstains with *Not enough information* without picking a side; 0.30–0.60 answers with a caveat. The verdict is returned under `negative`; `MEMORY_NEGATIVE_RETRIEVAL=false` disables it.
- Fix `memory_answer` failing with "Reader returned invalid grounded evidence" on Anthropic and Ollama providers: both now implement structured completion (forced tool call / JSON-schema `format`) instead of returning markdown-fenced JSON.
- Point the README Docker example at the current image.

## [14.0.0] - 2026-09-15

- Require Python 3.11 or newer. 13.x already imported `tomllib` and `datetime.UTC`, so it never ran on 3.10 despite declaring support; package metadata, installers and CI now state the real minimum.
- Reject contradictory grounded answers that declare both support and a missing premise after the bounded repair attempt.
- Set OpenMP/BLAS budgets before PyTorch import in rerankers, embeddings, NLI verification and calibration.
- Keep personal, team and shared workers warm by default; search cached workers first under smaller limits while preserving result ordering.
- Add Chromium, Firefox and WebKit tests for the team interface and local dashboard, with a required browser CI gate.
- Fix Windows UTF-8 storage, file locking, pip self-upgrade, background memory paths and per-user dashboard startup.
- Add an authenticated team server with isolated personal, team and shared stores, bounded worker processes, attributed history and revision conflicts.
- Add a server web interface and a standard-library-only remote MCP bridge; provide a separate Docker Compose server profile.
- Route internal text and image tasks through configured Ollama, OpenAI-compatible or Anthropic providers; require an explicit vision model.
- Bound embedding and PyTorch threads; stabilize NLI batch results with float32.
- Correct installer skip-heavy validation and show consistent product/version/release metadata.

- Redact private content before durable write intents; clear completed replay payloads.
- Handle nested and unfinished private sections without leaking their contents.
- Enforce project, branch, type and space scope after retrieval expansion; invalidate caches on database changes.
- Query each embedding space with its matching model and dimensions; expose incompatible-model diagnostics.
- Use typed iterative retrieval results and the installed runtime LLM provider.
- Bound context evidence, attach source references and scope cognitive retrieval.
- Separate recall usage from confirmation time; handle backdated temporal assertions without overlapping intervals.
- Claim representation jobs atomically; fix enrichment thread shutdown.
- Correct LoCoMo category routing and require explicit oracle mode for category prompts.
- Filter question stopwords from lexical retrieval; preserve negation and code identifiers.
- Add scoped full-evidence context windows and guidance for qualified, grounded inference.
- Use the configured provider for decomposition and enforce the no-LLM fast-path gate.
- Apply final scope checks to timeline neighbors.
- Withdraw the invalid retrieval-versus-answer-accuracy comparison; record controlled development measurements in docs/LOCOMO_V14_RESULTS.md.

## [13.0.4] — 2026-09-07 — the Docker image can finally speak stdio

### Added
- **`docker run -i … stdio`.** The image only ever started the supervisor,
  which serves MCP over Streamable HTTP on `:3737` and the dashboard on
  `:37737`. Every client that runs a local server the ordinary way — piping
  JSON-RPC over stdin/stdout, which is what Docker MCP Toolkit does — got a
  container that never answered. The engine always supported stdio
  (`MCP_TRANSPORT` defaults to it); nothing in the image reached that path.
  The entrypoint now has a `stdio` case that execs the server directly.
  Logs already went to stderr, so stdout carries JSON-RPC and nothing else —
  verified frame by frame.

### Fixed
- **`serverInfo` was wrong in both fields.** The name was still
  `claude-total-memory`, the package name retired in v7, and no version was
  ever passed to `Server(...)`, so clients rendered `version: ""`. It now
  reports `total-agent-memory` and the real version.

## [13.0.3] — 2026-09-07 — discoverability: MCP Registry, security policy, honest metadata

Nothing in the runtime changed. This release exists so the package carries
the metadata the wider ecosystem reads.

### Added
- `server.json` and an `mcp-name` marker in the README, so the package can be
  published to the official [MCP Registry](https://registry.modelcontextprotocol.io).
  The registry verifies PyPI ownership by matching that marker against the
  package description, which is why it needs a release rather than a commit.
- `SECURITY.md` — supported versions, private reporting through GitHub
  Security Advisories, and a threat model that says plainly what is in scope
  for a local-first server and what is not.
- `CONTRIBUTING.md` — development setup, the `memory_core` / `ai_layer` import
  rule that `tests/test_v11_layer_separation.py` enforces, and the two rules
  learned the hard way: tests must skip on absent gitignored corpora, and
  benchmark runners must pass `record_usage=False`.

### Fixed
- The PyPI description advertised **46 tools**; the server exposes **74**. The
  repository description on GitHub still quoted **LongMemEval R@5 97.45%**,
  a number 13.0.0 retired when the runner stopped measuring its own
  self-contained retrieval stack. Both now match the README: 74 tools,
  95.1% R@5.

## [13.0.2] — 2026-09-07 — the install carried 3 GB of CUDA it cannot reach

### Fixed — the torch stack moved to a `rerank` extra
A base install resolved `sentence-transformers`, `transformers`,
`FlagEmbedding` and `peft`. Each of them resolves torch, and torch on Linux
resolves the entire `nvidia-cu*` set. Measured with `uv pip compile` for
`x86_64-unknown-linux-gnu` / py3.12, summing the wheels PyPI serves:

| | packages | wheel bytes |
|---|---:|---:|
| before | 147 | **~3,108 MB** |
| after | 97 | **~113 MB** |

The largest single items were torch (529 MB), `nvidia-cudnn-cu13` (528 MB),
`nvidia-cublas` (404 MB) and triton (237 MB) — none of which a CPU host runs.
`FlagEmbedding` additionally dragged in `datasets`, `pyarrow` and `ir-datasets`.

This was not merely wasteful. The [Glama](https://glama.ai) build sandbox ran
out of disk unpacking `nvidia-cudnn-cu13` and the server failed to build there
at all.

And the default configuration cannot reach any of it. `MEMORY_MODE=fast` — the
default since v11 — sets `MEMORY_RERANK_ENABLED=false`, and it also sets
`MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false`, which is the exact flag gating the
fall-through to `SentenceTransformer` in `Recall._compute`. So the base install
shipped 3 GB for two code paths its own defaults forbid, and a third
(`ai_layer/verifier.py`) that only `benchmarks/v11_pipeline.py` calls.

Install it when you turn the reranker on with `MEMORY_MODE=deep` or
`MEMORY_RERANK_ENABLED=true`:

```bash
pip install "total-agent-memory[rerank]"      # pip / uvx / pipx
pip install -r requirements-rerank.txt        # clone / Docker
```

Both loaders in `src/reranker.py` now distinguish "not installed" from "failed
to load" and name the extra in the log line instead of reporting a bare
`ImportError`. Retrieval continues in RRF order without them.

### Fixed — every installer warmed a model the server does not use
`install.sh`, `install.ps1`, `install-codex.ps1` and `setup.sh` pre-downloaded
`all-MiniLM-L6-v2` through `sentence_transformers`. That is the *fallback*
model, not the fastembed default the server embeds with, so the warm-up
populated a cache nothing reads and the first real save still downloaded. The
`install.sh` copy also ran through the system `python3` rather than the venv it
had just built. All four now warm `FASTEMBED_MODEL` through the venv.

`EMBEDDING_MODEL=all-MiniLM-L6-v2` was likewise written into every IDE config,
the Dockerfile, `docker-compose.yml` and `docker/run-mcp.sh`; it named a model
the install can no longer load and has been dropped. `TRANSFORMERS_CACHE` and
`TORCHINDUCTOR_CACHE_DIR` left the image with the stack that used them.

`setup.sh` — unreferenced by the README since the rebrand — installed a
hand-written dependency list that floored `mcp[cli]` at `>=1.0.0`, the exact
floor that broke every install before 13.0.0, and never installed fastembed at
all. It now installs `requirements.txt` like every other path.

### Added
`tests/test_dependency_declaration.py` gains the mirror of its original guard.
It has always checked that nothing required is *missing* from the wheel
metadata; it now also checks that the torch stack is not *present* in it, that
the `rerank` extra carries everything `src/reranker.py` imports, that
`requirements-rerank.txt` and the extra agree, and that no installer warms a
model it cannot load.

## [13.0.1] — 2026-08-27 — the write path was quadratic

### Fixed — saves got slower as the store grew, and it was our own doing
`graph/auto_link.py` runs on every save and constructed its own
`ConceptExtractor` each time. The node-name cache lives on the instance, so it
was discarded immediately and `_get_node_names` re-read the entire
`graph_nodes` table per write — O(N) per save, O(N²) over an ingest.

Measured, and deliberately by counting reads rather than timing, so the number
does not depend on machine load:

| | full reads of `graph_nodes` per 1,000 saves | rows read |
|---|---:|---:|
| before | **1,000** | 500,499 (at ~5k nodes) |
| after | **1** | — |

At the 139k nodes a BEAM-1M ingest reaches, those same 1,000 saves would have
read roughly **139 million rows**. That is the throughput curve we published in
v13.0.0 as an open question: **25.6 → 10.8 → 6.3 messages/second** across the
100K / 500K / 1M scales, on identical code. It was not the storage engine and
not the embedding model — it was a constructor in the wrong place.

`shared_extractor(db)` returns one instance per connection. Three regression
tests: a read counter over 40 saves, instance identity per connection, and a
source guard against reintroducing the constructor call.

> v13.0.0 also shipped an incremental `_cache_put` for this cache. That change
> was correct and nearly useless on its own — the instance holding the cache
> did not survive a single save, which is why its A/B measured 3%. Fixing the
> cache without checking who owns it was the wrong order.

### Fixed — dependencies declared in only one of two places
- `fastembed`, `starlette`, `uvicorn`, `httpx` and `numpy` were in
  `requirements.txt` but not in `pyproject` dependencies, so `install.sh` and
  Docker users got them and every `pip` / `uvx` / `npx` / `brew` user did not.
  The last four arrived transitively; `fastembed` did not arrive at all, and
  without it the server falls back to sentence-transformers with a different,
  English-only model — the two install paths retrieved differently.
- Measured on one machine the fallback is in fact *lighter* (565 MB against
  921 MB) because the models differ in size, so this is a consistency fix and
  not a memory one. The ~1.5 GB users reported was eager torch, fixed in
  v13.0.0.
- `tests/test_dependency_declaration.py` compares the two lists and checks that
  anything on the optional allow-list really is imported defensively.

### Added — BEAM at the 1M scale
- 74,630 messages, 625 gradable probes: **R@5 0.448**, R@1 0.227, MRR 0.327.
  Recall decays gracefully — 13x the haystack from 100K costs 12.7 points — and
  the abilities that hold up hold up at every scale (knowledge update 0.886,
  contradiction resolution 0.871).
- The run exposed a **second**, unrelated scaling problem: search p50 grew from
  58 ms at 500K to **411 ms** at 1M for twice the data. `Store._binary_search`
  loads every active record's binary vector into numpy per query, so search is
  linear in store size. Not fixed here; recorded as the largest open
  performance item rather than left for a user to discover.

### Changed — benchmark methodology (no runtime effect)
- The LoCoMo LLM judge scored refusals as correct answers on factual
  categories, and hallucinations as correct abstentions on adversarial, where
  99.6% of golds are the empty string. Both now overruled deterministically.
  Published accuracy moved 0.551 → 0.486 (no-adv) and 0.966 → 0.904
  (adversarial).
- LLM-judged accuracy is published as a spread over three seeds rather than a
  single run, with the judge's own noise measured: the verdict flips on 2.7% of
  byte-identical answers.
- Every LoCoMo run now scores three degenerate baselines alongside the metric,
  so the floor ships with the number. The pipeline is 27× the best of them.

## [13.0.0] — 2026-08-27 — MCP 2026-07-28, and honest benchmarks

**Every install created since the MCP Python SDK went 2.0 was broken.** The
2.x line dropped the `@Server.list_tools()` / `@Server.call_tool()` decorators
this server was built on, and the dependency was floored at `mcp[cli]>=1.0.0`,
so a fresh `pip` / `uvx` / `npx` / `brew` / `docker` install resolved 2.x and
died at import with `AttributeError: 'Server' object has no attribute
'list_tools'`. Existing installs kept working only because their pinned 1.x
never moved. **If you installed after mcp 2.0 shipped, upgrade.**

Fixing it turned out to be the whole story: once tools registered again, the
server was already speaking the stateless 2026-07-28 protocol, because the SDK
serves both eras from one process. This release makes that explicit, adds the
2026 result shapes, and then goes back over the benchmark numbers — where a
feedback loop had been quietly inflating them.

### Fixed — MCP SDK 2.x (breaking for anyone on a fresh install)
- `src/server.py` registers `tools/list` and `tools/call` through whichever
  API the installed SDK exposes: the 1.x decorators, or 2.x's
  `add_request_handler(method, params_model, handler)`. `MCP_SDK_ERA` reports
  which path is live.
- `mcp[cli]>=1.9,<3` in both `requirements.txt` and `pyproject.toml`. The
  upper bound is the actual lesson: an unbounded floor is how 2.0 reached
  users unannounced.
- A tool call arriving before `_bootstrap_session()` crashed the transport —
  `store.raw_append` sat outside the `try`. Now guarded.
- An unknown tool name returned `{"error": "Unknown tool"}` with
  `isError: false`. It now raises, so clients see a real error result.

### Added — protocol revision 2026-07-28
- **Stateless era served end-to-end.** `tools/list`, `server/discover` and
  `tools/call` all answer without an `initialize` handshake, with protocol
  metadata carried per-request in `params._meta`. The legacy handshake era
  keeps working from the same process, so clients on older SDKs are
  unaffected. `tests/test_mcp_protocol_e2e.py` drives both eras against a
  real subprocess.
- **`structuredContent` on every JSON-answering tool.** 2026-07-28 lets
  structured content be any JSON value, so clients receive the parsed object
  instead of re-parsing a string. Error results carry text only. No
  `outputSchema` is declared, so this is purely additive.
- **Behaviour annotations on all 74 tools** — `readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`. Clients use these to
  decide what runs without a confirmation prompt. 38 tools are read-only;
  `memory_delete`, `memory_forget`, `memory_update`, `kg_invalidate_fact` and
  the two rebuild tools are marked destructive. A test fails if a new tool
  ships unclassified.

### Added — Claude Code plugin
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.mcp.json`
  and `hooks/hooks.json`: the MCP server, the `memory-protocol` skill and the
  seven capture hooks install in one step.

      /plugin marketplace add vbcherepanov/total-agent-memory
      /plugin install total-agent-memory@vbcherepanov

- `bin/tam_plugin_bootstrap.py` resolves a runnable server in order —
  an existing `.venv`, `PATH`, the plugin's own checkout, `uvx`/`npx`, and
  finally a venv it creates — then `exec`s it, so the client always sees one
  process. The first branch touches no network.
- `hooks/pre-edit.sh` and `hooks/on-bash-error.sh` moved from
  `examples/hooks/` into `hooks/` so the plugin references one directory.
  `install.sh` copies the whole directory as before.

### Fixed — benchmarks measured their own history
`Recall.search` bumps `recall_count` on every row it returns, and the scorer
adds `recall_boost = min(0.3, recall_count * 0.05)`. Spaced repetition is
wanted in normal use and fatal for measurement: successive runs against one
database scored 0.547 → 0.565 → 0.588 → 0.607 R@5 without a line of retrieval
code changing.

- `Recall.search(..., record_usage=False)` opts out. Both benchmark runners
  and `memory_explain_search` now pass it — a diagnostic must not change what
  it diagnoses. A clean run and a re-run are now byte-identical.
- `benchmarks/locomo_bench.py` had categories 2 and 3 **swapped** (it printed
  "multi-hop" over the temporal numbers and vice versa), and never computed
  overall MRR. Verified against `locomo10.json` and corrected.

### Added — BEAM (ICLR 2026)
- `benchmarks/beam_bench.py` scores retrieval on
  [BEAM](https://github.com/mohammadtavakoli78/BEAM) at the 100K / 500K / 1M
  scales across its ten memory abilities, using each probe's `source_chat_ids`
  as gold evidence. No LLM in the loop, so it is deterministic and free.
- Measured: **R@5 0.575** at 100K (5,732 messages, p50 17.7 ms) and **0.490**
  at 500K (38,058 messages, p50 58.5 ms). Contradiction resolution, knowledge
  update and temporal reasoning hold above 0.78 at both scales;
  `instruction_following` and `event_ordering` are near-zero at both, which is
  a statement about the primitive rather than the tuning.
- Ingest throughput halved between the two scales (25.6 → 10.8 msg/s), so the
  write path scales with store size. Not yet profiled; recorded rather than
  explained away.

### Added — LLM-judged accuracy, reported as a spread
- `benchmarks/locomo_bench_llm.py` gains `--seed`, threaded through to both the
  generator and the judge. A single LLM-judged run is a *sample*: temperature 0
  does not make the API deterministic and `seed` is documented as best-effort,
  so one number cannot answer "does it reproduce".
- Published as three seeded runs with the spread: **0.486 ± 0.002** over the
  1,540 non-adversarial questions, **0.594 ± 0.003** over all 1,986, adversarial
  **0.966**. Retrieval was byte-identical across all three seeds — only
  generation and judging vary.
- **The judge needed two deterministic guards, pointing opposite ways.** The
  second: **99.6% of LoCoMo's adversarial golds are the empty string**, and the
  judge accepts almost any fluent answer against an empty reference — 27-30
  invented answers per run scored correct, inflating the one category we used to
  lead on. With an empty gold only a refusal can be right. Effect: adversarial
  **0.966 → 0.904**, all **0.645 → 0.579**.
- **The first guard.** On ~100 of the 1,540
  non-adversarial questions per run it answered YES to a refusal: "Not mentioned
  in the conversation." scored correct against golds like `Sweden`, `June 2023`,
  `Single`, with F1 exactly 0.00. Almost certainly the adversarial rule bleeding
  across — the judge is told to accept a refusal when the gold also indicates no
  information. On categories 1-4 the gold *is* a fact, so a refusal cannot be
  correct; that is a rule, not a judgement, and it now runs at judging time
  (`judge_overruled` is recorded per record). **Effect: 0.551 → 0.486, -6.6pp.**
  Found by reading the judge's verdicts rather than its aggregate.
- Judge noise, measured by aligning all 1,986 questions across seeds: the
  generator's answer differed on 12.5%, the verdict on 5.1%, and on **2.7%** the
  judge flipped on a byte-identical answer. The aggregate holds within ±0.005
  because those flips roughly cancel, not because the instrument is precise.
- **Supersedes the previously published 0.582.** That run predates
  `record_usage=False` in this runner — it too was measuring its own earlier
  queries. The drop is the correction, not a regression.
- The report banner used to hardcode "v8" and "Haiku 4.5" whatever was actually
  run, and labelled token counts "Haiku tokens". It now prints the real version,
  models and seed, and the seed is recorded in the artifact — otherwise a
  published number cannot be traced to the run that produced it.

### Added — negative controls under the retrieval numbers
- Every LoCoMo run now scores three degenerate baselines on the same questions:
  ten random turns from the conversation (R@5 **0.012**), the ten earliest
  (**0.023**), the ten most recent (**0.003**), against the pipeline's
  **0.607** — **27x the best of them**.
- They run in the same pass as the metric, so the floor ships with the number
  instead of living in a script that stops being run. Technique borrowed from a
  sibling project that used exactly this to catch a scoring metric a deliberate
  non-answer could beat; ours survives it.

### Added — LongMemEval measures the product now
- The runner reimplemented its own BM25 / RRF / MMR / CrossEncoder stack, so
  the published 96.2% described *an algorithm*, not this software. A new
  `--modes store` ingests each question's haystack into a real `Store` and
  queries `Recall.search` — the exact path an agent takes — and is now the
  default mode. The reference implementations stay available for ablations,
  with the docstring saying plainly that `full` and `store` are not the same
  claim.

### Fixed — the server was carrying ~450 MB it never used
Reported by **d.snezhinskiy**, who noticed an MCP server sitting at ~1.5 GB and
sent a patch.

- `chromadb` and `sentence_transformers` were imported at module scope in
  `src/server.py`. Both are *fallback* paths — when fastembed is healthy
  neither is used — and `sentence_transformers` pulls in torch. Availability is
  now decided by `importlib.util.find_spec` and the real import deferred to
  first use. Measured on this machine:

  | | before | after |
  |---|---:|---:|
  | `import server` | 558 MB | **116 MB** |
  | serving, steady state | 1367 MB | **909 MB** |

  torch is no longer loaded at all unless the fallback is actually reached.
- A failed fastembed init used to be one log line, after which the server
  quietly fell back to sentence-transformers and gained ~400 MB. It now names
  the model cache and the memory cost, because the usual cause is macOS purging
  the system tmp dir where fastembed caches models by default.
- New `TAM_MODEL_CACHE` opts into a durable cache location. It is **not** the
  default: moving the cache orphans models the user already downloaded, so
  every install would re-fetch ~500 MB once and offline test runs would fail.
  The contributor's patch pinned it unconditionally; this ships the diagnosis
  loudly and leaves the move to the people who need it.

> The patch's other half pinned `mcp[cli]<2`. That is the same bug this release
> fixes, and pinning would cap every user at the 1.x SDK forever, so it is not
> taken — but it independently confirmed the failure on a client machine, with
> the symptom `MCP error -32000: Connection closed`.

### Fixed — other
- **`tree-sitter-language-pack` was in no requirements file.** The README sold
  "AST codebase ingest, 9 languages" as a differentiator while every user's
  `ingest_codebase` silently degraded to whole-file chunks. Now a dependency
  (2 MB wheel).
- **The enrichment worker shared the Store's sqlite connection.**
  `check_same_thread=False` permits cross-thread *reads*; it does not make
  writes safe, because sqlite3's implicit transaction lives on the Connection.
  Concurrent writes interleaved into `cannot start a transaction within a
  transaction`, which is what made long ingests die. The worker now opens its
  own connection — which is what WAL mode is for.
- **Migration 028 failed on every fresh database**, not just some. Root cause
  reported by [@juicetin](https://github.com/juicetin) in the "separate
  migration observation" section of #12: `Store._migrate()` added the
  subagent-lineage columns *before* `_apply_sql_migrations()` ran
  `028_agent_lineage.sql`, so 028 always hit `duplicate column name: agent_id`.
  Because SQLite has no `ADD COLUMN IF NOT EXISTS` and `executescript` is
  all-or-nothing, it aborted before its `CREATE INDEX` statements and was never
  recorded as applied — so it retried on every startup, forever.
  - The schema change now has exactly one owner: the SQL migration.
    `_migrate()` no longer duplicates it, and a test fails if any column is
    ever added by both `Store._migrate()` and a SQL migration again.
  - The runner additionally replays a duplicate-column script statement by
    statement, so databases already wedged by the old behaviour recover
    instead of needing the operator to notice.
- **`ai_layer/verifier.py` hardcoded `~/.claude-memory/nli_calibration.json`**
  and stopped finding calibrations after the `~/.tam` migration. It now
  follows the resolved memory dir, with the legacy path as a fallback.
- **The graph node cache was invalidated on every write.** `_ensure_node`
  dropped the whole `graph_nodes` name cache each time it created a node, and
  `extract_and_link` dropped it again at the end — so the 60s TTL never
  applied and the next save re-read the table. Created nodes are now inserted
  into the cache incrementally; the TTL still picks up writes from other
  processes. A clean A/B over 4,000 saves measured **103.0 vs 99.9 saves/s** —
  about 3%, within noise at that size, since the re-read cost grows with the
  node table. It is reported as measured rather than as the fix for the ingest
  slowdown it was chasing: **that slowdown (25.6 → 10.8 msg/s between a 5.7k
  and a 38k record store) is not explained by this and remains open.** Both
  arms of the A/B degrade on the same curve.
- **`vocabularies/` and `filters/` were missing from the wheel and the Docker
  image.** Same shape as the migrations bug PR #12 fixed, and just as quiet:
  `src/` resolves them as `parent.parent / <dir>`, so in a git checkout
  everything worked, while every `pip` / `uvx` / `npx` / `docker` install fell
  back to an empty tag vocabulary and turned every `memory_save(filter=…)`
  into a no-op. An installed wheel now loads all 54 canonical topics and 11
  filter configs. `tests/test_wheel_contents.py` builds the wheel, checks each
  asset directory, and greps `src/` so a *new* sibling directory cannot go
  missing the same way.

### Fixed — test suite
The suite was red on a clean checkout: 21 failed, 9 errors.
- Eight modules stub the LLM seam but still had to pass `config.has_llm()`,
  which probes for a live Ollama in `auto` mode — so they failed on any
  machine without one. New `llm_enabled` fixture; the stub is what those tests
  measure, so no network call should ever be attempted.
- `benchmarks/data/` is gitignored, so the LoCoMo few-shot and NLI calibration
  tests failed instead of skipping on a fresh clone.
- Embedding tests asserted raw vectors; the production OpenAI path
  L2-normalises so cosine equals dot product.

**1881 passing, 0 failing.**

## [12.4.0] — 2026-05-26 — 100% functional through every install path

`npx connect`, `bash install.sh`, `docker run`, `docker compose up` — same
4 services, same MCP HTTP API, same dashboard. No path is missing pieces
anymore.

### Added — MCP Streamable HTTP transport
- `src/server.py` now supports `MCP_TRANSPORT=http` (also accepts
  `streamable-http`). Boots a uvicorn ASGI app exposing:
  - `POST/GET/DELETE /mcp` — MCP Streamable HTTP (spec 2025-03-26)
  - `GET /healthz` — DB-independent liveness probe
- Same `Server` instance powers both stdio and HTTP, so all 60+ MCP tools
  are reachable over the network — IDEs, sidecars, k8s pods can all
  connect without a stdio bridge.
- ``stateless=False`` keeps in-process session multiplexing; clean
  SIGTERM via uvicorn's `timeout_graceful_shutdown=5` matches Docker
  stop-grace.

### Added — scheduler service (cron-style)
- `docker/scheduler_daemon.py` — stdlib-only scheduler that runs
  `orphan-backfill` 4×/day and `check-updates` weekly inside the same
  image. Replaces the `mcuadros/ofelia` external container and its
  Docker-socket mount.
- npm wrapper 1.3.0 ships matching scheduler services on macOS
  (`StartCalendarInterval`) and Linux (`OnCalendar` systemd timers).

### Added — full single-image stack
- `tam-entrypoint` shim routes `docker run … mcp|dashboard|reflection|scheduler|all`
  through the supervisor.
- Default `TAM_SUPERVISOR_SERVICES=mcp,dashboard,reflection,scheduler`
  → `docker run -p 3737:3737 -p 37737:37737 -v vol:/data ghcr.io/.../total-agent-memory`
  brings the whole product up with one command.
- `EXPOSE 3737 37737`; HEALTHCHECK probes both `/healthz` endpoints.
- ML caches pinned to the data volume (`HF_HOME`, `TRANSFORMERS_CACHE`,
  `TORCHINDUCTOR_CACHE_DIR`) — prevents torch._dynamo from crashing on
  containers with tmpfs `/tmp` < 100 MB.
- Default `OLLAMA_URL=http://host.docker.internal:11434` — single-image
  finds host Ollama out of the box on Docker Desktop / Windows / WSL2.
  Graceful degradation to FTS+embeddings if Ollama unreachable.

### Added (v12.3 carry-over — never tagged separately)
- `docker/tam_supervisor.py` — stdlib supervisor (prefix logging,
  exponential-backoff restarts, signal propagation).
- `/healthz` endpoint on the dashboard.
- Auto-initialise `memory.db` on first boot inside the container.
- `launchagents/com.total-agent-memory.dashboard.plist` (installed by
  `install.sh` on macOS).
- `.github/workflows/docker.yml` — multi-arch (amd64+arm64) build+push
  to ghcr on every git tag, with `/healthz` + `/api/stats` + docker
  healthcheck smoke before publish.
- `.github/workflows/smoke.yml` — install-matrix smoke on every PR.
- `tests/smoke/install-docker-pull.sh`, `tests/smoke/install-sh-macos.sh`.
- Plist placeholders unified to `__INSTALL_DIR__` / `__MEMORY_DIR__` /
  `__HOME__`. Old plists hardcoded `__HOME__/claude-memory-server/...`,
  breaking clones with any other directory name.

### Changed
- `docker-compose.yml` `mcp` service is back (was removed in v12.3 when
  HTTP transport didn't exist). Same image as `dashboard`/`reflection`,
  `command: ["mcp"]`.
- `scheduler` compose service now runs in the main image
  (`command: ["scheduler"]`) instead of `mcuadros/ofelia` — no more
  Docker socket mount in the stack.
- `install.sh` Step 5 (old `dashboard-service.sh install` call) removed
  — it created a `com.claude-total-memory.dashboard` plist that fought
  the canonical `com.total-agent-memory.dashboard` plist for port 37737.

### Fixed
- `bash install.sh` under any clone name now writes valid plist/unit
  paths instead of references to a non-existent `~/claude-memory-server/`.
- `bash install.sh --uninstall` cleans up the new dashboard plist too.

## [12.3.0] — never tagged — folded into 12.4.0

The 1.1.0 npm wrapper, `docker pull`, and certain `git clone` layouts all
left users with a non-functional web dashboard despite the module being
present in the package. v12.3 closes those gaps end-to-end.

### Added
- `docker/tam_supervisor.py` — single-container supervisor (~150 LOC,
  stdlib only) that runs dashboard + reflection together with prefix
  logging, exponential-backoff restarts, and clean SIGTERM propagation.
  Makes `docker run ghcr.io/.../total-agent-memory` give a working
  dashboard with one command.
- `tam-entrypoint` shim in the image — routes `docker run … mcp|dashboard|reflection|all`
  through the supervisor so compose services and single-image use share
  a single CMD shape.
- `/healthz` endpoint on the dashboard — DB-independent liveness probe
  suitable for Docker `HEALTHCHECK` and Kubernetes `livenessProbe`. Fresh
  volumes no longer report unhealthy until the first save.
- Auto-initialise `memory.db` on first boot in the supervisor.
- `launchagents/com.total-agent-memory.dashboard.plist` — keeps the
  dashboard running and restarts on crash. Installed automatically by
  `install.sh` on macOS.
- `.github/workflows/docker.yml` — build + push multi-arch
  (`amd64`, `arm64`) ghcr images on every git tag, with `/healthz` +
  `/api/stats` + Docker healthcheck smoke before publish. Tags produced:
  `:X.Y.Z`, `:X.Y`, `:X`, `:latest`.
- `.github/workflows/smoke.yml` — runs install-matrix smoke on every PR.
- `tests/smoke/install-docker-pull.sh` + `tests/smoke/install-sh-macos.sh`
  — black-box end-to-end install verifiers.

### Changed
- `install.sh` Step 5 (the old `dashboard-service.sh install` call)
  removed — it created a `com.claude-total-memory.dashboard` plist that
  fought the canonical `com.total-agent-memory.dashboard` plist for
  port 37737. One always ended up in crash-loop.
- Plist placeholders unified to `__INSTALL_DIR__` / `__MEMORY_DIR__` /
  `__HOME__`. Old plists hardcoded `__HOME__/claude-memory-server/...`,
  breaking any clone whose directory name wasn't `claude-memory-server`
  — the common case after the rebrand.
- Dockerfile `CMD` switched to the supervisor; `EXPOSE` narrowed to
  `37737` (the MCP HTTP port 3737 was never functional — see Known Issues).
- `docker-compose.yml` `mcp` service removed — `MCP_TRANSPORT=http` was
  silently ignored by `src/server.py` (stdio-only). For MCP, use
  `docker/run-mcp.sh` (stdio bridge). HTTP transport lands in v12.4.

### Fixed
- `bash install.sh` under any clone name now writes valid plist/unit
  paths instead of references to a non-existent `~/claude-memory-server/`.
- `bash install.sh --uninstall` cleans up the new dashboard plist too.
- New regression test `test_launchagents_substitute_install_dir_and_memory_dir`
  guards against placeholder leakage.

### Known issues
- MCP server is still stdio-only inside the Docker image — connect via
  `docker/run-mcp.sh`. Streamable HTTP transport is tracked for v12.4.

## [12.2.0] — 2026-05-24 — v11 W3 dispatch fix + Codex env alignment

Bugfix release that restores four previously-broken v11 W3 MCP tools and
aligns the Codex installer with the `.tam` memory layout.

### Fixed

- **`server._do` v11 W3 dispatch** — `memory_recall_iterative`,
  `memory_temporal_query`, `memory_entity_resolve`, and
  `memory_consolidate_status` forwarded the symbol `args`, which is not
  in scope inside `_do(name, a)`. The resulting `NameError` was caught
  by `call_tool`'s blanket `except` clause and returned to the client as
  `"Error: name 'args' is not defined"`, silently disabling all four
  tools on v12.0.0–v12.1.0. They now receive the correct per-call args.

### Added

- **`tests/test_v11_dispatch_args.py`** — regression coverage that
  exercises the four v11 W3 branches through `server.call_tool()` with
  monkey-patched handlers, asserting the args object reaches each
  handler unchanged.

### Changed

- **Codex installer env (`install.sh --ide codex`)** — writes the
  canonical `TAM_MEMORY_DIR`, retains `CLAUDE_MEMORY_DIR` as a
  compatibility alias, and defaults `MEMORY_MODE=fast` so freshly
  installed Codex clients land on the same hot-path config as
  Claude Code.
- **`hooks/lib/common.sh` and `hooks/user-prompt-submit.sh`** — resolve
  the memory dir via `TAM_MEMORY_DIR` → `CLAUDE_MEMORY_DIR` → `~/.tam`
  fallback. `hook_project_name` now honours the `MEMORY_PROJECT` env so
  hooks reporting from a wrapper project (Codex, sub-shells) can pin
  the project name explicitly.
- **`tests/test_install_ide_flag.py`** — asserts the new Codex env
  entries and sandboxes installer runs from the real user `systemctl`
  / `launchctl` / `XDG_CONFIG_HOME` via a fake `systemctl` shim, so
  contributors no longer touch their host services when running the
  suite.

### Compatibility

- No schema or wire-protocol changes. Pure server-side fix plus
  installer hardening.
- Existing installs continue to work — `CLAUDE_MEMORY_DIR` still
  resolves through the alias and `~/.claude-memory/` symlinking
  remains intact.

### Notes

- Codex hook registration was intentionally **not** included. The
  shell-style `<system-reminder>` payloads the existing hooks emit
  don't match Codex's structured JSON hook protocol; a Codex-native
  hook bridge will land in a follow-up once that protocol is
  exercised end-to-end.

## [12.1.0] — 2026-05-20 — Claude Code v2.1.145 subagent lineage

Adds first-class support for the **agent lineage** signals that Claude Code
v2.1.139+ began emitting (`x-claude-code-agent-id` / `x-claude-code-parent-agent-id`
HTTP headers; same as `agent_id` / `parent_agent_id` attributes on
`claude_code.tool` and `claude_code.llm_request` OTEL spans). Recall can now
answer "what did subagent X (dispatched from parent Y) produce".

### Added

- **Migration `028_agent_lineage.sql`** — adds nullable `agent_id` and
  `parent_agent_id` columns to `knowledge` plus two partial indexes
  (`WHERE … IS NOT NULL`) so lineage filters cost the same as a regular
  recall.
- **`memory_save` / `memory_save_fast` MCP tool inputs** — two new optional
  fields:
  - `agent_id` — subagent ID as carried by Claude Code (header / OTEL attribute).
  - `parent_agent_id` — the dispatching agent / parent span.
  Schemas are reflected by `/mcp` after a reconnect; pre-v2.1.145 callers
  see no behaviour change.
- **`save_knowledge()` Python signature** gains the same two keyword
  arguments. They flow into the outbox payload so a mid-save crash replay
  re-applies the lineage instead of dropping it.
- **`extract_transcript.py` ingests lineage from session `.jsonl`s.**
  - Reads `agent_id` / `agentId` and `parent_agent_id` / `parentAgentId`
    when Claude Code populates them (forward-compatible — naming is not
    yet contracted upstream).
  - Falls back to the **`isSidechain=true`** signal as a proxy: any session
    with at least one sidechain entry gets `agent_id = "session-<id>"` on
    its auto-extracted rows and a `has-subagent-work` tag, so recall can
    surface "sessions where a subagent ran" even before Anthropic writes
    the explicit fields.
  - Graceful schema check (`PRAGMA table_info`) keeps the script working
    on installs where migration 028 has not been applied.
- **Automatic `spawned_by` KG fact.** When `_save_knowledge_impl()` is
  called with both `agent_id` and `parent_agent_id`, it now records
  `TemporalKG.add_fact(agent, "spawned_by", parent, source="agent-lineage",
  invalidate_previous=False)`. Idempotent on (subject, predicate, object).
  `kg_at(timestamp)` and `kg_timeline()` can now reconstruct the subagent
  lineage tree at any past moment.

### Changed

- `INSERT INTO knowledge` in `server.py` now writes 15 → 17 columns,
  carrying the lineage on every save. The base `CREATE TABLE IF NOT EXISTS`
  is unchanged — the columns appear via the idempotent `_migrate()` step
  so fresh installs and upgrades converge to the same schema.

### Compatibility

- Both columns are nullable. Existing rows, callers that don't pass the
  ids, outbox replays of old intents, and consumers of `memory_recall`
  are unaffected.
- The new MCP tool fields are optional. Pre-v2.1.145 Claude Code does not
  pass them; behaviour matches v12.0.x.
- A reconnect of the MCP `memory` server is required for clients to see
  the updated `inputSchema` for `memory_save` / `memory_save_fast`.

### Companion changes outside this repo

- `~/.claude/hooks/on-stop.sh` — reads `background_tasks` and `session_crons`
  from the Stop hook payload (v2.1.145+) and appends a section to the
  recovery file. Also fixes a stale path: `~/claude-memory-server` →
  `~/total-agent-memory/.venv/bin/python`.
- `claude-statusline 1.2.0` — picks up the new `.github.repo` /
  `.github.pr.*` fields Claude Code injects in v2.1.145+ and renders a PR
  badge on line 2.

## [11.1.0] — 2026-05-14 — Graph dedup + proactive save nudges

Two production bug-fixes from a client report (2026-05-14): "graph
accumulates orphan nodes & duplicates" and "Claude ~never calls
`memory_save` on its own (~1 of 30 sessions)". Both fixed end-to-end.

### Fixed — orphan + duplicate `graph_nodes` (bug #1)

Root causes (4): no UNIQUE on `(name, type)`, case-sensitive lookup in
`add_node`, different extractors classifying the same entity under
different types (e.g. `vue/concept` vs `vue/technology` → two rows),
non-atomic `add_node`+`add_edge` (failed edge left orphan nodes).

- **Migration `026_graph_nodes_dedup.sql`** — adds `name_norm`
  (case-folded), backfill, triggers that keep it in sync, non-UNIQUE
  indexes (UNIQUE installed only post-cleanup).
- **`src/graph/store.py`** — `add_node` rewritten as case-insensitive
  UPSERT with type-collision detection (existing node of any type for
  the same `name_norm` is reused instead of forking). Race-safe via
  IntegrityError catch + re-select.
- **`GraphStore.link_pair(src_name, src_type, dst_name, dst_type, rel)`**
  — atomic create-or-reuse two nodes + edge. On edge failure deletes
  only the nodes this call freshly created. Eliminates the orphan
  pattern at the source.
- **`src/tools/merge_duplicate_nodes.py`** — one-shot cleanup tool.
  `--dry-run` default, `--apply` to mutate, `--case-only` to skip
  cross-type merges, `--add-unique` installs the final
  `UNIQUE(name_norm, type)` constraint. Repoints edges and
  knowledge_node links, dedupes collisions, drops self-loops.
- **Test coverage**: +24 tests across `test_graph.py` and the new
  `test_merge_duplicate_nodes.py`. Run on a real 8304-node production
  DB: merged 102 duplicates + 1472 stale edges, DB 118.5 → 108.8 MB.

### Fixed — model never calls `memory_save` on its own (bug #2)

Smaller models (Sonnet, Haiku) skip the priority-10 "save what matters"
rule unless reminded mid-session. The SessionStart hint fades long
before meaningful work happens. v11.1 adds in-session nudges that
Claude sees as system context on the next turn.

- **`hooks/lib/memory-nudge.sh`** — per-session counter in
  `~/.claude-memory/state/nudge-<session>.json` tracking
  `writes / edits / bashes / memory_saves`.
- **`hooks/post-tool-use.sh`** — was opt-in capture only; now always
  emits a stdout nudge when significant-writes-without-saves crosses a
  threshold. Soft nudge at 3 edits, hard at 7. Throttled to avoid spam;
  hard escalation bypasses throttle once. A `memory_save` releases
  pressure for the next 2×STEP edits.
- **`hooks/on-stop.sh`** — final `MEMORY_FINAL_WARNING` when the
  session is about to close with 0 saves but ≥3 edits.
- **`hooks/post-tool-use.ps1` + `on-stop.ps1`** — feature-parity on
  Windows.
- **New priority-10 behavioural rule**: "`MEMORY_NUDGE` in stdout is
  an immediate action signal, not information — call `memory_save`
  before the next significant edit". Installed via
  `self_rules(add_manual=...)`.
- **Tunables**: `MEMORY_NUDGE_DISABLE=1` to silence;
  `MEMORY_NUDGE_SOFT` / `_HARD` / `_STEP` to retune.
- **Test coverage**: 12 new tests (`test_memory_nudge_hook.py`)
  covering counters, threshold transitions, save-silences-pressure,
  hard escalation, summary emission.

### Operational notes

- Migration 026 is applied automatically by `_apply_sql_migrations()`
  on first MCP-server boot after upgrade — no manual SQL needed.
- `merge_duplicate_nodes.py` refuses to install the UNIQUE index if
  any duplicate row still exists — safe-by-default ordering.
- Nudge state directory is auto-pruned by `on-stop` (files >7 days).
- Hooks remain non-blocking; the inline Python reads only the cached
  temp file (no DB, no network, <50 ms typical).

### Migration

No manual steps required for users on v11.0:

```bash
git pull && pip install -e .   # or your usual upgrade path
# Next MCP-server start applies migration 026 automatically.
# (optional) clean up duplicates accumulated before the upgrade:
.venv/bin/python src/tools/merge_duplicate_nodes.py --dry-run
.venv/bin/python src/tools/merge_duplicate_nodes.py --apply --add-unique
```

## [11.0.0] — 2026-04-28 — Production Memory Engine + LoCoMo SOTA

**Headline:** LoCoMo benchmark **0.705 overall** (1986 QA, gpt-4o gen + gpt-4o-mini judge). Position **#5** on the public leaderboard — above Mem0 (0.669) and v9-ensemble3 internal best (0.696). Temporal **0.654** (+39pp vs v9 paper-method baseline). R@5 (no-adv) **0.673**. See `docs/v11/RELEASE-FINAL-2026-04-28.md` for full breakdown.

### Architecture (Wave 1 + Wave 2, ~5k LOC, ~460 new tests)

**Memory Core** (`src/memory_core/*`) — deterministic facades, no LLM in hot path:

- `episodes/{schema,extractor,retriever}.py` — Episode layer (when, who, where, what, why, outcome). Wired as **Tier 6** in `Recall._search_impl()` RRF fusion.
- `temporal/{allen,normalizer,arithmetic}.py` — Allen's 13 interval relations, 65 composition pairs, en+ru date normalisation, calendar-aware arithmetic.
- `entity_resolver.py` — Cross-session canonical entity resolution (NFKD unicode, multilingual pronoun guard, embedding fallback).
- `idk_router.py` — Threshold-based answer routing (Protocol-typed, layer-wall-safe).
- `negative_retrieval.py` — Active contradiction search for adversarial questions.
- `calibration.py` + `answer_router.py` — Platt-scaled retrieval scores, per-category routing, ECE 0.046 on validation fixture.

**AI Layer** (`src/ai_layer/*`) — LLM-touching modules, hot-path forbidden:

- `iterative_retriever.py` — IRCoT-style iterative retrieval on top of `query_rewriter`.
- `answerability.py` — Permissive answerability classifier (Haiku, JSON contract).
- `verifier.py` — Local NLI verifier (mDeBERTa-v3-base-xnli-multilingual, 270MB, p95 11.9ms on MPS). **Calibrated thresholds** (`p_entail=0.65, p_contradict=0.40`) loaded from `~/.claude-memory/nli_calibration.json`.

**Workers** (`src/workers/*`) — out-of-band consolidation:

- `consolidation_daemon.py` + `bin/consolidation-daemon` + macOS launchd plist — 24/7 idle-project consolidation. Picks oldest idle project, advisory TTL lock, pauses when project becomes active. Per-project budget 600s.

### Bench (`benchmarks/locomo_bench_llm.py`)

- `--v11-pipeline` — Post-processor: NLI veto + answerability + calibrated routing.
- `--v11-skip-nli` — Skip NLI model load (270MB).
- `--ce-rerank` now supports `V9_RERANKER_BACKEND=bge-v2-m3` (BAAI/bge-reranker-v2-m3, multilingual).
- Recall metric fix — case-insensitive dialog-id matching (`canonical_tags` lowercases tags; gold evidence keeps case).

### Existing v11 hot-path features (carried over from earlier 11.0 work)

- 4 modes: `ultrafast` / `fast` / `balanced` / `deep`. Default `fast` — zero LLM/network in save/search/recall hot path.
- Silent Ollama fallback in `Store.embed` GATED — `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true` to re-enable.
- Multi-embedding-space contract: every vector row records `embedding_provider/model/dimension/space/content_type/language`. Spaces: `text` / `code` / `log` / `config`.

### New MCP tools

- `memory_recall_iterative` — IRCoT search.
- `memory_temporal_query` — Allen relations + duration arithmetic + date normalisation.
- `memory_entity_resolve` — Cross-session canonical lookup.
- `memory_consolidate_status` — Daemon state + recent activity.
- (carried) `memory_save_fast`, `memory_search_fast`, `memory_explain_search`, `memory_warmup`, `memory_perf_report`, `memory_rebuild_fts`, `memory_rebuild_embeddings`, `memory_eval_*`.

### Embeddings

- OpenAI `text-embedding-3-large` (3072d, l2-normalised) supported via `MEMORY_EMBED_PROVIDER=openai MEMORY_EMBED_MODEL=text-embedding-3-large`. `scripts/reembed.py --provider openai --model text-embedding-3-large` re-encodes with cost preview.

### Few-shot

- `benchmarks/data/locomo_few_shot_v3.json` — 75 LoCoMo-derived pairs (15×5 categories), deterministic seed=42, md5=`29258307fdfb6a33c2668298a515fe65`.

### Migrations

- `023_episodes.sql` — `episodes_v11` + `episode_facts` + FTS mirror.
- `024_entity_aliases.sql` — `canonical_entities` + `entity_aliases` (UNIQUE on `(project, type, name_norm)`).
- `025_consolidation_state.sql` — `project_activity` + `consolidation_state`.

All idempotent.

### Layer separation

`tests/test_v11_layer_separation.py` — AST walks `src/memory_core/`; fails on any `import ai_layer`. Communication is via Protocol structural typing and dataclass parameters.

### Configuration knobs

| Env var | Default | Purpose |
|---|---|---|
| `MEMORY_MODE` | `fast` | `ultrafast`/`fast`/`balanced`/`deep`. |
| `MEMORY_EPISODE_TIER` | `true` | Enable episode-tier in Recall RRF. |
| `MEMORY_EMBED_PROVIDER` | `fastembed` | Set to `openai` for text-embedding-3-large. |
| `MEMORY_EMBED_MODEL` | depends | OpenAI model name when provider is OpenAI. |
| `V9_RERANKER_BACKEND` | `ce-marco` | `bge-v2-m3` selects multilingual BGE reranker. |
| `V11_PIPELINE` | `0` | When `1`, bench applies post-processor (NLI veto + router). |
| `V11_SKIP_NLI` | `0` | When `1`, skip NLI verifier in v11 pipeline. |

### Known regressions vs v9 paper-method baseline

- **multi-hop −10.4pp** in the unenhanced v11 stack (W4 v2). Disappears with the D146 (text-embedding-3-large + BGE + few-shot v3) stack — multi-hop returns to **+15.7pp** vs v9-ensemble3.
- **single-hop −5pp** vs v9-ensemble3 with `--query-rewrite` enabled (qrw favours retrieval over precision on single-hop). Trade-off with overall +0.9pp gain.

### Hot-path performance (warm, in-memory SQLite, M-series)

| metric              |   p50 |   p95 |   p99 |
|---------------------|------:|------:|------:|
| save_fast           |  6.2  |  8.9  | 11.4  |
| save_fast (cached)  |  0.3  |  0.4  |  1.4  |
| search_fast         |  3.4  |  4.7  |  6.0  |
| cached_search       |  3.1  |  3.4  |  3.6  |

`llm_calls = 0`, `network_calls = 0` in the deterministic hot path.

**Migration**: see [`docs/v11/MIGRATION-FROM-V10.md`](docs/v11/MIGRATION-FROM-V10.md). Architecture audit: [`docs/v11/audit.md`](docs/v11/audit.md). LoCoMo final report: [`docs/v11/RELEASE-FINAL-2026-04-28.md`](docs/v11/RELEASE-FINAL-2026-04-28.md). NLI calibration: [`benchmarks/results/nli-calibration-report.md`](benchmarks/results/nli-calibration-report.md). Failure analysis: [`benchmarks/results/baseline-failure-analysis.md`](benchmarks/results/baseline-failure-analysis.md).

## [10.5.0] — 2026-04-27

Universal skill, 9-IDE installer, cross-platform hardening, sub-agent protocol, and a fresh latency benchmark proving the v10.1 async worker delivers an **80× p95 reduction** on `memory_save`.

### Added
- **Universal `memory-protocol` skill** (`skills/memory-protocol/`) — single SKILL.md (`v10.5.0`) + 4 references (`tool-cheatsheet.md` covering all 60+ MCP tools, `workflow-recipes.md` with 15 production-tested recipes, `hooks-explained.md`, `ide-setup.md`, `subagent-protocol.md`) + 4 templates (`claude-code-settings.json`, `codex-config.toml`, `cursor-rules.mdc`, `cline-rules.md`, `codex-AGENTS-block.md`). Same canonical content for every IDE; only the wiring differs.
- **`install.sh --ide` extended from 5 to 9 IDEs**: claude-code, codex, cursor, **cline**, **continue**, **aider**, **windsurf**, gemini-cli, opencode. New helpers: `register_mcp_cline`, `register_mcp_continue`, `register_mcp_aider`, `register_mcp_windsurf`, plus `_json_merge_mcp_nested` for the dotted-key case (`cline.mcpServers`).
- **Auto-install of `skills/memory-protocol/`** on every `install.sh --ide <X>` run that targets an IDE with a skill API (claude-code / codex / opencode); IDEs without a skill API get a rules-file copy via their respective register function.
- **Sub-agent memory protocol** — universal header for any sub-agent (`php-pro`, `golang-pro`, `vue-expert`, `code-reviewer`, etc.). Documented in `skills/memory-protocol/references/subagent-protocol.md`.
- **`benchmarks/v10_5_latency.py`** — apples-to-apples sync vs async micro-bench. `--rounds N`, `--with-llm`, JSON output to `benchmarks/results/v10_5_latency.json`. Markdown report `benchmarks/v10_5_results.md`.

### Performance
- `memory_save` p95 with LLM stages on: **2150 ms (sync) → 27 ms (async)**, **80× reduction**.
- `memory_save` p99: **2179 ms → 27 ms**.
- `memory_save` mean: **348 ms → 23 ms** (15×).
- `memory_recall` p50 steady state: **3-5 ms** in both modes.
- On WSL2 with slow Ollama the same shape holds — sync p95 of 30-40 s becomes async p95 of ~300-1000 ms.

### Fixed
- **`update.sh` bash 3.2 incompatibility** — `${var,,}` (lowercase parameter expansion) replaced with `tr '[:upper:]' '[:lower:]'`. macOS default shell now parses cleanly.
- **Cross-platform shellcheck pass** — all production `.sh` scripts (`install*.sh`, `update.sh`, `setup.sh`, `hooks/*.sh`, `ollama/*.sh`) syntax-check under `/bin/bash 3.2.x` (macOS), `bash 5.x` (Linux / WSL2). Zero blocker findings; only style/info notes remain.

### Changed
- README badges: `version 10.5.0`, `tests 1153 passing`, `IDEs 9 supported`.
- README: new **IDE matrix** table after Install, updated **Performance Tuning** numbers, new **v10.5 Roadmap** entry.
- `install.sh` USAGE: documents all 9 IDEs.
- `pyproject.toml`: bumped to `10.5.0`.

### Test suite
- 1153 passing (no new tests this version — the additions are docs / installer / bench code that is exercised by smoke runs in the bench tool).

## [10.1.0] — 2026-04-27

Inbox/outbox async pipeline, two production bugfixes, and dashboard observability for the worker. Backwards compatible: all new behaviour is opt-in.

### Added
- **Async enrichment worker** (`src/enrichment_worker.py`, migration `020_async_enrichment.sql`). Opt-in via `MEMORY_ASYNC_ENRICHMENT=true`. Moves the heavy LLM-bound stages of `save_knowledge` (quality gate, entity-dedup audit, contradiction detector, episodic event linking, wiki refresh) to a background daemon thread that consumes `enrichment_queue`. Drops `memory_save` p99 latency from ~2.5 s to ~460 ms on macOS, and from 30–40 s to ~300–1000 ms on WSL2 with a slow Ollama. Soft-drop semantic: a `quality_gate` `drop` verdict marks the row `status='quality_dropped'` after the INSERT (instead of blocking it).
- **Stale-processing recovery** in `enrichment_worker`. Rows stuck in `status='processing'` longer than `MEMORY_ENRICH_STALE_AFTER_SEC` (default 60 s) flip back to `pending` automatically. Covers worker process kills mid-stage.
- **Dashboard panel `⚡ v10.1 enrichment worker`** — depth, throughput per minute, p50/p95 ms per task, oldest pending age (color-coded by SLO band), and last 5 failures with their error message. New endpoint `GET /api/v10/enrichment-queue`.
- **5 new env knobs** for the worker: `MEMORY_ASYNC_ENRICHMENT`, `MEMORY_ENRICH_TICK_SEC`, `MEMORY_ENRICH_BATCH`, `MEMORY_ENRICH_MAX_ATTEMPTS`, `MEMORY_ENRICH_STALE_AFTER_SEC`.
- **`Performance tuning` README section** with sync-vs-async benchmark and tuning guidance for slow-LLM hosts.
- **17 regression tests**: 15 for the worker (enqueue/claim/idempotency/retry/soft-drop/daemon/stale-recovery), 4 for `_binary_search` edge cases, 1 for coref RU→EN guard.

### Fixed
- **`Store._binary_search` `ValueError: kth(=N) out of bounds (N)` on small candidate pools.** `np.argpartition` requires `kth STRICTLY < N`; tiny test projects (≤ 50 active embeddings) used to silently break `contradiction_log` because the save-path swallowed the exception in a generic `except`. Hot path now takes the whole pool when `n_candidates >= len(pool)`.
- **`coref_resolver` translating Russian → English.** `qwen2.5-coder:7b` (and Llama 3.x) interpreted the rewrite prompt as an instruction to switch language. Prompt now pins output language explicitly (`Do NOT translate. Do NOT switch language even partially.`) and tests assert the guard remains in the prompt.
- **`embed_provider` test fixtures** rejecting the `context=` kwarg passed by certifi-aware production callers (Python 3.13 macOS). Fixture `_capture_urlopen.fake()` now accepts forward-compatible kwargs.

### Changed
- `sqlite3.connect(check_same_thread=False)` for the `Store` connection so the enrichment worker thread can share it. Safe under WAL + busy_timeout=5000.
- Test suite: 1124 → 1153 passing (+29).
- Bumped version to `10.1.0`; `pyproject.toml` aligned.

## [10.0.0] — 2026-04-27

Beever-Atlas-inspired feature wave: 10 new pipeline stages, 5 new migrations, 153 new tests.

### Added
- **Quality gate (Beever 6-Month Test)** — synchronous LLM scorer (specificity / actionability / verifiability) with threshold 0.5, fail-open. Blocks low-signal records before INSERT. `MEMORY_QUALITY_GATE_ENABLED`, `MEMORY_QUALITY_THRESHOLD`.
- **Importance boost** — `critical / high / medium / low` field on knowledge rows; multiplies recall RRF score (×1.5 / ×1.2 / ×1.0 / ×0.8). Reserved for migration-blocking decisions and security incidents.
- **Canonical tag vocabulary** — 86 topics in `vocabularies/canonical_topics.txt`, normalised on save via embedding cosine + Levenshtein. Aliases under length 3 ignored.
- **Coref resolver** (opt-in via `MEMORY_COREF_ENABLED=true` or `coref=True` per save). Expands pronouns/deictics using last 20 records from the same session before INSERT.
- **Auto contradiction detector** — same-type/project semantic neighbours scored by LLM; `≥0.8` confidence → automatic supersession, `0.5–0.8` → flagged. Audit trail in `contradiction_log`.
- **Outbox / write-intent journal** — every `save_knowledge` call writes an intent row before any side-effects, allowing crash recovery on restart. `_reconcile_outbox_at_startup` replays committed intents.
- **Embedding-based entity dedup** — non-canonical tags get a second-chance lookup against active `graph_nodes` via cosine ≥ 0.85. Audit log in `entity_dedup_log`.
- **Episodic save events** — every save spawns an `event` node in `graph_nodes` with `MENTIONED_IN` edges to entity nodes. Enables queries like "show me saves where Postgres and Bob were mentioned together".
- **Smart query router** — bilingual (EN+RU) heuristic classifier on `memory_recall`; relational queries (wh-words / connectors / multiple entities) get a graph_search pass with a +1.3× RRF boost.
- **Per-project Markdown wiki digest** (`memory_wiki_generate(project)`) — Top Decisions / Active Solutions / Conventions / Recent Changes. Files land in `<MEMORY_DIR>/wikis/<project>.md`.

### Migrations
- `015_quality_importance.sql`, `016_contradictions.sql`, `017_outbox.sql`, `018_entity_dedup.sql`, `019_episodic_links.sql` — applied automatically by `_apply_sql_migrations` at startup.

### Changed
- `save_knowledge` returns a 5-tuple `(rid, was_dedup, was_redacted, private_sections, quality_meta)` — was 4-tuple in v9.

## [8.0.0] — 2026-04-19

Major feature wave: task workflow phases, structured decisions, cloud providers, activeContext live-doc, and many other quality-of-life improvements.

### Added
- **Cloud LLM providers** — `MEMORY_LLM_PROVIDER=openai|anthropic|ollama`. OpenAI-compat for OpenRouter, Together, Groq, DeepSeek, LM Studio, llama.cpp server. Per-phase routing: `MEMORY_TRIPLE_PROVIDER`, `MEMORY_ENRICH_PROVIDER`, `MEMORY_REPR_PROVIDER` with independent models.
- **Cloud embeddings** — `MEMORY_EMBED_PROVIDER=fastembed|openai|cohere` with dimension-mismatch safety gate that blocks catastrophic re-embed accidents.
- **`<private>...</private>` inline-tag** for automatic secret redaction in `save_knowledge`.
- **Session auto-compression** — `session_end(auto_compress=True)` generates summary/next_steps/pitfalls via LLM provider.
- **Progressive disclosure 3-layer workflow** — `memory_recall(mode="index")` returns compact ID+title+score, `memory_get(ids=[...])` batched full-content fetch. ~83% token saving vs default full recall.
- **Task complexity classifier** — `classify_task(description)` returns {level: 1-4, suggested_phases, estimated_tokens}.
- **Task phases state machine** — `task_create` / `phase_transition` / `task_phases_list` / `complete_task` with L1-L4 routing (van→plan→creative→build→reflect→archive). Migration 012_task_phases.sql.
- **Structured `save_decision`** — title + options + criteria_matrix + selected + rationale + discarded + auto multi-representation indexing. `memory_recall(decisions_only=True)` filter.
- **`activeContext.md` live-doc** — Obsidian markdown projection of session_init/end for human-readable session state. `MEMORY_ACTIVECONTEXT_VAULT` env override.
- **Phase-scoped rules** — `self_rules_context(project, phase="build")` with `phase:X` tag filter (zero migration). `rule_set_phase` MCP tool.
- **HTTP citation endpoints** — `/api/knowledge/{id}`, `/api/session/{id}` with related-graph expansion. HTML views at `/knowledge/{id}` and `/session/{id}`.
- **UserPromptSubmit hook** — captures user prompts into `intents` table (migration 013). `save_intent` / `list_intents` / `search_intents` MCP tools.
- **PostToolUse capture hook** — opt-in (`MEMORY_POST_TOOL_CAPTURE=1`) tool observation capture via deferred reflection queue.
- **Unified installer** — `install.sh --ide {claude-code|cursor|gemini-cli|opencode|codex}`. `install-codex.sh` is now a 3-line backward-compat shim.
- **15+ new MCP tools** — total count now 60+.
- **9 new src modules** — privacy_filter, llm_provider, embed_provider, task_classifier, task_phases, decisions, active_context, intents, recall_modes.
- **3 new migrations** — 011 privacy_counters, 012 task_phases, 013 intents.
- **2 new hooks** — user-prompt-submit.sh, post-tool-use.sh.
- **Donation link** updated to PayPal.Me/vbcherepanov.

### Fixed
- **Regression restore** — commit 2976ca1 ("docs(v7.0): sync README, install.sh, src refresh", 2026-04-17) accidentally reverted merged PR #5 (timeout config functions). Restored `get_triple_timeout_sec`, `get_enrich_timeout_sec`, `get_repr_timeout_sec`, `get_triple_max_predict` and related callers.
- **`has_llm()` phase-aware** — now consults provider.available() for cloud providers instead of only probing local Ollama. Previously `MEMORY_LLM_PROVIDER=openai` with Ollama offline would early-return False from all callers.

### Changed
- **Test suite** — 501 → 749 passing tests (+248).
- **Dashboard bind** — already on 127.0.0.1 (no change, maintaining security baseline).

## [7.0.0] — 2026-04-15

See git history for previous releases.
