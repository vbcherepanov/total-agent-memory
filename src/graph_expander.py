"""1-hop knowledge graph expansion for retrieval.

Given seed knowledge.id values from top-K retrieval, returns additional
candidate knowledge_ids discovered via:
  1) knowledge_nodes junction -> graph_edges (weight-filtered) -> knowledge_nodes
  2) relations table (direct knowledge-to-knowledge links, used as `associations`)

The actual schema in <memory-dir>/memory.db:
  graph_nodes(id TEXT PK, type, name, content, ...)
  graph_edges(id, source_id TEXT, target_id TEXT, relation_type, weight REAL, ...)
  knowledge_nodes(knowledge_id INT, node_id TEXT, role, strength REAL)
  relations(from_id INT, to_id INT, type, created_at)   -- sparse, used as 'associations'
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Iterable

from paths import memory_dir

logger = logging.getLogger(__name__)

# Log schema-missing warning only once per process.
_schema_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _schema_warned:
        _schema_warned.add(key)
        logger.warning(msg)


def is_enabled() -> bool:
    """Graph expansion is opt-in via MEMORY_GRAPH_EXPAND env var."""
    return os.environ.get("MEMORY_GRAPH_EXPAND", "0").strip().lower() in ("1", "true", "yes", "on")


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _as_int_list(ids: Iterable[int]) -> list[int]:
    out: list[int] = []
    for x in ids:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _dedupe_merge(
    pairs: Iterable[tuple[int, float]], exclude: set[int]
) -> dict[int, float]:
    """Merge (kid, score) pairs; on dup, keep max score."""
    merged: dict[int, float] = {}
    for kid, score in pairs:
        if kid in exclude:
            continue
        prev = merged.get(kid)
        if prev is None or score > prev:
            merged[kid] = float(score)
    return merged


def expand_top_k(
    db: sqlite3.Connection,
    seed_ids: list[int],
    budget: int = 10,
    min_strength: float = 0.3,
) -> list[tuple[int, float]]:
    """Expand seed knowledge ids through graph_nodes/graph_edges 1-hop.

    Flow: seed knowledge.id -> knowledge_nodes.node_id (seed nodes)
          -> graph_edges (weight >= min_strength) -> neighbor node_id
          -> knowledge_nodes (reverse) -> candidate knowledge_id.
    """
    seeds = _as_int_list(seed_ids)
    if not seeds or budget <= 0:
        return []

    if not (
        _table_exists(db, "knowledge_nodes")
        and _table_exists(db, "graph_edges")
    ):
        _warn_once("graph_missing", "graph_expander: graph tables missing — skipping top_k expansion")
        return []

    kn_cols = _columns(db, "knowledge_nodes")
    ge_cols = _columns(db, "graph_edges")
    if not {"knowledge_id", "node_id"}.issubset(kn_cols):
        _warn_once("kn_cols", f"graph_expander: knowledge_nodes missing expected cols, have {kn_cols}")
        return []
    if not {"source_id", "target_id"}.issubset(ge_cols):
        _warn_once("ge_cols", f"graph_expander: graph_edges missing expected cols, have {ge_cols}")
        return []

    weight_col = "weight" if "weight" in ge_cols else ("strength" if "strength" in ge_cols else None)
    strength_col = "strength" if "strength" in kn_cols else None

    placeholders = ",".join("?" for _ in seeds)
    try:
        # 1) seed knowledge -> seed node_ids
        seed_node_rows = db.execute(
            f"SELECT DISTINCT node_id FROM knowledge_nodes WHERE knowledge_id IN ({placeholders})",
            seeds,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("graph_expander: seed node lookup failed: %s", e)
        return []

    seed_node_ids = [r[0] for r in seed_node_rows if r[0] is not None]
    if not seed_node_ids:
        return []

    node_placeholders = ",".join("?" for _ in seed_node_ids)
    weight_filter = f"AND {weight_col} >= ?" if weight_col else ""
    weight_select = weight_col if weight_col else "1.0"
    params: list = list(seed_node_ids)
    if weight_col:
        params.append(float(min_strength))

    try:
        # 2) walk 1 hop in both directions
        edge_rows = db.execute(
            f"""
            SELECT target_id AS neighbor, {weight_select} AS w
            FROM graph_edges
            WHERE source_id IN ({node_placeholders}) {weight_filter}
            UNION ALL
            SELECT source_id AS neighbor, {weight_select} AS w
            FROM graph_edges
            WHERE target_id IN ({node_placeholders}) {weight_filter}
            """,
            params + params,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("graph_expander: edge walk failed: %s", e)
        return []

    if not edge_rows:
        return []

    # neighbor node_id -> best incoming edge weight
    neighbor_best: dict[str, float] = {}
    seed_node_set = set(seed_node_ids)
    for neighbor, w in edge_rows:
        if neighbor is None or neighbor in seed_node_set:
            continue
        try:
            wf = float(w) if w is not None else 1.0
        except (TypeError, ValueError):
            wf = 1.0
        prev = neighbor_best.get(neighbor)
        if prev is None or wf > prev:
            neighbor_best[neighbor] = wf

    if not neighbor_best:
        return []

    # 3) neighbor node -> knowledge_ids (only active ones if status col on knowledge)
    neigh_list = list(neighbor_best.keys())
    neigh_ph = ",".join("?" for _ in neigh_list)
    strength_select = f", {strength_col}" if strength_col else ", 1.0"
    try:
        kn_rows = db.execute(
            f"""
            SELECT kn.knowledge_id, kn.node_id {strength_select}
            FROM knowledge_nodes kn
            WHERE kn.node_id IN ({neigh_ph})
              AND kn.knowledge_id NOT IN ({placeholders})
            """,
            neigh_list + seeds,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("graph_expander: neighbor->knowledge map failed: %s", e)
        return []

    # Combine edge weight * kn strength; keep max per knowledge_id.
    seed_set = set(seeds)
    pairs: list[tuple[int, float]] = []
    for row in kn_rows:
        kid = row[0]
        node_id = row[1]
        try:
            kn_strength = float(row[2]) if row[2] is not None else 1.0
        except (TypeError, ValueError):
            kn_strength = 1.0
        edge_w = neighbor_best.get(node_id, 0.0)
        score = edge_w * kn_strength
        if score <= 0:
            continue
        pairs.append((int(kid), score))

    merged = _dedupe_merge(pairs, exclude=seed_set)
    ranked = sorted(merged.items(), key=lambda p: p[1], reverse=True)[:budget]
    return ranked


def expand_via_associations(
    db: sqlite3.Connection, seed_ids: list[int], budget: int = 10
) -> list[tuple[int, float]]:
    """Expand via the 'associations'-like table.

    This DB has no `associations` table; the closest shape is `relations(from_id, to_id, type)`
    with integer knowledge ids. We use it here. Returns empty if missing.
    """
    seeds = _as_int_list(seed_ids)
    if not seeds or budget <= 0:
        return []

    if _table_exists(db, "associations"):
        table = "associations"
        cols = _columns(db, "associations")
        # Try (subject, object) or (from_id, to_id) naming.
        if {"subject", "object"}.issubset(cols):
            a_col, b_col = "subject", "object"
        elif {"from_id", "to_id"}.issubset(cols):
            a_col, b_col = "from_id", "to_id"
        else:
            _warn_once("assoc_cols", f"graph_expander: associations has unexpected cols {cols}")
            return []
    elif _table_exists(db, "relations"):
        table = "relations"
        cols = _columns(db, "relations")
        if not {"from_id", "to_id"}.issubset(cols):
            _warn_once("rel_cols", f"graph_expander: relations missing from_id/to_id, cols={cols}")
            return []
        a_col, b_col = "from_id", "to_id"
    else:
        return []

    placeholders = ",".join("?" for _ in seeds)
    try:
        rows = db.execute(
            f"""
            SELECT {b_col} AS other FROM {table} WHERE {a_col} IN ({placeholders})
            UNION ALL
            SELECT {a_col} AS other FROM {table} WHERE {b_col} IN ({placeholders})
            """,
            seeds + seeds,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("graph_expander: associations lookup failed: %s", e)
        return []

    seed_set = set(seeds)
    pairs: list[tuple[int, float]] = []
    for (other,) in rows:
        try:
            kid = int(other)
        except (TypeError, ValueError):
            continue
        if kid in seed_set:
            continue
        pairs.append((kid, 0.5))

    merged = _dedupe_merge(pairs, exclude=seed_set)
    ranked = sorted(merged.items(), key=lambda p: p[1], reverse=True)[:budget]
    return ranked


def expand(
    db: sqlite3.Connection, seed_ids: list[int], budget: int = 10
) -> list[tuple[int, float]]:
    """Combine graph-edge expansion and association expansion, dedupe, keep max score."""
    seeds = _as_int_list(seed_ids)
    if not seeds or budget <= 0:
        return []

    # Split budget generously then trim at the end.
    a = expand_top_k(db, seeds, budget=budget)
    b = expand_via_associations(db, seeds, budget=budget)

    merged = _dedupe_merge(a + b, exclude=set(seeds))
    ranked = sorted(merged.items(), key=lambda p: p[1], reverse=True)[:budget]
    return ranked


def fetch_records(
    db: sqlite3.Connection, knowledge_ids: list[int]
) -> list[dict]:
    """Return full knowledge rows for given ids, preserving input order."""
    ids = _as_int_list(knowledge_ids)
    if not ids:
        return []
    if not _table_exists(db, "knowledge"):
        return []

    cols = _columns(db, "knowledge")
    # Pick a safe, common subset but include whatever extras exist.
    preferred = [
        "id", "type", "content", "context", "project", "tags",
        "status", "confidence", "source", "created_at", "last_confirmed",
        "recall_count", "session_id",
    ]
    select_cols = [c for c in preferred if c in cols]
    if not select_cols:
        return []

    placeholders = ",".join("?" for _ in ids)
    try:
        rows = db.execute(
            f"SELECT {', '.join(select_cols)} FROM knowledge WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("graph_expander: fetch_records failed: %s", e)
        return []

    # Preserve caller-provided order.
    by_id: dict[int, dict] = {}
    for r in rows:
        d = {col: r[i] for i, col in enumerate(select_cols)}
        by_id[int(d["id"])] = d

    ordered: list[dict] = []
    seen: set[int] = set()
    for kid in ids:
        if kid in by_id and kid not in seen:
            ordered.append(by_id[kid])
            seen.add(kid)
    return ordered


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random
    import time

    db_path = str(memory_dir() / "memory.db")
    print(f"[smoke] db: {db_path}")
    print(f"[smoke] is_enabled(): {is_enabled()}  (MEMORY_GRAPH_EXPAND={os.environ.get('MEMORY_GRAPH_EXPAND')!r})")

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Prefer seeds that actually have graph_nodes links, to exercise the path.
    seed_rows = conn.execute(
        """
        SELECT k.id FROM knowledge k
        WHERE k.status = 'active'
          AND EXISTS (SELECT 1 FROM knowledge_nodes kn WHERE kn.knowledge_id = k.id)
        ORDER BY RANDOM() LIMIT 3
        """
    ).fetchall()
    seed_ids = [r[0] for r in seed_rows]
    print(f"[smoke] seed ids: {seed_ids}")

    if not seed_ids:
        print("[smoke] no seeds found; aborting")
        raise SystemExit(0)

    # Per-seed expansion count.
    for sid in seed_ids:
        t0 = time.perf_counter()
        e1 = expand_top_k(conn, [sid], budget=20, min_strength=0.3)
        dt = (time.perf_counter() - t0) * 1000
        print(f"[smoke] seed={sid:<5}  neighbors(top_k)={len(e1):<3}  in {dt:.1f} ms")

    # Associations (will be empty here — 'relations' has 1 row).
    assoc = expand_via_associations(conn, seed_ids, budget=10)
    print(f"[smoke] associations-based neighbors: {len(assoc)}")

    # Combined
    t0 = time.perf_counter()
    combined = expand(conn, seed_ids, budget=10)
    dt = (time.perf_counter() - t0) * 1000
    print(f"[smoke] expand() combined: {len(combined)} results in {dt:.1f} ms")
    for kid, score in combined[:5]:
        print(f"         kid={kid}  score={score:.3f}")

    recs = fetch_records(conn, [kid for kid, _ in combined])
    print(f"[smoke] fetch_records: {len(recs)} rows")
    for r in recs[:3]:
        content = (r.get("content") or "")[:80].replace("\n", " ")
        print(f"         id={r.get('id')}  type={r.get('type')}  project={r.get('project')}  content={content!r}")

    conn.close()
