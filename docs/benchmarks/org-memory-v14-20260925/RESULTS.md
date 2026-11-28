# Organisational memory on the team server, measured on 14.5.1, fixes released in 14.6.0 (tag v14.6.0, 7482d8a), 2026-09-25

**Status: DONE_WITH_CONCERNS.** All four experiments ran. E2b (LLM answer accuracy) was skipped. Two bugs were found and fixed as separate patches with regression tests. The fixes were released in 14.6.0. Timing numbers were taken on a shared machine with other jobs running (load is shown next to every timing table). The synthetic data is easy for retrieval (see E2a), so retrieval quality numbers are an upper bound, not a field result.

## What was measured, in one table

| Question | Result (base code 95ea2b8) | After patches 01+02 |
|---|---|---|
| E1: foreign department records returned, all 9 attempt types, 12 users, 2,532 attack calls | **0** | **0** |
| E1: same attempts 1, 2, 3, 5 on one local store, no client filter | 1,176 of 1,176 calls returned foreign-tagged records (1,149 with a foreign canary) | not re-run (patches do not touch the local store) |
| E1 control: own gold record in top 5 | 99.5% (1,194 of 1,200) | 99.5% |
| E2a new hire hit@5: not a member / reader / team scope only | 0.00 / 1.00 / 1.00 | 0.00 / 1.00 / 1.00 |
| E2 transfer: requests sent after the change that saw the old team | 0 of 170 | 0 of 164 |
| E2 write during membership removal: client got an error but the write was committed | **16 of 40** (bug 01) | **0 of 40** (15 committed and reported 200; 25 rejected and not committed) |
| E2 offboarding without the user's token file | not possible: only membership removal; shared stays writable (bug 02) | `user-disable`: 3 of 3 requests 401, new token refused |
| E3 concurrent edits, 2/4/8/16 clients x 100 rounds: rounds with exactly one success | 400 of 400, 0 lost updates | 400 of 400, 0 lost updates |
| E3 retries with the same request_id after a timeout: duplicates | 0 of 120 | 0 of 120 |
| E4 warm search p50, 8 scopes: team server, 8 workers / one store | 2,938 ms / 459 ms | not re-run (patches do not touch the read path) |
| E4 worker RSS sum, 8 scopes: 8 workers / one store | 9,035 MiB / 1,290 MiB | not re-run |
| E4c after cross-scope rerank (14.6.0), 8 scopes, 8 workers: warm p50 / CPU s per query | 2,962 ms / 16.8 s (main before the change) | 652 ms / 3.1 s; single-area results identical for 400 of 400 questions |
| E4b 4 users, 20 searches each, 3 workers: cold share and p50, users taking turns / users in blocks | 100% at about 1,970 ms / 5% at about 620 ms | not re-run (patches do not touch the read path) |

## Environment

- Code: base commit `95ea2b881f507cc303eda32fc1d9849c8cf228aa` (release 14.5.1, 2026-09-23). Package version 14.5.1.
- Runs labelled `base` used the unmodified `src/team_memory` (sha256 of the package files `78bae42c...`, recorded in every `raw/*/environment-*.json`). The second base E2 run used a pristine copy of `src` (same sha256) because the worktree already held the patches.
- Runs labelled `patched-01-02` (E1, E2, E3; package sha256 `84588e6c...`) used base plus `patches/01-write-success-after-revocation.patch` and `patches/02-user-disable.patch`.
- macOS 27.0 (Darwin 27.0.0), Apple M2 Max, 12 logical CPUs, 64 GiB RAM, Python 3.13.5, FastEmbed 0.8.0.
- Embedding model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (FastEmbed, ONNX). Search also runs the default cross-encoder reranker `Xenova/ms-marco-MiniLM-L-6-v2` (window 50). No settings were changed for it.
- `MEMORY_MODE=fast`, `MEMORY_LLM_ENABLED=false`, `MEMORY_QUALITY_GATE_ENABLED=false`, `HF_HUB_OFFLINE=1`. No LLM was called anywhere. API spend: $0.
- `TAM_TEAM_MAX_WORKERS=3` (the default) for E1 to E3. E4 varies it.
- Every server root, `TAM_MEMORY_DIR` and `HOME` was a fresh `mktemp -d` directory, removed after the run. `~/.tam`, `~/.tam-server` and user tokens were not used. The installed `tam` binary was not run.

## How to reproduce

```sh
PYTHONPATH=src python docs/benchmarks/org-memory-v14-20260925/run_all.py --seed 20260925 --label base
```

This regenerates `data/`, runs E1, the local comparison, E2, E3 and E4, and writes `raw/<label>/*.jsonl`, `summary.json` and `summary.md`. `--only e1,e2` runs a subset. `ORGBENCH_SRC=/path/to/src` runs the same harness against another source tree (this is how the before-fix E2 was produced). Full run time on this machine: about 2 hours (E1 about 35 min, local comparison 7 min, E2 about 20 min, E3 about 5 min, E4 about 60 min with load waits).

Other options: `--only e4multi` runs E4b. `--only quality` runs the E2a question set alone (the E4c quality table). `--cross-rerank on|auto|off` sets `MEMORY_CROSS_RERANK` for every server and worker. `--e4-passes forward|reverse` runs one E4 pass, so that two code versions can be interleaved A B B A (E4c).

Files:

- `orgbench/data.py`: the company generator (seed 20260925). Output: `data/company-seed20260925.json`, `data/questions-seed20260925.jsonl`.
- `orgbench/company.py`: creates teams, users, tokens with the real `tam-team` CLI and fills the server through `/api/call`.
- `orgbench/e1.py`, `orgbench/e1_local.py`, `orgbench/e2.py`, `orgbench/e3.py`, `orgbench/e4.py`: the experiments. `orgbench/report.py`: aggregation.
- `orgbench/e4_multiuser.py`: E4b.
- `raw/base/`, `raw/patched-01-02/`: raw JSONL, per-run environment, summaries. `raw/e4-multiuser/`: E4b. `raw/rerank-*`: E4c (A = before, B = after the gateway rerank).
- `patches/`: the fixes as measured against 14.5.1 (01 and 02) and the export/purge commands (03).

## Test company

- Teams: engineering, sales, hr, finance. Plus the shared area.
- 3 people per team: two editors, one reader. Each has an own token (12 users).
- 50 facts per team, from 10 templates (2 rules, 2 decisions, 2 procedures, 2 contacts, 2 restricted items such as salary bands, deal terms, vault paths, IBAN digits). Each fact has a canary `CANARY-<team>-NNNN`.
- 10 facts per team are later updated through `memory_update` by the other editor. The canary stays; one value changes.
- 20 shared facts, 3 personal notes per user (canary `CANARY-personal-<user>-NN`), 6 "twin" notes stored with identical text in two teams.
- 2 questions per fact: a direct one and a paraphrase. 100 questions per team, each with the ID of the correct record.
- E4 also uses two load-only teams, legal and ops, built by the same generator. They are not part of E1 to E3.

The data is synthetic. It is not a real company.

## E1. Isolation between departments

Method. Every one of the 12 users runs every attempt through HTTP `/api/call` against a real `tam-team serve` process. A leak is any foreign canary in the response body, or any result labelled with a team scope the user is not a member of. Personal canaries of other users also count as leaks. After all attacks, each team's full export is compared with a snapshot taken before them.

| # | Attempt | Calls | Calls with a leak | Responses |
|---|---|---:|---:|---|
| 1 | Search by the full text of a foreign record | 360 | 0 | 200 |
| 1 | Search by a foreign canary string | 360 | 0 | 200 |
| 1 | Search by the text of another user's personal note | 132 | 0 | 200 |
| 2 | Paraphrased question about a foreign record | 360 | 0 | 200 |
| 3 | Search by text stored identically in two teams | 72 | 0 | 200; own copy found in 36 of 36 cases where the user's team holds one |
| 4 | Foreign `team_id` in scope: recall, get, history, export, save, update, delete | 252 | 0 | 252 x 400 `forbidden`; 0 writes accepted |
| 5 | Save with tags `scope:team`, `team:<foreign>`, `scope:shared`, `user:<x>` | 96 | 0 | 96 x 400 `invalid_request` (reserved tag) |
| 5 | `team:<foreign>` inside the query text | 24 | 0 | 200, own areas only |
| 5 | `tags` field in a recall request | 12 | 0 | 12 x 400 `invalid_request` (unknown field) |
| 6 | Reader saves, updates, deletes in own team | 12 | 0 | 12 x 400 `forbidden`; 0 writes accepted |
| 7 | Revoked token: scopes, recall, get, save | 48 | 0 | 48 x 401 |
| 8 | Member removed, then 20 searches and one get | 84 | 0 | 80 x 200 with no team results, 4 x 400 `forbidden` for get |
| 9 | get/history/export with a foreign record ID but the user's own scope | 720 | 0 | 562 x 200 returning the user's own record with that number, 158 x 400 `conflict` (no such ID) |

Total: 2,532 attack calls (plus 12 control calls with a fresh token before revocation), 0 leaks. All four team exports were byte-identical before and after the attacks (63 records each).

Record IDs are per area: each area is a separate SQLite file (`worker.py:195`), so ID 17 in hr and ID 17 in sales are different records. Attempt 9 shows that a foreign ID in your own scope just addresses your own record.

Control. Each user asked the 100 questions of their own team with `limit=5` and no scope. The correct record was in the top 5 in 99.5% of 1,200 questions and at rank 1 in 98.0%. Readers and editors scored the same. The server does not refuse everything.

### Comparison: one local store, department as a tag

This is a different design, not a competitor. One local TAM store (`server.Store` and `server.Recall`, the same search code) holds all 200 facts, the updates, the shared facts and the twins. The department is a tag. Filtering is the client's job. The same users run attempts 1, 2, 3 and 5.

| Attempt | Calls | No client filter: calls with foreign records | Honest client filter: calls with foreign records |
|---|---:|---:|---:|
| 1 direct text | 360 | 360 | 0 |
| 1 canary | 360 | 360 | 0 |
| 2 paraphrase | 360 | 360 | 0 |
| 3 twin text | 72 | 72 (foreign-tagged results; 45 also carry a foreign canary) | 0 |
| 5 `team:` text in query | 24 | 24 | 0 |

Two more effects of the single store:

- Twins. The second save of identical text in another department replaced the first (`deduplicated: true` for 6 of 6 twins, `e1-local-meta.json`). With a tag filter, the first department loses its copy. On the team server both copies stay (separate files).
- Tag spoofing. A record written with tags `hr`, `team:hr`, `scope:team` by anybody is shown to hr users by an honest hr tag filter (3 of 3 hr users saw it). On the team server those tags are rejected at save time (attempt 5).

So the protection in the single store is exactly as good as every client's filter. On the team server it does not depend on the client.

## E2. New employee, transfer, offboarding

### E2a. New employee, no LLM

A new user per team asks the 100 questions of that team, `limit=10`. A: not a member (personal is empty, shared is searched). B: added as reader, no scope (personal, team, shared). C: ceiling, scope set to the team only.

| Variant | Questions | hit@1 | hit@5 | MRR@10 | Fresh version in top 5 (80 updated) | Old version in top 5 | Non-team results in top 5 (mean slots) |
|---|---:|---:|---:|---:|---:|---:|---:|
| A not a member | 400 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 5.0 |
| B reader | 400 | 0.980 | 1.000 | 0.986 | 1.000 | 0.000 | 2.1 |
| C team only | 400 | 0.980 | 1.000 | 0.989 | 1.000 | 0.000 | 0.0 |

- Superseded versions never appeared in any top 10. The fresh version was always found.
- B is almost equal to the ceiling C. The multi-area merge orders by rank inside each area first, then by score (`service.py:94-101`, `"ordering": "scope_rank_then_score"`). So hits from the other areas interleave with the team hits by rank. For the new hire the personal area is empty, so every non-team slot is a shared hit: 837 of the 2,000 top-5 slots over 400 questions, 2.09 of 5 on average (363 questions with 2 shared slots, 37 with 3). The most common top 5 is team, shared, team, shared, team (362 of 400). This cost MRR 0.003 because the team hit usually stays at rank 1. (An earlier version of this line said "the best shared hit and the best personal hit take slots 2 and 3". That was wrong: the new hire has no personal records. Checked in `raw/base/e2.jsonl` and `raw/patched-01-02/e2.jsonl`, identical.)
- Caveat. hit@5 = 1.0 means the synthetic questions are easy for this retriever (template wording overlaps with fact wording). Do not read these numbers as field accuracy.

E2b (LLM answer accuracy) was **not run**. No API key was configured in the environment, and the task allowed skipping it.

### Transfer, engineering to sales

`engineering-editor2` warmed the engineering worker with 10 searches. Then membership was changed directly in `identity.db` (remove engineering, add sales), which is what the CLI does.

- Sequential: 50 searches right after the change, the first sent 2.8 ms after it. 0 contained engineering data. All 50 could see sales. get, export and save on engineering returned `forbidden`; save to sales worked.
- Concurrent: 3 client threads searched in a loop while membership flipped 20 times between engineering and sales (1 to 3 s apart). 180 requests. 120 did not overlap a change: 0 of them saw a team the user did not hold when the request was sent. 60 overlapped a change: all 60 were rejected with `forbidden`. 0 strict leaks. The patched run gave the same picture: 174 requests, 0 of 114 non-overlapping saw old access, 60 of 60 overlapping rejected.

So a warm worker process never served the old access. The reason is that membership is read from SQLite on every call (`registry.py:110-119`), checked again under the pool lock before the work is sent (`worker.py:184-188`), and checked again after the worker answers (`service.py:85-86`, `89-91`). No membership cache exists.

### Write during a membership change (bug 01)

A finance editor saved a long record while an admin removed the membership 0 to 40 ms later. 40 attempts.

| Code | Saves committed | Client got an error | Error but the record was committed |
|---|---:|---:|---:|
| base 95ea2b8 | 16 | 40 | **16** |
| patched 01+02 | 15 | 25 | **0** |

Cause: the pool authorises the write under its lock and the worker commits it. The gateway then re-checks membership after the worker answered and raises `forbidden` (`service.py:85-86` at 95ea2b8). The client is told the write failed, but colleagues see it. A retry with the same `request_id` is now rejected too, so the client cannot learn the real outcome. Fix 01 keeps the post-check for reads and skips it for writes that the pool already authorised.

### Offboarding

`sales-editor1` (author of 25 sales facts): all tokens revoked with `token-revoke --file`, then `member ... remove`.

- Their requests: scopes, recall, get, save all returned 401.
- Colleagues: `sales-editor2` and `sales-reader` could read 25 of 25 of their records. `created_by` still names `sales-editor1` in 25 of 25.
- Personal area: see the answer to question 1 below. Release 14.6.0 adds `user-export` and `user-purge` for it.

Offboarding without the token file (bug 02). `token-revoke` needs the plaintext token file (`cli.py:28-29`, `registry.py:97-100`); only a hash is stored. The `users.active` column is checked on login (`registry.py:105`) but no code at 95ea2b8 ever sets it to 0. If the admin did not keep the token file, the only step left is membership removal. In the base run, `hr-editor1` after `member ... remove` could still write to shared (HTTP 200). By the code (`registry.py:110-119`), the same token also still opens their personal area. Fix 02 adds `tam-team user-disable <id>`: it sets `active=0`, revokes all tokens of the user and logs `user_disabled`. After it (patched run): scopes, recall and a shared save by `hr-editor1` returned 401, 401, 401, and `token-create` for that user failed with exit code 1.

## E3. Concurrent edits and retries

Concurrent edits. N editors (different users of one team) send `memory_update` for the same record with the same `expected_revision`, released by a barrier. 100 rounds per N. After each round the stored content is compared with the winner's content. After the last round the full history of the chain is read.

| Clients | Rounds | Rounds with exactly 1 success | Successes | Conflicts | Other errors | Lost updates | Latency p50 / p95 / max, ms |
|---:|---:|---:|---:|---:|---:|---:|---|
| 2 | 100 | 100 | 100 | 100 | 0 | 0 | 17 / 24 / 30 |
| 4 | 100 | 100 | 100 | 300 | 0 | 0 | 17 / 26 / 29 |
| 8 | 100 | 100 | 100 | 700 | 0 | 0 | 19 / 31 / 34 |
| 16 | 100 | 100 | 100 | 1500 | 0 | 0 | 26 / 38 / 50 |

History: for every N, 201 events (1 first insert, 100 inserts of new versions, 100 updates marking the old version superseded). Every successful update appears in the history with its author. The chain has 101 versions. The number of distinct authors in a chain equals N (2, 4, 8, 16) because E3 does not use the test company: it creates its own team with 16 editor accounts `editor00` to `editor15` and gives each concurrent client its own account (`orgbench/e3.py:42-49`, `68`).

All operations go through one lock in the pool (`worker.py:184`), so edits are serialised across the whole server, not only per record. This is why there are no races, and also why all users wait for each other.

Retries with the same `request_id` (20 repetitions per operation).

| Timeout | Operation | First attempt timed out | Of those, already committed | Exactly one effect after retry | Retry returned the same result | Other payload, same UUID, rejected |
|---|---|---:|---:|---:|---:|---:|
| client (HTTP client gives up after 1 ms) | save | 20 | n/a | 20 | 20 | 20 |
| client | update | 20 | n/a | 20 | 20 | 20 |
| client | delete | 20 | n/a | 20 | 20 | 20 |
| server (pool timeout 0.5 to 15 ms, worker killed) | save | 20 | 1 | 20 | 20 | 20 |
| server | update | 20 | 0 | 20 | 20 | 20 |
| server | delete | 0 | 0 | 20 | 20 | 20 |

The table is the base run. The patched run matched it (0 duplicates in 120, 120 of 120 different payloads rejected); there, 0 of the 20 timed-out server saves had committed. Delete finished faster than the smallest server timeout, so its server-timeout path was not exercised. The server-side timeout was injected in-process by lowering `WorkerPool.timeout` after warm-up; the code path is the real one.

Limitation (documented, not fixed). The request table lives inside each area's SQLite file (`audit.py:77-80`), in the same transaction as the write. That is what makes retries exact. It also means the same UUID in a different scope is not seen: a second save with the same UUID and different content in shared was accepted, and the same payload sent to a team scope was executed again (`E3-retry-cross-scope` in `e3-server-timeout.jsonl`). The docs say "another payload with this UUID is rejected"; that holds inside one scope. A central table would lose the single-transaction guarantee, so this is a design choice for the owner.

## E4. Cost of isolation

Method follows `docs/benchmarks/browser-cpu-v14-20260915/`: in-process gateway (`MemoryService` + `WorkerPool`, no HTTP), real worker processes, real models. One user per scope count: 1 = one team with an explicit scope; 2 = personal + shared; 3, 5, 8 = personal + shared + 1, 3, 6 teams. "single-store" puts exactly the same records into one area and searches it with one worker. Per condition: a new pool, one cold query, then 30 warm queries. Conditions ran A B C ... then ... C B A (two passes, 38 batches). Before each batch the harness waited until the 1-minute load was at most 6 (12 CPUs / 2). RSS and CPU come from `ps`; CPU of stopped workers comes from `getrusage(RUSAGE_CHILDREN)`.

| System | Scopes | Workers | Records | Cold first query, ms | Warm p50, ms | Warm p95, ms | Worker RSS sum, MiB | Gateway RSS, MiB | Worker processes started | Worker CPU s per warm query | Worker CPU s total | Load 1-min before / after (pass 1, 2) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| team-server | 1 | 1 | 50 | 904 | 526 | 556 | 1309 | 37 | 1 | 3.0 | 91.0 | 5.2,4.8 / 6.4,5.6 |
| single-store | 1 | 1 | 50 | 975 | 540 | 628 | 1308 | 37 | 1 | 3.1 | 93.7 | 5.8,5.2 / 6.7,6.6 |
| team-server | 2 | 1 | 23 | 1824 | 1107 | 1307 | 870 | 37 | 31 | 1.6 | 49.0 | 5.1,5.4 / 5.3,5.2 |
| team-server | 2 | 2 | 23 | 1938 | 213 | 249 | 1942 | 37 | 2 | 1.1 | 35.8 | 5.3,4.7 / 5.8,5.4 |
| single-store | 2 | 1 | 23 | 921 | 183 | 200 | 999 | 37 | 1 | 1.0 | 31.1 | 5.8,5.6 / 6.9,6.0 |
| team-server | 3 | 1 | 73 | 2841 | 2307 | 2676 | 872 | 37 | 31 | 4.0 | 122.9 | 5.4,5.5 / 4.9,5.6 |
| team-server | 3 | 2 | 73 | 2935 | 1250 | 1773 | 1861 | 37 | 32 | 3.6 | 110.9 | 4.9,5.1 / 7.4,5.5 |
| team-server | 3 | 3 | 73 | 2864 | 756 | 831 | 3275 | 37 | 3 | 4.2 | 128.6 | 5.6,6.0 / 7.2,6.6 |
| single-store | 3 | 1 | 73 | 924 | 535 | 572 | 1307 | 37 | 1 | 3.0 | 91.2 | 4.9,5.0 / 5.6,6.0 |
| team-server | 5 | 1 | 173 | 4712 | 4123 | 4380 | 872 | 37 | 31 | 5.6 | 171.6 | 5.6,5.2 / 5.3,6.2 |
| team-server | 5 | 2 | 173 | 4620 | 3662 | 3927 | 1858 | 37 | 62 | 7.6 | 233.5 | 5.3,5.6 / 6.5,5.2 |
| team-server | 5 | 3 | 173 | 4891 | 2714 | 2961 | 2857 | 37 | 63 | 7.0 | 214.1 | 5.3,5.0 / 5.4,5.6 |
| team-server | 5 | 5 | 173 | 4508 | 1577 | 1694 | 5568 | 37 | 5 | 8.9 | 270.8 | 5.4,5.4 / 7.5,7.8 |
| single-store | 5 | 1 | 173 | 919 | 513 | 555 | 1311 | 37 | 1 | 2.8 | 84.6 | 5.8,4.1 / 6.0,5.4 |
| team-server | 8 | 1 | 323 | 7382 | 6835 | 7246 | 885 | 38 | 31 | 8.3 | 256.2 | 6.0,5.7 / 4.4,4.1 |
| team-server | 8 | 2 | 323 | 7590 | 6415 | 6786 | 1855 | 38 | 62 | 10.7 | 327.9 | 4.4,5.6 / 5.2,5.7 |
| team-server | 8 | 3 | 323 | 7502 | 5849 | 6218 | 2858 | 38 | 93 | 11.9 | 366.3 | 5.2,5.6 / 6.0,6.3 |
| team-server | 8 | 8 | 323 | 7096 | 2938 | 3274 | 9035 | 38 | 8 | 16.6 | 506.2 | 6.0,5.4 / 9.7,9.5 |
| single-store | 8 | 1 | 323 | 910 | 459 | 538 | 1290 | 38 | 1 | 2.7 | 81.7 | 5.6,4.8 / 6.3,6.2 |

Load. The gate held before every batch (1-minute load 4.1 to 6.0). During batches the load rose above 6 in 17 of 38 batches, up to 9.7 in the 8-worker batches. Part of that rise is the benchmark itself (8 workers use many cores). Latencies under other conditions may be lower.

What the numbers say:

- Warm cost grows with the number of areas searched. With enough workers (workers = scopes), 8 areas cost 2.9 s p50 against 0.46 s for one store holding the same 323 records: 6.4 times the latency and 6.2 times the worker CPU per query.
- The reason is visible in a profile of one worker (10 searches, 50 records): 93% of search time is the cross-encoder reranker (`fastembed ... onnx_text_model._rerank_documents`, 5.2 of 5.6 s). It reranks up to 50 candidates per area (`DEFAULT_CROSS_RERANK_WINDOW = 50`, `config.py:825`). Every area runs its own rerank. One store runs one. So the cost scales with the number of areas, not with the number of records.
- With fewer workers than areas, every query restarts workers and reloads models (31 to 93 processes started for 31 queries). Latency then is dominated by model loading: 6.8 s p50 at 8 scopes and 1 worker.
- Memory: each warm worker holds about 0.9 to 1.3 GiB RSS on macOS. 8 workers: 9.0 GiB in total. One store: 1.3 GiB. RSS sums count shared pages (libraries, mapped model files) once per process, so the real footprint is lower than the sum. The gateway itself is 37 MiB.
- Cold first query: about 0.9 s per area that has to start (7.1 to 7.6 s for 8 areas; 0.9 s for one store).
- Caveat on thrashing conditions: the reranker loads in the background (`MEMORY_CROSS_RERANK=auto`). A freshly started worker may answer before it is loaded, so its answer may be ranked without the reranker. The smaller RSS (about 870 MiB) of the 1-worker conditions is consistent with that. Result order was not compared across E4 conditions.
- E4 was run once on base code and not repeated after the patches: both patches change only the write path and the CLI.

### E4b. Several users under one worker limit

`TAM_TEAM_MAX_WORKERS` limits worker processes for the whole server, and every user has a personal area. This run measures that directly instead of inferring it from code. 4 users, 20 searches each (80 per run), `TAM_TEAM_MAX_WORKERS=3`. Two orders: `rotate` (users take turns, one search each) and `blocks` (each user runs all 20 searches in a row). Runs in A B B A order. Raw data: `raw/e4-multiuser/`.

| Run | Order | Cold searches | Worker processes started | p50, ms | p95, ms | Cold p50, ms | Warm p50, ms | Load 1-min before / after |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | rotate | 80 of 80 | 161 | 1,978 | 2,197 | 1,978 | none | 5.85 / 6.87 |
| 2 | blocks | 4 of 80 | 9 | 620 | 1,089 | 2,143 | 617 | 5.26 / 8.61 |
| 3 | blocks | 4 of 80 | 9 | 626 | 922 | 2,029 | 620 | 5.68 / 7.20 |
| 4 | rotate | 80 of 80 | 161 | 1,957 | 2,295 | 1,957 | none | 4.77 / 4.89 |

What the numbers say:

- When users take turns, every search is cold: each user needs more areas than the 3 free workers can keep warm, so workers are stopped and started on every search (161 starts for 80 searches), and p50 is about 2.0 s.
- When each user searches in a block, only the first search of each user is cold (4 of 80), and p50 is about 0.62 s.
- So with a server-wide worker limit below the number of areas in active use, latency depends on how requests from different people interleave. Real traffic is closer to `rotate` than to `blocks` once several people work at the same time.
- The load rose above the 6.0 gate during runs (up to 8.6), so absolute latencies may be lower on an idle machine. The difference between the two orders is much larger than that effect.

### E4c. After cross-scope rerank (2026-09-25, for 14.6.0)

E4 found that 93% of search time is the cross-encoder, and that it runs once per area. The change for 14.6.0 moves it to the gateway. Each area now returns its fused candidate window without reranking. The gateway merges the windows of the areas the caller may read, reranks the merged window once (same model, same window of 50, same weight and neighbour context), keeps `limit` records and orders them by score. That last rule is what one area already did on its own. The gateway loads the model once, lazily, and uses it behind a lock. Workers no longer load it. If the model is not ready, fails, or runs past `TAM_TEAM_OPERATION_TIMEOUT`, the merged fused order is kept. Code: `src/team_memory/rerank.py`, `src/team_memory/service.py` (`_merge`), `defer_cross_rerank` in `src/server.py` and `src/team_memory/worker.py`. Tests: `tests/test_team_cross_rerank.py`. They cover merge order, fallbacks, authorisation boundaries, exact single-area equivalence, and that workers do not load the model.

Code measured. A ("before") is the main checkout as copied at 15:10 on 2026-09-25: 14.5.1 plus the unreleased 14.6.0 work, without this change (`team_memory` sha256 `bfb7959d…`). B ("after") is the same tree plus the six patches of this study: the write fix, `user-disable`/`user-enable`, request_id docs, `user-export`/`user-purge`, and the gateway rerank (`2227f2fa…`). Only the last patch touches the read path. Machine, model and settings are as in the Environment section. This E4c section and E4d below use `raw/rerank-*`.

Retrieval quality. The E2a question set, seed 20260925, `MEMORY_CROSS_RERANK=on` in both runs so that the model is always applied (with `auto`, the first searches of a fresh process may run before the model has loaded). The variants: B is a new hire added as reader, no scope. C is team scope only. D is an existing editor, with personal notes, no scope. Raw data: `raw/rerank-before-quality`, `raw/rerank-after-quality`.

| Variant | hit@1 before / after | hit@5 before / after | MRR@10 before / after | Fresh version in top 5, before / after | Non-team results in top 5 (mean), before / after |
|---|---|---|---|---|---|
| B reader, all areas | 0.980 / 0.980 | 1.000 / 1.000 | 0.986 / 0.989 | 1.000 / 1.000 | 2.09 / 0.11 |
| C team area only | 0.980 / 0.980 | 1.000 / 1.000 | 0.989 / 0.989 | 1.000 / 1.000 | 0 / 0 |
| D editor, all areas | 0.980 / 0.980 | 0.995 / 1.000 | 0.984 / 0.989 | 0.988 / 1.000 | 3.00 / 0.12 |

- Single area (C): the 10 returned record IDs and their order are identical for 400 of 400 questions. Moving the rerank did not change single-area results.
- All areas (B, D): the gold record's rank is the same for 392 of 400 questions and better for 8, never worse. Results from the other areas stop taking fixed slots. Before, they took 2 to 3 of the top 5 by the rank-within-area rule; after, 0.1 on average.

Cost. Same E4 conditions and queries as above. Four runs of one pass each, in the order A forward, B forward, B reverse, A reverse. Each condition therefore has 60 warm samples per version, from both pass directions. Before each batch the harness waited until the 1-minute load was at most 6. Over all batches the 1-minute load stayed between 2.3 and 9.6 for A and between 2.3 and 6.8 for B. CPU per query adds worker and gateway CPU. RSS adds worker and gateway RSS; shared pages are counted once per process, as above. In B, the gateway loaded the cross-encoder once per run before the first condition: 0.22 s and +202 MiB RSS. That once-per-server cost is not in the cold column.

| System | Scopes | Workers | p50 ms A / B | p95 ms A / B | CPU s per query A / B | RSS MiB A / B | Cold first query ms A / B |
|---|---:|---:|---|---|---|---|---|
| team-server | 1 | 1 | 532 / 554 | 557 / 590 | 3.03 / 3.35 | 1381 / 1486 | 917 / 1441 |
| single-store | 1 | 1 | 530 / 552 | 552 / 587 | 3.02 / 3.33 | 1351 / 1507 | 931 / 1464 |
| team-server | 2 | 1 | 1059 / 1098 | 1158 / 1159 | 1.52 / 2.01 | 917 / 1499 | 1804 / 1965 |
| team-server | 2 | 2 | 208 / 209 | 227 / 227 | 1.10 / 1.11 | 2009 / 2310 | 1832 / 2002 |
| single-store | 2 | 1 | 185 / 187 | 199 / 203 | 1.01 / 1.08 | 1063 / 1479 | 901 / 1081 |
| team-server | 3 | 1 | 2293 / 2372 | 2570 / 2582 | 3.96 / 5.13 | 937 / 1485 | 2707 / 3286 |
| team-server | 3 | 2 | 1261 / 1515 | 1699 / 1605 | 3.51 / 4.23 | 1913 / 2318 | 2748 / 3287 |
| team-server | 3 | 3 | 741 / 586 | 785 / 694 | 4.14 / 3.32 | 3343 / 3199 | 2745 / 3321 |
| single-store | 3 | 1 | 529 / 536 | 556 / 569 | 2.98 / 3.22 | 1374 / 1502 | 920 / 1437 |
| team-server | 5 | 1 | 4086 / 4242 | 4606 / 4366 | 5.63 / 6.76 | 924 / 1484 | 4711 / 5239 |
| team-server | 5 | 2 | 3665 / 3282 | 4099 / 3433 | 7.68 / 5.83 | 1894 / 2323 | 4530 / 5009 |
| team-server | 5 | 3 | 2776 / 2381 | 3052 / 2464 | 7.05 / 4.91 | 2598 / 3180 | 4775 / 5126 |
| team-server | 5 | 5 | 1709 / 610 | 1917 / 652 | 9.44 / 3.10 | 4209 / 4894 | 5030 / 5085 |
| single-store | 5 | 1 | 521 / 516 | 576 / 553 | 2.87 / 2.99 | 1362 / 1489 | 959 / 1413 |
| team-server | 8 | 1 | 7061 / 6902 | 7970 / 7171 | 8.73 / 9.30 | 948 / 1481 | 8395 / 7778 |
| team-server | 8 | 2 | 7037 / 6073 | 8172 / 6285 | 11.79 / 8.49 | 1927 / 2328 | 7599 / 7922 |
| team-server | 8 | 3 | 5920 / 5146 | 6245 / 5359 | 11.98 / 7.56 | 2874 / 3172 | 7405 / 7975 |
| team-server | 8 | 8 | 2962 / 652 | 3174 / 754 | 16.82 / 3.09 | 9147 / 7428 | 7488 / 7978 |
| single-store | 8 | 1 | 473 / 497 | 579 / 594 | 2.75 / 3.03 | 1337 / 1492 | 900 / 1494 |

What the numbers say:

- With a warm worker per area, search cost no longer grows with the number of areas. At 8 areas, p50 falls from 2,962 ms to 652 ms (4.5 times) and CPU per query from 16.8 s to 3.1 s. That is within 31% of one store holding the same 323 records (497 ms, 3.0 s). At 5 areas: 1,709 ms to 610 ms.
- One area and one store are unchanged within noise (p50 +4%, CPU +10%). The same rerank now runs in the gateway instead of the worker.
- With fewer workers than areas, latency is still dominated by stopping and starting workers (E4 above). The change helps there only a little (8 areas, 2 workers: 7,037 to 6,073 ms), because every search still reloads embedding models in the workers.
- Memory moves from the workers to the gateway. Each worker is about 470 MiB smaller (1,335 to 840 MiB with one area); the gateway grows from 46 to about 650 MiB (cross-encoder, onnxruntime and the search modules). The total falls where many workers are warm (8 areas, 8 workers: 9,147 to 7,428 MiB). It rises by about 550 MiB where only one or two workers run.
- Cold first query is 0.2 to 0.6 s higher after the change for small conditions. Before, a freshly started worker usually answered its first search before its own reranker had loaded (`auto`), so that search skipped the rerank. After, the gateway model is already loaded and the first search is reranked.

### E4d. Remaining limits

- The limit on workers is still server-wide (E4b). The gateway rerank does not change how many workers a user keeps warm.
- `MEMORY_CROSS_RERANK=off` restores the previous merge (rank inside each area, then score), and the results then carry `"ordering": "scope_rank_then_score"`.
- The quality numbers come from the synthetic E2a set, where hit@5 is already at the ceiling. They show that nothing got worse. They do not measure how much the single rerank helps on real data.

## E5. PostgreSQL backend (14.6.0)

Same host and machine as above, PostgreSQL in a local `pgvector/pgvector:0.8.1-pg18` container, final 14.6.0 working tree (2026-09-26 and 2026-09-27).

### E1 to E3 on PostgreSQL

Raw: `raw/pg-1460-final/` (seed 20260925).

| Question | Result |
|---|---|
| E1: foreign department records returned, all 9 attempt types, 2,544 calls | **0** |
| E1 control: own gold record in top 5 / at 1 | 100% / 98% (1,200 questions) |
| E2 transfer: requests sent after the change that saw the old team | 0 of 50 |
| E2 write during membership removal: client got an error but the write was committed | 0 of 40 (12 committed and reported 200; 28 rejected and not committed) |
| E2 `user-disable` | 3 of 3 requests 401 |
| E3 concurrent edits, 2/4/8/16 clients x 100 rounds: rounds with exactly one success | 400 of 400, 0 lost updates; p95 40 / 48 / 63 / 105 ms |
| E3 retries with the same request_id after a timeout | 120 of 120 with exactly one effect, 0 committed after the timeout |

The PostgreSQL run of E2 found a real defect: a root configured for PostgreSQL only through `TAM_TEAM_DATABASE_URL`, started once without that variable, silently created a new empty SQLite `identity.db`. 14.6.0 writes `postgres-installation.json` into the root and refuses such a start (`StartupRefusal.POSTGRES_DSN_MISSING`), see `docs/TEAM_POSTGRES.md`.

### Parity with SQLite

`benchmarks/pg_parity_bench.py`, results `benchmarks/results/pg-parity-14.6.0.{json,md}`: 320 facts, 600 questions, filler records up to 1k, 10k and 50k, 200 timed saves and 200 timed recalls per size, each backend in its own process.

| | SQLite | PostgreSQL |
|---|---|---|
| Recall@5 / Recall@10 | 99.67% / 100% | 99.67% / 100% |
| MRR / nDCG@10 | 0.9887 / 0.9915 | 0.9887 / 0.9915 |
| top-10 Jaccard and Kendall tau vs SQLite, fused / lexical / semantic | | 1.0 / 1.0 / 1.0 |
| recall p50 / p95, 1k records | 468 / 570 ms | 463 / 560 ms |
| recall p50 / p95, 10k records | 492 / 746 ms | 485 / 602 ms |
| recall p50 / p95, 50k records | 486 / 622 ms | 552 / 671 ms |
| save p50 / p95, 50k records | 33 / 39 ms | 26 / 28 ms |

All three acceptance checks pass: Recall@10 drop 0.0 pp (limit 1.0), lexical Jaccard 1.0 (limit 0.9), p95 recall at 10k 0.81 x SQLite (limit 1.5). The SQLite 10k window overlapped with other jobs on the machine, so its p95 is inflated; the p50 values (492 vs 485 ms) are the fairer comparison, and they are equal.

Before the last 14.6.0 fix, PostgreSQL recall at 50k was 2.5 times slower than SQLite (p95 1,591 vs 635 ms). A profile put 20 of 30 s in the neighbour-turn lookup of the reranker: the query used `project IS ?`, which PostgreSQL runs as `IS NOT DISTINCT FROM` and cannot serve from an index. With `project = ?` one lookup takes 0.02 ms instead of 8 ms.

### Test suite

Full suite on the final 14.6.0 tree, both backends (`pytest tests --backend=both`, temporary `HOME`): **3,873 passed**, 40 skipped. One test, `test_no_llm_hot_path_v11.py::test_code_save_uses_code_specific_embedding_model_by_default`, failed in the full run because macOS had purged the FastEmbed model cache in `$TMPDIR` and the test blocks network downloads; its file passed 12 of 12 on a rerun with the cache present.

## Bugs found

| # | Bug | Evidence (base) | Patch | Regression test | Proposed commit message |
|---|---|---|---|---|---|
| 01 | A write that was authorised and committed is reported to the client as `forbidden` if membership or the token is removed while the worker runs | 16 of 40 races (E2) | `patches/01-write-success-after-revocation.patch` (`src/team_memory/service.py`) | `tests/test_team_write_revocation.py`: fails on base (1 of 2 tests), passes after | `fix(team): report committed writes as success after revocation` |
| 02 | Offboarding needs the user's plaintext token file; `users.active` is checked but can never be set, so without the file the user keeps shared write access and the personal area | E2 base: shared write after `member remove` returned 200 | `patches/02-user-disable.patch` (`registry.py`, `cli.py`, `docs/team-server-reference.md`) | `tests/test_team_offboarding.py`: 2 of 2 fail on base, pass after | `fix(team): add user-disable to offboard without token files` |

Verification after the patches: `tests/test_team_memory.py`, `tests/test_team_worker_reuse.py`, `tests/test_team_lifecycle.py` and the two new files: 19 passed. Ruff on the changed files and the benchmark folder: clean (two pre-existing ruff findings in `app.py` and `lifecycle.py` were not touched).

Not a bug, documented: the same `request_id` in a different scope is not detected (E3).

Added for release 14.6.0 (not a bug fix): `tam-team user-export` and `tam-team user-purge` for the personal area of a disabled user (see question 1), with `tests/test_team_personal_offboarding.py`, and `tests/test_team_shared_scope.py` for the shared-area rule (see question 2).

## Answers to the three questions

Line numbers refer to 95ea2b8.

**1. What happens to the personal area of an employee who left?**

At 95ea2b8 nothing is deleted. The area is a separate SQLite file under `workspaces/personal_<sha256(user_id)>/memory.db` (`registry.py:114`, `worker.py:195`). After token revocation and membership removal it was still on disk with its 3 active records, and it is included in `tam-team backup` (`lifecycle.py:85`; verified in E2). Through the API nobody else can reach it: a personal scope always resolves to the caller's own file (`registry.py:114`, `121-127`), and a scope with an `owner_id` is rejected as an unknown field (`contracts.py:39`, observed HTTP 400). Other users searching its text got 0 of its canaries. At 95ea2b8 there is no command that deletes or exports it, and an admin could issue a new token for the same user id (`registry.py:86-95`); the user then sees the personal area again (verified in E2).

Owner's decision for release 14.6.0: offboarding gets explicit commands. `tam-team user-disable <id>` (patch 02) revokes every token and refuses new ones. `tam-team user-export <id> --out <file>` writes all personal records (every status, with authorship) and the full history as JSONL; the file is created with mode 0600 and an existing file is never overwritten. `tam-team user-purge <id> --confirm <id>` deletes the personal area. Both refuse an active user. Export reads the database read-only and works while the server runs. Purge needs the server stopped, like `backup`: a running server or a live workspace process is detected through their locks, and the command refuses (`src/team_memory/offboarding.py`). Both write an audit event (`personal_exported`, `personal_purged`) without any record content. Team and shared records written by the user live in other databases and keep `created_by`. Backups taken before the purge still contain the personal area (`docs/team-server-reference.md`). Tests: `tests/test_team_personal_offboarding.py`. These commands were added after the measured runs; E2 above measured 95ea2b8 and patches 01 and 02.

**2. Can every user edit shared, and how is that recorded?**

Yes, by design (owner's decision). Shared is the company-wide open area; the `reader` and `editor` roles restrict team areas only. The docs state it (`docs/team-server-reference.md:8`: shared memory is read and edited by every authenticated user of the server), and the code grants it: every authenticated user gets the shared area with `writable=True` (`registry.py:118`), whatever their team role, including readers. `tests/test_team_shared_scope.py` pins this. Each change runs in one transaction with the audit (`worker.py:69`, `audit.py:47-63`). Triggers write a `tam_history` row with the operation, the acting user and client from the token, the reason, the revision and the states before and after (`audit.py:84-105`). The original author stays in `created_by`; the editor goes to `updated_by` (`audit.py:93-95`, `worker.py:90`). Saving text that already exists adds a `confirm` event by the second user (`worker.py:118-123`). Within teams, `reader` cannot write (E1 attempt 6: 12 of 12 rejected); in shared the reader can.

**3. Does a membership change apply from the next request, or can there be a delay?**

From the next request. Membership is read from `identity.db` on every call; there is no cache (`registry.py:110-119`, `service.py:69-77`). It is checked again under the pool lock just before the work goes to the worker process (`worker.py:184-188`), and again after the worker answers (`service.py:85-86`, `89-91`). Measured: the first request 2.8 ms after a change saw the new access; 0 of 120 non-overlapping requests during 20 flips saw old access; all 60 requests that overlapped a change were rejected. Warm worker processes hold no access state. One consequence at 95ea2b8 was bug 01: a write already in the worker when the change lands is committed but reported as `forbidden`.

## What was not measured, and why

- E2b (LLM answer accuracy): skipped. No API key was configured, and the owner's rules allow skipping it.
- Moving a record between areas, OAuth, and real company data: these do not exist in the code or the study, so they were not measured and are not described as features.
- The MCP endpoint `/mcp` was not used for the attacks; `/api/call` calls the same `MemoryService.call` (`app.py:101`, `137`). The existing test suite covers `/mcp` auth.
- E4 ran on macOS on a shared machine, not on the Linux Docker setup of `browser-cpu-v14-20260915`. RSS numbers are not directly comparable to that report.
- Retrieval quality on real, messy data. The synthetic questions reach hit@5 = 1.0, which is a ceiling effect.
