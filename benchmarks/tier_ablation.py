#!/usr/bin/env python3
"""Per-tier ablation and weight tuning of `memory_recall`, without an LLM.

Every retrieval tier of the recall pipeline (FTS, semantic, fuzzy, graph, episode,
multi-representation, HyDE, atomic facts) is fused with a weight; 14.8.0 reads those
weights from MEMORY_RECALL_TIER_WEIGHTS and a weight of 0 switches the tier off. This
script measures what each tier is worth on LoCoMo and LongMemEval-S evidence recall:

  full        every tier at its default weight
  no_<tier>   the default weights with one tier switched off
  only_<tier> one tier alone
  <name>=<spec>  any weight table given with --weights, e.g. --weights lean=fuzzy=0;graph=0.3

Metric: the rank of the first gold evidence record in the ranked list (R@1/5/10, MRR),
per question category and overall; plus how often each tier produced candidates.

  python benchmarks/tier_ablation.py --bench locomo --split dev --work-root DIR --output OUT.json
  python benchmarks/tier_ablation.py --bench lme --split heldout --limit 100 --work-root DIR --output OUT.json
  python benchmarks/tier_ablation.py --bench locomo --split dev --tune --work-root DIR --output OUT.json
  python benchmarks/tier_ablation.py --bench live --store ~/.tam --sample 200 --work-root DIR --output OUT.json

`--bench live` probes an installed store (copied first, the original is untouched): for a
sample of records that carry a `context` field the query is that context and the gold is the
record itself, so the measurement is "does the memory come back from its own rationale". It
shows which tiers fire on real data (graph, multi-representation, episodes) that benchmark
stores never populate.

Stores are built once per unit (one LoCoMo conversation, one LongMemEval question) under
--work-root and reused by later runs. --tune runs coordinate ascent over a weight grid on
the given split and prints the best table; evaluate it on a held-out split afterwards with
--weights. No LLM is called; record_usage is off so a query cannot influence the next one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCOMO = ROOT / "benchmarks" / "data" / "locomo" / "data" / "locomo10.json"
LONGMEMEVAL = ROOT / "benchmarks" / "data" / "longmemeval_s.json"
LME_SPLITS = ROOT / "docs" / "benchmarks" / "head-to-head-v14" / "splits"
LIVE_MIN_CONTEXT_CHARS = 40
LIVE_MIN_CONTENT_CHARS = 80
LOCOMO_SPLITS = {"dev": range(3), "test": range(3, 10), "all": range(10)}
LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop"}
RANKS = (1, 5, 10)
SEARCH_LIMIT = 50
LME_PIECE_CHARS = 2000
EMBED_BATCH = 64
TUNE_GRID = (0.0, 0.5, 1.0, 1.5, 2.0)
TUNE_PASSES = 2
GOLD_TABLE = "ablation_gold"


# ── datasets ────────────────────────────────────────────────────────────────

def split_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > max_chars:
            pieces.append(current)
            current = ""
        while len(line) > max_chars:
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        current += line
    if current:
        pieces.append(current)
    return pieces


def locomo_units(split: str):
    data = json.loads(LOCOMO.read_text())
    for index in LOCOMO_SPLITS[split]:
        sample = data[index]
        conv = sample["conversation"]
        keys = sorted((k for k in conv if k.startswith("session_") and not k.endswith("_date_time")),
                      key=lambda k: int(k.split("_")[1]))
        fragments = []
        for key in keys:
            date = conv.get(f"{key}_date_time", "")
            for turn in conv[key] if isinstance(conv[key], list) else []:
                if not turn.get("text") or not turn.get("dia_id"):
                    continue
                content = f"[{date}] {turn.get('speaker', '')}: {turn['text']}"
                if turn.get("blip_caption"):
                    content += f"\n(image: {turn['blip_caption']})"
                fragments.append({"text": content, "session": key, "gold": turn["dia_id"]})
        questions = []
        for qn, qa in enumerate(sample.get("qa", [])):
            if qa.get("category") not in LOCOMO_CATEGORIES:
                continue
            gold = {g.strip() for raw in qa.get("evidence") or [] for g in re.split(r"[;,\s]+", raw) if g.strip()}
            if gold:
                questions.append({"id": f"c{index}q{qn}", "question": qa["question"], "gold": gold,
                                  "category": LOCOMO_CATEGORIES[qa["category"]]})
        yield f"locomo_{index}", fragments, questions


def lme_units(split: str, limit: int, categories: frozenset[str] = frozenset()):
    data = {entry["question_id"]: entry for entry in json.loads(LONGMEMEVAL.read_text())}
    if split == "all":
        ids = sorted(data, key=lambda qid: hashlib.sha256(qid.encode()).hexdigest())
    else:
        ids = (LME_SPLITS / f"longmemeval-{split}-ids.txt").read_text().split()
    taken = 0
    for qid in ids:
        if limit and taken >= limit:
            break
        entry = data[qid]
        if qid.endswith("_abs") or (categories and entry["question_type"] not in categories):
            continue
        fragments = []
        for sid, date, session in zip(entry["haystack_session_ids"], entry["haystack_dates"],
                                      entry["haystack_sessions"], strict=True):
            for tn, turn in enumerate(session):
                if not turn["content"].strip():
                    continue
                for pn, piece in enumerate(split_text(f"[{date}] {turn['role']}: {turn['content']}", LME_PIECE_CHARS)):
                    fragments.append({"text": piece, "session": sid,
                                      "gold": f"{sid}:{tn}:{pn}" if turn.get("has_answer") else None})
        gold = {f["gold"] for f in fragments if f["gold"]}
        if not gold:
            continue
        taken += 1
        yield f"lme_{qid}", fragments, [{"id": qid, "question": entry["question"], "gold": gold,
                                         "category": entry["question_type"]}]


def live_unit(source: Path, work_root: Path, sample: int, seed: int):
    """Copy an installed store and pick sample records with a usable `context`."""
    import random
    import sqlite3

    source_db = source / "memory.db" if source.is_dir() else source
    if not source_db.is_file():
        raise SystemExit(f"no memory.db under {source}")
    unit_dir = work_root / "live" / f"{source_db.stem}-{hashlib.sha256(str(source_db).encode()).hexdigest()[:8]}"
    unit_dir.mkdir(parents=True, exist_ok=True)
    target_db = unit_dir / "memory.db"
    if not target_db.exists():
        origin = sqlite3.connect(str(source_db))
        copy = sqlite3.connect(str(target_db))
        with copy:
            origin.backup(copy)
        copy.close()
        origin.close()
    db = sqlite3.connect(str(target_db))
    rows = db.execute(
        "SELECT id, project, context FROM knowledge WHERE status='active' AND context IS NOT NULL "
        "AND length(context) >= ? AND length(content) >= ? ORDER BY id",
        (LIVE_MIN_CONTEXT_CHARS, LIVE_MIN_CONTENT_CHARS)).fetchall()
    db.close()
    random.Random(seed).shuffle(rows)
    questions = [{"id": str(row[0]), "question": row[2], "gold": {str(row[0])}, "category": row[1] or "general",
                  "project": row[1]} for row in rows[:sample]]
    return unit_dir, questions


# ── store per unit ───────────────────────────────────────────────────────────

def open_store(srv, unit_dir: Path, fragments: list[dict] | None):
    """Return a Store for the unit, ingesting the fragments on first use (None = live store)."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    marker = unit_dir / "ingested.json"
    srv.MEMORY_DIR = unit_dir
    store = srv.Store(background_queues=False)
    if store._embed_mode == "none":
        raise RuntimeError("no embedding backend; the semantic tier cannot be measured")
    if fragments is None:
        return store
    # Gold ids live in a side table, not in tags: a tag becomes a graph concept node and
    # would feed the graph tier with the answer key.
    store.db.execute(f"CREATE TABLE IF NOT EXISTS {GOLD_TABLE} (knowledge_id INTEGER PRIMARY KEY, gold TEXT NOT NULL)")
    store.db.commit()
    if marker.exists() and json.loads(marker.read_text()).get("fragments") == len(fragments):
        return store
    started = time.perf_counter()
    texts = [fragment["text"] for fragment in fragments]
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), EMBED_BATCH):
        chunk = texts[offset:offset + EMBED_BATCH]
        batch = store.embed(chunk)
        if batch is None or len(batch) != len(chunk) or any(vector is None for vector in batch):
            raise RuntimeError("embedding backend returned no vectors")
        vectors.extend(batch)
    for fragment, vector in zip(fragments, vectors, strict=True):
        record_id, *_ = store.save_knowledge(
            fragment["session"], fragment["text"], "fact", project="ablation", tags=[],
            skip_dedup=True, skip_quality=True, source_format="conversation", repeat="confirm",
            embedding=vector)
        if record_id is None:
            raise RuntimeError("TAM did not store a fragment")
        if fragment.get("gold"):
            store.db.execute(f"INSERT OR REPLACE INTO {GOLD_TABLE} (knowledge_id, gold) VALUES (?, ?)",
                             (record_id, fragment["gold"]))
    store.db.commit()
    if store.cache is not None:
        store.cache.invalidate()
    if getattr(store, "v9_cache", None) is not None:
        store.v9_cache.invalidate_all()
    marker.write_text(json.dumps({"fragments": len(fragments), "seconds": round(time.perf_counter() - started, 1),
                                  "embed_model": store._active_embed_model_name()}))
    return store


def gold_of(store, ids: list[int]) -> list[set[str]]:
    if not ids:
        return []
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (GOLD_TABLE,)).fetchone():
        return [{str(record_id)} for record_id in ids]  # live store: a record is its own gold
    rows = store.db.execute(
        f"SELECT knowledge_id, gold FROM {GOLD_TABLE} WHERE knowledge_id IN ({','.join('?' * len(ids))})",
        ids).fetchall()
    gold = {row[0]: {row[1]} for row in rows}
    return [gold.get(record_id, set()) for record_id in ids]


# ── configurations ───────────────────────────────────────────────────────────

def parse_weight_spec(spec: str, tiers: tuple[str, ...]) -> dict[str, float]:
    """`fts=1;fuzzy=0` (';' or ',' separated) into a table over the known tiers."""
    weights: dict[str, float] = {}
    for part in re.split(r"[;,]", spec):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        name = name.strip().lower()
        if not sep or name not in tiers:
            raise ValueError(f"unknown tier {name!r}; known: {', '.join(tiers)}")
        weights[name] = float(value)
        if weights[name] < 0:
            raise ValueError(f"{name}: weight must be >= 0")
    return weights


def ablation_configs(defaults: dict[str, float], tiers: tuple[str, ...], extra: dict[str, dict[str, float]]):
    configs = {"full": dict(defaults)}
    for tier in tiers:
        configs[f"no_{tier}"] = {**defaults, tier: 0.0}
    for tier in tiers:
        configs[f"only_{tier}"] = {name: (defaults[name] if name == tier else 0.0) for name in tiers}
    for name, table in extra.items():
        configs[name] = {**defaults, **table}
    return configs


# ── evaluation ───────────────────────────────────────────────────────────────

class Evaluator:
    def __init__(self, srv, work_root: Path, bench: str):
        self.srv = srv
        self.work_root = work_root / bench
        self.bench = bench
        self.units: list[tuple[str, Path, list[dict], list[dict]]] = []

    def load(self, units) -> None:
        for unit_id, fragments, questions in units:
            if questions:
                self.units.append((unit_id, self.work_root / unit_id, fragments, questions))

    def load_live(self, unit_dir: Path, questions: list[dict]) -> None:
        self.units.append((unit_dir.name, unit_dir, None, questions))

    def ingest_all(self) -> None:
        for unit_id, unit_dir, fragments, _ in self.units:
            if fragments is None:
                continue
            started = time.perf_counter()
            store = open_store(self.srv, unit_dir, fragments)
            store.db.close()
            print(json.dumps({"unit": unit_id, "fragments": len(fragments),
                              "seconds": round(time.perf_counter() - started, 1)}), flush=True)

    def run(self, configs: dict[str, dict[str, float]]) -> dict[str, list[dict]]:
        """Per config: one row per question with the first gold rank and the tiers used."""
        rows: dict[str, list[dict]] = {name: [] for name in configs}
        for unit_id, unit_dir, fragments, questions in self.units:
            store = open_store(self.srv, unit_dir, fragments)
            store.bump_recall = lambda ids: None
            recall = self.srv.Recall(store)
            try:
                for question in questions:
                    for name, weights in configs.items():
                        started = time.perf_counter()
                        found = recall.search(question["question"], project=question.get("project", "ablation"),
                                              limit=SEARCH_LIMIT, detail="compact", record_usage=False,
                                              _explain=True, tier_weights=weights)
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        groups = found.get("results", {})
                        ranked = [hit["id"] for group in (groups.values() if isinstance(groups, dict) else [groups])
                                  for hit in group]
                        first = next((rank for rank, keys in enumerate(gold_of(store, ranked))
                                      if keys & question["gold"]), None)
                        rows[name].append({"unit": unit_id, "id": question["id"], "category": question["category"],
                                           "first_rank": first, "ms": round(elapsed_ms, 1),
                                           "tiers_used": found.get("_explain", {}).get("tiers_used", [])})
            finally:
                store.db.close()
        return rows


def summarize(rows: list[dict], tiers: tuple[str, ...]) -> dict:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets["all"].append(row)
        buckets[row["category"]].append(row)
    summary = {}
    for bucket, items in sorted(buckets.items(), key=lambda kv: (kv[0] != "all", kv[0])):
        ranks = [row["first_rank"] for row in items]
        entry = {"n": len(items)}
        for rank in RANKS:
            entry[f"R@{rank}"] = round(sum(1 for r in ranks if r is not None and r < rank) / len(items), 4)
        entry["MRR"] = round(sum(1.0 / (r + 1) for r in ranks if r is not None) / len(items), 4)
        summary[bucket] = entry
    summary["tier_fired"] = {tier: round(sum(1 for row in rows if tier in row["tiers_used"]) / len(rows), 3)
                             for tier in tiers}
    latencies = sorted(row["ms"] for row in rows)
    summary["latency_ms"] = {"p50": latencies[len(latencies) // 2], "p95": latencies[int(len(latencies) * 0.95)]}
    return summary


def objective(summary: dict) -> tuple[float, float]:
    return summary["all"]["R@10"], summary["all"]["MRR"]


def markdown(results: dict[str, dict], categories: list[str]) -> str:
    lines = ["| config | n | R@1 | R@5 | R@10 | MRR | p50 ms | " + " | ".join(f"{c} R@10" for c in categories) + " |",
             "|---|---:|---:|---:|---:|---:|---:|" + "---:|" * len(categories)]
    for name, summary in results.items():
        all_ = summary["all"]
        cells = [name, str(all_["n"]), f"{all_['R@1']:.3f}", f"{all_['R@5']:.3f}", f"{all_['R@10']:.3f}",
                 f"{all_['MRR']:.3f}", f"{summary['latency_ms']['p50']:.0f}"]
        cells += [f"{summary[c]['R@10']:.3f}" if c in summary else "-" for c in categories]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def tune(evaluator: Evaluator, defaults: dict[str, float], tiers: tuple[str, ...]) -> tuple[dict[str, float], dict]:
    """Coordinate ascent over TUNE_GRID, TUNE_PASSES passes, objective (R@10, MRR)."""
    best = dict(defaults)
    best_summary = summarize(evaluator.run({"current": best})["current"], tiers)
    best_score = objective(best_summary)
    print(json.dumps({"tune": "start", "score": best_score, "weights": best}), flush=True)
    for pass_no in range(TUNE_PASSES):
        for tier in tiers:
            candidates = {f"{tier}={value:g}": {**best, tier: value} for value in TUNE_GRID if value != best[tier]}
            for name, rows in evaluator.run(candidates).items():
                summary = summarize(rows, tiers)
                score = objective(summary)
                if score > best_score:
                    best, best_summary, best_score = candidates[name], summary, score
                    print(json.dumps({"tune": "improve", "pass": pass_no, "set": name, "score": score}), flush=True)
    return best, best_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bench", choices=("locomo", "lme", "live"), required=True)
    parser.add_argument("--store", type=Path, help="live: the installed memory dir (or memory.db) to copy and probe")
    parser.add_argument("--sample", type=int, default=200, help="live: how many records to probe")
    parser.add_argument("--seed", type=int, default=1, help="live: sample seed")
    parser.add_argument("--split", default="dev", help="locomo: dev|test|all; lme: dev|heldout|all")
    parser.add_argument("--limit", type=int, default=0, help="lme: first N questions of the split (0 = all)")
    parser.add_argument("--category", action="append", default=[], metavar="TYPE",
                        help="lme: keep only questions of this question_type (repeatable)")
    parser.add_argument("--work-root", type=Path, required=True, help="where the per-unit stores live")
    parser.add_argument("--output", type=Path, required=True, help="JSON results; a .md table is written next to it")
    parser.add_argument("--cross-rerank", choices=("on", "off"), default="off",
                        help="cross-encoder stage; off isolates the tiers, on measures the production path")
    parser.add_argument("--weights", action="append", default=[], metavar="NAME=SPEC",
                        help="extra configuration, e.g. lean=fuzzy=0;graph=0.3 (repeatable)")
    parser.add_argument("--only-extra", action="store_true", help="run only full plus the --weights configs")
    parser.add_argument("--tune", action="store_true", help="coordinate ascent instead of the ablation table")
    parser.add_argument("--ingest-only", action="store_true", help="build the stores and stop")
    args = parser.parse_args()

    os.environ["MEMORY_MODE"] = "fast"
    os.environ["MEMORY_LLM_ENABLED"] = "false"
    os.environ["MEMORY_ASYNC_ENRICHMENT"] = "false"
    os.environ["MEMORY_CROSS_RERANK"] = args.cross_rerank
    os.environ.setdefault("TAM_MEMORY_DIR", str(args.work_root / "bootstrap"))
    os.environ.setdefault("CLAUDE_MEMORY_DIR", str(args.work_root / "bootstrap"))
    args.work_root.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "src"))
    import config
    import server as srv

    tiers = config.RECALL_TIERS
    defaults = dict(config.DEFAULT_RECALL_TIER_WEIGHTS)
    extra = {}
    for item in args.weights:
        name, sep, spec = item.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"--weights needs NAME=SPEC, got {item!r}")
        extra[name.strip()] = parse_weight_spec(spec, tiers)

    evaluator = Evaluator(srv, args.work_root, args.bench)
    if args.bench == "live":
        if args.store is None:
            raise SystemExit("--bench live needs --store")
        evaluator.load_live(*live_unit(args.store.expanduser(), args.work_root, args.sample, args.seed))
    else:
        evaluator.load(locomo_units(args.split) if args.bench == "locomo"
                       else lme_units(args.split, args.limit, frozenset(args.category)))
    if not evaluator.units:
        raise SystemExit("no questions selected")
    started = time.perf_counter()
    evaluator.ingest_all()
    if args.ingest_only:
        return 0

    payload = {"bench": args.bench, "split": args.split, "limit": args.limit, "cross_rerank": args.cross_rerank,
               "units": len(evaluator.units), "questions": sum(len(q) for *_, q in evaluator.units),
               "defaults": defaults}
    if args.tune:
        best, summary = tune(evaluator, defaults, tiers)
        payload["tuned"] = {"weights": best, "summary": summary,
                            "env": ",".join(f"{name}={value:g}" for name, value in best.items())}
        results = {"full": summarize(evaluator.run({"full": defaults})["full"], tiers), "tuned": summary}
    else:
        configs = ({"full": defaults, **{name: {**defaults, **table} for name, table in extra.items()}}
                   if args.only_extra else ablation_configs(defaults, tiers, extra))
        rows = evaluator.run(configs)
        results = {name: summarize(rows[name], tiers) for name in configs}
        payload["configs"] = configs
        payload["rows"] = rows
    payload["results"] = results
    payload["seconds"] = round(time.perf_counter() - started, 1)
    categories = sorted({c for summary in results.values() for c in summary
                         if c not in ("all", "tier_fired", "latency_ms")})
    if args.bench == "live":
        categories = []  # one column per project is not a category breakdown
    table = markdown(results, categories)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=1))
    args.output.with_suffix(".md").write_text(
        f"# Tier ablation: {args.bench} {args.split} (cross-rerank {args.cross_rerank})\n\n{table}\n\n"
        f"Tier fired (full): {json.dumps(results['full']['tier_fired'])}\n")
    print(table)
    print(json.dumps({"seconds": payload["seconds"], "output": str(args.output)}))
    if args.tune:
        print(f"MEMORY_RECALL_TIER_WEIGHTS={payload['tuned']['env']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
