# v11.0.0 Release Final Summary (2026-04-28)

This is the session-end record. The full architecture overview lives in
`docs/archive/v11/RELEASE-NOTES.md`. This file captures **what actually shipped**
after benchmarking, what's known to regress, and what to do next.

## Final LoCoMo numbers (1986 QA, gpt-4o gen, gpt-4o-mini judge)

Run: `benchmarks/results/v11_FINAL_v2_recall_fixed.json`
Command: see `docs/archive/v11/RELEASE-NOTES.md` § Roadmap.

| Category | n | Acc | R@1 | R@5 | R@10 |
|---|---|---|---|---|---|
| single-hop | 282 | 0.443 | 0.199 | 0.461 | 0.585 |
| temporal | 321 | 0.573 | **0.427** | **0.673** | **0.754** |
| multi-hop | 96 | 0.469 | 0.208 | 0.396 | 0.458 |
| open-domain | 841 | 0.595 | 0.395 | 0.638 | 0.706 |
| adversarial | 446 | **0.998** | 0.282 | 0.529 | 0.552 |
| **OVERALL** | 1986 | **0.654** | — | — | — |
| no-adv | 1540 | 0.554 | 0.354 | 0.598 | 0.679 |

**Position:** #6 on the public LoCoMo leaderboard (Mem0 0.669 above us, LangMem 0.581 below).

**vs v9 paper-method baseline (0.624):** **+3.0pp overall, +31.3pp temporal, +15pp R@5**.

**vs v9 ensemble3 archive best (0.696):** **−4.2pp** — we did not run the
ensemble stack on v11 due to budget (would have cost ~$25 OpenAI).

## What's broken / known regressions

1. **multi-hop −10.4pp vs v9 paper-method baseline.** The episode tier
   substitutes episode summaries for exact dialog turns on 2-3-hop chains.
   Free fix lined up: per-category episode-tier weight (disable for cat 3).
   Subset evidence: episode-OFF gave +1.5pp overall on 200 QA (Mem0 ≈ +0.3pp shy
   of being above with this fix on a full run — unverified).

2. **single-hop R@1 = 0.20.** Retrieval not focused enough. `--query-rewrite`
   would help on this category but hurt others in the subset experiment.

3. **NLI verifier deployed but inactive** (`V11_SKIP_NLI=1` is the bench
   default after W5 calibration). The calibrated thresholds work cleanly on
   dialogue+paraphrase fixtures (5% false-contradict, 88% true-contradict
   recall), but they fire on IDK-styled answers. In the v11 router that's a
   no-op (the route is already IDK), so we ship calibration but keep the bench
   flag on.

## Things you (the user) need to do

### 1. Rotate the API keys

Both keys appeared verbatim in chat earlier. Treat as compromised.

- Anthropic: https://console.anthropic.com/settings/keys → revoke
  `sk-ant-api03-SDH...` and create a new one.
- OpenAI: https://platform.openai.com/api-keys → revoke `sk-proj-mjcQg9...`
  and create a new one.

### 2. (Optional) Make the project a git repo and commit

The project is currently not a git repository. Backup is at
`~/claude-memory-server-backup-20260428-083415.tar.gz` (6 MB).

```bash
cd ~/claude-memory-server
git init
git add .
git commit -m "feat: v11.0.0 — episodes, IRCoT, Allen's algebra, NLI verifier, idle daemon

- 9 new modules across memory_core/ai_layer/workers
- 4 new MCP tools (memory_recall_iterative, memory_temporal_query,
  memory_entity_resolve, memory_consolidate_status)
- Episode tier (Tier 6) in Recall._search_impl
- LoCoMo bench post-processor (--v11-pipeline)
- Migrations 023 (episodes), 024 (entity_aliases), 025 (consolidation_state)
- ~460 new tests; layer-wall enforced via AST
- Final LoCoMo: 0.654 overall (+3.0pp vs v9 paper-method baseline)"
git tag v11.0.0
```

Do not run these commands as Claude — Claude does not commit per project rules.

### 3. Run the daemon (optional)

```bash
mkdir -p ~/Library/Logs/claude-memory
sed -e "s|{{HOME}}|$HOME|g" \
    -e "s|{{REPO}}|$HOME/claude-memory-server|g" \
    -e "s|{{PYTHON}}|$HOME/claude-memory-server/.venv/bin/python|g" \
    ~/claude-memory-server/scripts/com.claude-memory.consolidation.plist \
  > ~/Library/LaunchAgents/com.claude-memory.consolidation.plist
launchctl load ~/Library/LaunchAgents/com.claude-memory.consolidation.plist
```

## Cost summary

| Item | Cost |
|---|---|
| W4 first attempt (Haiku-judge bug) | $0 (404'd before billable) |
| W4 retry (full 1986 gpt-4o) | ~$5 |
| Subset 50 (recall fix smoke) | ~$0.13 |
| Subset 200 (full v9-stack experiment, hurt) | ~$1 |
| Full 1986 v2 (recall-fixed) | ~$4 |
| Subset 200 (episode-off ablation) | ~$0.5 |
| **Total OpenAI burn** | **~$10.6 of $11** |

Anthropic credits: separate budget, used by Haiku-200 ablations and Wave 1 LLM-touching tests. Order of magnitude < $5.

## Roadmap to top-5 (for next session)

| Goal | Action | Cost |
|---|---|---|
| **#5 (≥ 0.696)** | `--v11-pipeline --query-rewrite --hyde --entity-boost --few-shot-pairs --ensemble 3 --ensemble-mode judge` (+ per-category episode weight fix) | ~$25 OpenAI |
| **#4 (≥ 0.71)** | + Day 3 schema-locked graph (30 canonical predicates) | + $5 + 3 h |
| **#3 (≥ 0.76)** | + text-embedding-3-large embed swap + LoCoMo-style synth + BGE-v2-m3 reranker | + $10 + 6 h |
| **#2 (≥ 0.80)** | + fine-tune embed on LoCoMo + custom answer prompts | + $30 + 2 days |
| **#1 (≥ 0.85, MemMachine)** | + multi-hop graph-walk retrieval OR fine-tuned generator | + $80 + 1 week |

Detailed pre-existing plan in MCP memory record #3787.
