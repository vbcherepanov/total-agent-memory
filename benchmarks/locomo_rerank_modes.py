#!/usr/bin/env python3
"""One LoCoMo retrieval run in one MEMORY_CROSS_RERANK mode, with per-question rows.

locomo_bench.py reports totals, which cannot show whether two runs ranked the
same records. This runner reuses its ingest and scoring and also writes, for
every question, the ranked record ids, the latency and whether the cross-encoder
had finished loading when the question was asked. Comparing those rows is how a
rerun is checked for identity (see --compare).

`--tree` points at any source tree, so a released tag can be measured unmodified.
Each run ingests into a fresh store unless --skip-ingest is given.

    python benchmarks/locomo_rerank_modes.py --mode auto --label run1 \
        --db-root /tmp/stores --out-dir benchmarks/results/rerank-modes
    python benchmarks/locomo_rerank_modes.py --compare a.rows.jsonl.gz b.rows.jsonl.gz
"""
from __future__ import annotations

import argparse
import gzip
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODES = ("off", "on", "auto")
GRADED_K = (1, 5, 10)
TOP_K = 10
ADVERSARIAL_CATEGORY = 5
PACKAGES = ("fastembed", "onnxruntime", "chromadb", "numpy")
PROGRESS_EVERY = 100


def cap_encoder_threads(threads: int) -> None:
    """Run the cross-encoder on `threads` threads so concurrent runs do not oversubscribe the CPU.

    onnxruntime otherwise uses every core in each process. Patched here, not in
    the product, so that a released tag stays unmodified.
    """
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    from memory_core.cross_rerank import CrossReranker
    from memory_core.fastembed_loader import load_model

    CrossReranker._load_fastembed = staticmethod(lambda model: load_model(TextCrossEncoder, model, threads=threads))


def load_bench(tree: Path):
    spec = importlib.util.spec_from_file_location("locomo_bench", tree / "benchmarks" / "locomo_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["locomo_bench"] = module
    spec.loader.exec_module(module)
    return module


def package_versions() -> dict[str, str]:
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def cpu_model() -> str:
    if sys.platform != "darwin":
        return platform.processor() or platform.machine()
    return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                          check=True).stdout.strip()


def tree_version(tree: Path) -> str:
    namespace: dict = {}
    exec((tree / "src" / "version.py").read_text(encoding="utf-8"), namespace)
    return namespace["VERSION"]


class RowRecorder:
    """Wraps Recall.search and keeps what each call returned."""

    def __init__(self, server_mod, gold_by_question: dict[tuple[str, str], tuple[int, list[str]]],
                 extract_dia_ids):
        from memory_core.cross_rerank import shared_reranker

        self.rows: list[dict] = []
        self._gold = gold_by_question
        self._extract = extract_dia_ids
        self._shared_reranker = shared_reranker
        self._original = server_mod.Recall.search

    def search(self, recall, *args, **kwargs):
        if self.rows and len(self.rows) % PROGRESS_EVERY == 0:
            print(f"[rerank-modes] {len(self.rows)} questions done", flush=True)
        reranker = self._shared_reranker()
        ready_before = bool(reranker and reranker.ready)
        started = time.perf_counter()
        response = self._original(recall, *args, **kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        entries = response.get("results", {}).get("fact", [])
        project, question = kwargs["project"], kwargs["query"]
        category, evidence = self._gold[(project, question)]
        dia = [(self._extract(entry) or [""])[0] for entry in entries]
        self.rows.append({
            "project": project,
            "question": question,
            "category": category,
            "evidence": evidence,
            "record_ids": [entry["id"] for entry in entries],
            "dia_ids": dia,
            "latency_ms": round(elapsed_ms, 3),
            "encoder_ready_before": ready_before,
        })
        return response


def gold_index(samples: list[dict]) -> dict[tuple[str, str], tuple[int, list[str]]]:
    gold = {}
    for index, sample in enumerate(samples):
        for qa in sample.get("qa", []):
            gold[(f"locomo_{index}", qa.get("question", ""))] = (qa.get("category", 0), list(qa.get("evidence") or []))
    return gold


def graded(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["category"] != ADVERSARIAL_CATEGORY and row["evidence"]]


def summarize(rows: list[dict]) -> dict:
    scored = graded(rows)
    recalls = {}
    for k in GRADED_K:
        hits = sum(1 for row in scored if set(row["dia_ids"][:k]) & set(row["evidence"]))
        recalls[f"R@{k}"] = round(hits / len(scored), 4) if scored else 0.0
    latencies = sorted(row["latency_ms"] for row in rows)
    scored_latencies = [row["latency_ms"] for row in scored]
    return {
        "queries": len(rows),
        "graded": len(scored),
        **recalls,
        "latency_p50_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "latency_p95_ms": round(latencies[int(0.95 * len(latencies))], 2) if latencies else 0.0,
        "latency_p50_graded_ms": round(statistics.median(scored_latencies), 2) if scored_latencies else 0.0,
        "queries_before_encoder_ready": sum(1 for row in rows if not row["encoder_ready_before"]),
    }


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def compare(first: Path, second: Path) -> dict:
    """How many questions got a different ranking in two runs, by record text position."""
    a, b = read_rows(first), read_rows(second)
    if [(r["project"], r["question"]) for r in a] != [(r["project"], r["question"]) for r in b]:
        raise ValueError("the two runs did not ask the same questions in the same order")
    differing = [i for i, (x, y) in enumerate(zip(a, b)) if x["dia_ids"] != y["dia_ids"]]
    top5 = [i for i in differing if a[i]["dia_ids"][:5] != b[i]["dia_ids"][:5]]
    hit_changed = [i for i in differing if (set(a[i]["dia_ids"][:5]) & set(a[i]["evidence"])) != (
        set(b[i]["dia_ids"][:5]) & set(b[i]["evidence"]))]
    return {
        "questions": len(a),
        "rankings_differ": len(differing),
        "top5_differ": len(top5),
        "r5_gold_hits_differ": len(hit_changed),
        "first_differing_positions": differing[:20],
        "last_differing_position": differing[-1] if differing else None,
    }


def run(args: argparse.Namespace) -> dict:
    tree = Path(args.tree).resolve()
    db_path = Path(args.db_root) / f"{args.version_label}-{args.mode}-{args.label}"
    if not args.skip_ingest and db_path.exists():
        shutil.rmtree(db_path)
    if args.mode == "auto":
        os.environ.pop("MEMORY_CROSS_RERANK", None)
    else:
        os.environ["MEMORY_CROSS_RERANK"] = args.mode

    bench = load_bench(tree)
    bench.setup_env(db_path, disable_llm=True)
    server_mod = bench.import_store()
    module_path = Path(server_mod.__file__).resolve()
    if tree not in module_path.parents:
        raise RuntimeError(f"server was imported from {module_path}, expected a file under {tree}")

    samples = bench.load_dataset(bench.DATASET)
    if args.limit_samples:
        samples = samples[:args.limit_samples]
    if args.encoder_threads:
        cap_encoder_threads(args.encoder_threads)
    ingest_stats = {} if args.skip_ingest else bench.ingest(server_mod, samples)

    from config import get_cross_rerank_mode
    from memory_core.cross_rerank import shared_reranker

    if args.wait_for_encoder and shared_reranker() is not None:
        shared_reranker().wait_ready()

    recorder = RowRecorder(server_mod, gold_index(samples), bench.extract_dia_ids)

    def search(recall, *search_args, **search_kwargs):
        return recorder.search(recall, *search_args, **search_kwargs)

    server_mod.Recall.search = search
    load_before = os.getloadavg()
    started = time.time()
    bench.eval_samples(server_mod, samples, top_k=TOP_K, limit_qa=args.limit_qa)
    eval_seconds = round(time.time() - started, 1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.version_label}-{args.mode}-{args.label}"
    with gzip.open(out_dir / f"{stem}.rows.jsonl.gz", "wt", encoding="utf-8") as handle:
        for row in recorder.rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {
        "version": tree_version(tree),
        "version_label": args.version_label,
        "commit": args.commit,
        "mode_requested": args.mode,
        "mode_effective": get_cross_rerank_mode(),
        "env_cross_rerank": os.environ.get("MEMORY_CROSS_RERANK", "<unset>"),
        "label": args.label,
        "fresh_store": not args.skip_ingest,
        "wait_for_encoder": args.wait_for_encoder,
        "encoder_threads": args.encoder_threads or "onnxruntime default",
        "lane_note": args.lane_note,
        "summary": summarize(recorder.rows),
        "ingest": ingest_stats,
        "eval_seconds": eval_seconds,
        "load_average_before": [round(v, 2) for v in load_before],
        "load_average_after": [round(v, 2) for v in os.getloadavg()],
        "machine": {"cpu": cpu_model(), "cores": os.cpu_count(), "os": platform.platform()},
        "python": platform.python_version(),
        "packages": package_versions(),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare", nargs=2, metavar="ROWS", help="compare two .rows.jsonl.gz files and exit")
    parser.add_argument("--mode", choices=MODES, help="auto leaves MEMORY_CROSS_RERANK unset, as an install does")
    parser.add_argument("--label", default="run1")
    parser.add_argument("--version-label", default="worktree")
    parser.add_argument("--commit", default="unknown")
    parser.add_argument("--tree", default=str(REPO_ROOT), help="source tree to measure")
    parser.add_argument("--db-root", default=None, help="parent directory of the store")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "benchmarks" / "results" / "rerank-modes"))
    parser.add_argument("--skip-ingest", action="store_true", help="reuse the store left by an earlier run")
    parser.add_argument("--wait-for-encoder", action="store_true", help="load the cross-encoder before the first query")
    parser.add_argument("--encoder-threads", type=int, default=None,
                        help="cross-encoder threads; default lets onnxruntime use every core")
    parser.add_argument("--limit-samples", type=int, default=None)
    parser.add_argument("--limit-qa", type=int, default=None)
    parser.add_argument("--lane-note", default="", help="free text: what else ran on the machine")
    args = parser.parse_args()

    if args.compare:
        print(json.dumps(compare(Path(args.compare[0]), Path(args.compare[1])), indent=2))
        return 0
    if not args.mode or not args.db_root:
        parser.error("--mode and --db-root are required for a run")
    report = run(args)
    print(json.dumps({key: report[key] for key in ("version", "mode_effective", "label", "summary")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
