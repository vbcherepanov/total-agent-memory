# total-agent-memory v11.0.0 — Production Memory Engine

**v11 = production memory engine: fast deterministic memory core + async AI enrichment layer. Default mode is `fast`: zero LLM, zero Ollama, zero network in the save/search/recall hot path.**

---

## Highlights

- **Layer split**: `src/memory_core/*` is now deterministic and forbidden from importing any LLM client. `src/ai_layer/*` owns every LLM-touching path. The boundary is enforced by `tests/test_no_llm_hot_path.py`.
- **Four modes**, one env var:

  | Mode         | Hot-path LLM | Async enrichment | Reranker |
  |--------------|:------------:|:----------------:|:--------:|
  | `ultrafast`  |     off      |       off        |   off    |
  | **`fast`** (default) | **off** |       off        |   off    |
  | `balanced`   |     off      |        on        |   off    |
  | `deep`       |     on (sync) |        on       |   on     |

- **Multi-embedding-space contract**: every vector row records `embedding_provider / embedding_model / embedding_dimension / embedding_space / content_type / language`. Spaces: `text` / `code` / `log` / `config`. Single Chroma backend; swapping per-space models is a config flip plus `memory_rebuild_embeddings(space=...)`.
- **Hot-path benchmark** (warm, in-memory SQLite, MacBook M-series, `MEMORY_MODE=fast`):

  | metric              |   p50 |   p95 |   p99 |
  |---------------------|------:|------:|------:|
  | `save_fast`         |  6.2  |  8.9  | 11.4  |
  | `save_fast` cached  |  0.3  |  0.4  |  1.4  |
  | `search_fast`       |  3.4  |  4.7  |  6.0  |
  | `cached_search`     |  3.1  |  3.4  |  3.6  |

  `llm_calls = 0`, `network_calls = 0` across the entire hot path.

---

## Breaking defaults (no public API changes)

Every MCP tool you used in v10.x still exists with the same signature. What changed is the *defaults*:

- **`MEMORY_MODE=fast`** — production default. Zero LLM, zero Ollama, zero network. Set `MEMORY_MODE=deep` to restore v10.5 behaviour.
- **Silent Ollama fallback in `Store.embed` is GATED** — set `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true` to re-enable.
- **`USE_ADVANCED_RAG=auto` no longer auto-fires HyDE / `analyze_query`** — explicit opt-in via `deep` mode or env.

Full per-stage migration: [`docs/archive/v11/MIGRATION-FROM-V10.md`](MIGRATION-FROM-V10.md).

---

## New MCP tools

| Tool                              | One-liner                                                                            |
|-----------------------------------|--------------------------------------------------------------------------------------|
| `memory_save_fast`                | Same surface as `memory_save`, always uses the fast deterministic path.              |
| `memory_search_fast`              | Same as `memory_recall`, fast path only.                                              |
| `memory_explain_search`           | Per-stage timings + candidate counts + applied `embedding_space` filter.             |
| `memory_warmup`                   | Pre-load FastEmbed model + caches into RAM. Run once at session start.               |
| `memory_perf_report`              | Current p50/p95/p99 + `llm_calls` + `network_calls` counters.                         |
| `memory_rebuild_fts`              | Rebuild SQLite FTS5 index after a bulk import.                                       |
| `memory_rebuild_embeddings`       | Re-encode a single embedding space. Use after swapping a per-space model.            |
| `memory_eval_locomo`              | Run the LoCoMo eval against the local DB.                                            |
| `memory_eval_recall`              | Recall@K micro-bench.                                                                 |
| `memory_eval_temporal`            | Temporal-reasoning eval.                                                             |
| `memory_eval_entity_consistency`  | Same-entity-different-name detection eval.                                           |
| `memory_eval_contradictions`      | Contradiction-detector accuracy eval (deep / balanced modes only).                   |
| `memory_eval_long_context`        | Long-conversation retention eval.                                                     |

---

## Migrations

Two new migrations, both **idempotent** and applied automatically on next start:

- `migrations/021_embedding_spaces.sql` — adds `embedding_provider / embedding_space / content_type / language` columns, backfills pre-v11 rows to `embedding_space='text'`.
- `migrations/022_embedding_cache_v11.sql` — new cache keyed by `sha256(provider + model + space + normalized_content)`.

---

## Verifying performance

```bash
./bin/memory-bench --warmup --rounds 200
```

Output goes to `docs/archive/v11/benchmark.md`. CI gate (fails the build if any p95 regresses by more than 25 %):

```bash
./bin/memory-perf-gate
```

---

## Migration

If you depended on synchronous quality_gate / contradiction_detector / coref before INSERT:

```bash
export MEMORY_MODE=deep
```

If you want LLM enrichment but not on the critical path:

```bash
export MEMORY_MODE=balanced
```

Otherwise the default `fast` is what you want. Full guide: [`docs/archive/v11/MIGRATION-FROM-V10.md`](MIGRATION-FROM-V10.md).

---

## Links

- Migration guide: [`docs/archive/v11/MIGRATION-FROM-V10.md`](MIGRATION-FROM-V10.md)
- Architecture audit: [`docs/archive/v11/audit.md`](audit.md)
- Bench artifact: [`docs/archive/v11/benchmark.md`](benchmark.md)
- CHANGELOG: [`CHANGELOG.md`](../../../CHANGELOG.md)
