# total-agent-memory (TAM) — LongMemEval-V2 system description (DRAFT)

Status: draft for a pilot on a 20-question subset of LME-V2-Small. Not a leaderboard
submission; numbers from the pilot are not reported here.

## Method

TAM (https://github.com/vbcherepanov/total-agent-memory, `src/`) is a local memory server:
SQLite with FTS5 full-text search, local sentence embeddings, reciprocal-rank fusion of
the lexical and vector rankings, and an optional local cross-encoder re-ranker. The
LongMemEval-V2 backend (`memory_type: "tam"`, file `tam_memory.py`) uses it as follows.

**Indexing (`insert`).** Each trajectory becomes
- one fragment per state: trajectory id, environment, outcome, step index, URL, the
  agent's thought, its action, the goal (first 300 characters), then the full text
  accessibility tree of the page;
- one overview fragment: goal, outcome, start URL and the ordered list of
  (URL, action, first 200 characters of the thought) for every step.

Fragments are saved into a private TAM store (one worker process and one temporary
directory per memory). FTS5 indexes the whole fragment; the embedding model reads its
leading part (URL, thought, action, goal).

**Retrieval (`query`).** TAM recall on the question text only: FTS5 + vector search, RRF
fusion, and — in the `rerank` operating point — re-ranking of the fused window by the
cross-encoder. The top 10 hits are returned as text items, best first, within 48,000
characters in total and 8,000 characters per hit. When a state is longer than its share,
the header is kept and the accessibility tree is cut to the contiguous window of lines
that shares the most (non-stopword) terms with the question.

**Operating points.** `norerank` (cross-encoder off) and `rerank` (cross-encoder on,
waited for before the first query). Everything else is identical.

**What is not used.**
- Screenshots are not indexed and question images are not used for retrieval: the
  memory is text-only. (The harness still passes the question image to the reader.)
- No LLM runs inside the memory: no summarisation, note-taking, query rewriting or
  agentic search. TAM runs in "fast" mode with enrichment and LLM hooks disabled, and no
  API key is present in its process.
- No benchmark metadata reaches the memory (the harness enforces this), and no setting
  was tuned on LongMemEval-V2 questions; all values are general defaults chosen before
  any run.

## Chunk index (`index_mode: "chunk"`, added 2026-09-29)

An opt-in second backend layout; the state layout above stays the default.

**Indexing.** Each state's accessibility tree is cleaned (element ids, the
`visible`/`clickable` flags, nameless layout containers and icon-glyph text removed;
nesting kept as one space per level) and cut into chunks of whole lines of at most 3,000
characters. Every chunk is indexed with the page title and URL path in front of it;
identical chunks (menus and headers repeated on many pages) are indexed once and remember
every state they occur in. Each state also gets a short step fragment (title, URL,
thought, the action that led to it, the next action, goal) and each trajectory the
overview fragment. Embeddings: `bge-m3` (Ollama `bge-m3-8k`, local, through TAM's
OpenAI-compatible embedding provider) over the first 4,000 characters of a fragment.

**Retrieval.** The answer-format sentences (`\boxed{}` / "final answer") are dropped from
the question; TAM recall (FTS5 + vectors + RRF, cross-encoder off) returns the top 200
fragments; hits are folded into states (each hit adds 1/(10 + rank) to every state it
occurs in) and trajectory overviews; at most 2 states per (page title, percent-decoded
URL path) are shown; each state is shown with its header and, when the cleaned page is
longer than 6,000 characters, only its matching chunks plus one chunk on either side
(at most 12,000 characters per state), best first, until 200,000 characters.

**Persistence.** The built index can be saved with the harness's `--save-memory` and
reloaded with `--load-memory-dir` (store copy + adapter state); index parameters must
match, query-time parameters may differ.

**Choices made on benchmark questions (disclosure).** Chunking, the page cap, query
cleaning and the 200,000-character budget were chosen while looking at the retrieved
contexts and accuracies of a 20-question pilot subset (seed 20260928); three budgets were
tried (48k-character state layout, 100k and 200k chunk layout). The configuration was then
fixed and checked on 40 other questions (seed 20260929). No gold answer or evidence
location is read by the memory.

## Models

| Role | Model | Where | Notes |
|---|---|---|---|
| Reader | Qwen3.5-9B, Ollama library build `qwen3.5:9b` (digest `6488c96fa5fa`), **GGUF Q4_K_M quantization** (file type 15, 9.7B parameters, 6.6 GB), served as `qwen3.5-9b` | Ollama 0.34.4, OpenAI-compatible API, Apple M2 Max (Metal) | Same weights as `qwen3.5:9b`; the alias only sets `num_ctx 65536` (Ollama's default context would truncate prompts) and matches the leaderboard's `qwen3.5-9b` name check. Thinking enabled (harness default); temperature 0.6, top-p 0.95, top-k 20 (harness defaults); Ollama's model default presence_penalty 1.5 applies; max completion tokens 20,000. This differs from the paper's bf16 vLLM reader: a 4-bit quantized reader may score lower. |
| Embeddings | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` via fastembed (ONNX, CPU) | local | TAM default |
| Cross-encoder | `Xenova/ms-marco-MiniLM-L-6-v2` via fastembed (ONNX, CPU) | local | `rerank` point only; TAM default |
| Judge | `gpt-5.2`, reasoning effort medium, max 4,096 completion tokens | OpenAI API | harness default; called only for `llm_abstention_checker` / `llm_gotchas_checker` questions |

## Latency

`memory_query_avg_seconds` is the harness's wall-clock time of `memory.query()` (TAM
recall + formatting), measured on one Apple M2 Max (12 CPU cores, 64 GB), CPU inference
for embeddings and the cross-encoder, one query at a time. Index construction is not
part of it.

## Data

LongMemEval-V2 dataset revision `f152293e235517d504809563c833d7190b8c713b`; the text part
of the 200 small-tier trajectories (full `trajectories.jsonl` streamed once and
sha256-verified, then filtered by `haystacks/lme_v2_small.json`). Trajectory screenshot
archives were not downloaded.

## Code

`docs/benchmarks/longmemeval-v2/` (backend `tam_memory.py`, runner `run_tam.py`) and
`docs/benchmarks/tam_bench_common/tam_worker.py` in the TAM repository. The official
harness (commit `2cc8c540bdb87fe6761629b585e727e1c4704520`) is used unmodified.
