# LoCoMo retrieval by cross-encoder mode

Per-question rows (`*.rows.jsonl.gz`) and run reports (`*.json`) from
[`benchmarks/locomo_rerank_modes.py`](../../locomo_rerank_modes.py). Every run
ingests locomo10 into a fresh store, asks all 1,986 questions and grades the
1,536 that are not adversarial and have evidence. No LLM is involved.

## v14.6.0 (commit 7482d8a), 2026-11-07

Apple M2 Max, 12 cores, macOS 27.2, Python 3.13.5, onnxruntime 1.30.0,
fastembed 0.8.0. Runs were serial, one at a time, with onnxruntime default
threads; other desktop work ran on the machine, so latency is not a quiet-machine
figure. Command:

```
benchmarks/locomo_rerank_matrix.sh <v14.6.0 tree> v14.6.0 7482d8a <store root>
```

| Condition | Runs | R@1 | R@5 | R@10 | p50 ms | p95 ms | Queries before encoder ready |
|---|---:|---:|---:|---:|---:|---:|---:|
| `off` | 3 | 0.3327 | 0.6107 | 0.7005 | 26-29 | 71-100 | all |
| `auto`, cold | 3 | 0.4727 | 0.7461 | 0.8262 | 968-976 | 1127-1149 | 2 |
| `auto`, warm | 3 | 0.4733 | 0.7461 | 0.8262 | 954-1000 | 1091-1245 | 0 |
| `on` | 3 | 0.4733 | 0.7461 | 0.8262 | 953-1019 | 1097-1211 | 1 (waits) |

- **cold** is the install default: `MEMORY_CROSS_RERANK` unset, first query
  right after ingest. **warm** loads the encoder (`--wait-for-encoder`) and asks
  three questions that are not in LoCoMo (`--warmup-queries 3`, 3.1-3.4 s)
  before the benchmark.
- In `on` mode the first query finds the encoder not loaded and waits for it, so
  it is counted as asked before ready but is reranked.
- The three runs of each condition returned the same ranking for every one of
  the 1,986 questions (`*-compare-run1-runN.json`).
- Cold and warm `auto` differ in one question, the first one asked: it is
  answered with the fused order. That changes R@1 by one question and leaves
  R@5 unchanged. Warm `auto` and `on` are identical on all questions.
- The `off` runs ran three at a time on 2026-11-04, so their latency is not
  solo. Their reports say 1,535 graded: the runner then lost one of the two
  labels of a question that locomo10 asks twice. The figures above are regraded
  from the saved rows with the fixed runner; rankings are unchanged.

## main 7d41e51 (v14.10.0), 2026-11-04

`off` only, three runs at a time: R@1 0.3307, R@5 0.6094, R@10 0.7012 on 1,536
questions (regraded from the rows as above), identical rankings across the three
runs.

Compare two runs with:

```
python benchmarks/locomo_rerank_modes.py --compare a.rows.jsonl.gz b.rows.jsonl.gz
```
