# What is new in 14.x and 15.0

Release summaries for the 14.x series and 15.0, moved here from the README. The
[CHANGELOG](../CHANGELOG.md) is the complete, authoritative record; this page
keeps the measurements and usage notes that the summaries added.

- [15.0.0](#version-1500--indexed-resources-warm-recall-checked-clients-snapshot-exports)
- [14.9.0](#version-1490--memory-through-the-hooks-rules-that-are-measured-a-warm-daemon)
- [14.8.0](#version-1480--every-recall-tier-measured-two-of-them-fixed)
- [14.7.0](#version-1470--whole-records-fill-the-context-budget)
- [14.6.0](#version-1460--a-company-memory-server-you-can-run-dashboard-roles-onboarding-postgresql)
- [14.5.0](#version-1450--no-significant-difference-from-mem0-platform-on-locomo-and-longmemeval)
- [14.4.0](#version-1440--opt-in-fact-supersession-cheaper-writes-at-1m-records)
- [14.3.1](#version-1431--updates-are-no-longer-dropped-recall-stays-fast-at-1m-records)
- [14.3.0](#version-1430--jev-as-the-contradiction-checker)
- [14.2.0](#version-1420--facts-that-change-over-time)
- [14.1.0](#version-1410--what-is-new)
- [14.0.0](#version-1400--what-is-new)

## Version 15.0.0 — indexed resources, warm recall, checked clients, snapshot exports

**Release date: 2026-11-16.** Back up the database before upgrading: SQLite migrations 047–059 and
PostgreSQL workspace migrations 0011–0022 run on first start (see [UPGRADE_15.md](UPGRADE_15.md)).
The [CHANGELOG](../CHANGELOG.md) lists every change.

| Change | How you use it |
|---|---|
| **Resource index** | `ingest_codebase` indexes a project root incrementally into separate tables: unchanged files are not reparsed, edits and deletions are reconciled, PDF, Office XML and archives are read locally, and `preview`, `search` and `status` actions inspect it. Search is lexical and separate from knowledge recall. Optional `[media]` adds OCR and audio transcription. |
| **AST for PHP and Vue** | Functions, classes, interfaces, traits, enums and methods in PHP, and the scripts of Vue single-file components, keep their original line numbers. |
| **Recall under load** | Personal tool calls run on one Store owner thread with a bounded queue, inference runs in a worker pool, and the cross-encoder batches by text length and caches scores. `MEMORY_CROSS_RERANK_BATCH_SIZE=0` and `MEMORY_CROSS_RERANK_CACHE_SCORES=0` restore the old behaviour. |
| **Model identity** | Vectors from a different model, provider or pooling are never mixed in a search; legacy vectors are re-queued. On PostgreSQL, build the HNSW index with `scripts/prepare_vector_index.py`; until then searches are exact. |
| **Clients that fail closed (#40)** | `tam-team` and the remote CLI check the server's advertised `capabilities` and refuse a flag an older server would ignore. |
| **Exports you can verify (#41)** | `memory_export` writes a header, a state per entry and a footer with `complete`; import rejects truncated or duplicated files. |
| **Incidents, similar records, `as_of` (#42–#44)** | Incidents carry `detection`, `affected` windows and `resolution`; `memory_save` returns `similar` and `revived`; `memory_recall(as_of=...)` chooses the snapshot first. |
| **One team router (#45)** | Team MCP calls go through the REST router; `?readonly=1` removes write tools. |
| **Quieter ambient recall (#47)** | Automation prompts get no recall and weak matches are dropped. |
| **Review fixes (#54–#58)** | Cross-project leak in `expand_context`, tags lost on repeat save, `benchmark` and `memory_eval_locomo` on pip and Docker installs, container health, and six smaller API problems. Auto-consolidated rules now reach `self_rules_context`. |

---

## Version 14.9.0 — memory through the hooks, rules that are measured, a warm daemon

**Release date: 2026-10-13.** The Claude Code plugin (`total-agent-memory-plugin`) is not updated in
this release and still pins 14.8.0; the hooks, tools and settings below come with an install from this
repository or the package. The catalogue grows from 77 to 79 tools.

**Ambient memory.** The hooks now recall, check rules and save without the agent calling a memory tool:
every prompt gets the matching rules and a short block of relevant records (`TAM_AMBIENT_TOKEN_BUDGET`,
400 tokens; `TAM_AMBIENT_LATENCY_MS`, 1500 ms), a Bash call or an edit that matches a rule gets a
reminder first, every tool call lands in an action log, and at the end of a turn a broken "never X" is
reported to the user. Docker installs get the same hooks. Settings: the ambient section of
[configuration.md](configuration.md#ambient-memory-hooks).

**Rules.** Loading rules no longer counts as firing them, which used to suspend helpful rules after ten
session starts. A rule is now suspended only on explicit ratings (5 or more, 95% Wilson upper bound of the
success rate below 0.4). `self_rules_context(query=...)` ranks rules for the task, and
`self_rules(action='followed' | 'report')` records and measures whether they were followed.

| Change | Before | 14.9.0 |
|---|---:|---:|
| First `memory_recall` of a session, `tam proxy` with the daemon running ([shared-daemon.md](shared-daemon.md)) | 1.51 s | **0.18 s** |
| Search while another connection holds the write lock for 1 s ([details](benchmarks/pmb-adoption-recall/RESULTS.md#2-usage-statistics-off-the-read-path)) | 1,008-1,108 ms | **16-17 ms** |
| "What was it before?" questions with the older value first ([details](benchmarks/pmb-adoption-recall/RESULTS.md#1-historical-value-questions)) | 0 / 48 | **48 / 48** |
| LongMemEval-S, 400 held-out questions, gpt-4.1-mini reader ([details](benchmarks/longmemeval-v14-9/RESULTS.md)) | 87.75 (14.5.1) | **88.0** |

The recall changes (historical values, intent-aware record types, compound-query slots, singleflight,
cross-lingual full-text damping, an off-by-default `query_entity` tier) left LoCoMo and LongMemEval-S
dev R@10 unchanged; the result cache, which never hit, now does.

| Also new | How you use it |
|---|---|
| **Shared warm daemon** | `tam setup register --client claude-code --shared-daemon`; `tam daemon status`. |
| **Smaller tool catalogues** | `TAM_TOOL_PROFILE=lean` (28 tools, about 4,400 tokens) or `minimal` (12, about 2,150) instead of about 15,200 for all 79 (`scripts/tool_catalogue_size.py`); hidden tools stay callable. |
| **Usage instructions and tool statistics** | Sent on connect (`TAM_MCP_INSTRUCTIONS`); per-tool p50/p95 in `memory_perf_report` (`TAM_TOOL_TELEMETRY`). |
| **Importers and encrypted bundles** | `tam import chatgpt\|claude\|mem0\|markdown <path>`, `tam export --encrypted`, `tam import-bundle` ([importing.md](importing.md)). |
| **Exploration memos** | `memory_exploration_record` / `memory_exploration_recall`: codebase conclusions that go stale when their files change. |
| **Relative dates in queries** | "last month", "three weeks ago" narrow the search; `memory_recall(as_of=...)` sets the moment they count from. |
| **Write-path reliability** | Records saved without the embedder get vectors later; swallowed errors and the importance budget show in `memory_stats`; decisions, conventions and lessons no longer age out. |

Fixed: hook text never reached the model on PreToolUse/PostToolUse (Claude Code ignores their plain
stdout), nothing was restored after `/compact`, SessionEnd read no messages from current transcripts,
image OCR on macOS returned nothing (`[ocr]` extra), a NUL in a query broke full-text search, and
onnxruntime could abort the process at exit (its telemetry is now off, so no usage events are sent).
CI now runs on every pull request: 3 OSes x 3 Pythons, ruff, CodeQL and the EN/RU/UK recall-quality
gate (`scripts/check.sh` locally). Migrations 038-042 run on first start. Details:
[CHANGELOG](../CHANGELOG.md).

## Version 14.8.0 — every recall tier measured, two of them fixed

**Release date: 2026-10-08.** Nothing in the Claude Code plugin, the hooks, the skill or the 77 tool
schemas changed; `tests/fixtures/tool_contract.json` now pins them.

`memory_recall` fuses up to ten retrieval tiers. Their weights are now configuration
(`MEMORY_RECALL_TIER_WEIGHTS`; a weight of 0 skips a tier), and
[`benchmarks/tier_ablation.py`](../benchmarks/tier_ablation.py) measures what each tier is worth on
LoCoMo, LongMemEval-S or a copy of an installed store
([results](benchmarks/tier-ablation-v14/RESULTS.md)). On a copy of the author's store the probe
found two tiers that did nothing useful:

| Live store, 150 queries, R@10 ([details](benchmarks/tier-ablation-v14/RESULTS.md)) | 14.7.0 code | 14.8.0 |
|---|---:|---:|
| full pipeline | 0.133 | **0.167** |
| p50 latency, cross-encoder off | 336 ms | **128 ms** |

- The **graph tier** read the `relations` table, which only `memory_relate` writes, so it had never
  fired. It now walks the knowledge graph (records sharing an entity node with a top hit), with hub
  nodes skipped and nodes weighted by how few records they link to.
- The **multi-representation tier** scored the first 100 representation rows in table order, an
  arbitrary old subset of the 10,000 on that store. It now scores every row of the scope with one
  matrix product.

Two new tiers cover question shapes the ranking missed: an advice-shaped question also searches the
project's conventions on their own (`directives`), and an ordering or comparison question can be split
into its sides (`multi_query`, off by default: measured neutral on the held-out splits). `session_end` now also keeps its summary, next steps and pitfalls as
a `note` record, so later recalls find them.

Operations: `MEMORY_LLM_FALLBACK_PROVIDERS` gives every LLM phase a chain of providers, so a stopped
Ollama no longer leaves the enrichment queues pending in silence; `memory_stats` and the dashboard
`/api/system/status` report the chain and a `stalled` flag. The hourly reflection job now consolidates idle
projects (episodes, duplicate merging, decay), which used to happen only on the team server.
Details: [CHANGELOG](../CHANGELOG.md).

## Version 14.7.0 — whole records fill the context budget

**Release date: 2026-09-30.**

`memory_recall(mode="context", fill_budget=true)` searches up to 100 hits deep and keeps whole
records, in rank order, until `context_max_chars` is used. The plain context mode takes the top
`limit` hits and excerpts them; with short records most of the budget stays empty. Off by default.

| Context holds the evidence ([details](benchmarks/context-fill-v14/RESULTS.md)) | Plain context | `fill_budget` |
|---|---:|---:|
| LoCoMo, 383 questions, at least one gold turn, 8,000 characters | 0.815 | **0.890** |
| LongMemEval-S, 94 questions, every gold turn, 16,000 characters | 0.766 | **0.830** |
| same, 40,000 characters | 0.766 | **0.904** |
| multi-session questions only (30), every gold turn, 40,000 characters | 0.333 | **0.733** |

**AMA-Bench, full test set.** TAM as the memory of a gpt-5-mini agent scored **0.658** (mean over
24 domain x capability cells, judge gpt-5.2) on all 208 episodes and 2,496 questions; GPT-5 mini
reading the whole trajectory scores 0.656 on the published board. The result is submitted to the
leaderboard and self-reported until the board verifies it. Adapters, configuration and the command:
[docs/benchmarks/ama-bench](benchmarks/ama-bench/README.md).

Also fixed: a code record saved while the code embedding model could not be loaded (offline, or
after macOS purged the model cache) was labelled with the code model and became invisible to
semantic search; it is now stored in the text space. Details: [CHANGELOG](../CHANGELOG.md).

---

## Version 14.6.0 — a company memory server you can run: dashboard, roles, onboarding, PostgreSQL

**Release date: 2026-09-27.**

The team server grows from a token-only endpoint into something a company can run and administer.
All of it is MIT, like the rest of TAM. Upgrades stay single-user; personal installs gain a settings page and
stricter privacy.

| Change | How you use it |
|---|---|
| **Setup wizard** | `tam setup` asks "Just me" or "Company server"; a new team server shows a one-time setup code and a web wizard at `/dashboard/`. |
| **Team dashboard with roles** | Invite codes, passwords, member / manager / company viewer / superadmin. Provider keys are entered in the browser and stored encrypted. |
| **Department onboarding** | `/onboard` in the agent: lessons built from the team's records, quizzes, results visible to the department head. |
| **PostgreSQL backend** | `TAM_TEAM_DATABASE_URL` or **Settings → Database**; `tam-team db-migrate` moves an existing server. Same top 10 as SQLite on the parity benchmark, recall p50 485 vs 492 ms at 10k records ([E5](benchmarks/org-memory-v14-20260925/RESULTS.md#e5-postgresql-backend-1460)). |
| **Continuous backup** | `TAM_TEAM_REPLICA_URL` turns on Litestream replication to S3-compatible storage or a directory; restore to any moment in the retention window. |
| **Offboarding** | `tam-team user-disable` revokes every token and blocks sign-in without the user's token files; `user-export` and `user-purge` handle the personal area. Team and shared records keep their author. |
| **Corrections rank above what they correct** | Automatic, in English and Russian, with or without the cross-encoder. An explicit schedule correction such as "The stand-up is at 10:15 on Thursdays." → "The stand-up moved to 9:30 on Mondays." ranks first even when both the day and time change. Events and projects stay separate; negated, tentative and differently qualified schedules are not treated as corrections. Both records remain available, and historical questions retain the older-first ordering. |
| **`memory_report`** | Activity report for a day, week, month or custom range, with record ids for every item. |
| **Settings in the browser** | The local dashboard's **Settings** page sets the language model, embeddings, search-answer size and log retention. API keys are stored encrypted; the setup wizard no longer writes them into client configs ([LOCAL_SETTINGS.md](LOCAL_SETTINGS.md)). |
| **Privacy** | Credentials are redacted from every write path, including the raw call log and the prompt hook. `tam redact-existing` cleans what older versions stored, and `memory_delete(hard=true)` erases a record with every copy of it. |
| **Security** | The local dashboard no longer sends `Access-Control-Allow-Origin: *`; the dashboard and the MCP HTTP transport check `Host` and `Origin` against DNS rebinding. Records that address the agent ("ignore previous instructions") are flagged in search results. |

Measured on the organisational-memory benchmark: 0 foreign-department records returned in 2,532 attack calls on SQLite
and 2,544 on PostgreSQL, and 0 lost updates in 400 concurrent rounds on each. Details and every other
change: [CHANGELOG](../CHANGELOG.md).

---

## Version 14.5.0 — no significant difference from Mem0 Platform on LoCoMo and LongMemEval

**Release date: 2026-09-23.**

Mem0 publishes the per-question answers behind its LoCoMo and LongMemEval figures. We graded
them and TAM's answers to the same held-out questions under two grading configurations each — the
judge the public numbers used and Mem0's current one. Within a configuration both systems' answers
go through the same judge model and prompt; the two LongMemEval configurations differ in both judge
model and rubric ([report, protocol and how to reproduce it](benchmarks/head-to-head-v14/RESULTS.md)):

| Held-out questions, accuracy % | LoCoMo (1,144), published judge | LoCoMo, Mem0 judge | LongMemEval-S (400), official judge | LongMemEval-S, Mem0 judge |
|---|---:|---:|---:|---:|
| Mem0 Platform (gpt-5 answering, top 200 memories) | 88.46 | 94.32 | 91.00 | 91.75 |
| TAM (gpt-5 answering) | 86.54¹ | 94.23 | **92.25** | 90.75 |
| TAM (gpt-4.1-mini answering) | 88.02¹ | **94.32**¹ | 87.50 | 88.25 |

¹ English embedding preset (`MEMORY_TEXT_EMBED_MODEL=BAAI/bge-base-en-v1.5`); with the default
multilingual model, 87.50 and 92.57. No difference between TAM and Mem0 Platform at the same
answering model is statistically significant on these questions. That is not a demonstrated
equivalence, and the reported split was not scored blind (the report gives the tuning history); TAM
gets there retrieving locally and calling no LLM when it writes or searches. The report also lists how Mem0's published setup differs from the
earlier public protocol: a more lenient judge, 156 re-run questions, and answer-prompt hints that
match individual LoCoMo gold answers.

What changed:

| Change | How you use it |
|---|---|
| **Cross-encoder reads the neighbouring turns** | Automatic. Each candidate is scored alone and with the turns before and after it; `MEMORY_CROSS_RERANK_CONTEXT` (400 characters, `0` = off). |
| **Relative dates resolved in context mode** | Automatic. "last Thursday [Thu 14 December 2023]", counted from the record's timestamp; `MEMORY_CONTEXT_RESOLVE_DATES=off` disables it. |
| **Context budget shared by rank** | Automatic. The first hits keep long records whole; answers about something the assistant said reach the reader whole for every development question instead of 38%. |
| **`MEMORY_TEXT_EMBED_MODEL` works; models above 2 GB load** | Set it to change the model of ordinary records (re-embed with `python src/reembed.py --fastembed`). |
| **Head-to-head tooling** | `benchmarks/crossgrade_mem0.py`, `benchmarks/retrieval_eval.py`; the QA harnesses take reasoning models (`--answer-model gpt-5-2025-08-07`). |

---

## Version 14.4.0 — opt-in fact supersession, cheaper writes at 1M records

**Release date: 2026-09-22.**

A record can now retire the value it replaces. `memory_save(supersede=true)` looks for active
records of the same project and type that share the new record's opening words and end in a
different value ("X's citizenship is Argentina" → "... is Armenia", "billing runs on PostgreSQL
16" → "18"), marks them `superseded` and returns their ids. It is off by default: on a real
5,128-record store the rule would have retired 138 records that were not updates, such as
"likes jazz" next to "likes rock". On MemoryAgentBench FactConsolidation with gpt-4o-mini
([findings](benchmarks/memoryagentbench/FINDINGS.md)):

| | 14.3.1 | 14.4.0, `supersede=true` |
|---|---:|---:|
| FC single-hop, 6k / 262k | 82 / 85 | 99 / 93 |
| FC multi-hop, 6k / 262k | 13 / 3 | 27 / 9 |

What changed, including two write-side costs that grew with the store:

| Change | How you use it |
|---|---|
| **Fact supersession** | `memory_save(..., supersede=true)` or `memory_save_fast(..., supersede=true)` for single-valued facts. The response lists retired ids under `superseded`. |
| **Vector cache patches instead of reloading** | Automatic. Migration 036 logs which record each change touched; a cached pool re-reads only those rows when a search next uses it. Unscoped recall right after a save at 1M records: 4.2 s → 0.37 s. |
| **Concept name refresh uses an index** | Automatic. Migration 037 indexes graph node names that can match text; the 60-second refresh that stalled one save a minute takes 2.6 ms. Save p99 at 1M records: 1,432 → 140 ms ([report](benchmarks/scale-v14/RESULTS.md)). |

---

## Version 14.3.1 — updates are no longer dropped, recall stays fast at 1M records

**Release date: 2026-09-21.**

Two problems that got worse as a store grew. Dedup treated near-identical texts as repeats, so
an update that changed one value ("Messi's citizenship is Argentina" → "... Armenia",
"PostgreSQL 16" → "18") was dropped and the old value kept; on MemoryAgentBench
FactConsolidation 14.3.0 lost 36 of 455 facts. And several queries behind every recall and save
read the whole store. Measured on one machine with 200 synthetic tenants
([report](benchmarks/scale-v14/RESULTS.md)):

| | 14.3.0 | 14.3.1 |
|---|---:|---:|
| Tenant-scoped `memory_recall`, 100k records, p50 / p95 | 781 / 1,901 ms | 25 / 35 ms |
| Tenant-scoped `memory_recall`, 1M records, p50 / p95 | 6–64 s | 105 / 147 ms |
| HTTP calls/s, 100k records, 16 clients | 1.3 | 37 (one process), 117 (`MCP_HTTP_WORKERS=4`) |

| Change | How you use it |
|---|---|
| **Updates are kept** | Automatic. A record is a repeat only when it has the same words in the same order (case, punctuation and ё/е aside). A repeat replaces the stored record, so "A", then "B", then "A" leaves A as the latest. |
| **Scoped search follows the project, not the store** | Automatic. Migration 035 puts the project into the full-text index (rebuilt once, about 30 s per million records); graph seeds and `available_solutions` use indexes. |
| **HTTP workers** | `MCP_HTTP_WORKERS=4` with `MCP_TRANSPORT=http`: four server processes on one port, stateless sessions, POSIX only. In Docker Compose: `TAM_MCP_WORKERS=4`. |
| **Scale benchmark** | `benchmarks/scale_bench.py` loads a synthetic multi-tenant corpus through the real save path and measures recall, save, HTTP throughput and concurrent writers. |
| **MemoryAgentBench** | FactConsolidation results: [findings](benchmarks/memoryagentbench/FINDINGS.md). |

---

## Version 14.3.0 — Jev as the contradiction checker

**Release date: 2026-09-21.**

`memory_answer` can now check retrieved facts for contradictions with TypeSafe's Jev, a
System One model that answers typed questions with calibrated probabilities instead of
generating text. Every (supporting, opposing) pair becomes one `noul` question, and all of
them go in a single request. Measured on the same questions against the Claude Haiku 4.5
scorer ([report](benchmarks/knowledge-update-v14/RESULTS.md#jev-as-the-contradiction-scorer-1430)):
accuracy is unchanged within noise (LongMemEval knowledge-update 35/78 vs 36/78, control
15/50 vs 16/50). The median contradiction pass drops from 3.1 s to 1.9 s, and Jev billed
$0.038 for all 78 questions.

| Change | How you use it |
|---|---|
| **Jev contradiction scorer** | `MEMORY_CONTRADICTION_SCORER=jev` plus `TYPESAFE_API_KEY`. The default stays `llm`. The client retries 408, 429, 5xx and connection errors like the official SDKs, honours `retry-after-ms` / `Retry-After` up to 60 s, and reports `jev_*` counters and a `jev_request_ms` latency histogram. |
| **API key hygiene** | An empty key, or one containing a newline or other control character, is rejected before any request, and the error never contains the key. (The official Python and JS SDKs echo it; reported upstream as typesafe-sdk-python#9 and typesafe-sdk-js#14.) |
| **Benchmark harness** | `benchmarks/knowledge_update_eval.py` records per-question contradiction-pass time and Jev token usage, so the two scorers can be compared on cost as well as accuracy. |

---

## Version 14.2.0 — facts that change over time

**Release date: 2026-09-21.**

"Mary loves red", saved in May, and "Mary no longer likes red; she has fallen for green",
saved in August: `memory_answer` now says *green, previously red*. Measured with Claude
Haiku 4.5 on the same questions ([report](benchmarks/knowledge-update-v14/RESULTS.md)):
LongMemEval knowledge-update rose from 12/78 to 35/78, a Russian + English update suite from
8/30 to 22/30, and five other LongMemEval categories from 11/50 to 16/50.

| Change | How you use it |
|---|---|
| **Recording dates reach the reader** | `memory_answer`'s reader and verifier see when each record was saved. The latest record about the same subject gives the current value unless its text describes the past, and a value stays current until a later record changes it. Answers come back in the language of the question. |
| **Contradictions are resolved, not refused** | A hard contradiction now passes both sides, with their dates, to the reader. `MEMORY_CONTRADICTION_POLICY=abstain` restores the 14.1.0 refusal. The contradiction scorer sees the question, so a conflict about someone else no longer blocks the answer. |
| **Russian word forms** | The lexical recall tier stems Cyrillic terms (Snowball, new dependency `snowballstemmer`): "Маша" in a question finds "Маше" in a record, and claim grounding accepts the inflected name. |
| **One timestamp format** | Records are stored as `2026-09-21T08:21:37.622445Z` (UTC). Migration 034 rewrites older rows: `+00:00` and fraction-less values are reformatted, and zone-less values, which older versions wrote in local time, are converted with that zone's DST rules. The instants do not change, so atomic facts are not rebuilt. |

---

## Version 14.1.0 — what is new

**Release date: 2026-09-21.**

| Change | How you use it |
|---|---|
| **Negative retrieval in `memory_answer`** | Before reading, the grounded reader runs a second, contradiction-seeking search: a small model inverts the question, and each (supporting, opposing) pair — at most 5 × 5 — is scored in one batched call. Score ≥ 0.60 answers *Not enough information* without picking a side; 0.30–0.60 answers with a caveat; below 0.30 the answer is unchanged. The verdict is returned under `negative`. `MEMORY_NEGATIVE_RETRIEVAL=false` turns it off. |
| **`memory_answer` on Anthropic and Ollama** | Both providers now return schema-constrained output (forced tool call / JSON-schema `format`). Before this, Claude Haiku wrapped JSON in a markdown fence and `memory_answer` failed with *Reader returned invalid grounded evidence*. |

---

## Version 14.0.0 — what is new

**Release date: 2026-09-15.**
At release time v14 was distributed as a prepared wheel and a source checkout. Later 14.x releases are published on PyPI; see [installation](installation.md).

| Change | How you use it |
|---|---|
| **Personal, team and shared memory** | Install one server; give Vasya and Petya separate tokens. Select a scope when saving; search all areas you can access. Each area has its own database, graph and index. |
| **Authorship and revision history** | The token identifies the author and client. See who changed a record, when and why; revision checks prevent overwriting another person's edit. |
| **Remote MCP and team web interface** | Connect an IDE through the lightweight Python bridge. In the browser, select a scope, search, browse, save, edit and inspect history. The interface displays the product name, version and release date. |
| **Lower CPU pressure** | Fast mode remains the default. Embedding and optional PyTorch models default to one compute thread. The team server retains three workspace workers to avoid repeated model loading. |
| **Configurable internal LLMs** | Use Ollama, an OpenAI-compatible endpoint or Anthropic for internal text tasks; explicitly select a vision model for images. |
| **Safer retrieval and answers** | Scoped context, model-aware vector search and privacy-safe write intents. The optional grounded reader checks supporting evidence and rejects contradictory claims; it is not the default answer path. |
| **Installation and packaging fixes** | Wheel, source archive and Docker checks cover Linux, Windows and macOS; the source archive now includes test fixtures and installation support files. |

**Validation:** 2,162 tests passed in the checkout; 2,145 passed from the source archive. Native wheel checks passed on Ubuntu, Windows and macOS; 12 browser scenarios passed across Chromium, Firefox and WebKit. The expanded CI matrix still requires execution. See the [final verification report](benchmarks/release-final-v14-20260915/RESULTS.md) for skips, exact platforms and artifact hashes.

**Performance limits:** BGE is optional. On the measured Linux ARM64 setup, its one-thread p95 was about **2,179 ms**, above the 200 ms target. Limiting threads reduces parallel CPU load; it does not eliminate CPU work. No top-10 ranking or new default answer-quality improvement is claimed. [CPU measurements](benchmarks/grounded-v14/CPU_RESULTS.md).

[Full v14 release notes](RELEASE_V14.md) · [Changelog and previous releases](../CHANGELOG.md)
