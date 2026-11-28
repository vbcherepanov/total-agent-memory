# Migrating from v10.5 to v11.0

v11.0 is **backwards-compatible at the public-API level** — every MCP
tool you used in v10.x still exists with the same signature. What
changed is the *defaults*: the hot path now runs zero LLM, zero Ollama,
zero network. If you depended on v10.5's synchronous LLM stages, set
`MEMORY_MODE=deep` and you are back where you started.

This guide covers everything you need to know to move from v10.5 to
v11.0 safely.

---

## 1. What changed in defaults

The single biggest change: `Store.save_knowledge` and `Recall.search`
no longer call any LLM by default. They no longer fall back to Ollama
silently when FastEmbed is unavailable. They no longer fire HyDE /
analyze_query / query_rewriter on retrieval.

| Surface                                         | v10.5 default                                  | v11.0 default                                       |
|-------------------------------------------------|------------------------------------------------|-----------------------------------------------------|
| `MEMORY_MODE`                                   | _(did not exist)_                              | `fast`                                              |
| `quality_gate.score_quality` (sync, on save)    | on if Ollama reachable                         | **off** in fast (async in balanced, sync in deep)   |
| `contradiction_detector.detect`                 | on for `decision` / `solution`                 | **off** in fast (async in balanced, sync in deep)   |
| `entity_dedup.canonicalize_entity_tags`         | on (`auto`)                                    | **off** in fast (async in balanced)                 |
| `coref_resolver.resolve`                        | off (`MEMORY_COREF_ENABLED=false`)             | off in fast (sync in deep)                          |
| `Store.embed` Ollama fallback ladder            | silent fallback                                | **`RuntimeError`** unless `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true` |
| `USE_ADVANCED_RAG`                              | `auto` (fired if Ollama reachable)             | **`false`** in fast                                 |
| `hyde_expand` / `analyze_query`                 | on when advanced-rag fired                     | off in fast                                         |
| `MEMORY_RERANK_ENABLED`                         | _(did not exist; `rerank=true` honoured)_      | `false` (caller `rerank=true` ignored unless flag set) |
| `MEMORY_ASYNC_ENRICHMENT`                       | `false` (opt-in)                               | auto-`true` in `balanced` / `deep`, else `false`    |

Source of truth: [`docs/archive/v11/audit.md` § A–G](audit.md).

---

## 2. Decision tree — which mode do I want?

### "I depended on synchronous quality_gate / contradiction_detector / coref before INSERT"

Set:

```bash
export MEMORY_MODE=deep
```

This restores **the exact v10.5.0 hot path**. quality_gate runs sync,
contradiction_detector runs sync, coref runs sync (when
`MEMORY_COREF_ENABLED=true`), the embed ladder falls back to Ollama,
the reranker honours `rerank=true`. Nothing is async-deferred.

### "I want sync behaviour but with the new layer separation"

This is **not a supported configuration**. The layer separation
(`memory_core/*` cannot import `llm_provider`) means LLM stages are
either run in the dispatcher (deep) or via the async worker (balanced).
There is no third option.

What you probably want is `MEMORY_MODE=balanced`:

```bash
export MEMORY_MODE=balanced
```

Same enrichment stages run, just out of the hot path. `memory_save`
returns in single-digit milliseconds; the worker drains
`enrichment_queue` in the background and updates the row's status when
each stage completes. quality_dropped rows are still hidden from
recall.

Gotchas with balanced mode:

- A `quality_gate` `drop` verdict no longer prevents the INSERT. The
  row is marked `status='quality_dropped'` after the worker scores it
  and is excluded from `memory_recall` via `idx_knowledge_status_quality`.
  If you need strict pre-INSERT gating (compliance), use `deep`.
- `contradiction_detector` runs against the row's neighbours **after**
  the row is inserted. The supersession edge appears a few seconds
  later. Real-time UIs that show "this contradicts X" need to poll.
- The worker is a single daemon thread. Tune
  `MEMORY_ENRICH_TICK_SEC` / `MEMORY_ENRICH_BATCH` if your save rate
  is sustained > 50 / sec.

### "I just want it to be fast"

Default. Do nothing.

```bash
# MEMORY_MODE=fast is the default
```

The fast hot path uses FastEmbed only. If FastEmbed cannot load (rare,
usually a corrupted model cache), saves and searches fail loudly with
a `RuntimeError("FastEmbed unavailable; no Ollama fallback allowed in
fast/balanced. Set MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true to re-enable
the legacy ladder.")` instead of silently rerouting through Ollama.
This is intentional — silent fallbacks were the v10.5 footgun the
audit flagged most often.

### "Throughput stress / CI"

```bash
export MEMORY_MODE=ultrafast
```

`fast` plus `MEMORY_VECTOR_ENABLED=false` and
`MEMORY_EMBED_ON_SAVE=false`. FTS-only retrieval. Use for benchmarking
or for short-lived CI runners that should not spin up an embedding
model.

---

## 3. New environment variables

All defaults shown.

| Variable                              | Default                                                                 | Purpose                                                                 |
|---------------------------------------|-------------------------------------------------------------------------|-------------------------------------------------------------------------|
| `MEMORY_MODE`                         | `fast`                                                                  | `ultrafast` / `fast` / `balanced` / `deep`. Resolves the other flags below at process start. |
| `MEMORY_USE_LLM_IN_HOT_PATH`          | `false`                                                                 | Master gate for sync LLM in `save` / `search`. `MEMORY_MODE=deep` flips this to `true`. |
| `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH`     | `false`                                                                 | Permit silent FastEmbed → Ollama fallback in `Store.embed`.            |
| `MEMORY_RERANK_ENABLED`               | `false`                                                                 | Honour caller's `rerank=true`. When `false`, CrossEncoder rerank is hard-disabled. |
| `MEMORY_ENRICHMENT_ENABLED`           | `false` in fast, `true` in balanced/deep                                | Run the async enrichment worker.                                       |
| `MEMORY_TEXT_EMBED_MODEL`             | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`           | Model for `embedding_space=text`.                                      |
| `MEMORY_CODE_EMBED_MODEL`             | _empty → falls back to TEXT model_                                      | Model for `embedding_space=code`.                                      |
| `MEMORY_LOG_EMBED_MODEL`              | _empty → TEXT_                                                          | Model for `embedding_space=log`.                                       |
| `MEMORY_CONFIG_EMBED_MODEL`           | _empty → TEXT_                                                          | Model for `embedding_space=config`.                                    |
| `MEMORY_DEFAULT_EMBEDDING_SPACE`      | `text`                                                                  | Space for unclassified content.                                        |

The pre-existing v10.x knobs (`MEMORY_QUALITY_GATE_ENABLED`,
`MEMORY_CONTRADICTION_DETECT_ENABLED`, `MEMORY_ENTITY_DEDUP_ENABLED`,
`MEMORY_COREF_ENABLED`, `USE_ADVANCED_RAG`, etc.) still work and still
override the mode preset if set explicitly.

---

## 4. New MCP tools

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

All previous tool names (`memory_save`, `memory_recall`, ...) continue
to work unchanged. The `_fast` variants are explicit opt-ins to the
fast path even when `MEMORY_MODE=deep` is set globally.

---

## 5. Schema changes

Two migrations apply on next start. Both are **idempotent** — running
them twice is a no-op.

### `migrations/021_embedding_spaces.sql`

```sql
ALTER TABLE embeddings ADD COLUMN embedding_provider TEXT;
ALTER TABLE embeddings ADD COLUMN embedding_space    TEXT;
ALTER TABLE embeddings ADD COLUMN content_type       TEXT;
ALTER TABLE embeddings ADD COLUMN language           TEXT;

UPDATE embeddings
   SET embedding_space    = COALESCE(embedding_space,    'text'),
       content_type       = COALESCE(content_type,       'text'),
       embedding_provider = COALESCE(embedding_provider, 'fastembed');

CREATE INDEX IF NOT EXISTS idx_embeddings_space    ON embeddings(embedding_space);
CREATE INDEX IF NOT EXISTS idx_embeddings_provider ON embeddings(embedding_provider);
```

Pre-v11 every embedding came from the text/markdown classifier path,
so `embedding_space='text'` is the safe default backfill. Chroma
metadata gets the same six fields appended on next write of each row.

### `migrations/022_embedding_cache_v11.sql`

New cache keyed by `sha256(provider + model + space + normalized_content)`
to keep different embedding spaces from colliding once you actually plug
a code-specific model. Existing v10 cache is read-through compatible.

---

## 6. Deprecation map (no removals)

Nothing was removed in v11.0. Some modules are now better accessed via
the new layer:

| If you used (v10.x)                          | Prefer (v11.0)                                            |
|----------------------------------------------|-----------------------------------------------------------|
| `from server import Store`                   | `from memory_core.storage import Storage`                 |
| `Store.embed(...)`                           | `memory_core.embeddings.embed(...)`                       |
| direct `quality_gate.score_quality(...)`     | `ai_layer.quality_gate.score_quality(...)` (or async job) |
| direct `contradiction_detector.detect(...)`  | `ai_layer.contradiction_detector.detect(...)`             |
| direct `coref_resolver.resolve(...)`         | `ai_layer.coref_resolver.resolve(...)`                    |
| direct `reranker.rerank_results(...)`        | `ai_layer.reranker.rerank_results(...)`                   |
| direct `query_rewriter.rewrite(...)`         | `ai_layer.query_rewriter.rewrite(...)`                    |

The v10 import paths still resolve via re-export shims. Tests and
`server.py` have been migrated; user code can take its time.

---

## 7. Known gotchas

### Async enrichment is OFF by default in `fast`

`MEMORY_ENRICHMENT_ENABLED=false` means the worker thread does not
start. This is deliberate: a `fast`-mode user opted into "no LLM, no
worker, no surprises". If you want enrichment without losing the fast
hot path, use **`balanced`** (worker on, sync stages off) — that is the
sweet spot.

### `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false` is an error, not a fallback

Previously, if FastEmbed failed to load, `Store.embed` silently HTTP'd
Ollama and returned. v11 raises `RuntimeError` instead. If you ran
v10.5 on a host without FastEmbed (rare, but exists), you must either:

1. Install FastEmbed (`pip install fastembed`), or
2. Explicitly opt into the legacy ladder: `export MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true`.

### `rerank=true` from MCP is honoured only when `MEMORY_RERANK_ENABLED=true`

CI environments and benchmark runs used to silently activate
CrossEncoder when a caller passed `rerank=true`. Now the env flag is
the gate. This makes regression numbers deterministic.

### `embedding_space` is mandatory on every new vector row

The classifier picks a space based on `content_type` and `language`.
For unknown content it falls back to
`MEMORY_DEFAULT_EMBEDDING_SPACE=text`. There is no path that writes a
NULL space. Old v10 rows are backfilled to `text` by migration 021.

### Per-space models that are empty fall back to TEXT, but the row still records its space

`MEMORY_CODE_EMBED_MODEL=""` does **not** mean "no code chunks". It
means "encode code chunks with the TEXT model for now". The vector row
still carries `embedding_space='code'`. When you later set
`MEMORY_CODE_EMBED_MODEL=jinaai/jina-embeddings-v2-base-code`, run
`memory_rebuild_embeddings(space="code")` to re-encode old rows;
otherwise old code chunks remain searchable in their own space but
with stale embeddings.

### `quality_dropped` rows are hidden, not deleted

A `quality_gate` `drop` verdict (in `balanced` mode) marks the row
`status='quality_dropped'`. `memory_recall` ignores them; the audit
trail stays in `quality_gate_log`. To purge:

```text
memory_forget(filter="status='quality_dropped'", dry_run=true)
```

### Bench numbers below 1ms are dominated by import overhead

If `scripts/memory-bench` shows p50 = 0.05 ms on `cached_search`, that's
the cache hit path, not steady-state retrieval. The `cached_search`
metric is included so you can tell whether your client cache is doing
its job; for ranking changes, look at `search_fast` instead.

---

## 8. Rollback

v11 does not remove any v10 functionality. Worst case:

```bash
# 1. revert mode default
export MEMORY_MODE=deep
# or, more aggressively:
git checkout v10.5.0
./update.sh --skip-migrations
```

Migrations 021 and 022 are additive — downgrading does not lose data.
Existing v10 code reads the new columns as NULL safely.

---

## 9. Quick checklist

- [ ] Read [`audit.md`](audit.md) § A–G to know exactly which stages
      changed.
- [ ] Pick a mode: keep default `fast` for new installs; set `deep` if
      you depended on sync LLM; set `balanced` if you want LLM
      enrichment but not on the critical path.
- [ ] Run `./scripts/memory-bench --warmup` once after upgrade to confirm
      your numbers match the artifact in
      [`benchmark.md`](benchmark.md).
- [ ] If you swap `MEMORY_CODE_EMBED_MODEL` / `MEMORY_LOG_EMBED_MODEL`
      / `MEMORY_CONFIG_EMBED_MODEL`, run
      `memory_rebuild_embeddings(space=...)` for that space.
- [ ] Update scripts that hardcoded `Store.embed` to use
      `memory_core.embeddings.embed` — the old path still works but is
      deprecated.
- [ ] If you have CI that asserted `rerank=true` had an effect, also
      set `MEMORY_RERANK_ENABLED=true`.
