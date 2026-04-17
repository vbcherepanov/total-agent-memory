# total-agent-memory

> **The only memory layer that learns _how_ you work — not just _what_ you said.**
> Persistent, local memory for AI coding agents: Claude Code, Codex CLI, Cursor, any MCP client.
> Temporal knowledge graph · procedural memory · AST codebase ingest · cross-project analogy · 3D WebGL visualization.

[![Version](https://img.shields.io/badge/version-7.0.0-8ad.svg)]()
[![Tests](https://img.shields.io/badge/tests-501%20passing-4a9.svg)]()
[![LongMemEval R@5](https://img.shields.io/badge/LongMemEval%20R@5-96.2%25-4a9.svg)](evals/longmemeval-2026-04-17.json)
[![vs Supermemory](https://img.shields.io/badge/vs%20Supermemory-%2B10.8pp-4a9.svg)](docs/vs-competitors.md)
[![p50 latency](https://img.shields.io/badge/p50%20warm-0.065ms-4a9.svg)](evals/results-2026-04-17.json)
[![Local-First](https://img.shields.io/badge/100%25-local-4a9.svg)]()
[![License](https://img.shields.io/badge/license-MIT-fa4.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-blue.svg)](https://modelcontextprotocol.io)
[![npm](https://img.shields.io/badge/npm-%40vbch%2Ftotal--agent--memory--client-cb3837.svg)](https://www.npmjs.com/package/@vbch/total-agent-memory-client)
[![Donate](https://img.shields.io/badge/PayPal-Donate-00457C.svg?logo=paypal&logoColor=white)](https://www.paypal.com/donate/?business=vbcherepanov%40gmail.com&currency_code=USD&item_name=total-agent-memory)

**Why this, not mem0 / Letta / Zep / Supermemory / Cognee?** → [docs/vs-competitors.md](docs/vs-competitors.md)

---

## Table of contents

- [The problem it solves](#the-problem-it-solves)
- [60-second demo](#60-second-demo)
- [Benchmarks — how it compares](#benchmarks--how-it-compares)
- [Competitor comparison](#competitor-comparison)
- [What you get](#what-you-get)
- [Architecture](#architecture)
- [Install](#install)
- [Quick start](#quick-start)
- [MCP tools reference](#mcp-tools-reference-46-tools)
- [TypeScript SDK](#typescript-sdk)
- [Dashboard](#dashboard-localhost37737)
- [Update](#update)
- [Ollama setup](#ollama-setup-optional-but-recommended)
- [Configuration](#configuration)
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
| MCP-native | via SDK | ❌ | 🟡 Graphiti | 🟡 | ❌ | ❌ | **✅ 46 tools** |
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

### Five capabilities nobody else ships

| Capability | Tool | One-liner |
|---|---|---|
| 🧠 **Procedural memory** | `workflow_predict` / `workflow_track` | "How did I solve this last time?" — predicts steps with confidence |
| 🔗 **Cross-project analogy** | `analogize` | "Was there something like this in another repo?" — Jaccard + Dempster-Shafer |
| ⚠️ **Pre-edit risk warnings** | `file_context` | Surfaces past errors / hot spots on the file you're about to edit |
| 🛡 **Self-improving rules** | `learn_error` + `self_rules_context` | Bash failures → patterns → auto-consolidated behavioral rules at N≥3 |
| 🕰 **Temporal facts** | `kg_add_fact` / `kg_at` | Append-only KG with `valid_from`/`valid_to` — query what was true at any point |

### Plus the basics done well

- **6-stage hybrid retrieval** (BM25 + dense + fuzzy + graph + CrossEncoder + MMR, RRF fusion) — 96.2% R@5 public
- **Multi-representation embeddings** — each record embedded as raw + summary + keywords + questions + compressed
- **AST codebase ingest** — tree-sitter across 9 languages (Python, TS/JS, Go, Rust, Java, C/C++, Ruby, C#)
- **Auto-reflection pipeline** — `memory_save` → LaunchAgent file-watch → graph edges appear ~30 s later
- **rtk-style content filters** — strip noise from pytest / cargo / git / docker logs while preserving URLs, paths, code
- **3D WebGL knowledge graph viewer** — 3,500+ nodes, 120,000+ edges, click-to-focus, filters
- **Hive plot & adjacency matrix** — alternate graph views sorted by node type
- **A2A protocol** — memory shared between multiple agents (backend + frontend + mobile in a team)

---

## Architecture

```
                  ┌─────────────────────────────────────────────────┐
                  │             Your AI coding agent                │
                  │   (Claude Code · Codex CLI · Cursor · any MCP)  │
                  └──────────────────────┬──────────────────────────┘
                                         │ MCP (stdio or HTTP)
                                         │ 46 tools
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

Two paths. Same 46 tools, same dashboard, different deployment shapes.

### Path A — native (simplest on macOS/Linux, uses host GPU via Ollama)

```bash
git clone https://github.com/vbcherepanov/claude-total-memory.git ~/claude-memory-server
cd ~/claude-memory-server
bash install.sh
```

The installer:

1. Clones + creates `~/claude-memory-server/.venv/`
2. Installs deps from `requirements.txt` and `requirements-dev.txt`
3. Pre-downloads the FastEmbed multilingual MiniLM model
4. Registers the MCP server via `claude mcp add-json memory ...` (stored in `~/.claude.json`, the canonical store Claude Code actually reads)
5. Registers **all hooks** in `~/.claude/settings.json` — including v7.0 `pre-edit.sh` and `on-bash-error.sh`
6. Grants `permissions.allow` for 20+ `mcp__memory__*` tools so hook-driven calls don't prompt for confirmation
7. On **Linux**: installs `systemd.path` + oneshot `.service` under `~/.config/systemd/user/` for autodrain of reflection queues (macOS uses LaunchAgent for the same job)
8. Applies all migrations to a fresh `memory.db`
9. Starts the dashboard at `http://127.0.0.1:37737`

Restart Claude Code → `/mcp` → `memory` should show **Connected** with 46 tools.

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

## MCP tools reference (46 tools)

<details>
<summary><b>Core memory (14)</b></summary>

`memory_recall` · `memory_save` · `memory_update` · `memory_delete` · `memory_search_by_tag` · `memory_history` · `memory_timeline` · `memory_stats` · `memory_consolidate` · `memory_export` · `memory_forget` · `memory_relate` · `memory_extract_session` · `memory_observe`

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

## Ollama setup (optional but recommended)

**Without Ollama:** works fully — raw content is saved, retrieval via BM25 + FastEmbed dense embeddings.

**With Ollama:** you also get LLM-generated summaries, keywords, question-forms, compressed representations, and deep enrichment (entities, intent, topics).

```bash
brew install ollama     # or: curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5-coder:7b        # default — best quality/speed on M-series
ollama pull nomic-embed-text        # optional, alternative embedder
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

> CPU-only / WSL hosts: if Ollama keeps timing out, lower `MEMORY_TRIPLE_MAX_PREDICT` before raising timeouts. `install-codex.sh` writes conservative defaults automatically.

Full config: see `claude_total_memory/config.py`.

---

## Roadmap

### Shipping soon (v7.1)
- `/api/mcp/call` HTTP gateway endpoint so `connectHttp()` works end-to-end for team / serverless setups
- Published `benchmark(dataset="longmemeval")` for end-users to reproduce on their own machines
- `preference` module — closing the 80% gap on LongMemEval preference tracking

### Planned (v7.2+)
- Postgres + pgvector backend (opt-in) for shared team memory
- Multi-tenant mode with row-level security
- gRPC transport for lowest-latency in-cluster calls
- Rust-powered tree-sitter AST indexer (currently Python bindings)
- Browser extension for auto-capture from ChatGPT / Claude web

### Under research
- Episodic ↔ semantic consolidation inspired by hippocampal replay
- Dreamed-counterfactual planning on the procedural memory index
- Cross-user federation (you opt-in to sharing patterns, not data)

---

## Support the project

**`total-agent-memory` is, and will always be, free and MIT-licensed.** No paid tier, no gated features, no "enterprise edition". The benchmarks on this page are the entire product.

If it's saving you hours of context-pasting every week and you want to help keep development going — or just say thanks — a donation means a lot.

<p align="center">
  <a href="https://www.paypal.com/donate/?business=vbcherepanov%40gmail.com&currency_code=USD&item_name=total-agent-memory">
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
  <a href="https://www.paypal.com/donate/?business=vbcherepanov%40gmail.com&currency_code=USD&item_name=total-agent-memory">Donate</a>
</p>
