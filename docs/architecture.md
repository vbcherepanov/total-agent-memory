# Architecture

![Write path, recall path and store layout](../paper/architecture.png)

An MCP client (Claude Code, Codex CLI, Cursor or any other) talks to the
server over stdio or HTTP. Writes go through `memory_save`, `memory_update`
and `kg_add_fact`; each change creates a new version and keeps the history.
Recall runs several retrieval tiers and fuses their ranked lists with
reciprocal rank fusion. The default `fast` profile computes embeddings locally
and makes no LLM call on write or search.

One store is one directory: a SQLite database with records, versions, the FTS5
index, float32 and binary vectors and the knowledge graph, plus a ChromaDB
fallback index. The team server runs one store and one worker per personal,
department or shared area behind a gateway with roles and an audit trail
([team-server.md](team-server.md)).

## Code layout

`src/memory_core/` is deterministic and makes no LLM calls: embeddings, vector
store, chunker, dedup, cache, graph links, storage. `src/ai_layer/` holds
everything that calls an LLM: the enrichment worker, summarizer, extractors,
contradiction detector and reflection. `ai_layer` may import from
`memory_core`; the reverse is forbidden and enforced by
`tests/test_v11_layer_separation.py`. `tests/test_no_llm_hot_path_v11.py` checks
that the `fast` hot path makes no LLM or network call.

## Components

```
                  ┌─────────────────────────────────────────────────┐
                  │             Your AI coding agent                │
                  │   (Claude Code · Codex CLI · Cursor · any MCP)  │
                  └──────────────────────┬──────────────────────────┘
                                         │ MCP (stdio or HTTP)
                                         │ 79 tools
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

`/graph/live`, `/graph/hive` and `/graph/matrix` are the dashboard's graph
views; see [tools.md](tools.md#dashboard). A longer walkthrough
of the v11 hot path and background workers, in Russian, is in
[SYSTEM-OVERVIEW.md](archive/SYSTEM-OVERVIEW.md).
