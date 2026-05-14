"""Merge duplicate graph_nodes accumulated before migration 026.

Production databases shipped with case-sensitive name matching and no
UNIQUE constraint, so the same logical entity ("vue", "Vue", "Vue.js")
or the same name with different classifier-assigned types ("vue/concept"
vs "vue/technology") could exist as separate nodes. This tool collapses
those duplicates after migration 026 has populated `name_norm`.

Algorithm:
  1. Build groups by (name_norm, type). For each group with N>1: pick
     winner = max mention_count, tie-break by oldest first_seen_at.
     Repoint edges/knowledge_nodes to winner, delete losers.
  2. Build groups by name_norm only (any type). For each remaining group
     with N>1: winner = same rule. Repoint edges/knowledge_nodes to
     winner. Aggregate mention_count. The losers keep their `type` value
     intact in the audit log before deletion — we do NOT silently change
     the winner's type, that decision belongs to the operator.
  3. Optionally `remove_orphans` (graph_nodes that have zero edges AND
     zero knowledge_nodes links).
  4. Optionally `--add-unique` to install the UNIQUE constraint that
     migration 026 deliberately omitted (it can only land safely after
     duplicates are gone).

Default mode is `--dry-run`. Run with `--apply` to mutate the database.
Always make a fresh `cp memory.db memory.db.before-dedup.bak` first.

Usage:
    .venv/bin/python src/tools/merge_duplicate_nodes.py [options]

Options:
    --dry-run            Report planned actions, do not mutate. (DEFAULT)
    --apply              Execute the merge.
    --case-only          Only merge case-variants (name_norm, type); skip
                         type-collisions. Conservative mode.
    --skip-orphans       Don't call remove_orphans at the end.
    --add-unique         After merge, install UNIQUE INDEX on
                         (name_norm, type). Refuses if any duplicate
                         remains.
    --db PATH            Override DB path (default: ~/.claude-memory/memory.db).
    --limit N            Cap number of groups processed per phase.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))


def _log(msg: str) -> None:
    sys.stderr.write(
        f"[merge-dups] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n"
    )
    sys.stderr.flush()


# ──────────────────────────────────────────────
# Group detection
# ──────────────────────────────────────────────


@dataclass
class DupGroup:
    name_norm: str
    type: str | None       # None when grouping across types
    winner: str
    losers: list[str] = field(default_factory=list)
    winner_name: str = ""
    winner_type: str = ""
    winner_mentions: int = 0
    loser_details: list[tuple[str, str, str, int]] = field(default_factory=list)
    # tuple: (id, name, type, mention_count)


def _pick_winner(rows: list[sqlite3.Row]) -> sqlite3.Row:
    """Highest mention_count, tie-break by oldest first_seen_at."""
    return sorted(
        rows,
        key=lambda r: (-int(r["mention_count"] or 0), r["first_seen_at"] or ""),
    )[0]


def detect_case_groups(
    db: sqlite3.Connection, limit: int | None = None
) -> list[DupGroup]:
    """Same (name_norm, type), N>1 — pure case variants."""
    rows = db.execute(
        """SELECT id, name, type, name_norm, mention_count, first_seen_at
             FROM graph_nodes
            WHERE name_norm IS NOT NULL AND name_norm != ''
            ORDER BY name_norm, type"""
    ).fetchall()

    buckets: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        buckets[(r["name_norm"], r["type"])].append(r)

    groups: list[DupGroup] = []
    for (norm, type_), members in buckets.items():
        if len(members) < 2:
            continue
        winner = _pick_winner(members)
        losers = [m for m in members if m["id"] != winner["id"]]
        groups.append(DupGroup(
            name_norm=norm,
            type=type_,
            winner=winner["id"],
            winner_name=winner["name"],
            winner_type=winner["type"],
            winner_mentions=int(winner["mention_count"] or 0),
            losers=[m["id"] for m in losers],
            loser_details=[(m["id"], m["name"], m["type"], int(m["mention_count"] or 0)) for m in losers],
        ))
    groups.sort(key=lambda g: -sum(int(d[3]) for d in g.loser_details))
    return groups[:limit] if limit else groups


def detect_type_collision_groups(
    db: sqlite3.Connection,
    already_processed: set[str],
    limit: int | None = None,
) -> list[DupGroup]:
    """Same name_norm, different types — classifier disagreement.

    Only includes node IDs that survived the case-merge phase.
    """
    rows = db.execute(
        """SELECT id, name, type, name_norm, mention_count, first_seen_at
             FROM graph_nodes
            WHERE name_norm IS NOT NULL AND name_norm != ''
            ORDER BY name_norm"""
    ).fetchall()

    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["id"] in already_processed:
            continue
        buckets[r["name_norm"]].append(r)

    groups: list[DupGroup] = []
    for norm, members in buckets.items():
        types = {m["type"] for m in members}
        if len(members) < 2 or len(types) < 2:
            continue
        winner = _pick_winner(members)
        losers = [m for m in members if m["id"] != winner["id"]]
        groups.append(DupGroup(
            name_norm=norm,
            type=None,
            winner=winner["id"],
            winner_name=winner["name"],
            winner_type=winner["type"],
            winner_mentions=int(winner["mention_count"] or 0),
            losers=[m["id"] for m in losers],
            loser_details=[(m["id"], m["name"], m["type"], int(m["mention_count"] or 0)) for m in losers],
        ))
    groups.sort(key=lambda g: -sum(int(d[3]) for d in g.loser_details))
    return groups[:limit] if limit else groups


# ──────────────────────────────────────────────
# Merge ops
# ──────────────────────────────────────────────


def _repoint_edges(
    db: sqlite3.Connection, loser_id: str, winner_id: str
) -> tuple[int, int, int]:
    """Move edges from loser to winner.

    Returns (relocated, deduped, self_loops_removed).
    """
    relocated = 0
    deduped = 0
    self_loops = 0

    # Outgoing: loser is source.
    rows = db.execute(
        "SELECT id, target_id, relation_type, weight, context, "
        "reinforcement_count FROM graph_edges WHERE source_id = ?",
        (loser_id,),
    ).fetchall()
    for r in rows:
        if r["target_id"] == winner_id:
            db.execute("DELETE FROM graph_edges WHERE id = ?", (r["id"],))
            self_loops += 1
            continue
        existing = db.execute(
            "SELECT id, weight, reinforcement_count FROM graph_edges "
            "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
            (winner_id, r["target_id"], r["relation_type"]),
        ).fetchone()
        if existing:
            new_w = min(float(existing["weight"]) + float(r["weight"] or 0.0) * 0.5, 10.0)
            new_rc = int(existing["reinforcement_count"] or 0) + int(r["reinforcement_count"] or 0) + 1
            db.execute(
                "UPDATE graph_edges SET weight=?, reinforcement_count=? WHERE id=?",
                (new_w, new_rc, existing["id"]),
            )
            db.execute("DELETE FROM graph_edges WHERE id = ?", (r["id"],))
            deduped += 1
        else:
            db.execute(
                "UPDATE graph_edges SET source_id = ? WHERE id = ?",
                (winner_id, r["id"]),
            )
            relocated += 1

    # Incoming: loser is target.
    rows = db.execute(
        "SELECT id, source_id, relation_type, weight, context, "
        "reinforcement_count FROM graph_edges WHERE target_id = ?",
        (loser_id,),
    ).fetchall()
    for r in rows:
        if r["source_id"] == winner_id:
            db.execute("DELETE FROM graph_edges WHERE id = ?", (r["id"],))
            self_loops += 1
            continue
        existing = db.execute(
            "SELECT id, weight, reinforcement_count FROM graph_edges "
            "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
            (r["source_id"], winner_id, r["relation_type"]),
        ).fetchone()
        if existing:
            new_w = min(float(existing["weight"]) + float(r["weight"] or 0.0) * 0.5, 10.0)
            new_rc = int(existing["reinforcement_count"] or 0) + int(r["reinforcement_count"] or 0) + 1
            db.execute(
                "UPDATE graph_edges SET weight=?, reinforcement_count=? WHERE id=?",
                (new_w, new_rc, existing["id"]),
            )
            db.execute("DELETE FROM graph_edges WHERE id = ?", (r["id"],))
            deduped += 1
        else:
            db.execute(
                "UPDATE graph_edges SET target_id = ? WHERE id = ?",
                (winner_id, r["id"]),
            )
            relocated += 1
    return relocated, deduped, self_loops


def _repoint_knowledge_links(
    db: sqlite3.Connection, loser_id: str, winner_id: str
) -> tuple[int, int]:
    """Move knowledge_nodes links from loser to winner.

    Returns (relocated, deduped).
    """
    relocated = 0
    deduped = 0
    rows = db.execute(
        "SELECT knowledge_id, role, strength FROM knowledge_nodes WHERE node_id = ?",
        (loser_id,),
    ).fetchall()
    for r in rows:
        existing = db.execute(
            "SELECT 1 FROM knowledge_nodes WHERE knowledge_id = ? AND node_id = ?",
            (r["knowledge_id"], winner_id),
        ).fetchone()
        if existing:
            db.execute(
                "DELETE FROM knowledge_nodes WHERE knowledge_id = ? AND node_id = ?",
                (r["knowledge_id"], loser_id),
            )
            deduped += 1
        else:
            db.execute(
                "UPDATE knowledge_nodes SET node_id = ? "
                "WHERE knowledge_id = ? AND node_id = ?",
                (winner_id, r["knowledge_id"], loser_id),
            )
            relocated += 1
    return relocated, deduped


def merge_group(db: sqlite3.Connection, group: DupGroup) -> dict[str, int]:
    """Apply merge for one group. Returns per-group counters."""
    stats = {
        "edges_relocated": 0, "edges_deduped": 0, "edges_self_loops": 0,
        "links_relocated": 0, "links_deduped": 0,
        "loser_mentions_absorbed": 0,
    }
    for loser_id in group.losers:
        er, ed, esl = _repoint_edges(db, loser_id, group.winner)
        lr, ld = _repoint_knowledge_links(db, loser_id, group.winner)
        stats["edges_relocated"] += er
        stats["edges_deduped"] += ed
        stats["edges_self_loops"] += esl
        stats["links_relocated"] += lr
        stats["links_deduped"] += ld

    # Aggregate mention counts onto winner; delete losers.
    total_mentions = sum(int(d[3]) for d in group.loser_details)
    stats["loser_mentions_absorbed"] = total_mentions
    db.execute(
        "UPDATE graph_nodes SET mention_count = mention_count + ? WHERE id = ?",
        (total_mentions, group.winner),
    )
    placeholders = ",".join("?" * len(group.losers))
    db.execute(
        f"DELETE FROM graph_nodes WHERE id IN ({placeholders})", group.losers,
    )
    return stats


def remove_orphans(db: sqlite3.Connection) -> int:
    cur = db.execute(
        """DELETE FROM graph_nodes
           WHERE id NOT IN (SELECT DISTINCT source_id FROM graph_edges
                            UNION
                            SELECT DISTINCT target_id FROM graph_edges)
             AND id NOT IN (SELECT DISTINCT node_id FROM knowledge_nodes)"""
    )
    return cur.rowcount


def install_unique_index(db: sqlite3.Connection) -> bool:
    """Install the UNIQUE index on (name_norm, type). Refuses if any
    duplicate still exists — caller should run merge first."""
    dup = db.execute(
        """SELECT 1 FROM graph_nodes
           WHERE name_norm IS NOT NULL AND name_norm != ''
           GROUP BY name_norm, type HAVING COUNT(*) > 1 LIMIT 1"""
    ).fetchone()
    if dup:
        _log("refusing --add-unique: (name_norm, type) duplicates still present")
        return False
    try:
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_nodes_name_type "
            "ON graph_nodes(name_norm, type)"
        )
        return True
    except sqlite3.IntegrityError as exc:
        _log(f"index install failed: {exc}")
        return False


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


def _parse_args(argv: list[str]) -> dict:
    opts = {
        "apply": False,
        "dry_run": True,
        "case_only": False,
        "skip_orphans": False,
        "add_unique": False,
        "db": None,
        "limit": None,
    }
    for a in argv:
        if a == "--apply":
            opts["apply"] = True
            opts["dry_run"] = False
        elif a == "--dry-run":
            opts["dry_run"] = True
            opts["apply"] = False
        elif a == "--case-only":
            opts["case_only"] = True
        elif a == "--skip-orphans":
            opts["skip_orphans"] = True
        elif a == "--add-unique":
            opts["add_unique"] = True
        elif a.startswith("--db="):
            opts["db"] = a.split("=", 1)[1]
        elif a.startswith("--limit="):
            opts["limit"] = int(a.split("=", 1)[1])
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
    return opts


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    db_path = Path(args["db"]) if args["db"] else Path.home() / ".claude-memory" / "memory.db"
    if not db_path.exists():
        _log(f"db not found: {db_path}")
        return 1

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    if args["apply"]:
        db.execute("PRAGMA journal_mode=WAL")

    # Sanity: migration 026 applied?
    try:
        db.execute("SELECT name_norm FROM graph_nodes LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        _log("ERROR: migration 026 not applied — name_norm column missing")
        _log("       run server once to trigger migrations, then retry")
        db.close()
        return 2

    total_before = db.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    edges_before = db.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    _log(f"baseline: {total_before} nodes, {edges_before} edges")

    case_groups = detect_case_groups(db, limit=args["limit"])
    _log(f"case-variant groups: {len(case_groups)} "
         f"(would remove {sum(len(g.losers) for g in case_groups)} nodes)")

    type_groups: list[DupGroup] = []
    if not args["case_only"]:
        processed = set()
        for g in case_groups:
            processed.add(g.winner)
            processed.update(g.losers)
        type_groups = detect_type_collision_groups(db, processed, limit=args["limit"])
        _log(f"type-collision groups: {len(type_groups)} "
             f"(would remove {sum(len(g.losers) for g in type_groups)} nodes)")

    # Preview top items
    def _preview(groups: list[DupGroup], label: str, n: int = 6):
        if not groups:
            return
        _log(f"--- {label} top examples ---")
        for g in groups[:n]:
            losers_desc = ", ".join(
                f"{d[1]!r}({d[2]}, mentions={d[3]})" for d in g.loser_details[:3]
            )
            tail = "" if len(g.loser_details) <= 3 else f" +{len(g.loser_details)-3} more"
            _log(
                f"  norm={g.name_norm!r} winner={g.winner_name!r}({g.winner_type}, "
                f"mentions={g.winner_mentions}) "
                f"losers=[{losers_desc}{tail}]"
            )

    _preview(case_groups, "case-variant")
    _preview(type_groups, "type-collision")

    if args["dry_run"]:
        _log("dry-run — no changes. Re-run with --apply to execute.")
        db.close()
        return 0

    # Apply
    totals = {
        "groups_merged": 0, "nodes_removed": 0,
        "edges_relocated": 0, "edges_deduped": 0, "edges_self_loops": 0,
        "links_relocated": 0, "links_deduped": 0,
        "mentions_absorbed": 0,
    }
    for g in case_groups + type_groups:
        s = merge_group(db, g)
        totals["groups_merged"] += 1
        totals["nodes_removed"] += len(g.losers)
        for k in ("edges_relocated", "edges_deduped", "edges_self_loops",
                  "links_relocated", "links_deduped"):
            totals[k] += s[k]
        totals["mentions_absorbed"] += s["loser_mentions_absorbed"]
    db.commit()

    orphans_removed = 0
    if not args["skip_orphans"]:
        orphans_removed = remove_orphans(db)
        db.commit()
        _log(f"orphans removed: {orphans_removed}")

    unique_installed = False
    if args["add_unique"]:
        unique_installed = install_unique_index(db)
        db.commit()
        _log(f"unique index installed: {unique_installed}")

    total_after = db.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    edges_after = db.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    _log(f"final: {total_after} nodes (-{total_before - total_after}), "
         f"{edges_after} edges (-{edges_before - edges_after})")
    _log(f"summary: {totals}, orphans_removed={orphans_removed}, "
         f"unique_index={unique_installed}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
