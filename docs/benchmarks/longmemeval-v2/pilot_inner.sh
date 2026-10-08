#!/usr/bin/env bash
# Inner loop of run_pilot.sh; runs under run_guarded.py (TAM_BENCH_PROXY_URL is set).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${TAM_BENCH_PROXY_URL:?pilot_inner.sh must run under run_guarded.py}"

for point in $OPERATING_POINTS; do
  case "$point" in
    norerank) rerank=off ;;
    rerank) rerank=on ;;
    *) echo "unknown operating point $point" >&2; exit 2 ;;
  esac
  for domain in ${DOMAINS:-web enterprise}; do
    selection=()
    explicit_ids_var="QUESTION_IDS_${domain}"
    if [ -n "${!explicit_ids_var:-}" ]; then
      selection=(--question-ids "${!explicit_ids_var}")
    elif [ "$QUESTIONS_PER_DOMAIN" != "all" ]; then
      selection=(--question-ids "$("$LME_PYTHON" "$HERE/select_questions.py" --data-root "$LME_DATA_ROOT" \
        --domain "$domain" --count "$QUESTIONS_PER_DOMAIN")")
    fi
    run_dir="$OUT_DIR/tam_${POINT_PREFIX:-}${point}_${domain}_small"
    started=$(date +%s)
    "$LME_PYTHON" "$HERE/run_tam.py" \
      --harness-dir "$LME_HARNESS_DIR" \
      --data-root "$LME_DATA_ROOT" \
      --domain "$domain" \
      ${selection[@]+"${selection[@]}"} \
      --output-dir "$run_dir" \
      --cross-rerank "$rerank" \
      --tam-python "$TAM_BENCH_PYTHON" \
      --work-root "$OUT_DIR/tam-stores" \
      --reader-model "$READER_MODEL" \
      --reader-base-url "$READER_BASE_URL" \
      ${READER_KEY_FILE:+--reader-key-file "$READER_KEY_FILE"} \
      ${TAM_EXTRA_MEMORY_PARAMS:+--extra-memory-params "$TAM_EXTRA_MEMORY_PARAMS"} \
      ${SAVE_MEMORY:+--save-memory} \
      ${LOAD_MEMORY_ROOT:+--load-memory-dir "$LOAD_MEMORY_ROOT/$domain"} \
      ${TAM_EXTRA_ARGS:-}
    echo "{\"event\": \"domain_done\", \"point\": \"$point\", \"domain\": \"$domain\", \"wall_seconds\": $(( $(date +%s) - started ))}"
  done
  [ -f "$OUT_DIR/tam_${POINT_PREFIX:-}${point}_enterprise_small/aggregated_metrics.json" ] &&
    [ -f "$OUT_DIR/tam_${POINT_PREFIX:-}${point}_web_small/aggregated_metrics.json" ] || continue
  "$LME_PYTHON" "$LME_HARNESS_DIR/leaderboard/combine_aggregated_metrics.py" \
    "$OUT_DIR/tam_${POINT_PREFIX:-}${point}_enterprise_small/aggregated_metrics.json" \
    "$OUT_DIR/tam_${POINT_PREFIX:-}${point}_web_small/aggregated_metrics.json" \
    -o "$OUT_DIR/tam_${POINT_PREFIX:-}${point}_small_combined_metrics.json"
done
