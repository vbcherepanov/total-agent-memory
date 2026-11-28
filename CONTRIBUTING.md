# Contributing

Issues, PRs and benchmark reproductions are all welcome. Docs-only and typo
PRs need no discussion — open them directly. For anything that touches the
save or recall hot path, open an issue first; those two paths carry latency
guarantees and a CI gate, and it is cheaper to agree on an approach than to
rewrite a merged PR.

## Getting help and reporting problems

- **Questions and usage help:** open an issue at
  <https://github.com/vbcherepanov/total-agent-memory/issues>. Questions are
  welcome there.
- **Bugs:** use the same tracker; see [Reporting a bug](#reporting-a-bug) for
  what to include.
- **Security problems:** follow [SECURITY.md](SECURITY.md); do not open a
  public issue.

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

```bash
git clone https://github.com/vbcherepanov/total-agent-memory.git
cd total-agent-memory
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

Python 3.11 or newer. The base install pulls ~97 packages; the reranker
stack lives behind an extra because it resolves torch and the whole
`nvidia-cu*` set:

```bash
.venv/bin/pip install -e ".[rerank]"   # only if you work on reranking
```

The default `MEMORY_MODE=fast` cannot reach that stack anyway — it sets
`MEMORY_RERANK_ENABLED=false` and `MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false`.
Set `MEMORY_MODE=deep` if you need the synchronous LLM path locally.

The team server also runs on PostgreSQL. Its driver lives behind an extra, and
tests that need it skip without it:

```bash
.venv/bin/pip install -e ".[postgres]"
.venv/bin/python -m pytest tests/ --backend=both   # starts a pgvector container via Docker
```

Set `TAM_TEST_PG_URL` to an admin URI instead to use a server you already
run. When you change SQL the team server issues, re-record the translation
corpus with `make dev-image sql-corpus` and commit `tests/fixtures/pg_sql_corpus.jsonl`;
`tests/test_pg_sql_corpus.py` prepares every statement on PostgreSQL.

## Ground rules for a PR

- **`pytest tests/` stays green.** The suite is about 3,700 tests. A PR that leaves it red will not be merged.
- **New tool means new tests.** Every MCP tool has coverage; a tool without
  it is an untested public API.
- **Tests must not read gitignored artifacts.** If a test needs a corpus
  under `benchmarks/data/`, it must `pytest.skip` when the corpus is
  absent. A green suite in this repo means nothing if it goes red on a
  user's machine during `update.sh` — that has happened, more than once.
- **Retrieval changes update the evals.** If you change ranking, fusion or
  scoring, update `evals/scenarios/*.json` and say in the PR what moved.
- **Benchmarks pass `record_usage=False`.** `Recall.search` bumps
  `recall_count`, and the scorer adds a recall boost on top of it. Any
  runner that records usage measures itself: successive runs against one
  database climbed 0.547 → 0.607 R@5 with no retrieval code changing.
- **No hardcoded paths, URLs or secrets.** Configuration goes through env
  vars resolved in `config.py`.

## Architecture, in one paragraph

`src/memory_core/` is deterministic and holds zero LLM calls — embeddings,
vector store, chunker, dedup, cache, graph links, storage. `src/ai_layer/`
holds everything LLM-touching: the enrichment worker, summarizer,
extractors, contradiction detector, reflection. **`ai_layer` may import
from `memory_core`; the reverse is forbidden** and there is a test that
enforces it (`tests/test_v11_layer_separation.py`). New work belongs on
whichever side its dependencies put it.

## Performance

`scripts/memory-bench` measures the hot path; `scripts/memory-perf-gate` exits
non-zero on a p95 regression or on any LLM/network call leaking into
`fast` mode. Run both before submitting anything that touches saving or
searching.

## Commits

Conventional Commits — `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`,
`perf:`, `test:`, `ci:`, `style:`. English, imperative mood, subject under
72 characters. Explain *why* in the body; the diff already says what.

## Reporting a bug

Include the version (`tam --version`), the client (Claude Code, Codex CLI,
Cursor…), `MEMORY_MODE`, and the OS. If it is a retrieval quality issue,
the query and the records you expected back are worth more than a
description of them.

Security issues go through
[private reporting](https://github.com/vbcherepanov/total-agent-memory/security/advisories/new),
not a public issue — see [SECURITY.md](SECURITY.md).
