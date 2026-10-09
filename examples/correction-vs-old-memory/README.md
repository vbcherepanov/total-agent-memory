# A newer correction versus an older stored memory

## Scenario

An agent saved a fact. Later the user corrected it. What does the memory server do with the old record, and what does recall return afterwards?

1. `memory_save` stores "The staging database is on port 5432" in project `demo`.
2. `memory_save` stores the correction "The staging database is on port 5433" with `supersede=true`.
3. `memory_get` reads both records back.
4. `memory_recall` asks "which port is staging on" in project `demo`.

The same question is then put to the temporal knowledge graph with dated facts:

5. `kg_add_fact` records `staging_database listens_on_port 5432`, valid from 2026-01-10.
6. `kg_add_fact` records `staging_database listens_on_port 5433`, valid from 2026-03-01.
7. `kg_at` asks for the port on 2026-02-01, on 2026-04-01, and now.
8. `kg_timeline` shows the full history of the subject.

Every step is one MCP tool call made in-process. The script runs against a throwaway database in a temporary directory. It never touches `~/.tam`.

## How to run

From the repository root, with the dependencies installed (`pip install -e .` or `./install.sh`):

```bash
python examples/correction-vs-old-memory/run.py 2>/dev/null
```

Server log lines go to stderr. Drop `2>/dev/null` to see them.

The run needs no LLM, no API keys and no network, except that the embedding model (`paraphrase-multilingual-MiniLM-L12-v2`, about 220 MB) is downloaded once on the first use of the server on a machine. It lives in the shared model cache, not in the throwaway database.

The exit code is 0 when every check passes and 1 otherwise.

## Expected output

```
PASS  save: the older fact is stored
      expected: saved=True
      actual:   saved=True id=1
PASS  save: the correction retires the older record
      expected: superseded=[1]
      actual:   superseded=[1]
PASS  get: the older record is marked superseded by the correction
      expected: status=superseded superseded_by=2
      actual:   status=superseded superseded_by=2
PASS  get: the correction stays active
      expected: status=active superseded_by=None
      actual:   status=active superseded_by=None
PASS  recall: the first hit is the correction
      expected: 'The staging database is on port 5433'
      actual:   'The staging database is on port 5433'
PASS  recall: the older value is not returned
      expected: 'The staging database is on port 5432' absent
      actual:   hits=['The staging database is on port 5433']
PASS  kg_at: before the correction date the old port is valid
      expected: ['5432']
      actual:   ['5432']
PASS  kg_at: after the correction date the new port is valid
      expected: ['5433']
      actual:   ['5433']
PASS  kg_at: now only the new port is valid
      expected: ['5433']
      actual:   ['5433']
PASS  kg_timeline: the old assertion is closed on the correction date
      expected: valid_to=2026-03-01T00:00:00Z superseded_by=2379b7dcbf574fbaa521b8d661ec98a7
      actual:   valid_to=2026-03-01T00:00:00Z superseded_by=2379b7dcbf574fbaa521b8d661ec98a7

10 passed, 0 failed
```

The knowledge graph assertion id is random and differs on every run.

## What it demonstrates

- `memory_save` with `supersede=true` detects that the new statement gives a new value for the same statement (same opening words, different trailing value) and retires the older active record. The response lists the retired ids in `superseded`.
- The retired record is kept with `status=superseded` and `superseded_by` pointing at the correction. `memory_history` shows both versions. Nothing is deleted.
- `memory_recall` returns the correction and not the retired value.
- On the temporal knowledge graph, a back-dated correction closes the older assertion on the correction date. `kg_at` returns the old value for a timestamp before that date and the new value after it.

## What it does not demonstrate

- Anything about answer quality. The checks compare stored strings and ids, not the text an LLM would produce from them.
- Automatic contradiction detection. The flag `supersede=true` is set by the caller. Without it both records stay active and ranking decides which comes first (see `tests/test_supersede_values.py`).
- Corrections that change the subject or the relation rather than the value. Those are not treated as updates and nothing is retired.

## Test

The same scenario runs in CI through the same functions:

```bash
python -m pytest tests/test_example_correction_vs_old_memory.py -q
```
