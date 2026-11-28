# 14.0.0 — implementation and validation

Source release prepared for 2026-09-15. No package publication, deployment, or live-database migration was performed in this preparation stage.

The new isolated team server provides personal, team and shared scopes, token-derived authorship, transactional audit, revision conflicts, a web interface and a lightweight remote bridge. It exposes eight core tools; the local catalogue is unchanged. See [server setup](../team-server-reference.md) and the [latest verification report](../benchmarks/release-final-v14-20260915/RESULTS.md).

Internal text and image tasks support configured Ollama, OpenAI-compatible and Anthropic providers. Embedding/PyTorch thread budgets are configurable; NLI uses float32 for stable batch results. Linux Compose, native Ubuntu, macOS and Windows installation checks passed. The expanded CI matrix still needs execution; top-10 ranking and the heavy BGE latency budget remain unverified or unmet.

The release defaults to fast mode. Heavy PyTorch loaders set OpenMP/BLAS limits before import; a Linux aarch64 BGE probe confirmed one active compute thread, at the cost of about2.13s median latency for10pairs. The200ms heavy-model target remains unmet. See the [CPU report](../benchmarks/grounded-v14/CPU_RESULTS.md). Two additional QA candidates were rejected for validation regressions; [prompt-order results](../benchmarks/grounded-v14/RESULTS_V4.md) and [answer-first results](../benchmarks/answer-first-v14/RESULTS.md) are retained. A contradictory grounded response with both support and a missing premise now yields an explicit rejection after bounded repair.

Three workers stay warm by default. Searches under smaller worker limits visit cached workspaces first without changing result tie ordering. The focused three-scope probe reduced median search latency from3419ms to33ms with identical returned content/order; this is a small-workload measurement, not a universal latency guarantee. Three MiniLM workers used about1975MiB summed RSS; shared pages are counted more than once. Configure the worker limit for available RAM.

## Changes

- Private sections are removed before outbox persistence, including nested and unclosed sections. Completed and superseded write intents discard their replay payload. Existing historical payloads are not rewritten.
- Retrieval applies project, type, branch and embedding-space restrictions after expansion. Project exports exclude other projects' sessions and cross-project relations. Recall caches observe local writes and commits through other SQLite connections.
- Each embedding space uses its configured model and dimensions. Incompatible stored models are excluded from semantic search and reported in `semantic_diagnostics`; lexical retrieval remains available. SQLite vector storage is the search authority for all spaces; Chroma remains a write-side mirror. HyDE is restricted to the compatible text space.
- Iterative retrieval accepts the production grouped result shape. Its planner and answerability guard use the installed runtime provider instead of importing benchmark code. `llm_model="configured"` selects the configured model; explicit overrides must be model identifiers accepted by that provider.
- Cognitive context includes record references (`knowledge:ID`, `rules:ID`, etc.), session provenance for knowledge and a conservative evidence budget. Project filtering covers knowledge, activated graph rules and skill applicability.
- `max_tokens` bounds included evidence using serialized UTF-8 bytes as a conservative upper bound, not an exact tokenizer count. Envelope keys and client prompts are outside this budget. Whole records that do not fit are omitted; `omitted_items` reports omissions at the final packing stage. The existing retrieval candidate limits still apply.
- Recall updates usage counters but no longer refreshes confirmation time. This does not establish that recalled content is true. Saving or explicitly updating a record retains the existing confirmation semantics.
- Backdated single-value assertions close the appropriate existing interval without replacing a later assertion. Recording time is separate from effective time; timezone-bearing timestamps are normalized. `invalidate_previous=False` remains the explicit option for multi-valued predicates. This is not a complete bitemporal audit ledger.
- Representation queue claims and duplicate enqueue protection are atomic across SQLite connections. A second job for an already-processing knowledge record waits. Automatic lease recovery is not introduced: abandoned processing jobs still require operator recovery. Deleted parents are skipped before generation. Historical derived content remains subject to the existing soft-delete policy.
- Enrichment workers stop and join correctly without shadowing Python Thread internals. Deletion removes the record from its per-space Chroma collection as well as SQLite embeddings.
- LoCoMo category mapping is corrected: 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial. Gold-category top-K and category prompts require explicit oracle routing. Scoring still uses gold labels, as expected. Historical result files are unchanged and must not be read as new v14 results.

## Ideas adopted from the review

Mem0's explicit scope boundaries informed retrieval/export checks; Graphiti's distinction between effective and recording time informed temporal corrections; MemOS's provenance informed context sources; LettaCode's budgeting informed context packing. Supermemory's available code was an SDK, so no claim about its private retrieval engine is made. Implementations here were written for this repository rather than copied wholesale.

The review, including pinned competitor revisions, is in the adjacent judge-probe project: `docs/TOTAL_AGENT_MEMORY_CODE_REVIEW.md`.

## Compatibility and measurement

The source candidate includes SQL migrations029–033 for evidence, graph integrity and vector-index revisions. Existing records are retained; validate an upgrade on a backup before applying it to an existing deployment. Changing an embedding model still requires re-embedding; v14 does not silently compare vectors from different models. Soft deletion is not a secure erase operation.

Controlled development measurements and their limits are recorded in the benchmark reports: ordinary LoCoMo66.49%, LongMemEval73.20%. This platform/CPU stage makes no new QA or BEAM claim. The former +9.7 percentage-point comparison of retrieval recall with another product's answer accuracy remains withdrawn. These measurements do not establish a world top-ten rank or superiority over another product.

## Retrieval follow-up

Question stopwords are removed from lexical queries while negation and identifiers remain. The optional `memory_recall(mode="context", neighbors=1)` returns full same-session evidence with source references, a maximum of 20 added neighbors, recalculated token estimates and answer guidance. Equal timestamps use ID ordering. Project/type/branch restrictions and excluded operational tags apply to neighbors. The standalone evidence-window service also accepts embedding-space restrictions.

Initial iterative decomposition now uses the selected provider, and ordinary fast search cannot invoke the optional rewriter. Timeline neighbors receive final scope checks. Existing search response shape is retained; context mode is explicit. Clients must use the returned answer guidance to reproduce the reasoned QA configuration. Record-count limits and token estimates are not an exact tokenizer budget.

## Reproduction

Build the development image with `make dev-image`; run `make test`, `make lint`, and `make build`. These execute inside Docker. `make lint` reports existing repository-wide lint debt as well as new findings. Use the session journal for the exact validation outcome and environment used for this release.
