# Claude Total Memory v6.0

> **Persistent, cross-session memory for Claude Code** — knowledge graph + multi-representation embeddings + auto-reflection + WebGL graph visualization.

[![Tests](https://img.shields.io/badge/tests-370%20passing-4a9.svg)]()
[![Version](https://img.shields.io/badge/version-6.0.0-8ad.svg)]()
[![License](https://img.shields.io/badge/license-MIT-fa4.svg)](LICENSE)

---

## Table of contents

- [What's new in v6.0](#whats-new-in-v60)
- [Quick install (from scratch)](#quick-install-from-scratch)
- [Upgrade from v5 / v4 / v3](#upgrade-from-v5--v4--v3)
- [Architecture at a glance](#architecture-at-a-glance)
- [Search pipeline](#search-pipeline)
- [Graph visualizations](#graph-visualizations)
- [Async pipelines](#async-pipelines)
- [Auto-update](#auto-update)
- [Benchmarks](#benchmarks)
- [Configuration](#configuration)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)

---

## What's new in v6.0

### Knowledge graph & embeddings

- **Auto-extracted triples** — Ollama deep extraction runs in async queue after every `memory_save`, builds `(subject, predicate, object)` edges in `graph_edges`
- **Multi-representation embeddings** (GEM-RAG style) — every record embedded as `raw + summary + keywords + questions + compressed`. Search hits any view, results fused via RRF
- **Semantic fact merger** — finds clusters of related (not duplicate) records, asks LLM to consolidate them. ContentValidator blocks lossy merges
- **Context expansion** — `memory_recall(expand_context=true)` adds 1-hop graph neighbors of search results
- **Deep enrichment** — auto-extract `entities + intent + topics` per record. Filter searches by `topics=[...] / entities=[...] / intent=...`

### Compression

- **rtk-style TOML content filters** — 11 builtin (`pytest, cargo, git_status, docker_ps, npm_yarn, http_log, sql_explain, json_blob, stack_trace, markdown_doc, generic_logs`)
- **Autofilter detection** — sniffer recognizes content type, applies the right filter without explicit param
- **ContentValidator safety net** — code blocks byte-for-byte, URLs, paths, headings preserved across any LLM transformation
- **5th `compressed` representation** for long content with validator guard

### Graph visualization

- **3 views** with shared tab navigation:
  - `/graph/live` — 3D WebGL force-directed (3d-force-graph + Three.js)
  - `/graph/hive` — D3 hive plot, nodes on radial axes by type
  - `/graph/matrix` — Canvas adjacency matrix sorted by type
- Importance/edge-weight sliders, hide-orphans, type filter, search, click-to-focus, ESC back

### Operations

- **Auto-reflection on save** — file-watch trigger via LaunchAgent. Save → 5s debounce → drain queues. Edges appear in graph within ~30s
- **Orphan backfill** — LaunchAgent runs 4×/day at 00/06/12/18, finds nodes with zero edges, enqueues them for Ollama re-extraction
- **Auto-update** — `update.sh` with 7 stages, DB snapshot rotation, hash-checked deps, pytest gate, services reload
- **Settings + Ollama detection** — single `has_llm()` gate, all LLM-using code degrades gracefully when Ollama unavailable
- **Auto-migrations** — schema upgrades apply idempotently on every Store init

### Performance & security

- **7 new perf indexes** — dashboard delta queries 300ms → 3ms
- **Drain scope** — small reflection bursts skip digest/synthesize → 30s vs 3min
- **`busy_timeout=5000`** + 20MB cache_size in SQLite — kills BUSY errors under contention
- **Dashboard binds 127.0.0.1** by default (was 0.0.0.0)
- **`UPDATE_URL` requires HTTPS + SHA-256 pin** + `tar --no-same-owner` — no MITM/path-traversal RCE
- **AppleScript injection escape** in update notifications

---

## Quick install (from scratch)

### Prerequisites

- macOS or Linux
- Python 3.11+ (tested on 3.13)
- [Claude Code](https://claude.com/claude-code) CLI installed
- **Ollama + a local LLM model — strongly recommended** (see [Ollama setup](#ollama-setup-required-for-full-functionality) below)

### Ollama setup — required for full functionality

Without Ollama ~40% of v6 features stay dormant. The system still works (saves, recalls, dashboard) but the knowledge graph won't grow beyond co-occurrence edges, representations stay at `raw` only, no entity/intent/topic extraction, no fact merging. **For the full experience install Ollama + pull the recommended model:**

```bash
# 1. Install Ollama — see https://ollama.ai or:
brew install ollama                        # macOS (or download .dmg)
curl -fsSL https://ollama.com/install.sh | sh  # Linux

# 2. Start the daemon (it runs on http://localhost:11434)
ollama serve &     # or the macOS app auto-starts

# 3. Pull the default model (4.7 GB, ~2 minutes on decent connection)
ollama pull qwen2.5-coder:7b

# 4. (Optional) Pull a dedicated embedder for Ollama mode
ollama pull nomic-embed-text      # 275 MB, 768-dim multilingual embeddings

# Verify
ollama list
```

**Feature matrix — what requires Ollama:**

| Feature | Without Ollama | With Ollama |
|---|:-:|:-:|
| `memory_save` / `memory_recall` | ✅ works | ✅ works |
| FTS5 + semantic search | ✅ | ✅ |
| Dashboard + 3D graph | ✅ | ✅ |
| Basic co-occurrence edges | ✅ | ✅ |
| `autofilter` compression | ✅ | ✅ |
| **Deep KG triples** (subject→predicate→object edges) | ❌ | ✅ |
| **Multi-representation embeddings** (summary/keywords/questions/compressed) | ❌ `raw` only | ✅ all 5 views |
| **Deep enrichment** (entities, intent, topics) | ❌ | ✅ |
| **Semantic fact merger** (LLM-consolidated related records) | ❌ | ✅ |
| **HyDE query expansion** | ❌ | ✅ |
| **Orphan backfill** (LaunchAgent re-extraction) | ❌ | ✅ |

**Recommended model:** `qwen2.5-coder:7b` — best balance of speed (~3s per extraction on M-series) and quality on code/tech content. Alternatives:

| Model | Size | Speed | Quality | Notes |
|---|---:|---:|---|---|
| `qwen2.5-coder:7b` ⭐ | 4.7 GB | fast | excellent on code | **default** |
| `qwen2.5-coder:32b` | 19 GB | slow | best quality | for 32GB+ RAM machines |
| `llama3.1:8b` | 4.7 GB | fast | general purpose | decent fallback |
| `phi3:mini` | 2.2 GB | very fast | lower quality | low-spec machines |

Set your choice via env: `MEMORY_LLM_MODEL=qwen2.5-coder:7b` in the LaunchAgent plist or shell.

### One command

```bash
git clone https://github.com/vbcherepanov/claude-total-memory.git ~/claude-memory-server
cd ~/claude-memory-server
bash install.sh
```

The installer:

1. Creates `~/.claude-memory/` (DB, embeddings, blobs, transcripts, backups)
2. Sets up Python venv in `~/claude-memory-server/.venv/`
3. Installs deps from `requirements.txt` and `requirements-dev.txt`
4. Pre-downloads the FastEmbed multilingual MiniLM model
5. Registers the MCP server via `claude mcp add-json` (stored in `~/.claude.json`) and grants `permissions.allow` for memory tools in `~/.claude/settings.json`
6. Applies all migrations 001..007 to a fresh `memory.db`
7. Optionally installs LaunchAgents (reflection + orphan backfill + check-updates)
8. Starts the dashboard at `http://127.0.0.1:37737`

### Verify

```bash
# In Claude Code: /mcp → memory should show "Connected"
# Then in your conversation:
memory_save(content="installation works", type="fact")
memory_stats()
```

Open the dashboard: <http://127.0.0.1:37737/>

---

## Upgrade from v5 / v4 / v3

### Automatic (recommended)

```bash
cd ~/claude-memory-server
bash update.sh
```

What it does (7 stages):

1. **Pre-flight** — disk space check, snapshot DB to `~/.claude-memory/backups/memory.db.YYYYMMDD_HHMMSS.gz` (keeps 7 last)
2. **Source pull** — `git pull --ff-only` if repo, or HTTPS+SHA-256-verified tarball if `UPDATE_URL` set
3. **Dependencies** — `pip install -r requirements.txt -r requirements-dev.txt` only if either file hash changed
4. **Tests** — full pytest suite. Aborts (with snapshot kept) if red
5. **Schema** — `Store()` init applies pending migrations idempotently. v3/v4/v5 → v6 means up to 7 migrations roll forward
6. **Services** — reloads LaunchAgents + restarts dashboard
7. **MCP** — macOS notification + instruction to do `/mcp` reconnect (only Claude Code can respawn the MCP server)

### Manual

```bash
cd ~/claude-memory-server
git pull
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python src/tools/version_status.py    # see pending migrations
.venv/bin/python -m pytest tests/                # gate
# Restart MCP from Claude Code: /mcp → memory → Reconnect
# Reload LaunchAgents:
launchctl unload ~/Library/LaunchAgents/com.claude.memory.*.plist 2>/dev/null
cp launchagents/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.claude.memory.*.plist
```

### Migration matrix

| From | What rolls forward | Notes |
|---|---|---|
| **v5.0** | migrations 002..007 | KG already present; new tables for queues, representations, enrichment, filter savings, perf indexes |
| **v4.x** | migrations 001..007 | Adds full v5 KG schema + everything from v6 |
| **v3.x** | migrations 001..007 + branch column | Same as v4, plus `branch` column on knowledge/sessions |
| **v2.x** | full schema rebuild | Backup + reinstall (data preserved via export/import) |

Migration order is enforced by sorted filename prefix (`001_*.sql` first). Each is recorded in the `migrations(version, description, applied_at)` table — re-running is a no-op.

### Rollback

```bash
# Find your snapshot
ls -lt ~/.claude-memory/backups/

# Restore
gunzip < ~/.claude-memory/backups/memory.db.YYYYMMDD_HHMMSS.gz > ~/.claude-memory/memory.db

# Roll back code
cd ~/claude-memory-server && git checkout v5.0
# Restart MCP via /mcp in Claude Code
```

---

## Architecture at a glance

```
                          memory_save(content)
                                   │
                                   ▼
       ┌───────────────────────────────────────────────────────┐
       │  src/server.py — Store.save_knowledge                │
       │  • autofilter.detect_filter() ← optional compression │
       │  • _sanitize_content() ← privacy strip                │
       │  • INSERT INTO knowledge                              │
       │  • _upsert_embedding() ← FastEmbed / Ollama vector    │
       │  • auto_link_knowledge() ← create graph_nodes for tags│
       │  • enqueue × 3 (triples / enrichment / representations)│
       │  • touch ~/.claude-memory/.reflect-pending            │
       └───────────────────────────────────────────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────┐
                │  LaunchAgent WatchPaths picks up │
                │  the touch (<1s)                 │
                └──────────────────────────────────┘
                                   │
                                   ▼ (5s debounce)
       ┌───────────────────────────────────────────────────────┐
       │  src/tools/run_reflection.py                         │
       │  scope = drain (small) | full (big) | weekly         │
       │                                                       │
       │  Phase 3: triple_extraction_queue → Ollama deep_extract│
       │           → graph_edges (subject, predicate, object)   │
       │  Phase 5: deep_enrichment_queue → entities/intent/topics│
       │           → knowledge_enrichment table                 │
       │  Phase 6: representations_queue → 5 LLM views          │
       │           → knowledge_representations table            │
       │  + Digest (dedup, decay, contradictions) on full mode  │
       │  + Synthesize (clusters, patterns) on full mode        │
       │  + FactMerger (LLM consolidation) on full mode         │
       └───────────────────────────────────────────────────────┘
                                   │
                                   ▼
                  ⇒ Graph and search are now richer
```

### Layered storage

| Layer | Purpose | Where |
|---|---|---|
| **Short-term** | Live conversation context | Claude Code session window |
| **Episodic** | Sessions, transcripts, events | `sessions`, `episodes` tables |
| **Semantic** | Facts, knowledge, lessons, decisions, conventions | `knowledge` table + FTS5 + embeddings |
| **Structured** | Concepts + relationships | `graph_nodes`, `graph_edges`, `knowledge_nodes` |
| **Procedural** | Skills (HOW to do things) | `skills`, `skill_uses` |
| **Self-model** | Competencies, blind spots, user model | `competencies`, `blind_spots`, `user_model` |
| **Meta** | Errors, insights, rules (SOUL self-improvement) | `errors`, `insights`, `rules` |

---

## Search pipeline

`memory_recall(query, ...)` runs through 6 tiers, fuses with RRF (Reciprocal Rank Fusion, k=60), enriches with cognitive context, optionally reranks:

```
query
  │
  ├─[Tier 1] FTS5 + BM25                  ~5-15 ms   keyword + relevance
  ├─[Tier 2] semantic cosine               ~15-30 ms  binary-quantized HNSW
  ├─[Tier 2b] HyDE (optional Ollama)       ~2-15 s    hypothetical answer embed
  ├─[Tier 2c] multi-repr search            ~10-20 ms  RRF over summary/keywords/questions/compressed
  ├─[Tier 3] fuzzy SequenceMatcher         ~10-30 ms  typo-tolerant
  └─[Tier 4] graph 1-hop                   ~5-10 ms   neighbor records via KG
       │
       ▼
   RRF fusion (rank-based, scale-invariant)
       │
       ├─ enrichment_filter (if topics/entities/intent set)
       ├─ cognitive_engine (rules, past failures, applicable skills)
       ├─ context_expander (if expand_context=true) — 1-hop graph neighbors
       ├─ CrossEncoder rerank (if rerank=true) — boost-only ms-marco
       └─ MMR diversify (if diverse=true)
       │
       ▼
   top-K results
```

### `memory_recall` parameters

```python
memory_recall(
    query: str,                                        # required
    project: str = None,
    type: "decision|fact|solution|lesson|convention|all" = "all",
    limit: int = 10,
    detail: "compact|summary|full|auto" = "full",      # NEW: auto-picks based on query shape
    branch: str = None,
    fusion: "rrf|legacy" = "rrf",
    rerank: bool = False,                              # CrossEncoder boost
    diverse: bool = False,                             # MMR diversification
    expand_context: bool = False,                      # NEW v6: 1-hop graph
    expand_budget: int = 5,
    topics: list[str] = None,                          # NEW v6: filter by enrichment topics
    entities: list[str] = None,                        # NEW v6: filter by entities
    intent: str = None,                                # NEW v6: filter by intent
)
```

---

## Graph visualizations

The dashboard at <http://127.0.0.1:37737> ships three graph views, switched via top tabs:

| URL | Renderer | Best for |
|---|---|---|
| `/graph/live` | **3d-force-graph** (Three.js + WebGL) | rotate / pan / zoom in 3D, fly-to-node click |
| `/graph/hive` | D3 hive plot | typed networks — concepts vs technologies vs projects on radial axes |
| `/graph/matrix` | Canvas adjacency matrix | dense graphs without edge crossings, sorted by type |

All three share controls:

- **importance ≥ N** — show only nodes mentioned in ≥N records (default 3)
- **edge weight ≥ N** — show only edges with weight ≥N (default 2)
- **type filter** — concept / technology / project / person / company / product / pattern / domain
- **search by name**
- **hide orphans** toggle
- **click → focus** + ESC to back

The main dashboard page (`/`) has live panels for token savings, queue depths, representations coverage, and an SSE connection pill in the header.

---

## Async pipelines

Every `memory_save` enqueues into three queues. A LaunchAgent (or manual cron) drains them:

| Queue | What it does | Tool that drains it |
|---|---|---|
| `triple_extraction_queue` | Ollama deep extract → `(subject, predicate, object)` triples → `graph_edges` | `ConceptExtractor.extract_and_link(deep=True)` |
| `deep_enrichment_queue` | Ollama → entities, intent, topics → `knowledge_enrichment` | `deep_enricher.deep_enrich()` |
| `representations_queue` | LLM-generated `summary, keywords, questions, compressed` + embeddings of each | `representations.generate_representations()` + `MultiReprStore.upsert()` |

Drain happens automatically:

- **On save** — file-watch triggers reflection within 5s (debounce)
- **Hourly** — LaunchAgent safety-net periodic run
- **4× daily** — orphan backfill scans for nodes with zero edges, re-enqueues them

---

## Auto-update

Single-command upgrade with rollback safety:

```bash
bash update.sh                # full update with all 7 stages
bash update.sh --check        # dry-run, report only
bash update.sh --skip-tests   # NOT recommended
```

Weekly auto-check (notify-only by default):

```bash
launchctl load ~/Library/LaunchAgents/com.claude.memory.check-updates.plist
# Set UPDATE_GH_REPO=vbcherepanov/claude-total-memory in the plist for GitHub release polling
```

---

## Benchmarks

Measured on a real working install (1759 active records, 3507 graph nodes, 120912 graph edges, ~78MB DB, M-series Mac):

### Search latency (`memory_recall`, 20 diverse queries)

| Mode | P50 | P95 | P99 | Notes |
|---|---:|---:|---:|---|
| default (RRF, hybrid) | 1145 ms | 1784 ms | 1789 ms | All tiers, no rerank, no expansion |
| `rerank=true` | 1440 ms | 4770 ms | 4862 ms | + CrossEncoder ms-marco — heavy but boost-only |
| `detail="auto"` | 1277 ms | 2024 ms | 2036 ms | Same as default + verbosity inference |

> Hot-cache hits return in under 5ms (LRU 200 entries, 5min TTL). Numbers above are cold-path on 1759-record DB.

### Save latency (`memory_save`, real path)

| Action | Time |
|---|---:|
| `save_knowledge` (incl 3 enqueues + autofilter + auto_link) | **2.5 ms / save** |
| 50 saves in a batch | 125 ms total |

### Quality (LongMemEval R@5)

- **97.45%** on hybrid mode (BM25 + semantic + RRF)
- Beats most open-source MCP memory implementations on the same eval

### Compression (TOML filters, real CLI output)

| Filter | Avg reduction | Best case |
|---|---:|---:|
| `pytest` | 78% | 990 → 222 chars |
| `generic_logs` | 52% | 465 → 223 chars |
| `stack_trace` | 41% | 824 → 490 chars |
| `sql_explain` | 29% | 717 → 511 chars |

### Storage (78 MB total at 1759 records)

| Component | Size |
|---|---:|
| `knowledge` + FTS5 | ~5 MB |
| `graph_nodes` + `graph_edges` (35k+ edges) | ~15 MB |
| `embeddings` (binary-quantized 96 bytes/vec) | ~150 KB |
| `knowledge_representations` (4 views × 232 rows) | ~3 MB |

### Tests

```
370 passed in ~21 s
```

(13 v5 baseline test files + 12 new v6 unit-test files + 7 integration test files + 1 end-to-end test)

---

## Configuration

Environment variables (set in shell, LaunchAgent plist, or MCP server config):

| Variable | Default | What |
|---|---|---|
| `MEMORY_LLM_MODEL` | `qwen2.5-coder:7b` | Ollama model used for deep extraction, enrichment, representations, fact merging |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama base URL |
| `MEMORY_LLM_ENABLED` | `auto` | `auto` (probe Ollama) / `true` / `force` (skip probe) / `false` (degrade) |
| `MEMORY_LLM_PROBE_TTL_SEC` | `60` | Cache TTL for the Ollama availability probe |
| `CLAUDE_MEMORY_DIR` | `~/.claude-memory` | DB + blobs + chroma + backups location |
| `DASHBOARD_PORT` | `37737` | Dashboard HTTP port |
| `DASHBOARD_BIND` | `127.0.0.1` | Bind address. Set `0.0.0.0` only with auth proxy in front |
| `REFLECT_DEBOUNCE_SEC` | `5` | LaunchAgent reflection runner debounce window |
| `UPDATE_GH_REPO` | (unset) | GitHub repo for `check_updates.py`. e.g. `vbcherepanov/claude-total-memory` |
| `UPDATE_URL` | (unset) | Tarball URL for non-git installs (must be HTTPS + `UPDATE_URL_SHA256`) |
| `USE_BINARY_SEARCH` | `auto` | `auto` / `true` (always binary HNSW) / `false` (ChromaDB) |
| `USE_ADVANCED_RAG` | `auto` | HyDE + reranker availability gate |

---

## Operations

### Logs

```bash
tail -f /tmp/claude-memory-reflection.log         # reflection runner
tail -f /tmp/claude-memory-orphan-backfill.log    # orphan backfill
tail -f /tmp/claude-memory-update.log             # last update.sh run
tail -f /tmp/claude-memory-check-updates.log      # weekly update check
tail -f /tmp/dashboard.log                        # dashboard
```

### LaunchAgents

```bash
launchctl list | grep claude.memory                       # status
launchctl start com.claude.memory.reflection              # force run now
launchctl unload ~/Library/LaunchAgents/com.claude.memory.<name>.plist  # disable
launchctl load ~/Library/LaunchAgents/com.claude.memory.<name>.plist    # enable
```

### State diagnostics

```bash
~/claude-memory-server/.venv/bin/python ~/claude-memory-server/src/tools/version_status.py
# → code version + applied/pending migrations + DB size

curl -s http://127.0.0.1:37737/api/v6/queues | python3 -m json.tool
# → pending/processing/done/failed per queue

curl -s http://127.0.0.1:37737/api/v6/savings | python3 -m json.tool
# → token savings totals + per-filter breakdown

curl -s http://127.0.0.1:37737/api/v6/coverage | python3 -m json.tool
# → % of active records with representations + enrichment
```

### Force backfill orphan edges

```bash
~/claude-memory-server/.venv/bin/python \
  ~/claude-memory-server/src/tools/backfill_orphan_edges.py \
  --min-mentions=1 --trigger-now
```

### Import projects in bulk

```bash
~/claude-memory-server/.venv/bin/python \
  ~/claude-memory-server/src/tools/import_projects_now.py \
  ~/Projects ~/work/repos ~/sandbox
```

Walks each path, summarizes README + manifest + `CLAUDE.md` + structure for every subdir, bulk-inserts into knowledge, enqueues into all 3 v6 queues.

---

## Troubleshooting

### "MCP shows Disconnected"

In Claude Code: `/mcp` → `memory` → `Reconnect`. If still failing, check `~/.claude-memory/memory.db` exists and is writable.

### "Graph is empty / not loading"

Check the dashboard: <http://127.0.0.1:37737/api/v6/coverage> — if `representations_records: 0`, queues haven't drained yet. Either:

- Wait ~30s after a save (file-watch trigger)
- Force a drain: `launchctl start com.claude.memory.reflection`
- Run reflection manually via MCP: `memory_reflect_now(scope="full")`

### "Token savings stuck at 0"

`memory_save(filter="pytest")` — pass an explicit filter for known content types. Or rely on autofilter for content matching common patterns (pytest, cargo, git, docker, npm, http, sql, json, stack traces, markdown docs).

### "Ollama not installed / queues constantly fail"

Set `MEMORY_LLM_ENABLED=false` (or remove Ollama). System runs in **degraded mode**:

- `memory_save` works, queues fill up but won't drain LLM phases
- `memory_recall` works (no HyDE, no fact merger)
- Graph stays at co-occurrence edges only

When you install Ollama later, set `MEMORY_LLM_ENABLED=auto` and the queues drain on next reflection cycle.

### "Tests fail after update"

```bash
# Restore last DB snapshot
gunzip < $(ls -t ~/.claude-memory/backups/*.gz | head -1) > ~/.claude-memory/memory.db
# Roll back code
cd ~/claude-memory-server && git reset --hard HEAD~1
# Reload services
bash update.sh
```

---

## License

MIT — see [LICENSE](LICENSE).
