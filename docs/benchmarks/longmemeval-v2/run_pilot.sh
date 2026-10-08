#!/usr/bin/env bash
# LongMemEval-V2 small-tier pilot with TAM: two operating points (cross-encoder off / on),
# both domains, reader qwen3.5-9b on local Ollama, judge gpt-5.2 through the local budget
# proxy with a hard dollar ceiling shared by the whole pilot.
#
#   LME_HARNESS_DIR=... LME_DATA_ROOT=... LME_PYTHON=... run_pilot.sh OUT_DIR
#
# Environment:
#   LME_HARNESS_DIR   checkout prepared by setup_harness.sh (required)
#   LME_DATA_ROOT     small-tier data from fetch_small_tier.py (required)
#   LME_PYTHON        interpreter of the harness virtualenv (required)
#   TAM_BENCH_PYTHON  interpreter with TAM's dependencies (default <repo>/.venv/bin/python)
#   CEILING_USD       hard budget for all judge calls of the pilot (default 15)
#   QUESTIONS_PER_DOMAIN   stratified question sample per domain, or "all" (default 10)
#   OPERATING_POINTS  space-separated subset of "norerank rerank" (default both)
#   READER_BASE_URL   default http://127.0.0.1:11434/v1 (Ollama)
#   READER_MODEL      default qwen3.5-9b (ollama create qwen3.5-9b -f Modelfile.qwen3.5-9b)
#   READER_KEY_FILE   file holding only the reader API key, for a hosted reader
#                     (e.g. READER_BASE_URL=https://api.deepinfra.com/v1/openai,
#                     READER_MODEL=Qwen/Qwen3.5-9B); the harness reads it, it is never exported
#   KEY_FILE / UPSTREAM   as in tam_bench_common/run_guarded.py (dry runs: local stub + dummy key)
#
# Outputs under OUT_DIR: tam_<point>_<domain>_small/ (harness outputs: per_question.jsonl,
# aggregated_metrics.json, run_args.json), tam_<point>_small_combined_metrics.json,
# budget/ledger.jsonl + budget/summary.json, run.log. Exit code 3 = budget stop.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
OUT_DIR="${1:?usage: run_pilot.sh OUT_DIR}"
: "${LME_HARNESS_DIR:?set LME_HARNESS_DIR}" "${LME_DATA_ROOT:?set LME_DATA_ROOT}" "${LME_PYTHON:?set LME_PYTHON}"
CEILING_USD="${CEILING_USD:-15}"
UPSTREAM="${UPSTREAM:-https://api.openai.com/v1}"
export TAM_BENCH_PYTHON="${TAM_BENCH_PYTHON:-$REPO_ROOT/.venv/bin/python}"
export QUESTIONS_PER_DOMAIN="${QUESTIONS_PER_DOMAIN:-10}"
export OPERATING_POINTS="${OPERATING_POINTS:-norerank rerank}"
export READER_BASE_URL="${READER_BASE_URL:-http://127.0.0.1:11434/v1}"
export READER_MODEL="${READER_MODEL:-qwen3.5-9b}"
export LME_HARNESS_DIR LME_DATA_ROOT LME_PYTHON

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
export OUT_DIR
# TAM stores live in OUT_DIR/tam-stores only while a run is alive.
trap 'rm -rf "$OUT_DIR/tam-stores"' EXIT

guard_args=(--ceiling-usd "$CEILING_USD" --allow-model gpt-5.2 --out-dir "$OUT_DIR" --upstream "$UPSTREAM")
if [ -n "${KEY_FILE:-}" ]; then
  guard_args+=(--key-file "$KEY_FILE")
fi

set +e
caffeinate -i "$LME_PYTHON" "$REPO_ROOT/docs/benchmarks/tam_bench_common/run_guarded.py" "${guard_args[@]}" -- \
  bash "$HERE/pilot_inner.sh" 2>&1 | tee "$OUT_DIR/run.log"
status="${PIPESTATUS[0]}"
set -e
cat "$OUT_DIR/budget/summary.json"
exit "$status"
