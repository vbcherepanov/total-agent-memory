# LongMemEval-V2 with total-agent-memory (TAM)

TAM as a memory backend (`memory_type: "tam"`) for the official
[LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) harness, small tier
(one 100-trajectory haystack per domain). Method, models and what is *not* used
(screenshots, question images for retrieval, any LLM inside the memory):
[SYSTEM_DESCRIPTION.md](SYSTEM_DESCRIPTION.md).

## Files

| File | Purpose |
|---|---|
| `tam_memory.py` | The backend: `insert()` / `query()` per the harness interface |
| `run_tam.py` | One domain: materialise questions + haystack (as `evaluation/run_eval.py` does), register `tam`, call the unmodified `evaluation/harness.py` |
| `run_pilot.sh`, `pilot_inner.sh` | The pilot: both operating points x both domains behind the budget guard, then `combine_aggregated_metrics.py` |
| `select_questions.py` | Seeded question subset, stratified by question type within a domain |
| `fetch_small_tier.py` | Text-only small-tier data from the pinned dataset revision, sha256-verified |
| `setup_harness.sh` | Harness checkout at commit `2cc8c540` + harness requirements + data |
| `Modelfile.qwen3.5-9b` | Ollama alias `qwen3.5-9b` = `qwen3.5:9b` weights with `num_ctx 65536` |
| `requirements-harness.txt`, `requirements-tam.txt` | The two interpreters: harness (Python >= 3.11, torch for the harness's token counting) and TAM worker |

Budget guard, key handling, prices and the stub: `../tam_bench_common/README.md`.

## Setup

```bash
S=/path/to/scratch
uv venv --python 3.12 "$S/lme-venv"
docs/benchmarks/longmemeval-v2/setup_harness.sh "$S/LongMemEval-V2" "$S/lme-small" "$S/lme-venv/bin/python"
ollama create qwen3.5-9b -f docs/benchmarks/longmemeval-v2/Modelfile.qwen3.5-9b   # needs `ollama pull qwen3.5:9b`
```

The leaderboard packager requires the reader name in `run_args.json` to contain
`qwen3.5-9b` (and the judge to contain `gpt-5.2`); Ollama's `qwen3.5:9b` does not match,
hence the alias. The TAM worker runs in the TAM repo's `.venv` (`TAM_BENCH_PYTHON`).
The first query downloads the `Qwen/Qwen3.5-9B` processor files from Hugging Face (the
harness counts memory-context tokens with it).

The key goes into `~/.config/tam-bench/openai.env` (mode 600, `OPENAI_API_KEY=...`). Only
the judge uses it, and only through the budget proxy.

## Pilot (20 questions: 10 web + 10 enterprise, two operating points, ceiling $15)

```bash
S=/path/to/scratch
LME_HARNESS_DIR="$S/LongMemEval-V2" LME_DATA_ROOT="$S/lme-small" LME_PYTHON="$S/lme-venv/bin/python" \
CEILING_USD=15 QUESTIONS_PER_DOMAIN=10 \
  docs/benchmarks/longmemeval-v2/run_pilot.sh "$S/runs/lme-pilot-$(date +%Y%m%d-%H%M)"
```

Selected questions (seed 20260928):
- web: `0401f0c8,15e5efa6,4121647c,42d13006,5d6b993a,5d6ecdeb,8957a127,96497069,d16a18fe,d3300354`
- enterprise: `229675cd,2721ca7f,7586cf7c,89439d62,95ae824f,a58c5548,aa64a8bd,ce1cdb2d,d1f82e80,f0882f6e`

8 of the 20 are judged by gpt-5.2 (abstention / gotchas checkers), so each operating
point makes 8 judge calls; the other questions are scored by string/choice matching.

Outputs: `tam_{norerank,rerank}_{web,enterprise}_small/` (harness `per_question.jsonl`,
`aggregated_metrics.json` with `memory_query.avg_seconds`, `run_args.json`),
`tam_<point>_small_combined_metrics.json`, `budget/ledger.jsonl`, `budget/summary.json`,
`run.log`. Exit code 3 = the guard stopped the run before the ceiling.

Full small tier (240 web + 211 enterprise questions): `QUESTIONS_PER_DOMAIN=all`.

## Hosted reader and embeddings

The paper serves Qwen3.5-9B in bf16 with vLLM. A local Ollama build is quantized and takes
minutes per question on a laptop; a hosted OpenAI-compatible endpoint serving the same
weights in bf16 (DeepInfra `Qwen/Qwen3.5-9B`) runs a question in seconds. The packager
lowercases the reader name before its `qwen3.5-9b` check, so `Qwen/Qwen3.5-9B` passes.

```bash
printf '%s\n' "$DEEPINFRA_KEY" > ~/.config/tam-bench/deepinfra.key; chmod 600 ~/.config/tam-bench/deepinfra.key
READER_BASE_URL=https://api.deepinfra.com/v1/openai READER_MODEL=Qwen/Qwen3.5-9B \
READER_KEY_FILE=~/.config/tam-bench/deepinfra.key LME_HARNESS_DIR=... LME_DATA_ROOT=... LME_PYTHON=... \
  docs/benchmarks/longmemeval-v2/run_pilot.sh "$S/runs/lme-hosted"
```

TAM's embeddings can use the same endpoint through `--extra-memory-params` (or
`TAM_EXTRA_MEMORY_PARAMS`): `embed_provider: "openai"`, `embed_env` with
`MEMORY_EMBED_API_BASE` and `MEMORY_EMBED_MODEL`, and `embed_key_file`. The TAM worker reads the
key from that file when it starts; the key is not in `memory_config.json` (which goes into a
leaderboard package) and not in the harness's environment. A hosted base must be listed in
`HOSTED_EMBED_BASES` (`tam_bench_common/tam_worker.py`) and needs `embed_key_file`; any other
non-local base is refused.

## Dry run (no paid call)

```bash
printf 'OPENAI_API_KEY=dry-run-dummy\n' > "$S/dummy.env"; chmod 600 "$S/dummy.env"
.venv/bin/python docs/benchmarks/tam_bench_common/stub_openai.py --port 18802 \
  --expect-key-file "$S/dummy.env" --log "$S/stub-requests.jsonl" &
LME_HARNESS_DIR=... LME_DATA_ROOT=... LME_PYTHON=... UPSTREAM=http://127.0.0.1:18802/v1 KEY_FILE="$S/dummy.env" \
  docs/benchmarks/longmemeval-v2/run_pilot.sh "$S/dry/lme"
# fast plumbing check without Ollama: READER_BASE_URL=http://127.0.0.1:18802/v1 (the stub answers \boxed{UNKNOWN})
```

## Cost expectation

A judge call carries the question, the reference and the reader's full response
(thinking text included when the reader returns it), a few thousand tokens at most, plus
up to 4,096 reasoning+output tokens at gpt-5.2 prices ($1.75 / $14 per 1M): at most about
$0.07 per call, typically $0.01–0.02. The pilot makes 16 judge calls (about $0.3); the full
small tier (156 judged questions per operating point) about $2–5 per point.

## Measured locally (Apple M2 Max, 64 GB; 2026-09-28 dry runs)

| Step | web (100 traj., 1,837 fragments) | enterprise (100 traj., 3,458 fragments) |
|---|---:|---:|
| TAM index build (embed + save) | 157 s | 281 s |
| `memory_query` avg, `norerank` | 0.11 s | 0.15 s |
| `memory_query` avg, `rerank` | 3.3 s | 3.5 s |

Reader qwen3.5-9b (Q4_K_M, Ollama, thinking on): 42–71 s per question (prompt 8–18k
tokens, 640–1,220 completion tokens), about 55 s on average. Expected pilot wall time:
about 15 min of index builds + about 40 min of reader calls for 2 x 20 questions.
