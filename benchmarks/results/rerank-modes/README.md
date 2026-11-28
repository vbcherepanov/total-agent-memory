# LoCoMo retrieval by cross-encoder mode

Per-question rows (`*.rows.jsonl.gz`) and run reports (`*.json`) from
[`benchmarks/locomo_rerank_modes.py`](../../locomo_rerank_modes.py). Every run
ingests locomo10 into a fresh store, asks all 1,986 questions and grades the
1,536 that are not adversarial and have evidence. No LLM is involved.

## v15.0.2 (commit f373d21), 2026-11-28

Same machine and packages as v15.0.0 below. Runs were serial. Other heavy jobs
ran on the machine (load average 8 to 16 during the `off` runs and 113 to 174
during the `auto` runs, on 12 cores), so **latency in these reports is not a
measurement**; `lane_note` in the reports understates the load, the
`load_average_*` fields record it. The retrieval code is the same as in
v15.0.0, whose latency was measured on a quiet machine. Commands:

```
MEMORY_LLM_ENABLED=false python benchmarks/locomo_rerank_modes.py --tree . --version-label v15.0.2 --commit f373d21 --db-root <store root> --mode off --label run1
MEMORY_LLM_ENABLED=false python benchmarks/locomo_rerank_modes.py --tree . --version-label v15.0.2 --commit f373d21 --db-root <store root> --mode off --label run2
MEMORY_LLM_ENABLED=false python benchmarks/locomo_rerank_modes.py --tree . --version-label v15.0.2 --commit f373d21 --db-root <store root> --mode auto --label warm-run1 --wait-for-encoder --warmup-queries 3
```

and the same `auto` command with `--label warm-run2`.

| Condition | Runs | R@1 | R@5 | R@10 | Queries before encoder ready |
|---|---:|---:|---:|---:|---:|
| `off` | 2 | 0.3314 | 0.6094 | 0.7012 | all |
| `auto`, warm | 2 | 0.4883 | 0.7454 | 0.8190 | 0 |

- The two runs of each condition returned the same ranking for every one of
  the 1,986 questions (`v15.0.2-off-compare-run1-run2.json`,
  `v15.0.2-auto-warm-compare-run1-run2.json`).
- All figures equal those of v15.0.0.
- Ingest: 5,882 turns in 300 to 320 s (18.4 to 19.6 turns per second) in the
  first three runs and 432 s in the fourth, against 288 to 292 s on the quiet machine for v15.0.0.

### One question depends on the calendar date

Against the v15.0.0 runs of 2026-11-17, each v15.0.2 run of 2026-11-28 ranks
one question differently, the same one in both settings
(`v15.0.2-*-compare-v15.0.0-*.json`, position 1594): "What hobby did Evan
start practicing a few years ago that he enjoys?". The ten records are the
same, their order differs, and no metric changes.

The cause is the phrase "a few years ago". Recall resolves relative dates
against the current date, which is right for a user asking today. The phrase
becomes a 60-day window around the date three years back: 2023-10-19 to
2023-12-18 on 17 November 2026 and 2023-10-30 to 2023-12-29 on 28 November
2026. A LoCoMo session dated 2023-12-26 is outside the first window and inside
the second, and records inside the window are ranked higher.

`--relative-date-anchor 2026-11-17T09:00` makes the runner resolve relative
dates from that day. A v15.0.2 `off` run of 2026-11-28 with this option returns
the same ranking as the v15.0.0 run of 2026-11-17 for all 1,986 questions
(`v15.0.2-off-anchored-20261117-compare-v15.0.0-off-run1.json`). The product is
not changed.

## v15.0.0 (commit dc8ecf9), 2026-11-17

Apple M2 Max, 12 cores, macOS 27.2.1, Python 3.13.5, onnxruntime 1.30.0,
fastembed 0.8.0. One run per condition, serial, onnxruntime default threads.
The default cross-encoder is now multilingual
(`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, INT8 ONNX on ARM64). Commands:

```
MEMORY_LLM_ENABLED=false python benchmarks/locomo_rerank_modes.py --tree . --version-label v15.0.0 --commit dc8ecf9 --db-root <store root> --mode auto --label warm-run1 --wait-for-encoder --warmup-queries 3
MEMORY_LLM_ENABLED=false python benchmarks/locomo_rerank_modes.py --tree . --version-label v15.0.0 --commit dc8ecf9 --db-root <store root> --mode off --label run1
```

| Condition | Runs | R@1 | R@5 | R@10 | p50 ms | p95 ms | Queries before encoder ready |
|---|---:|---:|---:|---:|---:|---:|---:|
| `off` | 1 | 0.3314 | 0.6094 | 0.7012 | 53 | 68 | all |
| `auto`, warm | 1 | 0.4883 | 0.7454 | 0.8190 | 901 | 1122 | 0 |

- Against v14.6.0 with the English `Xenova/ms-marco-MiniLM-L-6-v2`, the
  multilingual default raises R@1 (0.4733 to 0.4883), keeps R@5 (0.7461 to
  0.7454) and lowers R@10 (0.8262 to 0.8190), at a similar median.
- The `off` figures match v14.10.0 (`main 7d41e51` below) within one question
  at R@1 and exactly at R@5 and R@10.

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
