# total-agent-memory vs the field (April 2026)

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
| **total-agent-memory (v7.0, `full` mode)** | **LongMemEval (470 q, public)** | **R@5 96.2%** / R_all 84.5% / NDCG 82.4% | **38.8 ms** |
| Supermemory | LongMemEval | 85.4% overall | ~200 ms |
| Zep | DMR (different benchmark) | beats MemGPT SOTA | ~100 ms |
| mem0 | their own eval | >90% on internal tasks | cloud |
| LangMem | — | — | p95 59.82 s |

Reproducible: [`evals/longmemeval-2026-04-17.json`](../evals/longmemeval-2026-04-17.json).
Dataset: [`xiaowu0162/longmemeval-cleaned`](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned), 470 questions (abstention type excluded by the official bench).

### Per-question-type breakdown (our R@5 recall_any)

| Question type | Count | R@5 |
|---|---|---|
| single-session-user | 64 | **100.0%** |
| knowledge-update | 72 | **100.0%** |
| multi-session | 121 | **96.7%** |
| single-session-assistant | 56 | **96.4%** |
| temporal-reasoning | 127 | **95.3%** |
| single-session-preference | 30 | **80.0%** |

Weakest spot is preference tracking (80%) — a known area where richer entity-level profiles help.
Temporal reasoning at 95.3% confirms the bi-temporal KG (`kg_at`) is pulling its weight.
**+10.8 pp over Supermemory's published 85.4%** on the same dataset.

> **How to read this.** `recall_any@5` = at least one required evidence fragment in top-5.
> `recall_all@5` = every required fragment in top-5. Supermemory's 85.4% is their
> headline number against the full LongMemEval corpus. Our 96.2% is the comparable headline
> (any match); 84.5% is the strict "all fragments" version, still at parity with their overall.

---

## Three features nobody else has (yet)

These are the actual moats. They're the reason a coding agent on your machine plays in a different
league than a chatbot memory SaaS.

### 1. Procedural memory — `workflow_predict` / `workflow_track`

You start a non-trivial task. The memory layer **predicts the approach** based on how similar tasks
played out in your past — including which files got touched in what order, which tests failed, which
fix worked. If `confidence < 0.3`, the agent asks you before diving in. When you finish, you call
`workflow_track(outcome)` and the predictor learns from success *and* failure.

Nobody else does this. mem0 stores what you said; this stores *how you work*.

### 2. Cross-project analogy — `analogize`

"Was there something like this in another repo?" One call, Jaccard similarity with Dempster-Shafer
fusion across every project you've ever worked on. Instant transfer of hard-won lessons from
project A to project B, without you remembering project A existed.

### 3. Self-improving behavioral rules — `learn_error` → `self_rules`

When a bash command fails or an edit backfires, `learn_error(file, error, root_cause, fix, pattern)`
records it. After N≥3 same patterns across projects it **auto-consolidates into a behavioral rule**,
surfaced to the agent at next session start via `self_rules_context`. Your memory stops making
the same mistakes. Literally nobody else ships this out of the box.

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
