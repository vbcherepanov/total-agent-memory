#!/usr/bin/env bash
# LoCoMo in the default cross-encoder mode (auto) with and without warm-up, and in
# `on`, three fresh-store runs each, one run at a time so latency is not shared.
#
#   benchmarks/locomo_rerank_matrix.sh <tree> <version-label> <commit> <db-root> [out-dir]
#
# Rounds interleave the conditions, so slow drift of the machine spreads over all of
# them. After the runs, every rerun is compared with run 1 of the same condition.
set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "usage: $0 <tree> <version-label> <commit> <db-root> [out-dir]" >&2
  exit 2
fi

TREE=$1
VERSION=$2
COMMIT=$3
DB_ROOT=$4
REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT=${5:-$REPO/benchmarks/results/rerank-modes}
PY=${PYTHON:-$REPO/.venv/bin/python}
RUNS=${RUNS:-3}
WARMUP_QUERIES=${WARMUP_QUERIES:-3}
RUNNER=$REPO/benchmarks/locomo_rerank_modes.py
NOTE="serial runs, one at a time; onnxruntime default threads"

export MEMORY_LLM_ENABLED=false
mkdir -p "$DB_ROOT" "$OUT"

measure() {
  local mode=$1 label=$2
  shift 2
  echo "[matrix] $(date -u +%H:%M:%S) $VERSION $mode $label" >&2
  "$PY" "$RUNNER" --tree "$TREE" --version-label "$VERSION" --commit "$COMMIT" \
    --db-root "$DB_ROOT" --out-dir "$OUT" --mode "$mode" --label "$label" --lane-note "$NOTE" "$@"
  rm -rf "${DB_ROOT:?}/$VERSION-$mode-$label"
}

for run in $(seq 1 "$RUNS"); do
  measure auto "cold-run$run"
  measure auto "warm-run$run" --wait-for-encoder --warmup-queries "$WARMUP_QUERIES"
  measure on "run$run"
done

for stem in auto-cold auto-warm on; do
  base=${stem%-*}
  [[ $stem == on ]] && base=on
  suffix=${stem#auto-}
  [[ $stem == on ]] && suffix=""
  first="$OUT/$VERSION-$base-${suffix:+$suffix-}run1.rows.jsonl.gz"
  for run in $(seq 2 "$RUNS"); do
    other="$OUT/$VERSION-$base-${suffix:+$suffix-}run$run.rows.jsonl.gz"
    "$PY" "$RUNNER" --compare "$first" "$other" > "$OUT/$VERSION-$stem-compare-run1-run$run.json"
  done
done
echo "[matrix] $(date -u +%H:%M:%S) $VERSION done" >&2
