# Upgrading to 15.0.0

This guide covers upgrading total-agent-memory from 14.x to 15.0.0. It lists what runs automatically on first start, what you must do yourself, and what changes in behaviour. The full list of changes is in the [CHANGELOG](../CHANGELOG.md).

## Before you upgrade

1. Stop every process that uses the store (MCP servers, the dashboard, background daemons).
2. Make a consistent backup. For SQLite, use the SQLite backup API (for example `sqlite3 <db> ".backup <file>"`), not a plain file copy. For PostgreSQL, use your usual tools (`pg_dump` or a base backup).
3. Note the installed version and your configuration, so you can restore both if you need to roll back.

Migrations run on the first start of 15.0.0. There is no downgrade path: **to roll back, reinstall the previous version and restore the backup you took in step 2.** An older version is not guaranteed to work against a migrated schema.

## Schema migrations

| Backend | Migrations | What they add |
|---|---|---|
| SQLite | 047–049 | Claim tokens and next-retry time for representation jobs, embedding identity, cache revision counter |
| SQLite | 050–052 | Separate resource index, persisted watch policies, a compact postings table |
| SQLite | 053–059 | Repaired scope of auto-consolidated rules (including whitespace-only project names), similarity candidates, knowledge state history, backfill of retired records' history, incident structure, indexed 24-character similarity keys |
| PostgreSQL workspace | 0011–0013 | Same as SQLite 047–049 |
| PostgreSQL workspace | 0014–0015 | Same as SQLite 050–052 (resource index and watch policies) |
| PostgreSQL workspace | 0016–0022 | Same as SQLite 053–059 |
| PostgreSQL compat | 0002–0003 | Date functions that follow SQLite 3.50 (fractional seconds are truncated, not rounded) |

SQLite migration 051 rebuilds the postings table inside a transaction. It copies the data into a temporary table first, so keep some free disk space available during the upgrade. The old file size can remain as reusable free pages. No automatic `VACUUM` runs.

### State history starts at the upgrade

State history starts at the upgrade, with an approximate past. A record that was already deleted, archived or otherwise retired is recorded as active from its `created_at` and retired at the best time the old schema holds: the creation time of its successor for superseded records, otherwise its `last_confirmed` time (never before `created_at`). `as_of` queries return it before that time. For deleted or archived records this time is a lower bound, because the old schema stored no deletion time; a retired record with neither timestamp stays invisible at every `as_of`. Migrations SQLite 058 and PostgreSQL workspace 0021 do this and rewrite only histories that consist of the single `unknown` backfill event.

## Representation identity and regeneration

Every stored representation now records the model, provider, pooling and library that produced it.

- Representations with `auto`, an unknown provider or an incompatible identity are excluded from search. Matching dimensions are not enough: two models with the same vector size are not interchangeable.
- These representations are queued for regeneration when the background queues and reflection start. Jobs that are already pending or have failed for good are not duplicated.
- Until regeneration finishes, search may return fewer results from this layer. Primary embeddings are checked separately.
- `memory_stats` shows coverage (missing, stale, unknown, pending, failed and unqueued) and records that are not in the queue.
- A job that fails permanently is not retried forever. Fix the cause of the failure first.

### Custom reflection embedders

If you inject your own embedder into `ReflectionAgent`, you must also pass an `embed_identity` callback. It returns the concrete model and provider of the most recent embedding. If the identity is unknown, or the provider changes in the middle of one job, the job fails and the batch of representations is not published.

## PostgreSQL HNSW index

Searches no longer create HNSW indexes. An administrator provisions the index once, using an administrative connection. Pass the DSN through an environment variable, not on the command line:

```sh
export TAM_INDEX_ADMIN_DSN="postgresql://admin@db.example.com/tam"
tam-prepare-vector-index --dsn-env TAM_INDEX_ADMIN_DSN --schema "$TAM_WORKSPACE_SCHEMA" --dimension 384
```

In a source checkout you can run `python scripts/prepare_vector_index.py` with the same arguments.

| Argument | Meaning |
|---|---|
| `--dsn-env` | Name of the environment variable that holds the administrative DSN (required) |
| `--schema` | Workspace schema to index (required; it must be a TAM workspace schema) |
| `--dimension` | Vector dimension of your embedding model (required; 384 above is an example) |

The command uses an autocommit connection and builds the index without blocking writers. An existing index with the wrong definition is rejected. An invalid index with the expected definition is rebuilt. Until a valid index exists, searches fall back to exact scoring, which is correct but slower on large stores.

## Resource limits and diagnostics

Defaults are shown in parentheses.

| Variable | Purpose |
|---|---|
| `MEMORY_REPR_CACHE_BYTES` (33554432) | Memory budget for cached representation matrices and norms. `0` disables the cache. It is invalidated when a project, its visibility or its representations change. |
| `TAM_STORE_MAX_PENDING` (32) | Limit on unfinished personal tool calls, including calls waiting for inference. When it is exceeded, the call returns an MCP tool error. `/healthz` stays available. |
| `TAM_RECALL_INFERENCE_WORKERS` (4) | Size of the bounded worker pool for cross-encoder inference in `memory_recall`. |
| `MEMORY_CROSS_RERANK_THREADS` (4) | Internal ONNX threads for the cross-encoder. |
| `TAM_STORE_SHUTDOWN_SECONDS` (10) | How long shutdown waits for the Store owner thread. |
| `MEMORY_INGEST_URL_MAX_BYTES` (20971520) | Largest accepted response for URL ingestion. |
| `MEMORY_INGEST_URL_TIMEOUT_SECONDS` (10) | Download timeout for URL ingestion. |

Raising the inference and thread values can increase latency and memory use.

How the personal server now works:

- The personal stdio, HTTP and daemon servers run all Store operations on one owner thread with a bounded queue. The transport stays responsive during slow calls. If a client cancels, work that has not started is dropped. A write that has started finishes exactly once. Team workers keep their own process model.
- If the data revision changes during cross-encoder inference, recall repeats the search. After two repeats it finishes sequentially. Usage counting happens once, for the final result.
- At shutdown, queued calls are rejected and a running operation is neither cancelled nor restarted. Exceeding `TAM_STORE_SHUTDOWN_SECONDS` is logged as an error. A thread cannot interrupt a hung native call, and a transaction that is still open when the process is killed is not guaranteed to be saved.
- Telemetry is written from a bounded background queue, with counters for retries and drops. These are diagnostic events, not a durable queue of user writes.
- Recall timers include singleflight waits, query embedding, FTS and fuzzy search, hydration, extra candidate stages and the reranker. Some intervals are nested, so do not add them up as independent parts of the total latency.

## Persistent resource index

`ingest_codebase` now writes a persistent, project-scoped index by default. The index lives in its own tables (SQLite 050, PostgreSQL 0014), separate from knowledge records, and moves with the normal SQLite to PostgreSQL transfer. To keep the old behaviour with no writes, pass `action="preview"`. Use the same `path` and `project` to update, search and check status:

```json
{"path":"/allowed/project","project":"example","action":"index"}
{"path":"/allowed/project","project":"example","action":"search","query":"OrderService","limit":10}
{"path":"/allowed/project","project":"example","action":"status","limit":100,"offset":0}
```

- **Updates.** A repeated `index` compares content hashes, re-reads changed files and removes deleted ones. A failed walk or an exceeded total limit leaves the previous index untouched. A failure on one file removes that file's stale chunks and records the error.
- **Search.** It is lexical: all normalised query words must match, Unicode is supported, and camelCase and snake_case parts are matched. It does not use embeddings or an LLM.
- **Languages.** The language comes from the tree-sitter registry. If a grammar is unavailable, the text is kept with `parser_unavailable`. Unknown extensions and files without an extension are split into searchable text chunks.
- **Documents.** PDF files with a text layer, Office XML, and ZIP, TAR and GZIP archives are read locally. For archives, member names and available text are kept, and nothing is extracted to disk. Encrypted, binary or secret members are counted as `unextracted_members`.
- **Paths.** `TAM_ALLOWED_PATH_ROOTS` restricts which roots can be indexed.

### Excluded files and secrets

By default `.git`, `.hg`, `.svn`, `venv`, `.venv`, `node_modules` and `__pycache__` are excluded, and the directory is reported as excluded. Pass `exclude=[]` to turn these defaults off. Files such as `.env`, keys and credentials stay excluded by protected patterns, also inside archives. `.env.example`, sample and template files are allowed, with secrets redacted. Symbolic links are not followed. Private blocks and known secret formats are removed before anything is stored or parsed, and line numbers are preserved.

### Limits

All variables below start with `TAM_INDEX_`.

| Variable | Default |
|---|---|
| `MAX_FILE_BYTES` | 8388608 |
| `MAX_FILES` | 50000 |
| `MAX_SCAN_BYTES` | 268435456 |
| `CHUNK_CHARS` | 4000 |
| `MAX_ARCHIVE_MEMBERS` | 1000 |
| `MAX_EXPANDED_BYTES` | 16777216 |
| `MAX_QUERY_TERMS` | 32 |
| `MAX_RESULTS` | 100 |

Higher limits use more memory and time. `ingest_codebase` runs off the transport loop on its own connection and a dedicated indexing pool (`TAM_INDEX_WORKERS`, default 1), so other tool calls and recall inference continue while a large folder is indexed.

Without the `media` extra, PDF text is extracted in a separate process, limited by `TAM_INDEX_PDF_TIMEOUT_SECONDS` (30) and `TAM_INDEX_PDF_MEMORY_BYTES` (536870912). The parent process checks the worker's memory every 50 ms and stops it when the limit is exceeded, so a short overshoot between checks is possible. On Linux the address space is also limited. Stopping the watcher cancels any running PDF or media worker.

## Optional media extraction

Install the extra to extract text from images, scanned PDF pages, audio, video, RTF and XLS:

```sh
pip install "total-agent-memory[media]"
```

**Prerequisite.** OCR needs Tesseract installed on the system, with the `eng`, `rus` and `ukr` language data. The extra does not install it. Set `TAM_INDEX_MEDIA_OCR_LANGUAGES` to change the languages.

- **Transcription.** Whisper uses the `tiny` model on CPU with int8 by default and downloads the open weights on first use. For a fully offline setup, set `TAM_INDEX_MEDIA_WHISPER_MODEL` to the path of a model you downloaded earlier. `TAM_INDEX_MEDIA_MODEL_CACHE` sets the weights directory. Video files are transcribed from their audio track. Video frames are not analysed.
- **Fallbacks.** Encrypted PDFs and unsupported formats stay `metadata_only`. A missing dependency is stated in the `detail` field. A corrupt supported file gets an `error`.
- **Limits.** Media extraction runs in a bounded, cancellable subprocess. Variables start with `TAM_INDEX_MEDIA_`:

| Variable | Default |
|---|---|
| `TIMEOUT_SECONDS` | 180 |
| `MEMORY_BYTES` | 1610612736 |
| `MAX_PIXELS` | 25000000 |
| `MAX_PAGES` | 100 |
| `MAX_DURATION_SECONDS` | 600 |
| `RENDER_DPI` | 150 |
| `THREADS` | 2 |

The parent and child memory is checked every 50 ms, so a brief overshoot is possible. The shared file and text limits from the previous section still apply. The repository includes `docker/Dockerfile.media`, an image with these dependencies that runs as an unprivileged user.

## Automatic index refresh

After a successful `ingest_codebase(action="index")`, the personal SQLite server stores the root's filters (SQLite 052, PostgreSQL 0015) and keeps the index fresh in the background.

- Pass `watch=false` when indexing to opt a root out.
- `action="status"` returns `watch.enabled`, `watch.checked_at`, `watch.error` and the state of the global switch.
- `TAM_INDEX_WATCH_ENABLED=false` turns the worker off.
- `TAM_INDEX_WATCH_INTERVAL_SECONDS` (5) sets the check interval. `TAM_INDEX_WATCH_RECONCILE_SECONDS` (300) sets how often content is re-read in full.
- A process lock prevents duplicate scanners across personal sessions.
- Changes made while the server is stopped are picked up when it starts again.
- **Indexes created before 15.0.0 have no stored filters.** Call `action="index"` once for each root to save them.
- A missing or unreachable root keeps its existing data and reports an error. Deleting a file inside a reachable root removes it from the index.

The watcher serves the personal SQLite server only. The team server does not expose `ingest_codebase` yet. It is not a service for watching remote file systems.

## Other behaviour changes

- Auto-consolidated rules from `learn_error` now carry the scope `global` or `project:<name>`, so they reach `self_rules_context`. Migration 053 repairs the stored rows.
- `memory_recall(expand_context=true)` no longer returns records from other projects.
- PostgreSQL date functions now match SQLite 3.50 (compat migrations 0002–0003).
- Cross-encoder scoring groups candidates into batches by text length. Set `MEMORY_CROSS_RERANK_BATCH_SIZE=0` for the previous batching, and `MEMORY_CROSS_RERANK_CACHE_SCORES=0` to disable the score cache.

## After the upgrade

1. Start the server and check that the migrations finished without errors.
2. If you use PostgreSQL, provision the HNSW index as described above.
3. Run `memory_stats` and watch the embedding coverage until the pending count reaches zero.
4. Re-run `ingest_codebase` with `action="index"` for each root you want refreshed automatically.
