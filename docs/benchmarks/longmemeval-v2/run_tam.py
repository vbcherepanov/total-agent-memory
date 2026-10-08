#!/usr/bin/env python3
"""Run the LongMemEval-V2 harness with the TAM memory backend for one domain.

The official harness (evaluation/harness.py of github.com/xiaowu0162/LongMemEval-V2) is
used unmodified: this script mirrors evaluation/run_eval.py (materialise the selected
questions and their haystack, write the memory config, call harness.main), registers the
`tam` backend by importing tam_memory.py, and adds no benchmark-specific logic.

The judge (gpt-5.2) must go through the budget proxy: --evaluator-base-url defaults to
TAM_BENCH_PROXY_URL (set by tam_bench_common/run_guarded.py) and the script refuses to
start without it. The reader is a local OpenAI-compatible server (Ollama) and gets no key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
READER_KEY_ENV = "LME_READER_KEY"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness-dir", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--domain", required=True, choices=["web", "enterprise"])
    parser.add_argument("--tier", default="small", choices=["small", "medium"])
    parser.add_argument("--question-ids", default=None, help="comma-separated ids (default: all in the domain)")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cross-rerank", required=True, choices=["on", "off"])
    parser.add_argument("--tam-python", default=str(REPO_ROOT / ".venv" / "bin" / "python"),
                        help="interpreter with TAM's dependencies for the TAM worker process")
    parser.add_argument("--work-root", required=True, help="parent directory for the temporary TAM store")
    parser.add_argument("--top-k", type=int, default=10, help="recall limit when the budget fill is off")
    parser.add_argument("--no-fill-budget", dest="fill_budget", action="store_false",
                        help="plain top-k instead of TAM's budget fill")
    parser.add_argument("--fill-pool", type=int, default=100, help="ranked hits the budget fill draws from")
    parser.add_argument("--context-radius", type=int, default=1,
                        help="states before and after each hit (same trajectory) that come with it")
    parser.add_argument("--max-context-chars", type=int, default=48000)
    parser.add_argument("--per-hit-max-chars", type=int, default=8000)
    parser.add_argument("--index-mode", default="state", choices=["state", "chunk"],
                        help="state: one fragment per page state; chunk: cleaned page regions, deduplicated")
    parser.add_argument("--chunk-chars", type=int, default=1200, help="chunk mode: page region size")
    parser.add_argument("--chunk-pool", type=int, default=200, help="chunk mode: ranked fragments folded into states")
    parser.add_argument("--state-full-chars", type=int, default=6000,
                        help="chunk mode: pages up to this size are shown whole")
    parser.add_argument("--state-max-chars", type=int, default=12000, help="chunk mode: cap per shown state")
    parser.add_argument("--per-page-cap", type=int, default=2,
                        help="chunk mode: states shown per (page title, URL path)")
    parser.add_argument("--clean-query", action="store_true",
                        help="drop the answer-format sentences from the question before searching")
    parser.add_argument("--extra-memory-params", default=None,
                        help="JSON object merged into memory_params (e.g. embed_provider/embed_env for a local "
                             "embedding server)")
    parser.add_argument("--save-memory", action="store_true",
                        help="also save the built TAM index to OUTPUT_DIR/memory_state (harness --save-memory)")
    parser.add_argument("--load-memory-dir", default=None,
                        help="reuse a saved memory_state (same index parameters) instead of rebuilding")
    parser.add_argument("--reader-model", default="qwen3.5-9b")
    parser.add_argument("--reader-base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--reader-key-file", default=None,
                        help="file holding only the reader API key (a hosted reader); read by the harness, "
                             "never placed in the environment")
    parser.add_argument("--reader-temperature", type=float, default=0.6)
    parser.add_argument("--reader-top-p", type=float, default=0.95)
    parser.add_argument("--reader-top-k", type=int, default=20)
    parser.add_argument("--reader-max-concurrent-requests", type=int, default=1)
    parser.add_argument("--max-completion-tokens", type=int, default=20000)
    parser.add_argument("--memory-context-max-tokens", type=int, default=200000)
    parser.add_argument("--evaluator-model", default="gpt-5.2")
    parser.add_argument("--evaluator-base-url", default=os.environ.get("TAM_BENCH_PROXY_URL"))
    parser.add_argument("--evaluator-reasoning-effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--evaluator-max-completion-tokens", type=int, default=4096)
    args = parser.parse_args()
    if args.reader_key_file and not Path(args.reader_key_file).expanduser().is_file():
        parser.error(f"--reader-key-file {args.reader_key_file!r} does not exist")
    if not args.evaluator_base_url:
        parser.error("--evaluator-base-url (or TAM_BENCH_PROXY_URL from run_guarded.py) is required: "
                     "the judge only runs behind the budget proxy")
    return args


EMPTY_TEXT_ATTEMPTS = 3


def _retry_empty_reader_text(harness, attempts: int) -> None:
    """A reader that spends its whole completion budget on thinking returns empty text; the
    harness then raises and loses every answer of the domain. Ask again (sampling is on, so a
    retry can finish), and after the last attempt record an empty answer — the harness's own
    outcome for a request the provider rejects (BadRequestError)."""
    original = harness.call_reader_model_async

    async def call_with_retry(client, args, messages):
        for attempt in range(1, attempts + 1):
            try:
                return await original(client, args, messages)
            except RuntimeError as exc:
                if str(exc) != "Model returned empty text":
                    raise
                print(f"Reader returned empty text (attempt {attempt}/{attempts})", file=sys.stderr, flush=True)
        return "", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    harness.call_reader_model_async = call_with_retry


def main() -> None:
    args = parse_args()
    harness_dir = args.harness_dir.resolve()
    data_root = args.data_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    sys.path.insert(0, str(harness_dir))
    sys.path.insert(0, str(HERE))

    import tam_memory  # noqa: F401  (registers memory_type "tam")
    from data.public_data import materialize_runtime_haystack, materialize_runtime_questions, write_json

    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    question_ids = [item.strip() for item in (args.question_ids or "").split(",") if item.strip()] or None
    selected = materialize_runtime_questions(data_root=data_root, domain=args.domain, question_ids=question_ids,
                                             limit=None, output_path=runtime_dir / "questions.json")
    materialize_runtime_haystack(data_root=data_root, tier=args.tier, selected_questions=selected,
                                 output_path=runtime_dir / "haystack.json")
    memory_config = {
        "memory_type": "tam",
        "memory_params": {
            "tam_src": str(REPO_ROOT / "src"),
            "python_executable": args.tam_python,
            "top_k": args.top_k,
            "fill_budget": args.fill_budget,
            "fill_pool": args.fill_pool,
            "context_radius": args.context_radius,
            "max_context_chars": args.max_context_chars,
            "per_hit_max_chars": args.per_hit_max_chars,
            "index_mode": args.index_mode,
            "chunk_chars": args.chunk_chars,
            "chunk_pool": args.chunk_pool,
            "state_full_chars": args.state_full_chars,
            "state_max_chars": args.state_max_chars,
            "per_page_cap": args.per_page_cap,
            "clean_query": args.clean_query,
            "cross_rerank": args.cross_rerank,
            "work_root": args.work_root,
        },
    }
    if args.extra_memory_params:
        extra = json.loads(args.extra_memory_params)
        if not isinstance(extra, dict):
            raise SystemExit("--extra-memory-params must be a JSON object")
        memory_config["memory_params"].update(extra)
    write_json(runtime_dir / "memory_config.json", memory_config)

    harness_argv = [
        "evaluation.harness",
        "--domain", args.domain,
        "--questions-path", str(runtime_dir / "questions.json"),
        "--haystack-path", str(runtime_dir / "haystack.json"),
        "--trajectories-path", str(data_root / "trajectories.jsonl"),
        "--memory-config-path", str(runtime_dir / "memory_config.json"),
        "--output-dir", str(output_dir),
        "--model", args.reader_model,
        "--base-url", args.reader_base_url,
        "--api-key-env", READER_KEY_ENV,
        "--temperature", str(args.reader_temperature),
        "--top-p", str(args.reader_top_p),
        "--top-k", str(args.reader_top_k),
        "--max-completion-tokens", str(args.max_completion_tokens),
        "--memory-context-max-tokens", str(args.memory_context_max_tokens),
        "--reader-max-concurrent-requests", str(args.reader_max_concurrent_requests),
        "--prompt-build-max-workers", "1",
        "--evaluator-model", args.evaluator_model,
        "--evaluator-base-url", args.evaluator_base_url,
        "--evaluator-api-key-env", "OPENAI_API_KEY",
        "--evaluator-reasoning-effort", args.evaluator_reasoning_effort,
        "--evaluator-max-completion-tokens", str(args.evaluator_max_completion_tokens),
    ]
    if args.reader_key_file:
        harness_argv.extend(["--api-key-file", str(Path(args.reader_key_file).expanduser().resolve())])
    if args.save_memory:
        harness_argv.append("--save-memory")
    if args.load_memory_dir:
        harness_argv.extend(["--load-memory-dir", str(Path(args.load_memory_dir).expanduser().resolve())])
    print(json.dumps({"event": "run_tam_start", "runtime_dir": str(runtime_dir), "domain": args.domain,
                      "questions": len(selected), "cross_rerank": args.cross_rerank}), flush=True)
    os.environ.pop(READER_KEY_ENV, None)
    sys.argv = harness_argv
    import evaluation.harness as harness
    from evaluation.harness import main as harness_main

    _retry_empty_reader_text(harness, attempts=EMPTY_TEXT_ATTEMPTS)

    harness_main()


if __name__ == "__main__":
    main()
