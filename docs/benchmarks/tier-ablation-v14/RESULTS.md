# Recall tier ablation (14.8.0)

`memory_recall` fuses the ranked lists of its retrieval tiers with weighted Reciprocal Rank Fusion.
Until 14.8.0 the weights were constants in the server and no measurement said what each tier was
worth. This page measures every tier on its own and switched off, on two benchmark corpora and on
a copy of an installed store, with [`benchmarks/tier_ablation.py`](../../../benchmarks/tier_ablation.py).

No LLM is involved. The metric is the rank of the first gold evidence record in the ranked list
(R@1, R@5, R@10, MRR), so it measures candidate generation and fusion, not answers.

Summary: FTS and the semantic tier are the pipeline; each loses 3-10 points of R@10 alone. The graph
and representation tiers only exist on an enriched store, and both had defects (fixed in 14.8.0).
Retuned weights and the new `multi_query` tier change the held-out numbers by less than the noise.

## Setup

- Script: `benchmarks/tier_ablation.py`. One store per unit (a LoCoMo conversation, a LongMemEval-S
  question, or the copied installed store), built through the same `Store.save_knowledge` as
  `memory_save` with precomputed embeddings; searches go through the same `Recall.search` as the
  server with `record_usage=False`, so no query influences the next one. Cross-encoder off, to see
  the tiers rather than the reranker.
- Configurations: `full` (defaults), `no_<tier>` (that tier's weight 0, which skips it), `only_<tier>`.
- **LoCoMo**: conversations 0-2 (dev, 383 questions with evidence) and 3-9 (held-out, 1,153), one
  record per turn, gold = the `evidence` turn ids.
- **Live**: a copy of the author's store (`~/.tam`, 5,426 active records, 203,708 graph edges,
  10,326 representations, 602 sessions). 150 records that carry a `context` field, chosen with seed
  1: the query is the context (the "why" written at save time), the gold is the record itself. This
  is harder than ordinary use, where queries are short topic phrases, but it is the only probe with
  gold on real data and the only one where the graph, representation and episode tiers can fire.
- TAM at `db3086e` plus the 14.8.0 changes, default multilingual embedding
  (`paraphrase-multilingual-MiniLM-L12-v2`), Python 3.13, Apple M2 Max.

## LoCoMo

| config | dev R@10 | dev MRR | held-out R@10 | held-out MRR | held-out multi-hop R@10 |
|---|---:|---:|---:|---:|---:|
| full | **0.689** | 0.423 | **0.672** | 0.439 | 0.572 |
| no_fts | 0.632 | 0.365 | 0.637 | 0.376 | 0.596 |
| no_semantic | 0.637 | 0.434 | 0.637 | 0.440 | 0.471 |
| only_fuzzy | 0.034 | 0.023 | 0.030 | 0.021 | 0.010 |
| no_hyde, no_multi_repr, no_fuzzy, no_graph, no_episode, no_atomic_facts | = full | = full | = full | = full | = full |

FTS and the semantic tier are complementary: each alone loses 3.5 points of R@10 on the held-out
split, and multi-hop questions need the semantic tier most (0.572 to 0.471 without it). Every other
tier is identical to `full` because it never fires on a benchmark store: there are no LLM-made
representations, no extracted triples, no episodes and no explicit relations. The fuzzy tier only
runs when fewer than the candidate pool is found, which never happens here.

Full tables: [`raw/locomo_dev.json`](raw/locomo_dev.json), [`raw/locomo_test.json`](raw/locomo_test.json).

## Live store

Three runs of the same 150 queries, as the code changed ([`raw/live_tam.json`](raw/live_tam.json),
[`raw/live_tam2.json`](raw/live_tam2.json), [`raw/live_tam3.json`](raw/live_tam3.json)):

| config | R@10, first graph tier | R@10, specificity + cap | R@10, final (+ representation fix) | p50 ms, final |
|---|---:|---:|---:|---:|
| full | 0.133 | 0.167 | 0.167 | 128 |
| no_graph | 0.167 | 0.173 | 0.187 | 80 |
| no_multi_repr | 0.133 | - | 0.180 | 90 |
| only_fts | 0.260 | - | - | 18 |
| fts=2.0 | - | - | 0.200 | 119 |

Tier fired on `full`: fts 1.00, semantic 1.00, multi_repr 0.87, graph 0.86, multi_query 0.10, the rest 0.

What the probe found, and what changed because of it:

1. **The graph tier never fired before 14.8.0.** It read the `relations` table, which only
   `memory_relate` writes (one row on this store). The new tier walks `knowledge_nodes` and
   `graph_nodes`. Its first version fired on 86% of queries, cost 240 ms and made `full` worse than
   `no_graph` (0.133 against 0.167): rank fusion is rank-based, so a long graph list competed as an
   equal with FTS and the semantic tier. Node specificity (1 / ln(1 + degree)), keeping only
   candidates within half of the best graph score, at most `limit` of them from the 16 most
   specific seed nodes, brought it to 0.167 at +48 ms; the default weight is 0.3, where it is
   neutral on this probe and still adds one-hop neighbours for multi-hop questions.
2. **The multi-representation tier compared the query with an arbitrary subset.** It fetched the
   first 100 representation rows in table order and scored those. On this store (10,326
   representations) the tier fired on 87% of queries, cost about 230 ms and added noise. It now
   scores every row of the scope with one matrix product (17 ms for the four representation types).
3. **FTS alone beats the fusion on this probe** (0.260 against 0.167 for `full`). A record's own
   rationale shares its vocabulary with the record, which favours lexical search; on LoCoMo, where
   questions are paraphrases, the fusion wins by 3.5 points. The defaults were not moved towards FTS
   on the strength of this probe alone; see the weight tuning below.

## Weight tuning

`--tune` runs coordinate ascent over the grid 0, 0.5, 1, 1.5, 2 for every tier, two passes, on the
LoCoMo dev split, objective R@10 then MRR ([`raw/locomo_dev_tune.json`](raw/locomo_dev_tune.json)).
It moved three weights: `fts` 1.0 to 2.0, `semantic` 1.2 to 2.0 (so the two main tiers end up
equal, and the smaller tiers count half as much), `multi_query` 0.8 to 0. Checked on the held-out
split ([`raw/locomo_test_tuned.json`](raw/locomo_test_tuned.json)):

| config | dev R@10 | dev MRR | held-out R@10 | held-out MRR |
|---|---:|---:|---:|---:|
| defaults | 0.689 | 0.423 | 0.671 | 0.439 |
| tuned (fts=2, semantic=2, multi_query=0) | 0.692 | 0.431 | 0.675 | 0.444 |
| fts=1, semantic=1 | - | - | 0.674 | 0.443 |
| defaults, multi_query=0 | - | - | 0.672 | 0.439 |

The held-out gain is 0.4 points of R@10 on 1,153 questions, inside the noise of the split (about
1.4 points at one standard error), so the defaults were left as they were. The `multi_query` tier
fires on 5.6% of LoCoMo questions and changes R@10 by 0.1 point either way; its value is decided
on LongMemEval-S below.

## LongMemEval-S, held-out

The first 100 questions of the held-out id list of [head-to-head-v14](../head-to-head-v14/RESULTS.md)
(abstention questions excluded), one store per question, one record per turn (turns above 2,000
characters split), gold = the turns flagged `has_answer`
([`raw/lme_heldout100.json`](raw/lme_heldout100.json); an earlier pass over the first 48 is in
[`raw/lme_heldout48.json`](raw/lme_heldout48.json)):

| config | R@1 | R@5 | R@10 | MRR | multi-session R@10 (24) | preference R@10 (3) | temporal R@10 (25) |
|---|---:|---:|---:|---:|---:|---:|---:|
| full | 0.530 | 0.850 | **0.900** | 0.668 | 0.833 | 1.000 | 0.800 |
| no_fts | 0.400 | 0.770 | 0.820 | 0.559 | 0.833 | 0.333 | 0.680 |
| no_semantic | 0.580 | 0.830 | 0.880 | 0.687 | 0.792 | 1.000 | 0.800 |
| multi_query on (0.8) | 0.530 | 0.830 | 0.900 | 0.666 | 0.833 | 1.000 | 0.800 |
| tuned (fts=2, semantic=2) | 0.600 | 0.840 | 0.890 | 0.705 | 0.792 | 1.000 | 0.800 |

The same picture as LoCoMo: FTS carries single-session questions (preference questions go from
1.000 to 0.333 without it), the semantic tier carries multi-session ones. `multi_query` changes
nothing at R@10 and lowers R@5 by two points while adding about 20 ms on the questions where it
fires, so it ships off by default (`MEMORY_RECALL_TIER_WEIGHTS=multi_query=0.8` turns it on).
The tuned weights raise R@1 and MRR here as on LoCoMo (0.530 to 0.600, 0.668 to 0.705) and lower
R@10 by one point; the reader gets the whole fused window, so R@10 is the number that matters and
the defaults stay. `directives` never fires: the corpus has no convention records.

## User-turn boost for advice-shaped questions

LongMemEval-S preference questions ask for a recommendation ("suggest accessories for my
photography setup"); the gold turn is what the user said earlier about their gear, which rarely
shares words with the request, and the assistant's replies on the same topic outrank it. For
advice-shaped questions the fused score of user turns (`[date] user: ...`) is multiplied by
`MEMORY_RECALL_USER_TURN_BOOST`. All 30 preference questions of the dataset, one store each
([`raw/lme_pref_boost1.0.json`](raw/lme_pref_boost1.0.json),
[`raw/lme_pref_boost1.3.json`](raw/lme_pref_boost1.3.json)):

| boost | R@1 | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|
| 1.0 (off) | 0.333 | 0.633 | 0.700 | 0.460 |
| 1.3 (default) | **0.400** | **0.667** | 0.700 | **0.509** |

On the 100 held-out questions of all types the boost changed R@5 from 0.840 to 0.850 and nothing
else, so it costs no accuracy elsewhere. The three preference questions that stay outside the top
10 are ones whose gold turn names a product the request does not hint at.

## Cross-encoder window

Everything above runs with the cross-encoder off. With it on (`MEMORY_CROSS_RERANK=on`, default
model `Xenova/ms-marco-MiniLM-L-6-v2`, 400 characters of neighbouring turns per candidate), LoCoMo
held-out, 1,153 questions ([`raw/locomo_test_ce50.json`](raw/locomo_test_ce50.json),
[`raw/locomo_test_ce100.json`](raw/locomo_test_ce100.json)):

| `MEMORY_CROSS_RERANK_WINDOW` | R@1 | R@5 | R@10 | MRR | multi-hop R@10 | p50 ms |
|---|---:|---:|---:|---:|---:|---:|
| off | 0.314 | 0.586 | 0.672 | 0.439 | 0.572 | 27 |
| 50 (default) | 0.458 | 0.739 | 0.814 | 0.579 | 0.721 | 1,014 |
| 100 | 0.464 | 0.761 | **0.832** | 0.589 | 0.750 | 1,990 |

The cross-encoder is worth 14 points of R@10, far more than any tier weight. A window of 100 adds
1.8 points (2.9 on multi-hop) and doubles the latency on a CPU, so the default stays at 50; the
benchmark adapters and any install with a GPU or a patient agent can set
`MEMORY_CROSS_RERANK_WINDOW=100`.

## Reproduce

```bash
.venv/bin/python benchmarks/tier_ablation.py --bench locomo --split dev --work-root "$(mktemp -d)" --output /tmp/locomo_dev.json
.venv/bin/python benchmarks/tier_ablation.py --bench locomo --split test --work-root "$(mktemp -d)" --output /tmp/locomo_test.json
.venv/bin/python benchmarks/tier_ablation.py --bench live --store ~/.tam --sample 150 --work-root "$(mktemp -d)" --output /tmp/live.json
```

The LoCoMo file goes to `benchmarks/data/locomo/data/locomo10.json` as for the other QA runs. The
live probe copies the store first and never writes to `~/.tam`.
