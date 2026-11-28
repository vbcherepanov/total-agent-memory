# v11.0 Phase 0 — Hot-path audit

Date: 2026-04-27
Baseline: v10.5.0 (`src/version.py`).
Scope: every synchronous LLM / Ollama / network / heavy-ML call inside
`Store.save_knowledge` and `Recall.search`.

The goal of this document is a single source of truth for "what blocks the
hot path today" so Phase 1 can flip every default to off and Phase 2 can lock
the new behaviour with regression tests.

## Method

```bash
grep -l "from llm_provider\|import llm_provider" src/*.py
grep -l "import ollama\|from ollama"            src/*.py
grep -l "generate_text\|llm_chat\|.chat("        src/*.py
grep -l "CrossEncoder\|cross_encoder"            src/*.py
```

Then read the relevant blocks of `server.py` and the public API of every
hit so we know the exact env-flag default and the failure mode.

## A. `Store.save_knowledge` — sync external calls

Code anchors are the line numbers inside the v10.5.0 `src/server.py`
(5148 lines).

| # | Stage | server.py | Cost | Default today | After v11.0 |
|---|---|---|---|---|---|
| A1 | `coref_resolver.resolve` | 1275–1281 | LLM | `MEMORY_COREF_ENABLED=false` (off) | stays off in fast, opt-in |
| A2 | `autofilter.detect_filter` | 1236–1241 | regex | always on | stays on (no LLM) |
| A3 | `content_filter.filter_with_stats` | 1244–1261 | regex / TOML | only if `filter_name` set | unchanged |
| A4 | `canonical_tags.normalise_tags` | 1287–1292 | dict lookup | always on | stays on (no LLM) |
| A5 | `entity_dedup.canonicalize_entity_tags` | 1300–1315 | embed similarity (no LLM, but `self.embed`) | `MEMORY_ENTITY_DEDUP_ENABLED=auto` (on) | off in fast, async |
| A6 | `quality_gate.score_quality` | 1330–1378 | **sync LLM** | `MEMORY_QUALITY_GATE_ENABLED=auto` (on if LLM available) | off in fast, async |
| A7 | `_find_duplicate` | 1380–1399 | DB only | on | unchanged |
| A8 | `self.embed([content])` for binary store | ~1410 | embed (FastEmbed → Ollama fallback) | on | FastEmbed-only in fast |
| A9 | `triple_extraction_queue.enqueue` | 1443–1447 | INSERT | on (async) | unchanged |
| A10 | `deep_enrichment_queue.enqueue` | 1449–1454 | INSERT | on (async) | unchanged |
| A11 | `representations_queue.enqueue` | 1456–1461 | INSERT | on (async) | unchanged |
| A12 | `entity_dedup.log_decisions` | 1511–1519 | DB only | on (when A5 fired) | unchanged |
| A13 | `contradiction_detector.detect_contradictions` | 1521–1563 | **sync LLM** + embed neighbours | `MEMORY_CONTRADICTION_DETECT_ENABLED=auto` (on for decision/solution) | off in fast, async |
| A14 | `project_wiki.maybe_auto_refresh` | 1574–1588 | LLM (when refresh fires) | `MEMORY_WIKI_AUTO_REFRESH_EVERY_N=0` (off) | stays off in fast |

### Bypass that already exists

`server.py:1324–1328` reads `MEMORY_ASYNC_ENRICHMENT` and, when true, sets
`_async_enrich=True`. That single flag already short-circuits A6, A12, A13,
A14. v10.1 worker (`enrichment_worker.py`) replays them async. **Phase 1
will turn this flag on by default in fast mode** and gate every `if not
_async_enrich:` block on `MEMORY_USE_LLM_IN_HOT_PATH=true` instead.

A1 (`coref`) and A5 (`entity_dedup`) are NOT in the existing async-skip
block — Phase 1 has to add gates for them too.

## B. `Recall.search` — sync external calls

| # | Stage | server.py | Cost | Default today | After v11.0 |
|---|---|---|---|---|---|
| B1 | `query_rewriter.rewrite` (Anthropic API) | 2377–2387 | **HTTP LLM** | `MEMORY_QUERY_REWRITE=0` (off) | stays off in fast |
| B2 | `_should_use_advanced_rag` | 2338–2347 | gate | `USE_ADVANCED_RAG=auto` → on if Ollama reachable | `false` in fast |
| B3 | `hyde_expand` | 2459, 2505 | **sync LLM** | on when B2 true | off in fast |
| B4 | `analyze_query` | 2392 | LLM-light | on when B2 true | off in fast |
| B5 | `multi_hop_expand` | reranker.py | LLM | gated by B2 | off in fast |
| B6 | `rerank_results` (CrossEncoder) | 2710 | heavy ML (~30 ms) | only when `rerank=true` from caller | unchanged, off in fast |
| B7 | `self.embed([query])` | 2438 | embed | on (with Ollama fallback today) | FastEmbed-only in fast |

## C. `Embed` fallback chain (`Store.embed` and helpers)

`server.py:540–600` (`embed`):

```
fastembed.embed()       # primary
   ↓ except
embedder.encode()       # SentenceTransformer → silent fallback
   ↓ except
self._ollama_embed()    # Ollama HTTP → silent fallback
   ↓ except
[]                      # gives up
```

This silent fallback ladder is exactly what TZ §3.3 forbids. Phase 1 adds
`MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false` and raises a config error instead
of dropping into Ollama.

## D. Modules that import `llm_provider`

- `quality_gate.py` (A6)
- `contradiction_detector.py` (A13)
- `coref_resolver.py` (A1)
- `deep_enricher.py` (async only, deep_enrichment_queue worker)
- `representations.py` (async only, representations_queue worker)
- `session_continuity.py` (read-only tool, only triggered by explicit MCP
  call `memory_session_continuity` — not a save/search hook)
- `config.py` itself (re-exports `has_llm()`)

After Phase 4 the first three move to `src/ai_layer/` and only the worker
imports them. `memory_core.*` will have a regression test that imports
nothing from `llm_provider` (via `tests/test_no_llm_hot_path.py`).

## E. CrossEncoder usage

Only `reranker.py` and `server.py` (the import + the `rerank=true` branch
at 2710). Default for `rerank` MCP arg is False, so it rarely fires today.
Phase 1 adds explicit `MEMORY_RERANK_ENABLED=false` and ignores the
caller's `rerank=true` when the env flag is off (so CI can pin it).

## F. Inventory: who fires what under v10.5.0 defaults

If no env vars are set on a fresh install:

```
save_knowledge for ktype="solution"
  → quality_gate.score_quality           [SYNC LLM]
  → entity_dedup.canonicalize_entity_tags [SYNC EMBED]
  → embed([content])                      [SYNC EMBED, may fall to Ollama]
  → contradiction_detector.detect         [SYNC LLM] (decision/solution)
  → 3 enqueues                            [DB only]

memory_recall for normal query
  → embed([query])                        [SYNC EMBED, may fall to Ollama]
  → semantic search                       [DB only]
  → if Ollama reachable: hyde_expand      [SYNC LLM]
                          analyze_query   [SYNC LLM]
  → CrossEncoder rerank only if rerank=true from caller
```

Net result: a single `memory_save` of a solution today touches Ollama
2 times synchronously (quality_gate + contradiction). A single search
touches Ollama 2 times when reachable. This is what Phase 1 removes.

## G. Phase 1 plan derived from this audit

1. New env flags (defaults shown):

   ```
   MEMORY_MODE=fast
   MEMORY_USE_LLM_IN_HOT_PATH=false
   MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false
   MEMORY_RERANK_ENABLED=false
   MEMORY_CROSS_ENCODER_ENABLED=false
   MEMORY_ENRICHMENT_ENABLED=false       # async worker disabled by default too
   ```

2. Resolve mode at process start in `config.py:resolve_mode()`. Set the
   following derived values when `MEMORY_MODE=fast`:

   - `MEMORY_QUALITY_GATE_ENABLED=false` (A6)
   - `MEMORY_CONTRADICTION_DETECT_ENABLED=false` (A13)
   - `MEMORY_ENTITY_DEDUP_ENABLED=false` (A5)
   - `MEMORY_COREF_ENABLED=false` (A1)
   - `MEMORY_WIKI_AUTO_REFRESH_EVERY_N=0` (A14, already 0)
   - `USE_ADVANCED_RAG=false` (B2)
   - `MEMORY_QUERY_REWRITE=0` (B1, already 0)
   - `MEMORY_ASYNC_ENRICHMENT=true` (so all the disabled sync stages can
     still run async via the worker when `MEMORY_ENRICHMENT_ENABLED=true`)

3. `balanced` mode = same as v10.5.0 defaults (= `MEMORY_ASYNC_ENRICHMENT=true`
   plus async worker on, sync stages off).
4. `deep` mode = sync stages on (current behaviour), reranker on.
5. `ultrafast` mode = `fast` + `MEMORY_VECTOR_ENABLED=false`,
   `MEMORY_EMBED_ON_SAVE=false` (FTS-only).

6. `embed` ladder: when `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false`, raise
   `RuntimeError("FastEmbed unavailable; no Ollama fallback allowed in fast/balanced. Set MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true to re-enable the legacy ladder.")` instead of falling through.

## H. Telemetry counter (temporary)

Phase 1 adds `telemetry.llm_calls_count` and increments it inside
`quality_gate.score_quality`, `contradiction_detector._cd_llm`,
`coref_resolver.resolve`, `hyde_expand`, `analyze_query`,
`query_rewriter.rewrite`, `_ollama_embed`. Phase 5 benchmark asserts the
counter stays at 0 for fast `save_knowledge` and `Recall.search`.

This counter is removed (or hidden behind `MEMORY_DEBUG_LLM_COUNTER=1`)
before v11.0.0 ships — it's strictly an enforcement aid for Phase 2's
regression tests.

## I. Out of scope for v11.0

- Replacing ChromaDB with SQLite BLOB cosine — kept as-is per user
  decision; `memory_core/vector_store.py` will be the abstraction layer.
- Deleting any v10.x module — everything stays under feature flags.
- Changing reflection scheduler cadence.
- New eval datasets — `eval_harness.py` is reused.

## J. Multi-embedding-space support (in scope)

User requirement: even though there is one physical Chroma backend in
v11.0, the architecture must already separate embedding spaces. Future
work can plug a dedicated code/log embedding model without an architecture
migration — only an env-flag flip.

### Required spaces

```
text     general prose, markdown body
code     program text (any language)
log      logs / stacktraces
config   sql / json / yaml / toml / ini / env / shell
```

`memory_core/embedding_spaces.resolve_space(content_type, language)` is the
single point of truth that maps a chunk's classifier output to a space.
Initial mapping table:

```
content_type=code   → code
content_type=sql, json, yaml, toml, ini, env, shell → config
content_type=log, stacktrace                       → log
content_type=text, markdown, mixed, unknown        → text
```

### Required record metadata

Every vector row written by the hot path — both the SQLite `embeddings`
table and the Chroma `knowledge` collection — MUST carry these fields:

```
content_type           # text|markdown|code|sql|json|yaml|toml|ini|env|shell|log|stacktrace|...
language               # python|go|...|null
embedding_provider     # fastembed|sentence-transformers|openai|...
embedding_model        # e.g. BAAI/bge-small-en-v1.5
embedding_dimension    # int
embedding_space        # text|code|log|config
```

Search must accept an `embedding_space` filter (str | list[str] | None).
When unset, all spaces are searched; when set, only matching rows are
considered both at the SQLite binary-search tier and the Chroma fallback
(`where={"embedding_space": {"$in": [...]}}`).

### Model selection

```
MEMORY_TEXT_EMBED_MODEL    sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2  (current default)
MEMORY_CODE_EMBED_MODEL    ""    # empty → falls back to TEXT model, but space=code
MEMORY_LOG_EMBED_MODEL     ""    # empty → falls back to TEXT model, but space=log
MEMORY_CONFIG_EMBED_MODEL  ""    # empty → falls back to TEXT model, but space=config
MEMORY_DEFAULT_EMBEDDING_SPACE=text
```

If a per-space model env var is empty, the row still records
`embedding_space=<space>` and `embedding_model=<text-model>`. When the user
later sets `MEMORY_CODE_EMBED_MODEL=jinaai/jina-embeddings-v2-base-code`,
new code chunks get the new model; old chunks stay searchable in their
own space; a backfill MCP tool (`memory_rebuild_embeddings`) can re-encode
a single space at a time.

### Migration 021 — schema diff

```sql
-- migrations/021_embedding_spaces.sql

ALTER TABLE embeddings ADD COLUMN embedding_provider TEXT;
ALTER TABLE embeddings ADD COLUMN embedding_space    TEXT;
ALTER TABLE embeddings ADD COLUMN content_type       TEXT;
ALTER TABLE embeddings ADD COLUMN language           TEXT;

-- Backfill existing rows. Pre-v11 every embedding came from the
-- text/markdown classifier path, so 'text' is the safe default.
UPDATE embeddings
   SET embedding_space    = COALESCE(embedding_space,    'text'),
       content_type       = COALESCE(content_type,       'text'),
       embedding_provider = COALESCE(embedding_provider, 'fastembed');

CREATE INDEX IF NOT EXISTS idx_embeddings_space   ON embeddings(embedding_space);
CREATE INDEX IF NOT EXISTS idx_embeddings_provider ON embeddings(embedding_provider);
```

### Insert path diff

`Store._upsert_embedding(knowledge_id, embedding, *, model_name, provider,
content_type, language, embedding_space)` — keyword-only new params, no
positional break for existing call-sites that haven't been migrated yet
(they pass keyword `provider="fastembed"`, `content_type="text"`,
`embedding_space="text"`).

Chroma `metadatas[0]` gets the same six fields appended to whatever it
carries today.

### Phase mapping

- **Phase 1b** (new): env flags + migration 021 + new keyword args on
  `_upsert_embedding` + Chroma metadata + space resolver module.
- **Phase 3**: `memory_core/vector_store.VectorStore` interface declares
  `add(records: list[VectorRecord])` where `VectorRecord` requires those
  six fields. Chroma adapter implements it.
- **Phase 5**: chunker emits `content_type`/`language`/`embedding_space`
  per chunk. Hot path passes them through.
- **Phase 6b** (new): `Recall.search(embedding_space=...)` — filter
  applied at both backends.
- **Phase 7**: embedding cache key = sha256(provider + model + space +
  normalized_content). Cache table grows `embedding_space` column.

### Tests added in Phase 2 (currently xfail until 1b lands)

- `test_save_writes_embedding_space_metadata` — every write goes with a
  non-null `embedding_space`, defaulting to `text`.
- `test_existing_vectors_get_text_space_after_migration` — backfill
  populated old rows.
- `test_search_filter_by_embedding_space` — request `embedding_space=code`,
  results contain only rows tagged `code`.
- `test_code_chunk_uses_code_space_even_when_only_text_model_configured` —
  `MEMORY_CODE_EMBED_MODEL=""`, but the row still carries
  `embedding_space=code`.

## Audit done.
