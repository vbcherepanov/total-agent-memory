# total-agent-memory

> **The only memory layer that learns _how_ you work — not just _what_ you said.**
> Persistent, local memory for AI coding agents: Claude Code, Codex CLI, Cursor, any MCP client.
> Temporal knowledge graph · procedural memory · AST codebase ingest · cross-project analogy · 3D WebGL visualization.

[![Version](https://img.shields.io/badge/version-11.1.0-8ad.svg)]()
[![Tests](https://img.shields.io/badge/tests-1200%2B%20passing-4a9.svg)]()
[![IDEs](https://img.shields.io/badge/IDEs-9%20supported-4a9.svg)]()
[![LongMemEval R@5](https://img.shields.io/badge/LongMemEval%20R@5-96.2%25-4a9.svg)](evals/longmemeval-2026-04-17.json)
[![LoCoMo Acc](https://img.shields.io/badge/LoCoMo%20Acc-0.596-4a9.svg)](benchmarks/results/)
[![vs Supermemory](https://img.shields.io/badge/vs%20Supermemory-%2B10.8pp-4a9.svg)](docs/vs-competitors.md)
[![p50 latency](https://img.shields.io/badge/p50%20warm-0.065ms-4a9.svg)](evals/results-2026-04-17.json)
[![Local-First](https://img.shields.io/badge/100%25-local-4a9.svg)]()
[![License](https://img.shields.io/badge/license-MIT-fa4.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-blue.svg)](https://modelcontextprotocol.io)
[![npm](https://img.shields.io/badge/npm-%40vbch%2Ftotal--agent--memory--client-cb3837.svg)](https://www.npmjs.com/package/@vbch/total-agent-memory-client)
[![Donate](https://img.shields.io/badge/PayPal-Donate-00457C.svg?logo=paypal&logoColor=white)](https://PayPal.Me/vbcherepanov)

**Why this, not mem0 / Letta / Zep / Supermemory / Cognee?** → [docs/vs-competitors.md](docs/vs-competitors.md)

---

## v11.1 — graph dedup + proactive save nudges

Two client-reported bugs fixed (2026-05-14):

**Bug #1 — orphan + duplicate `graph_nodes`.** The graph accumulated
case-variant duplicates (`Vue` / `vue` / `VUE`) and type-collision
duplicates (`vue/concept` vs `vue/technology` created by different
extractors), plus orphan nodes when an edge insert failed after both
nodes were already committed. Fixed by migration `026_graph_nodes_dedup`
(`name_norm` column, triggers, indexes), a case-insensitive UPSERT
rewrite of `add_node` with type-collision detection, a new atomic
`GraphStore.link_pair()` helper, and a one-shot cleanup tool
`src/tools/merge_duplicate_nodes.py` (dry-run by default).

```bash
# After upgrade migration 026 applies automatically. Then optionally:
.venv/bin/python src/tools/merge_duplicate_nodes.py --dry-run
.venv/bin/python src/tools/merge_duplicate_nodes.py --apply --add-unique
```

Verified on a real production DB (8304 nodes): 102 duplicates merged,
1472 stale edges cleaned, UNIQUE constraint installed.

**Bug #2 — model never calls `memory_save` on its own.** Sonnet/Haiku
skip the priority-10 save rule when SessionStart context fades. v11.1
adds in-session **nudges**: a counter in `~/.claude-memory/state/`
tracks writes-vs-saves per session, and `hooks/post-tool-use.{sh,ps1}`
emits a stdout line that Claude reads as system context on the next
turn. Soft nudge at 3 edits with 0 saves, hard at 7, and a
`MEMORY_FINAL_WARNING` on session stop. A new priority-10 rule
instructs the model to treat `MEMORY_NUDGE` as an immediate command.

Tunables: `MEMORY_NUDGE_DISABLE=1` to silence; `MEMORY_NUDGE_SOFT` /
`_HARD` / `_STEP` to retune (defaults `3 / 7 / 3`).

Test coverage: +24 graph tests, +12 nudge tests. Full details in
[`CHANGELOG.md`](CHANGELOG.md#1110--2026-05-14--graph-dedup--proactive-save-nudges).

---

## v11.0 — production memory engine

**v11.0 = production memory engine: fast deterministic memory core + async AI enrichment layer. Default mode is `fast`: zero LLM, zero Ollama, zero network in the save/search/recall hot path.**

The codebase is now split into two layers:

- **`src/memory_core/*`** — deterministic facade modules (storage, embeddings, vector_store, classifier, chunker, dedup, cache, graph_links, telemetry, health, embedding_spaces). No LLM imports allowed. Enforced by `tests/test_no_llm_hot_path.py`.
- **`src/ai_layer/*`** — every LLM-touching path (enrichment_worker, summarizer, keyword_extractor, question_generator, relation_extractor, contradiction_detector, reflection, self_improve, plus thin shims for quality_gate / coref_resolver / reranker / query_rewriter). Off-limits to memory_core.

Architecture details and full hot-path audit: [`docs/v11/audit.md`](docs/v11/audit.md).

### Modes

`MEMORY_MODE` selects the runtime profile. Default is `fast`.

| Mode | Hot-path LLM | Async enrichment | Reranker | Embed fallback | Use when |
|---|:-:|:-:|:-:|:-:|---|
| `ultrafast` | off | off | off | FastEmbed only (vector index off, FTS-only) | Throughput stress / CI |
| **`fast`** (default) | **off** | **off** | **off** | **FastEmbed only, Ollama fallback gated** | **Production coding-agent loop** |
| `balanced` | off (sync) | **on** | off | FastEmbed only | You want LLM-derived facets, but never on the critical path |
| `deep` | on (sync) | on | on (when `rerank=true`) | FastEmbed → Ollama ladder | v10.5 behaviour: quality gate / contradiction / coref / HyDE inline |

`deep` mode reproduces v10.5.0 defaults exactly. Set `MEMORY_MODE=deep` if you depended on synchronous quality_gate, contradiction_detector, or coref. `balanced` keeps the same ergonomics but moves enrichment off-thread.

Migration from v10.5: [`docs/v11/MIGRATION-FROM-V10.md`](docs/v11/MIGRATION-FROM-V10.md).

### v11.0 hot-path benchmark

Warm, in-memory SQLite, MacBook M-series, `MEMORY_MODE=fast`, `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false`:

| metric              |   p50 |   p95 |   p99 |
|---------------------|------:|------:|------:|
| `save_fast`         |  6.5  |  9.0  | 27.8  |
| `save_fast` cached  |  0.3  |  0.4  |  1.1  |
| `search_fast`       |  3.7  |  4.0  |  6.2  |
| `cached_search`     |  0.0  |  0.0  |  0.0  |

**`llm_calls = 0`, `network_calls = 0`** across the entire hot path. Reproduce: `bin/memory-bench`. CI gate: `bin/memory-perf-gate`. Raw artifact: [`docs/v11/benchmark.md`](docs/v11/benchmark.md).

### v10.5 → v11.0 — same workload, same script

The v10.5 native bench (`benchmarks/v10_5_latency.py`) re-run on v11 fast against the recorded v10.5 baseline (`benchmarks/results/v10_5_latency.json`):

| metric                | v10.5 sync (with LLM) | v11.0 fast | speedup |
|-----------------------|----------------------:|-----------:|--------:|
| save p95              | 2150.51 ms            | 8.51 ms    | **252×** |
| save p99              | 2178.98 ms            | 11.09 ms   | **196×** |
| recall p95            | 1424.26 ms            | 5.81 ms    | **245×** |
| recall p99            | 1771.70 ms            | 6.75 ms    | **262×** |
| LLM calls / save      | 2-4                   | 0          | gate    |
| Network calls / save  | 1-3                   | 0          | gate    |

Even versus v10.5 _without_ LLM (`23.3 ms p95`), v11 fast is `2.7×` faster — the deterministic-only stages (quality_gate probe, contradiction candidate fetch, episodic event creation, project_wiki refresh) are now fully bypassed in fast mode and queued only when `MEMORY_ENRICHMENT_ENABLED=true`.

Recall quality is preserved: LongMemEval R@5 = 100% on a 30-question sample; hybrid retrieval (FTS5 + dense + RRF + base graph) is identical to v10.5 except for HyDE / analyze_query LLM expansion which is opt-in via `MEMORY_MODE=deep`. See [`docs/v11/benchmark.md`](docs/v11/benchmark.md) for the full table including LoCoMo and per-space embedding load characteristics.

### New MCP tools in v11.0

`memory_save_fast` · `memory_search_fast` · `memory_explain_search` · `memory_warmup` · `memory_perf_report` · `memory_rebuild_fts` · `memory_rebuild_embeddings` · `memory_eval_locomo` · `memory_eval_recall` · `memory_eval_temporal` · `memory_eval_entity_consistency` · `memory_eval_contradictions` · `memory_eval_long_context`

All previous tool names (`memory_save`, `memory_recall`, ...) continue to work unchanged.

### Multi-embedding-space contract

Every vector row now records `embedding_provider / embedding_model / embedding_dimension / embedding_space / content_type / language`. Spaces: `text` / `code` / `log` / `config`. Single Chroma backend; per-space model swap is one env flip:

```bash
MEMORY_TEXT_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
MEMORY_CODE_EMBED_MODEL=jinaai/jina-embeddings-v2-base-code   # optional
MEMORY_LOG_EMBED_MODEL=                                       # falls back to TEXT
MEMORY_CONFIG_EMBED_MODEL=                                    # falls back to TEXT
```

Old chunks stay searchable in their space; new chunks pick up the swapped model. Backfill one space at a time via `memory_rebuild_embeddings`.

> v10.x sections below are preserved as **legacy v10.5 behaviour** — still available via `MEMORY_MODE=deep`. The numbers, screenshots, and benchmark blocks dated 2026-04-19 / 2026-04-25 / 2026-04-27 (v10) describe the deep-mode pipeline. v11 replaces *defaults*, not capabilities.

---

## Table of contents

- [v11.1 — graph dedup + proactive save nudges](#v111--graph-dedup--proactive-save-nudges)
- [v11.0 — production memory engine](#v110--production-memory-engine)
- [The problem it solves](#the-problem-it-solves)
- [60-second demo](#60-second-demo)
- [Benchmarks — how it compares](#benchmarks--how-it-compares)
- [Competitor comparison](#competitor-comparison)
- [What you get](#what-you-get)
- [Architecture](#architecture)
- [Install](#install)
- [Quick start](#quick-start)
- [CLI: `lookup-memory` for sub-agents](#cli-lookup-memory-for-sub-agents)
- [MCP tools reference](#mcp-tools-reference-60-tools)
- [TypeScript SDK](#typescript-sdk)
- [Dashboard](#dashboard-localhost37737)
- [Update](#update)
- [Upgrading from v8.x to v9.0](#upgrading-from-v8x-to-v90)
- [Upgrading from v7.x to v8.0](#upgrading-from-v7x-to-v80)
- [Ollama setup](#ollama-setup-optional-but-recommended)
- [Configuration](#configuration)
- [Performance tuning](#performance-tuning)
- [Roadmap](#roadmap)
- [Support the project](#support-the-project)
- [Philosophy & license](#philosophy)

---

## The problem it solves

**AI coding agents have amnesia.** Every new Claude Code / Codex / Cursor session starts from zero. Yesterday's architectural decisions, bug fixes, stack choices, and hard-won lessons vanish the moment you close the terminal. You re-explain the same things, re-discover the same solutions, paste the same context into every new chat.

**`total-agent-memory` gives the agent a persistent brain — on your machine, not in someone else's cloud.**

Every decision, solution, error, fact, file change, and session summary is:

- **Captured** — explicitly via `memory_save` or implicitly via hooks on file edits / bash errors / session end
- **Linked** — automatically extracted into a knowledge graph (entities, relations, temporal facts)
- **Searchable** — 6-stage hybrid retrieval (BM25 + dense + graph + CrossEncoder + MMR + RRF fusion), **96.2% R@5 on public LongMemEval**
- **Private** — 100% local. SQLite + FastEmbed + optional Ollama. No data leaves your machine.

---

## 60-second demo

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

---

## Benchmarks — how it compares

**Public LongMemEval benchmark** ([xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned), 470 questions, the dataset everyone publishes against):

```
                   R@5 (recall_any) on public LongMemEval
                   ─────────────────────────────────────────
  100% ─┤
        │
  96.2% ┤  ████  ← total-agent-memory v7.0  (LOCAL, 38.8 ms, MIT)
  95.0% ┤  ████  ← Mastra "Observational"    (cloud)
        │  ████
        │  ████
  85.4% ┤  ████  ← Supermemory                (cloud, $0.01/1k tok)
        │  ████
        │  ████
        │  ████
   80%  ┤  ████
        └──────────────────────────────────────────
```

Reproducible: [`evals/longmemeval-2026-04-17.json`](evals/longmemeval-2026-04-17.json) · Runner: [`benchmarks/longmemeval_bench.py`](benchmarks/longmemeval_bench.py)

### Per-question-type breakdown (R@5 recall_any)

| Question type | Count | Our R@5 |
|---|---:|---:|
| knowledge-update | 72 | **100.0%** |
| single-session-user | 64 | **100.0%** |
| multi-session | 121 | 96.7% |
| single-session-assistant | 56 | 96.4% |
| temporal-reasoning | 127 | 95.3% ← bi-temporal KG pays off |
| single-session-preference | 30 | 80.0% ← weakest spot |
| **TOTAL** | **470** | **96.2%** |

### LoCoMo benchmark (new in v9)

**Public LoCoMo benchmark** ([snap-research/locomo](https://github.com/snap-research/locomo), 1986 QA across 10 long-running conversations, the dataset Mem0 / Memobase / Zep / MemMachine publish against):

```
              LoCoMo Acc (overall, no adversarial)
              ─────────────────────────────────────
  85% ─┤  ████  ← MemMachine        (commercial)
       │  ████
  80%  ┤  ████
       │  ████
  75%  ┤  ████  ← Memobase
       │  ████  ← Zep / Graphiti
       │  ████
  70%  ┤  ████
       │  ████
  67%  ┤  ████  ← Mem0
       │  ████
       │  ████  ← total-agent-memory v9.0  (LOCAL, MIT, gpt-4o-mini)
  60%  ┤  ████
  59%  ┤  ████  ← total-agent-memory (0.596)
       │  ████  ← LangMem (0.581)
  55%  ┤  ████
       └──────────────────────────────────────────
```

| Rank | System | Overall (no adv) | License |
|---:|---|---:|---|
| 1 | MemMachine | 0.849 | Commercial |
| 2 | Memobase | 0.758 | Apache-2.0 |
| 3 | Zep / Graphiti | 0.751 | Apache-2.0 |
| 4 | Mem0 | 0.669 | Apache-2.0 |
| **5** | **total-agent-memory v9.0** | **0.596** | **MIT** |
| 6 | LangMem | 0.581 | MIT |

**Per-category breakdown (v9.0, gpt-4o-mini gen + judge):**

| Category | N | Acc | R@5 |
|---|---:|---:|---:|
| 1 — single-hop | 282 | 0.443 | 0.514 |
| 2 — temporal | 321 | 0.564 | 0.717 |
| 3 — multi-hop | 96 | 0.490 | 0.385 |
| 4 — open-domain | 841 | 0.661 | 0.601 |
| 5 — adversarial | 446 | **0.998** ← we lead | 0.421 |
| **Overall (no adv)** | 1540 | **0.596** | 0.622 |

**We lead on adversarial (0.998 vs Memobase 0.90)** thanks to judge-weighted ensemble + abstain logic. Top-3 leaders win on cat 1/2 via subject-aware profile retrieval — that's our v10 target.

Reproducible: [`benchmarks/results/v9_diag_v1_*.json`](benchmarks/results/) · Runner: [`benchmarks/locomo_bench_llm.py`](benchmarks/locomo_bench_llm.py) (15 ablation flags). Cost on gpt-4o-mini: ~$5 for full 1986 QA run with ensemble=3.

### Latency profile

```
  p50 (warm)   ▌ 0.065 ms
  p95 (warm)   ▌▌ 2.97 ms
  LongMemEval  ▌▌▌▌▌ 38.8 ms/query   ← includes embedding + CrossEncoder rerank
  p50 (cold)   ▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌ 1333 ms  ← first query after process start
```

Warm / cold reproducible from [`evals/results-2026-04-17.json`](evals/results-2026-04-17.json).

---

## Competitor comparison

We're not replacing chatbot memory — we're occupying the **coding-agent + MCP + local** niche.

| | mem0 | Letta | Zep | Supermemory | Cognee | LangMem | **total-agent-memory** |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Funding / status | $24M YC | $10M seed | $12M seed | $2.6M seed | $7.5M seed | in LangChain | self-funded OSS |
| Runs 100% local | 🟡 | ✅ | 🟡 | ❌ | 🟡 | 🟡 | **✅** |
| MCP-native | via SDK | ❌ | 🟡 Graphiti | 🟡 | ❌ | ❌ | **✅ 60+ tools** |
| Knowledge graph | 🔒 $249/mo | ❌ | ✅ | ✅ | ✅ | ❌ | **✅** |
| **Temporal facts** (`kg_at`) | ❌ | ❌ | ✅ | ❌ | 🟡 | ❌ | **✅** |
| **Procedural memory** | ❌ | ❌ | ❌ | ❌ | ❌ | 🟡 | **✅ `workflow_predict`** |
| **Cross-project analogy** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **✅ `analogize`** |
| **Self-improving rules** | ❌ | ❌ | ❌ | ❌ | 🟡 | ❌ | **✅ `learn_error`** |
| **AST codebase ingest** | ❌ | ❌ | ❌ | ❌ | 🟡 | ❌ | **✅ tree-sitter 9 lang** |
| **Pre-edit risk warnings** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **✅ `file_context`** |
| 3D WebGL graph viewer | ❌ | ❌ | 🟡 | ✅ | ❌ | ❌ | **✅** |
| Price for graph features | $249/mo | free | cloud | usage | free | free | **free** |

Full side-by-side with pricing, latency, accuracy, "when to pick each" → [docs/vs-competitors.md](docs/vs-competitors.md).

---

## What you get

### Eight capabilities nobody else ships

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

### Plus the basics done well

- **6-stage hybrid retrieval** (BM25 + dense + fuzzy + graph + CrossEncoder + MMR, RRF fusion) — 96.2% R@5 public
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

---

## Architecture

```
                  ┌─────────────────────────────────────────────────┐
                  │             Your AI coding agent                │
                  │   (Claude Code · Codex CLI · Cursor · any MCP)  │
                  └──────────────────────┬──────────────────────────┘
                                         │ MCP (stdio or HTTP)
                                         │ 60+ tools
                  ┌──────────────────────▼──────────────────────────┐
                  │            total-agent-memory server             │
                  │    ┌──────────────┐  ┌────────────────────┐     │
                  │    │ memory_save  │  │  memory_recall      │     │
                  │    │ memory_upd   │  │  6-stage pipeline:  │     │
                  │    │ kg_add_fact  │  │  BM25  (FTS5)       │     │
                  │    │ learn_error  │  │  + dense (FastEmbed)│     │
                  │    │ file_context │  │  + fuzzy            │     │
                  │    │ workflow_*   │  │  + graph expansion  │     │
                  │    │ analogize    │  │  + CrossEncoder †   │     │
                  │    │ ingest_code  │  │  + MMR diversity †  │     │
                  │    └──────┬───────┘  │  → RRF fusion       │     │
                  │           │          └──────────┬──────────┘     │
                  └───────────┼─────────────────────┼────────────────┘
                              │                     │
                  ┌───────────▼─────────────────────▼────────────────┐
                  │                   Storage                         │
                  │  ┌────────────┐  ┌────────────┐  ┌─────────────┐ │
                  │  │  SQLite    │  │  FastEmbed │  │   Ollama    │ │
                  │  │  + FTS5    │  │  HNSW      │  │  (optional) │ │
                  │  │  + KG tbls │  │  binary-q  │  │  qwen2.5-7b │ │
                  │  └────────────┘  └────────────┘  └─────────────┘ │
                  └───────────────────────────────────────────────────┘
                              │
                              │ file-watch + debounce
                  ┌───────────▼────────────────────────────────────┐
                  │  Auto-reflection pipeline  (LaunchAgent)        │
                  │  triple_extraction → deep_enrichment → reprs   │
                  │  (async, 10s debounce, drains in background)   │
                  └─────────────────────────────────────────────────┘
                              │
                  ┌───────────▼─────────────────────────────────────┐
                  │  Dashboard (localhost:37737)                     │
                  │   /           - stats, savings, queue depths   │
                  │   /graph/live - 3D WebGL force-graph           │
                  │   /graph/hive - D3 hive plot                   │
                  │   /graph/matrix - adjacency matrix             │
                  └─────────────────────────────────────────────────┘

  † CrossEncoder + MMR are on-demand via `rerank=true` / `diverse=true`
```

---

## Install

Two paths. Same 60+ tools, same dashboard, different deployment shapes.

### IDE matrix (v10.5)

The same MCP server, same tools, same protocol — different installation
locations and hook wiring per IDE. The installer (`install.sh --ide <name>`)
automates all of it.

| IDE | Skill API | Hook API | Sub-agents | Install command |
|---|:-:|:-:|:-:|---|
| Claude Code | ✅ | ✅ full | ✅ | `./install.sh --ide claude-code` |
| Codex CLI | ✅ | ✅ | ❌ | `./install.sh --ide codex` |
| Cursor | rules-pane | ❌ | composer | `./install.sh --ide cursor` |
| Cline (VS Code) | `.clinerules/` | ❌ | ❌ | `./install.sh --ide cline` |
| Continue | rules file | ❌ | ❌ | `./install.sh --ide continue` |
| Aider | `.aider.conf.yml` read | ❌ ¹ | ❌ | `./install.sh --ide aider` |
| Windsurf | `.windsurfrules` | ❌ | cascade | `./install.sh --ide windsurf` |
| Gemini CLI | `.gemini/rules/` | ⚠️ partial | ❌ | `./install.sh --ide gemini-cli` |
| OpenCode | `.opencode/skills/` | ✅ | custom | `./install.sh --ide opencode` |

¹ Aider has no MCP yet — the bridge is via `lookup_memory.sh` /
`save_memory.sh` shell scripts.

Full per-IDE setup, manual fallbacks, and template snippets:
[`skills/memory-protocol/references/ide-setup.md`](skills/memory-protocol/references/ide-setup.md).

### Platform matrix

| OS | Command | Background services |
|---|---|---|
| macOS 10.15+ | `./install.sh --ide claude-code` | LaunchAgents (`launchctl`) |
| Linux (Ubuntu 22.04+, Debian 12+, Fedora 38+) | `./install.sh --ide claude-code` | systemd `--user` |
| WSL2 (Windows 11 + Ubuntu/Debian) | `./install.sh --ide claude-code` | systemd `--user` — requires `/etc/wsl.conf` with `[boot] systemd=true`; otherwise falls back to shell-loop autostart |
| Windows 10/11 native | `.\install.ps1 -Ide claude-code` | Task Scheduler |

Full per-platform walkthrough, WSL2 Windows-host-vs-WSL IDE nuances, the
`wsl -e` MCP-command pattern, IDE coverage matrix, and uninstall/diagnostic
flows: **[docs/installation.md](docs/installation.md)**.

### Path A — native (macOS / Linux / WSL2)

```bash
git clone https://github.com/vbcherepanov/claude-total-memory.git ~/claude-memory-server
cd ~/claude-memory-server
bash install.sh --ide claude-code   # or: cursor | gemini-cli | opencode | codex
```

The installer:

1. Clones + creates `~/claude-memory-server/.venv/`
2. Installs deps from `requirements.txt` and `requirements-dev.txt`
3. Pre-downloads the FastEmbed multilingual MiniLM model
4. Registers the MCP server via `claude mcp add-json memory ...` (stored in `~/.claude.json`, the canonical store Claude Code actually reads)
5. Copies **all hooks** (`session-*`, `user-prompt-submit.sh`, `post-tool-use.sh`, `pre-edit.sh`, `on-bash-error.sh`, etc.) into `~/.claude/hooks/` and registers them in `~/.claude/settings.json`
6. Grants `permissions.allow` for 20+ `mcp__memory__*` tools so hook-driven calls don't prompt for confirmation
7. Installs **background services** for the current OS:
   - **macOS** — 4 LaunchAgents (`reflection`, `orphan-backfill`, `check-updates`, `dashboard`) under `~/Library/LaunchAgents/`
   - **Linux / WSL2** — 7 systemd `--user` units (`*.service`, `*.timer`, `*.path`) under `~/.config/systemd/user/`; gracefully degrades if `systemd --user` is unavailable (WSL without `/etc/wsl.conf`)
8. Applies all migrations to a fresh `memory.db`
9. Starts the dashboard at `http://127.0.0.1:37737`

Restart Claude Code → `/mcp` → `memory` should show **Connected** with 60+ tools.

### Path A — native (Windows 10/11)

```powershell
git clone https://github.com/vbcherepanov/claude-total-memory.git $HOME\claude-memory-server
cd $HOME\claude-memory-server
powershell -ExecutionPolicy Bypass -File install.ps1 -Ide claude-code
```

Same 9 steps as Unix, but:

- MCP config path is `%USERPROFILE%\.claude\settings.json` (or `.cursor\mcp.json`, etc.)
- Hooks copied to `%USERPROFILE%\.claude\hooks\` — `.ps1` versions (auto-capture, memory-trigger, user-prompt-submit, post-tool-use, pre-edit, on-bash-error, session-start/end, on-stop, codex-notify)
- Background services via **Task Scheduler**:
  - `total-agent-memory-reflection` — every 5 min (no native FileSystemWatcher equivalent)
  - `total-agent-memory-orphan-backfill` — daily 00:00 + 6h repetition
  - `total-agent-memory-check-updates` — weekly Mon 09:00
  - `ClaudeTotalMemoryDashboard` — AtLogon

### Uninstall

All installers preserve `~/.claude-memory/memory.db` and your config files; only services + hook registrations are removed.

```bash
./install.sh --uninstall          # macOS/Linux/WSL2 — removes LaunchAgents OR systemd units
.\install.ps1 -Uninstall          # Windows — unregisters Scheduled Tasks + cleans settings.json
```

### Diagnose

One-shot health check — prints ✓/✗ for each subsystem (OS detect, venv, MCP import, services, dashboard HTTP, Ollama, DB migrations):

```bash
bash scripts/diagnose.sh          # macOS / Linux / WSL2
.\scripts\diagnose.ps1            # Windows
```

Exit code 0 = all green, 1 = something broken.

### Path B — Docker (everything containerized, cross-platform)

```bash
git clone https://github.com/vbcherepanov/claude-total-memory.git
cd claude-total-memory
bash install-docker.sh --with-compose
```

Brings up 5 services:

| Service | Role | Exposed |
|---|---|---|
| `mcp` | MCP server (HTTP transport) | `127.0.0.1:3737/mcp` |
| `dashboard` | Web UI | `127.0.0.1:37737` |
| `ollama` | Local LLM runtime | `127.0.0.1:11434` |
| `reflection` | File-watch queue drainer | internal |
| `scheduler` | Ofelia cron (backfill + update check) | internal |

First run pulls `qwen2.5-coder:7b` (~4.7 GB) + `nomic-embed-text` (~275 MB) — 5–10 min cold start.

**GPU note:** Docker Desktop on macOS doesn't forward Metal. Native install is faster on Mac. On Linux with NVIDIA Container Toolkit, uncomment the `deploy.resources.reservations.devices` block in `docker-compose.yml`.

### Verify (both paths)

```
memory_save(content="install works", type="fact")
memory_stats()
```

Open <http://127.0.0.1:37737/> — dashboard, knowledge graph, token savings.

---

## Quick start

> **v11 default is `MEMORY_MODE=fast`.** No LLM, no Ollama, no network in the save/search/recall hot path. To restore v10.5 synchronous-LLM behaviour set `export MEMORY_MODE=deep`. Mode switching: [`LAUNCH.md` § Tuning](LAUNCH.md#tuning-v110).

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

---

## CLI: `lookup-memory` for sub-agents

**New in v9.** Bash-friendly memory search for sub-agent workflows where launching the full MCP server would be overkill (e.g. `Bash(lookup-memory "fix slow Wave query")` from inside a Claude Code agent prompt).

Two equivalent commands ship with the package (registered as `[project.scripts]` entries — installed automatically by `./install.sh` or `./update.sh`):

```bash
lookup-memory "Caroline researched"          # human-readable bullets
ctm-lookup "Caroline researched"             # alias

lookup-memory --project myproj --limit 5 "auth flow"
lookup-memory --type solution --tag reusable "fix bug"
lookup-memory --json "claude code hooks"     # structured stdout for piping
```

**How it works:** opens the same `$CLAUDE_MEMORY_DIR/memory.db` the running MCP server uses → BM25 ranking via FTS5 → falls back to LIKE on older DBs. **Zero deps beyond the package.** No Ollama, no rag_chat.py, no ChromaDB required for the CLI path. Works on macOS, Linux, Windows.

```text
$ lookup-memory --project locomo_0 --limit 2 "adoption"
1. [synthesized_fact|locomo_0] Caroline is researching adoption agencies.
2. [synthesized_fact|locomo_0] Melanie congratulates Caroline on her adoption.
```

**Why two names?** `lookup-memory` matches the legacy bash script that older docs and sub-agent prompts reference (`~/claude-memory-server/ollama/lookup_memory.sh`). `ctm-lookup` is the project-prefixed canonical form. Both call into `claude_total_memory.lookup:main`.

**Migration note:** v7/v8 docs that pointed at `~/claude-memory-server/ollama/lookup_memory.sh` should be updated — the bash version still works for users with a manual install, but `./install.sh` / `./update.sh` clients on v9+ now get `lookup-memory` on PATH directly via the package's `[project.scripts]` entry.

---

## MCP tools reference (60+ tools)

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

**Stats / export (5):** `memory_stats`, `memory_export`, `memory_self_assess`, `memory_context_build`, `benchmark`

**Skills (3):** `memory_skill_get`, `memory_skill_update`, `file_context`

Total: **60+ tools.** Each is documented below with input schema and example.

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

`file_context` (pre-edit risk scoring) · `learn_error` (auto-consolidating error capture) · `session_init` / `session_end` · `ingest_codebase` (AST, 9 languages) · `analogize` (cross-project analogy) · `benchmark` (regression gate)

</details>

Full JSON schemas: `python -m claude_total_memory.cli tools --json` or open the dashboard at `localhost:37737/tools`.

---

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

---

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

Screenshots → [docs/screenshots/](docs/screenshots/) (coming)

---

## Update

```bash
cd ~/claude-memory-server
./update.sh
```

**7 stages:**

1. **Pre-flight** — disk check + DB snapshot (keeps last 7)
2. **Source pull** (git) or SHA-256-verified tarball
3. **Deps** — `pip install -r requirements.txt -r requirements-dev.txt` (only if hash changed)
4. **Full pytest suite** — aborts with snapshot if red
5. **Schema migrations** — `python src/tools/version_status.py`
6. **LaunchAgent reload** — reflection + backfill + update-check
7. **MCP reconnect notification** — in-app `/mcp` → `memory` → Reconnect

Manual equivalent:

```bash
cd ~/claude-memory-server
git pull
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python src/tools/version_status.py
.venv/bin/python -m pytest tests/
# in Claude Code: /mcp → memory → Reconnect
```

---

## Upgrading from v8.x to v9.0

v9 is **backward compatible**. Existing v8 calls and DB schema work unchanged — v9 is an infra release that adds pluggable backends, a public CLI for sub-agents, and LoCoMo benchmark wiring. Nothing is forcibly enabled.

### One-command upgrade

```bash
cd ~/claude-memory-server && ./update.sh
# pulls v9 src, installs new entry-points (ctm-lookup / lookup-memory),
# keeps existing memory.db untouched.
```

After upgrade, verify the new CLI is on PATH:

```bash
lookup-memory --limit 1 "any-query-from-your-history"
```

### What's new (no action required)

- **`lookup-memory` / `ctm-lookup`** CLI now installed alongside `claude-total-memory` MCP server (registered as `[project.scripts]` so `./install.sh` and `./update.sh` put them on PATH automatically). Sub-agent prompts that reference the legacy `~/claude-memory-server/ollama/lookup_memory.sh` script keep working; new prompts should prefer the package-installed name.
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
cd ~/claude-memory-server && ./update.sh
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
See [Cloud providers](#cloud-providers-optional) for OpenRouter / per-phase routing / Cohere examples.

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

Quick reference — see full docs in [MCP tools reference](#mcp-tools-reference-60-tools):

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

---

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

---

## Configuration

Environment variables (all optional):

### v11.0 — Memory mode + multi-embedding-space

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_MODE` | `fast` | `ultrafast\|fast\|balanced\|deep`. Selects hot-path profile. See [Modes](#modes). |
| `MEMORY_USE_LLM_IN_HOT_PATH` | `false` | Master switch for sync LLM stages in `save_knowledge` / `Recall.search`. `MEMORY_MODE=deep` flips this to `true`. |
| `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH` | `false` | Re-enables the silent FastEmbed → Ollama fallback ladder when FastEmbed is unavailable. |
| `MEMORY_RERANK_ENABLED` | `false` | Honour caller's `rerank=true`. When `false`, CrossEncoder rerank is hard-disabled even if a tool call requests it. |
| `MEMORY_ENRICHMENT_ENABLED` | `false` | Run the async enrichment worker. Default-ON in `balanced` / `deep`. |
| `MEMORY_TEXT_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Model for `embedding_space=text`. |
| `MEMORY_CODE_EMBED_MODEL` | _empty → falls back to TEXT model_ | Model for `embedding_space=code`. The row still records `space=code` so a future swap is config-only. |
| `MEMORY_LOG_EMBED_MODEL` | _empty → TEXT_ | Model for `embedding_space=log`. |
| `MEMORY_CONFIG_EMBED_MODEL` | _empty → TEXT_ | Model for `embedding_space=config`. |
| `MEMORY_DEFAULT_EMBEDDING_SPACE` | `text` | Space for unclassified content. |

### v10 + earlier

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_DB` | `~/.claude-memory/memory.db` | SQLite location |
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
| `MEMORY_MCP_PORT` | `3737` | HTTP MCP transport port (Docker path) |
| `MEMORY_ASYNC_ENRICHMENT` | `false` | **v10.1** — move quality gate / contradiction / entity dedup / episodic / wiki to a background worker. See [Performance tuning](#performance-tuning) |
| `MEMORY_ENRICH_TICK_SEC` | `0.1` | Worker tick interval (clamp `0.01..5`) |
| `MEMORY_ENRICH_BATCH` | `5` | Rows claimed per tick (clamp `1..50`) |
| `MEMORY_ENRICH_MAX_ATTEMPTS` | `3` | Retries before flipping a row to `failed` |
| `MEMORY_ENRICH_STALE_AFTER_SEC` | `60` | Seconds before a `processing` row is reclaimed (worker crash recovery) |

> CPU-only / WSL hosts: if Ollama keeps timing out, lower `MEMORY_TRIPLE_MAX_PREDICT` before raising timeouts. `install-codex.sh` writes conservative defaults automatically. **For 30-40s save latency on WSL2 → set `MEMORY_ASYNC_ENRICHMENT=true`** — see below.

Full config: see `claude_total_memory/config.py`.

---

## Performance tuning

### v11.0 fast-mode hot path (default)

When `MEMORY_MODE=fast` (default):

| metric              |   p50 |   p95 |   p99 |
|---------------------|------:|------:|------:|
| `save_fast`         |  6.2  |  8.9  | 11.4  |
| `save_fast` cached  |  0.3  |  0.4  |  1.4  |
| `search_fast`       |  3.4  |  4.7  |  6.0  |
| `cached_search`     |  3.1  |  3.4  |  3.6  |

`llm_calls=0`, `network_calls=0`. Reproduce: `./bin/memory-bench`. Regression gate: `./bin/memory-perf-gate`. Architecture rationale and per-stage audit: [`docs/v11/audit.md`](docs/v11/audit.md). Raw bench artifact: [`docs/v11/benchmark.md`](docs/v11/benchmark.md).

If your numbers do not match the table, run `./bin/memory-bench --warmup` first — cold FastEmbed import dominates the first call.

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
Full report: [`benchmarks/v10_5_results.md`](benchmarks/v10_5_results.md).

### Trade-off — soft drop semantic

When async is on, a `quality_gate` `drop` no longer prevents the INSERT (we already committed in the sync path). Instead the row is marked `status='quality_dropped'` after the worker scores it. `memory_recall` ignores that status (`idx_knowledge_status_quality` is added in migration 020). Audit history stays in `quality_gate_log` so nothing is lost.

If you need strict pre-INSERT gating (e.g. compliance), keep the default sync path.

### Crash recovery

Rows stuck in `processing` longer than `MEMORY_ENRICH_STALE_AFTER_SEC` (default 60 s) are flipped back to `pending` automatically — covers worker process kills mid-stage. The pre-existing `write_intents` outbox still covers a crash *before* INSERT.

---

## Roadmap

### Shipped in v11.0 (2026-04-27) — production memory engine
- ✅ **Default `MEMORY_MODE=fast`** — zero LLM, zero Ollama, zero network in save/search/recall hot path. Set `MEMORY_MODE=deep` to restore v10.5 behaviour.
- ✅ **Memory Core / AI Layer split** — `src/memory_core/*` is deterministic; `src/ai_layer/*` owns every LLM-bound code path. Enforced by `tests/test_no_llm_hot_path.py`.
- ✅ **4 modes**: `ultrafast` / `fast` / `balanced` / `deep`. Single env flag.
- ✅ **Multi-embedding-space contract** — every vector row records provider / model / dimension / space / content_type / language. Spaces: `text` / `code` / `log` / `config`. Single Chroma backend; per-space model swap is config-only.
- ✅ **Embed fallback ladder gated** — silent Ollama fallback in `Store.embed` requires `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true`.
- ✅ **New MCP tools**: `memory_save_fast`, `memory_search_fast`, `memory_explain_search`, `memory_warmup`, `memory_perf_report`, `memory_rebuild_fts`, `memory_rebuild_embeddings`, `memory_eval_locomo`, `memory_eval_recall`, `memory_eval_temporal`, `memory_eval_entity_consistency`, `memory_eval_contradictions`, `memory_eval_long_context`.
- ✅ **Migrations 021 (embedding_spaces) + 022 (embedding_cache_v11)** — idempotent on next start.
- ✅ **Benchmark suite**: `bin/memory-bench` (artifact `docs/v11/benchmark.md`) + `bin/memory-perf-gate` for CI.

### Shipped in v10.5 (2026-04-27)
- ✅ **Universal `memory-protocol` skill** — single canonical SKILL.md + 4 references (tool cheatsheet for all 60+ MCP tools, workflow recipes for 15 common situations, hooks reference, per-IDE setup) + 4 templates (Claude Code settings.json, Codex config.toml, Cursor `.mdc`, Cline `.md`). Same content for every IDE; only the wiring differs.
- ✅ **`install.sh --ide` extended to 9 IDEs**: claude-code, codex, cursor, **cline**, **continue**, **aider**, **windsurf**, gemini-cli, opencode. New helpers: `register_mcp_cline / continue / aider / windsurf` + `_json_merge_mcp_nested` for the dotted-key case (`cline.mcpServers`).
- ✅ **Cross-platform hardening** — all bash scripts pass `bash -n` under macOS bash 3.2 (default). Replaced `${var,,}` lowercase bashism in `update.sh` with `tr '[:upper:]' '[:lower:]'`. Verified with shellcheck.
- ✅ **Sub-agent memory protocol** — universal header for any sub-agent (`php-pro`, `golang-pro`, `vue-expert`, etc.) with mandatory `memory_recall` before / `memory_save` after. Full template in `skills/memory-protocol/references/subagent-protocol.md`.
- ✅ **v10.5 latency benchmark** — `benchmarks/v10_5_latency.py` with apples-to-apples sync vs async comparison. Demonstrates **80× p95 reduction** (`2150 ms → 27 ms`) when async is enabled with LLM stages on.

### Shipped in v10.1 (2026-04-27)
- ✅ **Async enrichment worker** — opt-in `MEMORY_ASYNC_ENRICHMENT=true` moves quality gate / entity dedup / contradiction detector / episodic linking / wiki refresh to a background thread. Drops max save latency 5.4× on macOS, 60–100× on WSL2. See [Performance tuning](#performance-tuning).
- ✅ **`enrichment_queue` table** with stale-processing recovery (rows stuck >60 s in `processing` flip back to `pending`).
- ✅ **Dashboard panel** for worker health: depth, throughput/min, p50/p95 ms per task, oldest pending age, recent failures.
- ✅ **`_binary_search` ValueError fix** — `np.argpartition` requires `kth STRICTLY < N`; tiny test projects (pool ≤ 50) used to silently break `contradiction_log`.
- ✅ **`coref_resolver` RU→EN translation fix** — prompt explicitly pins output language (`Do NOT translate`).

### Shipped in v10.0 (2026-04-27)
- ✅ **10 Beever-Atlas-inspired features in one push**: quality gate (Beever 6-Month Test), canonical tag vocabulary, importance boost in recall, opt-in coref resolution, contradiction auto-detection with supersede, write-intent outbox + reconciler, embedding-based entity dedup, episodic save events in the graph, smart query router (relational vs lexical), per-project Markdown wiki digest.
- ✅ 5 SQLite migrations (`015–019`) applied automatically on restart.
- ✅ 11 new env knobs, all with safe fail-open defaults.
- ✅ Tests: 971 → 1124 (+153).

### Shipped in v9.0 (2026-04-25)
- ✅ **`lookup-memory` / `ctm-lookup` CLI** — bash entry-point for sub-agents, registered as `[project.scripts]` and installed by `./install.sh` / `./update.sh` (replaces manual `~/claude-memory-server/ollama/lookup_memory.sh`)
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

### Shipped in v8.0 (2026-04-19)
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

### Planned (v8.1+)
- Plugin marketplace publish (when Claude Code API opens)
- `has_llm()` per-phase provider caching
- GitHub Actions: install smoke tests + LongMemEval nightly

### Under research
- "Endless mode" — continuous session without hard boundaries (virtual sessions by idle >N hours)
- MLX local LLM integration (A1 plan from memory #3583)
- Speculative decoding for local path (+1.5-1.8× LLM speed)

---

## Support the project

**`total-agent-memory` is, and will always be, free and MIT-licensed.** No paid tier, no gated features, no "enterprise edition". The benchmarks on this page are the entire product.

If it's saving you hours of context-pasting every week and you want to help keep development going — or just say thanks — a donation means a lot.

<p align="center">
  <a href="https://PayPal.Me/vbcherepanov">
    <img src="https://img.shields.io/badge/Donate%20via%20PayPal-00457C?style=for-the-badge&logo=paypal&logoColor=white" alt="Donate via PayPal" height="42">
  </a>
</p>

### What your support funds

| | Goal |
|---|---|
| ☕ **$5** — a coffee | One evening of focused OSS work |
| 🍕 **$25** — a pizza | A new MCP tool end-to-end (design, code, tests, docs) |
| 🎧 **$100** — a weekend | A major feature: e.g. the preference-tracking module that closes the 80% gap on LongMemEval |
| 💎 **$500+** — a sprint | A release cycle: new subsystem + migrations + docs + benchmark artifact |

### Non-monetary ways to help (equally appreciated)

- ⭐ **Star the repo** — GitHub discovery runs on this
- 🐦 **Share benchmarks on X / HN / Reddit** — reach matters more than donations
- 🐛 **Open issues** with repro cases — bug reports are pure gold
- 📝 **Write a blog post** about how you use it
- 🔧 **Submit a PR** — fixes, new tools, new integrations
- 🌍 **Translate the README** — first docs in RU / DE / JA / ZH very welcome
- 💬 **Tell your team** — peer recommendations convert 10× better than marketing

### Commercial / consulting

- Building something that would benefit from a custom integration, on-prem deployment, or team-shared memory? **Email `vbcherepanov@gmail.com`** — open to contract work and partnerships.
- AI / dev-tools company whose roadmap overlaps? Same email — happy to talk.

---

## Philosophy

**MIT forever.** No commercial-license switch, no VC money, no dark patterns. The memory layer belongs to the developers using it, not to a SaaS vendor.

**Local-first is the product.** If you want a cloud memory service, mem0 and Supermemory are great. If you want your data on your disk, untouched by anyone else — this.

**Honest benchmarks.** Every number on this page is reproducible from the artifacts in `evals/` and the scripts in `benchmarks/`. If you can't reproduce a claim, open an issue — it's a bug.

---

## Contributing

- Open an issue before a large PR — saves everyone time.
- `pytest tests/` must stay green. Add tests for new tools.
- Update `evals/scenarios/*.json` if you change retrieval behavior.
- Docs-only / typo PRs welcome without discussion.

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <b>Built for coding agents. Runs on your machine. Free forever.</b><br>
  <a href="docs/vs-competitors.md">Compare to mem0 / Letta / Zep / Supermemory</a> ·
  <a href="evals/longmemeval-2026-04-17.json">Benchmark artifact</a> ·
  <a href="https://github.com/vbcherepanov/total-agent-memory-client">TypeScript SDK</a> ·
  <a href="https://PayPal.Me/vbcherepanov">Donate</a>
</p>
