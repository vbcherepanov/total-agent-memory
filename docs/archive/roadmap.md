# Roadmap and release history (v8 to v13)

This page was written at v13.0.0 (2026-08-27) and moved from the README. Some
open items below may have been addressed in 14.x; the
[CHANGELOG](../../CHANGELOG.md) is authoritative, and 14.x is summarised in
[whats-new.md](../whats-new.md).

## Shipped in v13.0.0 (2026-08-27)
- ✅ **MCP SDK 2.x compatibility** — the blocker: every install created after
  `mcp` 2.0 shipped was dead on arrival. Tools register through either SDK era;
  dependency bounded `>=1.9,<3`.
- ✅ **Protocol revision 2026-07-28** — stateless era served end-to-end
  (`tools/list` / `server/discover` / `tools/call` with no handshake), legacy
  handshake era from the same process, `structuredContent` on JSON-answering
  tools, behaviour annotations on all 74.
- ✅ **Claude Code plugin** — `/plugin install total-agent-memory@vbcherepanov`
  wires the MCP server, the skill and seven hooks in one step.
- ✅ **Reproducible benchmarks** — `record_usage=False` stops runs from
  measuring their own history; category labels in the LoCoMo runner corrected.
- ✅ **BEAM (ICLR 2026)** added to the suite at 100K / 500K / 1M.
- ✅ **`tree-sitter-language-pack` is now an actual dependency** — AST ingest
  had been silently degrading to whole-file chunks for every user.
- ✅ **Enrichment worker owns its sqlite connection** — long ingests no longer
  die on `cannot start a transaction within a transaction`.

## Shipped in v11.0 (2026-04-27) — production memory engine
- ✅ **Default `MEMORY_MODE=fast`** — zero LLM, zero Ollama, zero network in save/search/recall hot path. Set `MEMORY_MODE=deep` to restore v10.5 behaviour.
- ✅ **Memory Core / AI Layer split** — `src/memory_core/*` is deterministic; `src/ai_layer/*` owns every LLM-bound code path. Enforced by `tests/test_no_llm_hot_path.py`.
- ✅ **4 modes**: `ultrafast` / `fast` / `balanced` / `deep`. Single env flag.
- ✅ **Multi-embedding-space contract** — every vector row records provider / model / dimension / space / content_type / language. Spaces: `text` / `code` / `log` / `config`. Single Chroma backend; per-space model swap is config-only.
- ✅ **Embed fallback ladder gated** — silent Ollama fallback in `Store.embed` requires `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true`.
- ✅ **New MCP tools**: `memory_save_fast`, `memory_search_fast`, `memory_explain_search`, `memory_warmup`, `memory_perf_report`, `memory_rebuild_fts`, `memory_rebuild_embeddings`, `memory_eval_locomo`, `memory_eval_recall`, `memory_eval_temporal`, `memory_eval_entity_consistency`, `memory_eval_contradictions`, `memory_eval_long_context`.
- ✅ **Migrations 021 (embedding_spaces) + 022 (embedding_cache_v11)** — idempotent on next start.
- ✅ **Benchmark suite**: `scripts/memory-bench` (artifact `docs/archive/v11/benchmark.md`) + `scripts/memory-perf-gate` for CI.

## Shipped in v10.5 (2026-04-27)
- ✅ **Universal `memory-protocol` skill** — single canonical SKILL.md + 4 references (tool cheatsheet for all MCP tools, workflow recipes for 15 common situations, hooks reference, per-IDE setup) + 4 templates (Claude Code settings.json, Codex config.toml, Cursor `.mdc`, Cline `.md`). Same content for every IDE; only the wiring differs.
- ✅ **`install.sh --ide` extended to 9 IDEs**: claude-code, codex, cursor, **cline**, **continue**, **aider**, **windsurf**, gemini-cli, opencode. New helpers: `register_mcp_cline / continue / aider / windsurf` + `_json_merge_mcp_nested` for the dotted-key case (`cline.mcpServers`).
- ✅ **Cross-platform hardening** — all bash scripts pass `bash -n` under macOS bash 3.2 (default). Replaced `${var,,}` lowercase bashism in `update.sh` with `tr '[:upper:]' '[:lower:]'`. Verified with shellcheck.
- ✅ **Sub-agent memory protocol** — universal header for any sub-agent (`php-pro`, `golang-pro`, `vue-expert`, etc.) with mandatory `memory_recall` before / `memory_save` after. Full template in `skills/memory-protocol/references/subagent-protocol.md`.
- ✅ **v10.5 latency benchmark** — `benchmarks/v10_5_latency.py` with apples-to-apples sync vs async comparison. Demonstrates **80× p95 reduction** (`2150 ms → 27 ms`) when async is enabled with LLM stages on.

## Shipped in v10.1 (2026-04-27)
- ✅ **Async enrichment worker** — opt-in `MEMORY_ASYNC_ENRICHMENT=true` moves quality gate / entity dedup / contradiction detector / episodic linking / wiki refresh to a background thread. Drops max save latency 5.4× on macOS, 60–100× on WSL2. See [Performance tuning](../configuration.md#background-enrichment-worker).
- ✅ **`enrichment_queue` table** with stale-processing recovery (rows stuck >60 s in `processing` flip back to `pending`).
- ✅ **Dashboard panel** for worker health: depth, throughput/min, p50/p95 ms per task, oldest pending age, recent failures.
- ✅ **`_binary_search` ValueError fix** — `np.argpartition` requires `kth STRICTLY < N`; tiny test projects (pool ≤ 50) used to silently break `contradiction_log`.
- ✅ **`coref_resolver` RU→EN translation fix** — prompt explicitly pins output language (`Do NOT translate`).

## Shipped in v10.0 (2026-04-27)
- ✅ **10 Beever-Atlas-inspired features in one push**: quality gate (Beever 6-Month Test), canonical tag vocabulary, importance boost in recall, opt-in coref resolution, contradiction auto-detection with supersede, write-intent outbox + reconciler, embedding-based entity dedup, episodic save events in the graph, smart query router (relational vs lexical), per-project Markdown wiki digest.
- ✅ 5 SQLite migrations (`015–019`) applied automatically on restart.
- ✅ 11 new env knobs, all with safe fail-open defaults.
- ✅ Tests: 971 → 1124 (+153).

## Shipped in v9.0 (2026-04-25)
- ✅ **`lookup-memory` / `tam-lookup` / `ctm-lookup` (legacy) CLI** — bash entry-point for sub-agents, registered as `[project.scripts]` and installed by `./install.sh` / `./update.sh` (replaces manual `~/claude-memory-server/ollama/lookup_memory.sh`)
- ✅ **Pluggable embedding backends**: `openai-3-small`, `openai-3-large` (3072d), `bge-m3`, `e5-large`, `locomo-tuned-minilm` (fine-tuned on user data)
- ✅ **Pluggable reranker backends**: `ce-marco`, `bge-v2-m3`, `bge-large`, `off` (env `V9_RERANKER_BACKEND`, hot-swap)
- ✅ **Subject-aware retrieval** — LLM extracts (subject, action) from question → SQL graph lookup → DIRECT FACTS prepended to context (LoCoMo cat 1/2 lift)
- ✅ **Judge-weighted ensemble** — category-aware scoring rubric + abstain logic for LoCoMo-style adversarial gold
- ✅ **Fine-tune embedding pipeline** (`scripts/finetune_embedding.py`) — mine triplets from your data, train on top of MiniLM via `sentence-transformers`
- ✅ **Few-shot pair mining** (`scripts/mine_locomo_fewshot.py`) — augment per-category prompts with held-in (Q,A) pairs
- ✅ **Schema-specific graph extractor** (closed canonical predicate vocabulary, optional)
- ✅ **SSL fix for macOS Python.org installs** — `urllib` requests now use certifi by default
- ✅ **HTTP retry with exponential backoff** for embedding providers (5xx/timeout)
- ✅ LoCoMo benchmark integration (`benchmarks/locomo_bench_llm.py` with 14 ablation flags)

## Shipped in v8.0 (2026-04-19)
- ✅ Task workflow phases (L1-L4 classifier + 6-phase state machine)
- ✅ Structured `save_decision` with criteria matrix + multi-representation criterion indexing
- ✅ Cloud LLM/embed providers (OpenAI, Anthropic, Cohere, any OpenAI-compat)
- ✅ `session_end(auto_compress=True)` via LLM provider
- ✅ Progressive disclosure: `memory_recall(mode="index")` + `memory_get(ids)`
- ✅ `activeContext.md` Obsidian live-doc projection
- ✅ Phase-scoped rules via tag filter
- ✅ `<private>...</private>` inline redaction
- ✅ HTTP citation endpoints `/api/knowledge/{id}` + `/api/session/{id}`
- ✅ UserPromptSubmit + PostToolUse (opt-in) capture hooks
- ✅ Unified `install.sh --ide {claude-code|cursor|gemini-cli|opencode|codex}`

## Next — what the v13 numbers say to fix

The benchmarks point at specific gaps rather than a general "make retrieval
better", so the roadmap names them:

- **`instruction_following` R@5 = 0.075, `event_ordering` = 0.150 (BEAM).**
  These probes ask *whether a stated instruction was followed* or *in what
  order things happened*. Semantic similarity to the question does not find the
  message where the instruction was given — retrieval is the wrong primitive.
  Needs a directive index (statements of the form "always/never/from now on")
  and ordering-aware traversal over the episodic graph.
- **`multi-hop` R@5 = 0.413 (LoCoMo).** Weakest category, and the one where
  the leaders win. Query decomposition without putting an LLM back in the hot
  path is the open design question.
- **`single_session_preference` R@5 = 0.80 (LongMemEval), `preference_following`
  = 0.282 (BEAM).** The same weakness from two directions: preferences are
  stated once, in passing, and never restated.
- **BEAM-10M.** The 1M scale runs today; 10M is the interesting claim.
- **Search is linear in store size.** BEAM 1M measured p50 411 ms against 58 ms
  at 500K — `Store._binary_search` loads every active record's binary vector
  into numpy per query. An ANN index over those vectors is the obvious answer.
  This is the largest open performance item.
- ~~Profile the write path~~ — done in v13.0.1: `auto_link` constructed a
  `ConceptExtractor` per save and threw away its node cache, re-reading the
  whole `graph_nodes` table on every write.

## Planned
- GitHub Actions: install smoke tests + a nightly retrieval gate, so a
  regression in R@5 fails CI the way `scripts/memory-perf-gate` already fails on
  latency.
- `has_llm()` per-phase provider caching.

## Under research
- "Endless mode" — continuous session without hard boundaries (virtual sessions by idle >N hours)
- MLX local LLM integration
- Speculative decoding for local path (+1.5-1.8× LLM speed)
