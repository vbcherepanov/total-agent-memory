# Benchmarks

Measured results for total-agent-memory (TAM) and the commands that reproduce
them. The current release comes first; older measurements follow and say which
version they describe.

- [Current release: LoCoMo retrieval](#current-release-locomo-retrieval)
- [Repeatability](#repeatability)
- [Speed and cost](#speed-and-cost)
- [Reproducing the benchmarks](#reproducing-the-benchmarks)
- [Negative controls](#negative-controls)
- [Earlier measurements (13.0.0)](#earlier-measurements-1300)
- [End-to-end accuracy](#end-to-end-accuracy)
- [Comparison with other systems](#comparison-with-other-systems)
- [Studies](#studies)

## Current release: LoCoMo retrieval

Retrieval asks one question: is a passage that holds the answer among the top K
results? It needs no LLM, so it costs nothing to rerun and does not depend on a
provider's model version.

[LoCoMo](https://github.com/snap-research/locomo) has 10 long conversations
(5,882 turns) and 1,986 questions. 1,536 of them are graded: the 446
adversarial questions and 4 questions without evidence labels are asked but not
scored. Each run writes the turns into a fresh store through `memory_save` and
asks every question through `memory_recall`.

Release 15.0.2, Apple M2 Max (12 cores), no LLM, no API key:

| Setting | R@1 | R@5 | R@10 | Median per query |
|---|---:|---:|---:|---:|
| Default (cross-encoder on) | 0.488 | 0.745 | 0.819 | about 0.9 s |
| `MEMORY_CROSS_RERANK=off` | 0.331 | 0.609 | 0.701 | about 50 ms |

The default setting reranks the fused candidates with a local cross-encoder. It
finds more and takes longer; switching it off is the choice when latency
matters more than recall.

By question category, same runs:

| Category | N | R@5 default | R@5 off | R@1 default | R@1 off |
|---|---:|---:|---:|---:|---:|
| single-hop | 841 | 0.792 | 0.639 | 0.521 | 0.365 |
| temporal | 321 | 0.766 | 0.682 | 0.573 | 0.405 |
| multi-hop | 282 | 0.691 | 0.518 | 0.387 | 0.206 |
| open-domain | 92 | 0.413 | 0.370 | 0.207 | 0.152 |
| **all graded** | **1,536** | **0.745** | **0.609** | **0.488** | **0.331** |

Open-domain questions need knowledge that is not in the conversation, so
retrieval finds their evidence least often.

The latency column is from the 15.0.0 runs on a quiet machine. The 15.0.2 runs
shared the machine with other work, so their timings are not used; the
retrieval code did not change between the two releases and the rankings agree
(see below). Per-question rows, run reports and commands are in
[benchmarks/results/rerank-modes](../benchmarks/results/rerank-modes/README.md).

## Repeatability

Two runs of 15.0.2 on fresh stores returned the same ranked list for every one
of the 1,986 questions, with the cross-encoder off and with the default setting
(model loaded before the first query). On 14.6.0 three runs per setting (off,
default, on) did the same.

Two things can change a ranking between runs. Both are known, and the runner
controls both:

1. **Cross-encoder not loaded yet.** With the default setting the model loads
   in the background. A query that arrives before it is ready returns the fused
   order without reranking. On 14.6.0 this affected the first one or two
   queries of a process. `--wait-for-encoder` loads the model first, and every
   report counts the queries answered without it.
2. **The calendar date.** A question such as "What did he start a few years
   ago?" is resolved against today's date, as a user would expect. One LoCoMo
   question of 1,986 contains such a phrase with a window that moves across the
   conversation dates, so its top ten are ordered differently on 17 and 28
   November 2026. The ten records are the same and no metric changes.
   `--relative-date-anchor 2026-11-17T09:00` pins the date: with it, a run of
   28 November returned the rankings of 17 November for all 1,986 questions.

Every runner for the public benchmarks also passes `record_usage=False`. A
search normally counts how often a record was returned and ranks frequently
used records slightly higher; without the flag a second run against the same
store would score better than the first.

## Speed and cost

No LLM is called on write or search in the default profile, so the cost of
both is local CPU time.

| Operation | Measured | Where |
|---|---|---|
| Write one record (`memory_save`) | 31 ms median at 10,000 records, 43 ms at 100,000, 52 ms at 1,000,000 | [scale study](benchmarks/scale-v14/RESULTS.md), 14.3.1 |
| Write a benchmark corpus | 5,882 LoCoMo turns in 288 to 320 s, 18 to 20 turns per second, including embedding and graph linking | 15.0.0 and 15.0.2 run reports |
| Search, cross-encoder off | 53 ms median on LoCoMo; 19 to 105 ms within one project at 10,000 to 1,000,000 records | 15.0.0 run report; scale study |
| Search, default | about 0.9 s median on LoCoMo, CPU only | 15.0.0 run report |
| API spend for a retrieval benchmark | none | |
| API spend for an LLM-judged benchmark | 1.71 USD for 400 LongMemEval questions; about 40 USD for the whole head-to-head study | [longmemeval-v14-9](benchmarks/longmemeval-v14-9/RESULTS.md), [head-to-head](benchmarks/head-to-head-v14/RESULTS.md) |

All timings are from an Apple M2 Max with 12 cores and depend on the CPU.

## Reproducing the benchmarks

Use a source checkout with the development environment from the
[README](../README.md#running-the-tests). The datasets are not in the
repository because each has its own licence. Put them here:

| Benchmark | Source | Expected path |
|---|---|---|
| LoCoMo | [snap-research/locomo](https://github.com/snap-research/locomo) | `benchmarks/data/locomo/data/locomo10.json` |
| LongMemEval | [xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) | `benchmarks/data/longmemeval_s.json` |
| BEAM | [mohammadtavakoli78/BEAM](https://github.com/mohammadtavakoli78/BEAM) | parquet shards in `benchmarks/data/beam/` |

The table at the top of this page:

```bash
export MEMORY_LLM_ENABLED=false
.venv/bin/python benchmarks/locomo_rerank_modes.py --db-root /tmp/tam-locomo/stores --out-dir /tmp/tam-locomo/out --mode off --label run1
.venv/bin/python benchmarks/locomo_rerank_modes.py --db-root /tmp/tam-locomo/stores --out-dir /tmp/tam-locomo/out --mode off --label run2
.venv/bin/python benchmarks/locomo_rerank_modes.py --db-root /tmp/tam-locomo/stores --out-dir /tmp/tam-locomo/out --mode auto --label warm-run1 --wait-for-encoder --warmup-queries 3
.venv/bin/python benchmarks/locomo_rerank_modes.py --compare /tmp/tam-locomo/out/worktree-off-run1.rows.jsonl.gz /tmp/tam-locomo/out/worktree-off-run2.rows.jsonl.gz
```

A run with the cross-encoder off takes about 7 minutes, a default run about 35.
The last command prints how many questions got a different ranking; the
expected value is 0.

The other retrieval runners, also without an API key:

```bash
.venv/bin/python benchmarks/locomo_bench.py --wipe            # LoCoMo with negative controls
.venv/bin/python benchmarks/longmemeval_bench.py --modes store
.venv/bin/python benchmarks/beam_bench.py --scale 100K --wipe
```

The LLM-judged runners (`benchmarks/locomo_bench_llm.py`,
`benchmarks/locomo_qa.py`, `benchmarks/longmemeval_qa.py`,
`benchmarks/crossgrade_mem0.py`) need an API key for the answering and judging
models. Each study report has a "Reproduce" section with its exact commands and
the expected spend.

## Negative controls

A retrieval score needs a floor. `benchmarks/locomo_bench.py` scores three
strategies that use no retrieval at all, on the same 1,536 questions:

| Strategy | R@1 | R@5 | R@10 |
|---|---:|---:|---:|
| random: ten turns from the same conversation | 0.001 | 0.012 | 0.023 |
| first: the ten earliest turns | 0.000 | 0.023 | 0.039 |
| recency: the ten most recent turns | 0.001 | 0.003 | 0.011 |
| TAM 15.0.2, cross-encoder off | 0.331 | 0.609 | 0.701 |
| TAM 15.0.2, default | 0.488 | 0.745 | 0.819 |

The controls do not depend on the TAM version. They are computed in the same
pass as the metric and saved in
[v13-locomo-retrieval.json](../benchmarks/results/v13-locomo-retrieval.json).

## Earlier measurements (13.0.0)

These tables were measured on 13.0.0 (2026-08-27), before the cross-encoder
became the default. They describe retrieval **without** the cross-encoder and
have not been repeated on 15.0.

### LongMemEval

[LongMemEval](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned):
470 questions of six types. Each question's history is written into a store and
queried with the same search an agent uses.

| Question type | N | R@5 (any gold session) |
|---|---:|---:|
| knowledge-update | 72 | 100.0% |
| multi-session | 121 | 98.3% |
| single-session-user | 64 | 95.3% |
| single-session-assistant | 56 | 94.6% |
| temporal-reasoning | 127 | 92.9% |
| single-session-preference | 30 | 80.0% |
| **total** | **470** | **95.1%** |

With every gold session required in the top 5, the figure is 85.7%; NDCG@5 is
88.9%. Until 13.0.0 this runner used a separate retrieval stack of its own and
reported 96.2%; `--modes store` runs the product's search and is the default.

Reproduce: `python benchmarks/longmemeval_bench.py --modes store`. Saved run:
[longmemeval-2026-08-27-v13-store.json](../evals/longmemeval-2026-08-27-v13-store.json).

### BEAM

[BEAM](https://github.com/mohammadtavakoli78/BEAM) uses conversations of
100,000, 500,000 and 1,000,000 tokens and probes ten memory abilities. A probe
counts as found when one of its source messages is in the top K.

| Scale | Conversations | Messages | Probes | R@1 | R@5 | R@10 |
|---|---:|---:|---:|---:|---:|---:|
| 100K | 20 | 5,732 | 355 | 0.313 | 0.575 | 0.651 |
| 500K | 35 | 38,058 | 629 | 0.280 | 0.490 | 0.596 |
| 1M | 35 | 74,630 | 625 | 0.227 | 0.448 | 0.578 |

R@5 by ability:

| Ability | 100K | 500K | 1M |
|---|---:|---:|---:|
| contradiction_resolution | 1.000 | 0.943 | 0.871 |
| temporal_reasoning | 0.975 | 0.786 | 0.686 |
| knowledge_update | 0.925 | 0.855 | 0.886 |
| multi_session_reasoning | 0.675 | 0.614 | 0.429 |
| information_extraction | 0.625 | 0.443 | 0.371 |
| summarization | 0.444 | 0.286 | 0.288 |
| preference_following | 0.282 | 0.300 | 0.246 |
| event_ordering | 0.150 | 0.029 | 0.157 |
| instruction_following | 0.075 | 0.157 | 0.086 |

Retrieval by similarity to the question does poorly on `event_ordering` and
`instruction_following`: those probes ask in what order things happened or
whether an instruction was followed, and the message that matters does not
resemble the question.

On 13.0.0 the median search time grew from 18 ms at 100K to 412 ms at 1M, and
ingest slowed from 25.6 to 5.0 messages per second. Both were addressed later:
the ingest slowdown was a cache rebuilt on every save (fixed in 13.0.1), and
the [scale study](benchmarks/scale-v14/RESULTS.md) of 14.3.1 measures a median
of 105 ms for a search within one project at 1,000,000 records.

Reproduce: `python benchmarks/beam_bench.py --scale 100K --wipe`. Saved runs:
[100K](../benchmarks/results/v13-beam-100K.json),
[500K](../benchmarks/results/v13-beam-500K.json),
[1M](../benchmarks/results/v13-beam-1M.json).

## End-to-end accuracy

In an end-to-end run a model answers from the retrieved context and a second
model judges the answer. The result depends on both models and on the prompts,
so it is not a property of the memory alone.

LoCoMo on 13.0.0, gpt-4o answering, gpt-4o-mini judging, three seeds:

| Category | N | Mean | Min | Max |
|---|---:|---:|---:|---:|
| single-hop | 841 | 0.570 | 0.567 | 0.573 |
| temporal | 321 | 0.426 | 0.424 | 0.427 |
| multi-hop | 282 | 0.366 | 0.358 | 0.372 |
| open-domain | 96 | 0.292 | 0.281 | 0.302 |
| adversarial | 446 | 0.904 | 0.899 | 0.908 |
| **all but adversarial** | 1,540 | 0.486 | 0.484 | 0.488 |
| **all** | 1,986 | 0.579 | 0.578 | 0.582 |

The saved files of that release name three categories wrongly (282 as
single-hop, 96 as multi-hop, 841 as open-domain). The table above uses the
names of the dataset's category numbers.

Retrieval was identical in the three runs; only answering and judging varied.
Between seeds the answer text differed on 12.5% of the questions, the verdict
on 5.1%, and on 2.7% the judge gave a different verdict to the same answer. One
LLM-judged run is therefore a sample, and we report several.

The judge needed two fixed rules, applied in code at judging time:

- On categories 1 to 4 the gold answer is a fact, so a refusal cannot be
  correct. Without this rule the judge accepted "Not mentioned in the
  conversation" on about 100 questions per run, which raised accuracy by 3.2
  points (single-hop) to 14.3 points (temporal).
- 99.6% of the adversarial gold answers are empty, so only a refusal can be
  correct there. Without this rule 27 to 30 invented answers per run were
  accepted.

Together the rules lowered the figures from 0.551 to 0.486 (all but
adversarial) and from 0.966 to 0.904 (adversarial).

Saved runs:
[v13-locomo-llm-3seeds.json](../benchmarks/results/v13-locomo-llm-3seeds.json).
Runner: [locomo_bench_llm.py](../benchmarks/locomo_bench_llm.py). Later
end-to-end results, under the protocols that published figures use, are in the
[head-to-head report](benchmarks/head-to-head-v14/RESULTS.md) and in
[longmemeval-v14-9](benchmarks/longmemeval-v14-9/RESULTS.md).

## Comparison with other systems

Numbers from different systems are comparable only when the answering model,
judge, prompts and question set match. Mem0 publishes 92.5 on LoCoMo and 94.4
on LongMemEval from its managed platform, with gpt-5 answering and judging, and
it also publishes the per-question answers behind them. Those answers were
graded next to TAM's answers to the same held-out questions under the same
judge. At the same answering model, no difference between the two systems was
statistically significant on either benchmark. That is not a demonstrated
equivalence; the protocol, the tuning history and the differences from the
earlier public protocol are in the
[head-to-head report](benchmarks/head-to-head-v14/RESULTS.md). Where a project
has not published per-question answers, we do not compare against its number.

## Studies

Each report states its version, setup, commands and limits.

| Study | What it measures | Report |
|---|---|---|
| LoCoMo by cross-encoder setting (14.6.0 to 15.0.2) | retrieval, latency and run-to-run identity | [rerank-modes](../benchmarks/results/rerank-modes/README.md) |
| Head-to-head with Mem0 Platform (14.5.0) | LoCoMo and LongMemEval-S accuracy on held-out questions, two judges each | [head-to-head-v14](benchmarks/head-to-head-v14/RESULTS.md) |
| LongMemEval-S after the 14.8.0 retrieval work (14.9) | answer accuracy on the held-out split | [longmemeval-v14-9](benchmarks/longmemeval-v14-9/RESULTS.md) |
| Recall pipeline changes (14.9) | six changes to `memory_recall`, each measured before and after | [pmb-adoption-recall](benchmarks/pmb-adoption-recall/RESULTS.md) |
| Recall tier ablation (14.8.0) | each retrieval tier alone and switched off | [tier-ablation-v14](benchmarks/tier-ablation-v14/RESULTS.md) |
| Context budget fill (14.7.0) | how often the assembled context holds the evidence | [context-fill-v14](benchmarks/context-fill-v14/RESULTS.md) |
| Organisational memory (14.6.0) | access isolation between departments, concurrent updates, SQLite and PostgreSQL | [org-memory-v14-20260925](benchmarks/org-memory-v14-20260925/RESULTS.md) |
| LoCoMo and LongMemEval QA (September 2026, against 14.3.1) | accuracy under the protocols the public numbers use | [qa-v14](benchmarks/qa-v14/RESULTS.md) |
| Scale (14.3.1, 14.4.0) | recall and save latency, HTTP throughput at 10,000 to 1,000,000 records | [scale-v14](benchmarks/scale-v14/RESULTS.md) |
| MemoryAgentBench FactConsolidation (14.3.1, 14.4.0) | whether updated facts replace old ones | [memoryagentbench](benchmarks/memoryagentbench/FINDINGS.md) |
| Knowledge update (14.2.0, 14.3.0) | `memory_answer` on facts that change over time | [knowledge-update-v14](benchmarks/knowledge-update-v14/RESULTS.md) |
| AMA-Bench | TAM as a memory method on agent trajectories | [ama-bench](benchmarks/ama-bench/README.md) |
| Release verification (14.0.0) | test counts, platform checks, artifact hashes | [release-final-v14-20260915](benchmarks/release-final-v14-20260915/RESULTS.md) |
| CPU use of the optional BGE reranker (14.0.0) | one-thread latency on Linux ARM64 | [grounded-v14](benchmarks/grounded-v14/CPU_RESULTS.md) |
