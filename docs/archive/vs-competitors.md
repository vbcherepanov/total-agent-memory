# total-agent-memory vs the field (April 2026)

> **Snapshot, not maintained.** This page was written on 2026-04-17. Funding, star counts,
> prices and feature marks for other projects are the author's reading of their public pages at that
> time and have not been re-checked; tool counts for this project are out of date (77 tools as of
> 14.6.0). For measured comparisons, see [benchmarks.md](../benchmarks.md#comparison-with-other-systems).

The agent-memory space grew up fast in 2025–2026. This page is an honest side-by-side with
the money, features, and real numbers — so you can pick the right tool for your job.

> **TL;DR.** If you build a **chatbot** or a **second-brain note app**, pick mem0 or Supermemory.
> If you need a **stateful agent runtime**, pick Letta. If you want **enterprise temporal KG**, pick Zep.
> If you run a **coding agent** (Claude Code, Codex CLI, Cursor) and you want a **local, MCP-native brain
> that learns how you work** — this project is for you.

---

## The competitive field

| Project | Funding (2026) | GitHub | Positioning |
|---|---|---|---|
| **[mem0](https://mem0.ai)** | $24M Series A (YC, AWS exclusive memory for Agent SDK) | ~48k⭐ | Universal memory SDK for LLM apps |
| **[Letta](https://letta.com)** (ex-MemGPT) | $10M seed | ~17k⭐ | Stateful agent platform (LLM-as-OS, memory blocks) |
| **[Zep / Graphiti](https://getzep.com)** | $12M seed | ~3.5k⭐ (Zep) + ~6k⭐ (Graphiti) | Enterprise temporal KG memory |
| **[Supermemory](https://supermemory.ai)** | $2.6M seed | ~15k⭐ | Consumer second-brain + SaaS Memory API |
| **[Cognee](https://cognee.ai)** | $7.5M seed (OpenAI + FAIR founder checks) | ~7k⭐ | Open-source AI memory engine, GraphRAG |
| **[LangMem](https://github.com/langchain-ai/langmem)** | part of LangChain | — | Long-term memory primitives for LangGraph |
| **[Claude Memory](https://claude.ai)** (Anthropic) | built-in, GA March 2026 | — | In-product memory for Claude consumer tiers |
| **[ChatGPT Memory](https://chat.openai.com)** (OpenAI) | built-in, since 2024 | — | In-product memory for ChatGPT consumer |
| **total-agent-memory** | self-funded | this repo | **Local-first memory for coding agents via MCP** |

---

## Feature matrix — what each one actually has

Legend: ✅ native · 🟡 partial/addon · ❌ not available · 🔒 gated behind paid plan

| Feature | mem0 | Letta | Zep | Supermemory | Cognee | LangMem | Claude Memory | **total-agent-memory** |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **Open-source core** | ✅ | ✅ | 🟡 | 🟡 | ✅ | ✅ | ❌ | ✅ |
| **Runs 100% local** (no network required) | 🟡 | ✅ | 🟡 | ❌ | 🟡 | 🟡 | ❌ | ✅ |
| **MCP server native** (Claude Code, Codex, Cursor) | via SDK | ❌ | 🟡 (Graphiti MCP) | 🟡 | ❌ | ❌ | — | ✅ (46 tools) |
| **Semantic search** (vectors) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Hybrid search** (BM25 + dense + graph + RRF) | 🟡 | ❌ | ✅ | ✅ | ✅ | ❌ | — | ✅ 6-tier |
| **Knowledge graph** | 🔒 Pro $249/mo | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ |
| **Temporal facts** (`valid_from`/`valid_to`, point-in-time queries) | ❌ | ❌ | ✅ bi-temporal | ❌ | 🟡 | ❌ | ❌ | ✅ |
| **Procedural memory** (learns workflows, predicts next task) | 🟡 (API only) | ❌ | ❌ | ❌ | ❌ | 🟡 | ❌ | ✅ `workflow_predict` |
| **Self-improving rules** (errors → patterns → auto-rules) | ❌ | ❌ | ❌ | ❌ | 🟡 memify | ❌ | ❌ | ✅ |
| **Cross-project analogy** (found-similar-in-another-repo) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ `analogize` |
| **AST codebase ingest** (tree-sitter, 9 langs) | ❌ | ❌ | ❌ | ❌ | 🟡 docs | ❌ | 🟡 | ✅ `ingest_codebase` |
| **Pre-edit risk warnings** (read file hot-spots before Edit) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ `file_context` |
| **Post-bash-error learning** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ `learn_error` |
| **Multi-agent A2A protocol** | ❌ | 🟡 | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| **3D WebGL live graph dashboard** | ❌ | ❌ | 🟡 | ✅ | ❌ | ❌ | ❌ | ✅ |
| **Privacy**: your data never leaves your machine | 🟡 | ✅ | 🟡 | ❌ | 🟡 | 🟡 | ❌ | ✅ |

---

## Price — what you pay to get the good parts

| Project | Free tier | Paid | What's gated |
|---|---|---|---|
| mem0 | Vector search only | **$19/mo Standard**, **$249/mo Pro** | Graph, multi-hop queries, structured knowledge |
| Letta | OSS full-feature | Managed cloud (custom) | Hosting, SSO |
| Zep | OSS Community | Zep Cloud (from ~$99/mo seat) | Temporal KG, managed Neo4j, support |
| Supermemory | 1M tokens/mo, 10k queries | **$0.01 / 1k tokens** usage-based | Tokens beyond free tier |
| Cognee | OSS full-feature | Managed Cloud (beta) | Hosting |
| LangMem | OSS | LangSmith / LangGraph Platform | Hosting, observability |
| Claude Memory | free tier included | Pro / Max plan quotas | Larger memory budget |
| ChatGPT Memory | free with ChatGPT | Plus / Team / Enterprise | Larger memory budget |
| **total-agent-memory** | **full, forever** | — | — (BYO Ollama for LLM enrichment) |

---

## Speed — p50/p95 you can reproduce

| Project | p50 | p95 | Source |
|---|---|---|---|
| Supermemory | ~200 ms | <300 ms | [supermemory blog](https://blog.supermemory.ai/) |
| Zep | ~100 ms | — | Zep paper (arxiv 2501.13956) |
| LangMem | — | **59.82 s** | third-party (atlan.com) |
| **total-agent-memory (cold)** | 1332 ms | 2314 ms | `evals/results-2026-04-17.json` |
| **total-agent-memory (warm)** | **0.065 ms** | **2.97 ms** | `evals/results-2026-04-17.json` |

Cold = first invocation after process start (model load + index warmup).
Warm = steady-state with HNSW in memory, FTS5 cached, LRU query cache hit.

Run it yourself on your data:

```bash
python -m claude_total_memory.cli benchmark
# or via MCP:  benchmark()
```

---

## Accuracy — retrieval quality on the public LongMemEval benchmark

| Project | Benchmark | Score | Avg latency |
|---|---|---|---|
| Mastra "Observational Memory" | LongMemEval | 95.0% | cloud |
| **total-agent-memory (v13.0.0, `store` mode)** | **LongMemEval (470 q, public)** | **R@5 95.1%** / R_all 85.7% / NDCG 88.9% | **27.6 ms** |
| Supermemory | LongMemEval | 85.4% overall | ~200 ms |
| Zep | DMR (different benchmark) | beats MemGPT SOTA | ~100 ms |
| mem0 | LoCoMo / LongMemEval / BEAM | 92.5 / 94.4 / 62.0 (BEAM-1M) — end-to-end accuracy, their generator + judge | ≤1.1 s p50 |
| LangMem | — | — | p95 59.82 s |

Reproducible: [`evals/longmemeval-2026-04-17.json`](../../evals/longmemeval-2026-04-17.json).
Newer, product-path retrieval numbers on LoCoMo and BEAM:
[`benchmarks/results/v13-locomo-retrieval.json`](../../benchmarks/results/v13-locomo-retrieval.json),
[`v13-beam-100K.json`](../../benchmarks/results/v13-beam-100K.json).
Dataset: [`xiaowu0162/longmemeval-cleaned`](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned), 470 questions (abstention type excluded by the official bench).

### Per-question-type breakdown (our R@5 recall_any)

| Question type | Count | R@5 |
|---|---|---|
| knowledge-update | 72 | **100.0%** |
| multi-session | 121 | **98.3%** |
| single-session-user | 64 | **95.3%** |
| single-session-assistant | 56 | **94.6%** |
| temporal-reasoning | 127 | **92.9%** |
| single-session-preference | 30 | **80.0%** |

Weakest spot is preference tracking (80%) — and BEAM's `preference_following`
(0.28 R@5) says the same thing from another angle: preferences get stated once,
in passing, and never restated.
These are historical retrieval measurements, not answer accuracy. They do not
establish superiority over another product or isolate the contribution of the
temporal graph. That requires a shared end-to-end protocol and ablation runs.

`recall_any@5` means at least one required evidence fragment appears in the top five;
`recall_all@5` requires every fragment. Neither can be subtracted from a competitor's
answer accuracy. The former +9.7 percentage-point comparison was invalid and has
been withdrawn. Version 14 has not been evaluated on this corpus yet.

> **Two caveats we would rather state than have you find.**
>
> 1. **These are different metrics.** Everything in our column is *retrieval* —
>    did the right passage come back in the top-5. mem0's 92.5 / 94.4 are
>    *end-to-end accuracy*: a generator answers and an LLM judges. A retrieval
>    number and an accuracy number are not comparable, in either direction. We
>    lead on retrieval being reproducible: the runner, the corpus and the gold
>    labels are public, and you can re-run everything without an API key.
> 2. **This number changed in v13, downward, on purpose.** Until v13 the
>    LongMemEval runner used its own self-contained BM25 / RRF / MMR /
>    CrossEncoder stack, so the 96.2% we used to publish described *an
>    algorithm* rather than this software. `--modes store` drives the real
>    `Recall.search` path and is now the default: 95.1%. We would rather
>    publish the smaller number that is about the product.

---

## Three features we did not find in the systems above (April 2026)

These target coding agents rather than chat assistants.

### 1. Procedural memory — `workflow_predict` / `workflow_track`

You start a non-trivial task. The memory layer **predicts the approach** based on how similar tasks
played out in your past — including which files got touched in what order, which tests failed, which
fix worked. If `confidence < 0.3`, the agent asks you before diving in. When you finish, you call
`workflow_track(outcome)` and the predictor learns from success *and* failure.

mem0 stores what you said; this stores *how you work*.

### 2. Cross-project analogy — `analogize`

"Was there something like this in another repo?" One call, Jaccard similarity with Dempster-Shafer
fusion across every project you've ever worked on. Instant transfer of hard-won lessons from
project A to project B, without you remembering project A existed.

### 3. Self-improving behavioral rules — `learn_error` → `self_rules`

When a bash command fails or an edit backfires, `learn_error(file, error, root_cause, fix, pattern)`
records it. After N≥3 same patterns across projects it **auto-consolidates into a behavioral rule**,
surfaced to the agent at next session start via `self_rules_context`. Your memory stops making
the same mistakes.

---

## When to pick each one

- **mem0** — You're building a chatbot for customers and want a battle-tested SDK with enterprise
  support. AWS Agent SDK integration is a huge plus if you live in that ecosystem.
- **Letta** — You want to build a stateful agent from scratch and you're ok buying into their runtime.
  Their memory blocks + Sonnet 4.5 self-editing loop is genuinely cool.
- **Zep / Graphiti** — You're shipping an enterprise assistant where temporal correctness of facts
  matters (healthcare, finance, compliance). The bi-temporal model is the best in the field.
- **Supermemory** — You want a personal second brain with a beautiful UI across devices. B2C wins.
- **Cognee** — You want a research-grade open-source GraphRAG stack and you're willing to integrate it
  yourself with your agent.
- **LangMem** — You already use LangGraph and want long-term memory primitives that slot in naturally.
- **Claude Memory / ChatGPT Memory** — You're a consumer user and just want the chat to remember you.
- **total-agent-memory** — You are a **developer** using **Claude Code / Codex CLI / Cursor / any MCP
  agent**, and you want: (a) 100% local/private, (b) MCP-native, (c) persistent across sessions and
  projects, (d) memory that actually learns your workflow, not just stores your conversation.

---

_Last updated: 2026-04-17. Benchmarks reproducible via `evals/results-2026-04-17.json` and
`python -m claude_total_memory.cli benchmark`. PRs with corrections welcome — if we got your
project wrong, open an issue._
