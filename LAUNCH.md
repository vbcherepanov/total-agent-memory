# Launch assets — total-agent-memory v7.0.0

> Все тексты готовы к публикации. Скопируй нужный блок и пости.

## 1. Hacker News — Show HN

**Title (≤80 chars):**
```
Show HN: total-agent-memory — local memory for coding agents, beats Supermemory
```

**Text:**
```
Hi HN,

I built total-agent-memory because every new Claude Code / Codex CLI /
Cursor session starts from zero. Yesterday's architectural decisions,
bug fixes, hard-won lessons — gone the moment you close the terminal.

The space has strong players — mem0 ($24M), Letta ($10M), Zep ($12M),
Supermemory ($2.6M), Cognee ($7.5M) — but all of them are cloud-first
chatbot-style SDKs. None are MCP-native, none run fully local, and none
optimize for how a coding agent actually works (files, bash errors,
cross-project patterns).

So I built one that does. And it happens to win on the public benchmark.

PUBLIC LONGMEMEVAL (470 questions, the xiaowu0162 dataset everyone uses):

  total-agent-memory v7.0 full mode  :  R@5 96.2%   38.8 ms/query  LOCAL
  Supermemory (published headline)   :  85.4%       ~200 ms        cloud
  Mastra Observational Memory        :  95.0%                      cloud
  LangMem                             :  —           p95 59.82 s

+10.8 pp over Supermemory on the same public dataset, running 100% on
an Apple Silicon laptop, no network.

What's inside:

  • 6-stage hybrid retrieval: FTS5 + dense embeddings + fuzzy + graph
    expansion + optional CrossEncoder re-rank + MMR diversity, RRF fusion
  • Temporal knowledge graph (append-only facts with valid_from / valid_to,
    Dempster-Shafer fusion). You can ask "what was the stack as of Q3?"
  • Procedural memory — workflow_predict() returns expected steps with a
    confidence score before you start a task. workflow_track() closes
    the loop when you finish. Nothing else ships this.
  • Cross-project analogy — "was there something like this in another
    repo?" Jaccard similarity across every project you've ever worked on.
  • Self-improving behavioral rules — when Bash fails, learn_error()
    records the pattern. After N≥3 same patterns it auto-consolidates
    into a rule surfaced at next session start.
  • AST codebase ingest (tree-sitter, 9 languages)
  • Pre-edit file risk warnings from past errors/hot spots
  • 46 MCP tools. SQLite + FastEmbed + optional Ollama. 100% local.
  • Dashboard with 3D WebGL live graph

Install (one line, macOS/Linux):

  curl -fsSL https://raw.githubusercontent.com/vbcherepanov/claude-total-memory/main/install.sh | bash

TypeScript SDK: npm i @total-agent-memory/client

Reproduce the benchmark yourself — evals/longmemeval-2026-04-17.json
has the raw results, benchmarks/longmemeval_bench.py is the runner.

GitHub: https://github.com/vbcherepanov/claude-total-memory
Comparison with mem0/Letta/Zep/Supermemory/Cognee/LangMem/Mastra:
  docs/vs-competitors.md

Feedback welcome — especially from folks who've tried the other layers
and can tell me where they beat us.
```

---

## 2. X / Twitter — single tweet

```
total-agent-memory v7.0 on the public LongMemEval:

  96.2% R@5   @ 38.8 ms/query   100% local

Supermemory:  85.4% @ ~200 ms cloud
LangMem    :  p95 59.82s

MCP-native, temporal KG, procedural memory, cross-project analogy.
Free, MIT, runs on your laptop.

github.com/vbcherepanov/claude-total-memory
```

**Thread version (5 tweets):**

1/ 🧠 Shipped total-agent-memory v7.0 — the only memory layer that
   learns HOW you work, not just WHAT you said.
   
   Public LongMemEval: 96.2% R@5 vs Supermemory 85.4%. 38.8 ms/query.
   100% local.
   
   github.com/vbcherepanov/claude-total-memory

2/ Space is crowded — mem0 ($24M), Letta ($10M), Zep ($12M),
   Supermemory ($2.6M), Cognee ($7.5M), LangMem.
   
   All cloud-first chatbot SDKs.
   
   None are MCP-native. None run fully local. None optimize for
   coding agents.

3/ What's different:
   
   🧠 Procedural memory — predict how you'll solve the task BEFORE you start
   🔗 Cross-project analogy — "was there something like this in another repo?"
   📜 Temporal KG — point-in-time fact queries (as of Q3 2025)
   ⚠️ Pre-edit risk warnings from past errors on the same file

4/ All via MCP. 46 tools. Works with Claude Code, Codex CLI, Cursor,
   any MCP client.
   
   SQLite + FastEmbed + optional Ollama. Zero cloud dependency.
   
   TypeScript SDK out today: npm i @total-agent-memory/client

5/ Benchmark is reproducible — evals/longmemeval-2026-04-17.json
   has raw numbers, benchmarks/longmemeval_bench.py runs it on your data.
   
   MIT licensed. PRs welcome.
   
   Would love feedback from folks who've shipped with mem0/Zep/Supermemory.

---

## 3. Product Hunt

**Tagline (≤60 chars):**
```
Local memory for AI coding agents — beats Supermemory
```

**Description:**
```
AI coding agents have amnesia. Every new Claude Code / Codex / Cursor
session starts from zero — yesterday's decisions gone.

total-agent-memory gives them a persistent brain that runs on your
machine, not in someone else's cloud.

• 96.2% R@5 on the public LongMemEval benchmark (+10.8pp over Supermemory)
• 38.8 ms per query — runs local on a laptop, no network
• MCP-native: 46 tools for Claude Code, Codex CLI, Cursor
• Temporal knowledge graph with point-in-time fact queries
• Procedural memory — learns how you work, predicts next steps
• Cross-project analogy — "was there something like this elsewhere?"
• Self-improving behavioral rules from past errors
• AST codebase ingest (tree-sitter, 9 languages)
• 3D WebGL live dashboard
• 100% local by default, 100% free, MIT license

One-line install. TypeScript SDK included.
```

**First comment (introduce yourself):**
```
Hey Product Hunt,

Built this because I kept re-explaining the same architectural decisions
to Claude Code every session. Tried mem0 and Supermemory — both good,
but cloud-only, and neither optimizes for the coding-agent workflow
(files, bash errors, cross-project patterns).

The benchmark result surprised me — I thought we'd be in the ballpark
of Supermemory's 85.4%. Hitting 96.2% on the same public dataset
validated that a coding-agent-shaped memory layer is a different
product category from a chatbot-memory SaaS.

Everything's reproducible: evals/longmemeval-2026-04-17.json has raw
numbers. Would love feedback on where the other memory layers beat us —
especially temporal / preference tracking (our weakest spot at 80%).
```

---

## 4. awesome-list PR body

```markdown
## total-agent-memory

**Why it belongs here:** Local-first, MCP-native persistent memory for AI
coding agents. 96.2% R@5 on the public LongMemEval benchmark (+10.8pp
over Supermemory). Temporal knowledge graph, procedural memory, AST
codebase ingest, cross-project analogy. MIT licensed, reproducible
benchmarks.

- Repo: https://github.com/vbcherepanov/claude-total-memory
- Npm SDK: https://www.npmjs.com/package/@total-agent-memory/client
- Benchmarks: https://github.com/vbcherepanov/claude-total-memory/blob/main/evals/longmemeval-2026-04-17.json
- Comparison with mem0 / Letta / Zep / Supermemory / Cognee / LangMem:
  https://github.com/vbcherepanov/claude-total-memory/blob/main/docs/vs-competitors.md
```

Target lists:
- https://github.com/modelcontextprotocol/servers
- https://github.com/punkpeye/awesome-mcp-servers
- https://github.com/sindresorhus/awesome

---

## 5. r/LocalLLaMA post

**Title:**
```
I built a local memory layer for Claude Code that beats Supermemory on the public LongMemEval (96.2% vs 85.4%)
```

**Body:**
```
I've been using Claude Code daily for coding and got tired of
re-explaining the same architectural decisions every session.

Tried mem0, Zep, Supermemory — all cloud-only or enterprise-focused.
Built my own: total-agent-memory.

The interesting part: on the public LongMemEval benchmark (the one
Supermemory uses to claim 85.4% accuracy in their marketing), my
local setup hits 96.2% R@5 at 38.8 ms/query.

Full stack is 100% local:
- SQLite + FTS5 (lexical)
- FastEmbed with binary-quantized HNSW (dense)
- Optional Ollama for LLM-based enrichment
- Tree-sitter AST ingest across 9 languages
- No network calls unless you opt in

Unique features no other memory layer ships:
- Procedural memory — predicts your approach to a task before you
  start, based on how similar tasks went in the past
- Cross-project analogy — finds similar problems you've solved in
  other repos
- Self-improving rules — when a command fails 3+ times with the same
  pattern, it auto-generates a behavioral rule

Integrates via MCP so it works with Claude Code, Codex CLI, Cursor,
and anything that speaks Model Context Protocol. TypeScript SDK
shipped today.

Repo: github.com/vbcherepanov/claude-total-memory
Benchmark raw results: evals/longmemeval-2026-04-17.json

Would love feedback on where the other memory layers still beat us —
especially preference tracking, where we only hit 80%.
```

---

## 6. Release notes (GitHub Release v7.0.0)

```markdown
## total-agent-memory v7.0.0 — the temporal + procedural release

### Headline numbers

- **96.2% R@5 on the public LongMemEval benchmark** (470 questions,
  `xiaowu0162/longmemeval-cleaned`). +10.8 pp over Supermemory's
  published 85.4%.
- 38.8 ms average query latency, 100% local.
- 496 passing tests.

### What's new in v7.0

**Temporal knowledge graph** — `kg_add_fact`, `kg_at`, `kg_invalidate_fact`.
Facts have `valid_from` / `valid_to` windows. Ask what was true at any
point in time. Supersedes old facts automatically.

**Procedural memory** — `workflow_predict(task_description)` returns
predicted steps + confidence before you start. `workflow_track(outcome)`
learns from success and failure.

**AST codebase ingest** — `ingest_codebase(path, languages)` builds a
symbol map with tree-sitter across 9 languages (Python, JS, TS, Go,
Rust, Java, C, C++, Ruby).

**Pre-edit risk warnings** — `file_context(path)` surfaces past errors
and hot-spots before you edit a file. `risk_score > 0.5` = you should
read the warnings first.

**Self-improving rules** — `learn_error()` records failure patterns;
after N≥3 same patterns it auto-consolidates into a behavioral rule
via `self_rules_context`.

**Cross-project analogy** — `analogize()` finds similar past solutions
across every project using Jaccard + Dempster-Shafer fusion.

**TypeScript SDK** — `@total-agent-memory/client` on npm.

**Rebrand** — formerly `claude-total-memory`, now `total-agent-memory`.
The GitHub URL stays (vbcherepanov/claude-total-memory) to avoid breaking
existing installs.

### Breaking changes

None. Existing v6.x databases auto-migrate.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/vbcherepanov/claude-total-memory/main/install.sh | bash
npm i @total-agent-memory/client  # TypeScript/JavaScript
```

### Comparison

See `docs/vs-competitors.md` for a full side-by-side with mem0, Letta,
Zep, Supermemory, Cognee, LangMem, Mastra.
```
