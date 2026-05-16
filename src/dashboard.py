#!/usr/bin/env python3
"""
Claude Total Memory — Web Dashboard

Standalone HTTP server using only Python stdlib.
Provides a read-only web interface for browsing memory data.

Usage:
    python src/dashboard.py

Environment:
    DASHBOARD_PORT      — HTTP port (default: 37737)
    TAM_MEMORY_DIR        — Path to memory storage (default: ~/.tam). Legacy CLAUDE_MEMORY_DIR still supported with deprecation warning.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import memory_dir

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "37737"))
MEMORY_DIR = memory_dir()
DB_PATH = MEMORY_DIR / "memory.db"


def get_db() -> sqlite3.Connection | None:
    """Open a read-only SQLite connection."""
    if not DB_PATH.exists():
        return None
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    # Note: cannot set PRAGMA journal_mode on a read-only handle.
    # Callers that want WAL should enable it on the writer process
    # (Store.__init__ already does this for the main MCP server).
    return db


def q(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a query and return a list of dicts."""
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    except Exception:
        return []


def q1(db: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    """Execute a query and return a single dict or None."""
    try:
        r = db.execute(sql, params).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def api_stats(db: sqlite3.Connection) -> dict:
    """Gather statistics about the memory database."""
    total_knowledge = db.execute(
        "SELECT COUNT(*) FROM knowledge WHERE status='active'"
    ).fetchone()[0]

    by_type = dict(db.execute(
        "SELECT type, COUNT(*) FROM knowledge WHERE status='active' GROUP BY type"
    ).fetchall())

    by_project = dict(db.execute(
        "SELECT project, COUNT(*) FROM knowledge WHERE status='active' GROUP BY project"
    ).fetchall())

    sessions_count = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    stale = db.execute("""
        SELECT COUNT(*) FROM knowledge
        WHERE status='active' AND last_confirmed < datetime('now', '-90 days')
    """).fetchone()[0]

    never_recalled = db.execute("""
        SELECT COUNT(*) FROM knowledge
        WHERE status='active' AND (recall_count = 0 OR recall_count IS NULL)
    """).fetchone()[0]

    health_score = round(
        max(0.0, 1.0
            - (stale / max(total_knowledge, 1)) * 0.5
            - (never_recalled / max(total_knowledge, 1)) * 0.3),
        2,
    )

    db_mb = DB_PATH.stat().st_size / 1048576 if DB_PATH.exists() else 0
    chroma_dir = MEMORY_DIR / "chroma"
    chroma_mb = 0.0
    if chroma_dir.exists():
        chroma_mb = sum(
            f.stat().st_size for f in chroma_dir.rglob("*") if f.is_file()
        ) / 1048576
    raw_dir = MEMORY_DIR / "raw"
    raw_mb = 0.0
    if raw_dir.exists():
        raw_mb = sum(
            f.stat().st_size for f in raw_dir.iterdir() if f.is_file()
        ) / 1048576

    storage_mb = round(db_mb + chroma_mb + raw_mb, 2)

    obs_count = 0
    try:
        obs_count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    except Exception:
        pass

    return {
        "total_knowledge": total_knowledge,
        "by_type": by_type,
        "by_project": by_project,
        "sessions_count": sessions_count,
        "health_score": health_score,
        "storage_mb": storage_mb,
        "stale_90d": stale,
        "never_recalled": never_recalled,
        "observations_count": obs_count,
    }


def api_knowledge(
    db: sqlite3.Connection,
    search: str | None = None,
    ktype: str | None = None,
    project: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """Paginated knowledge listing with optional filters."""
    conds = ["status='active'"]
    params: list = []

    if search:
        conds.append("(content LIKE ? OR context LIKE ? OR tags LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if ktype:
        conds.append("type=?")
        params.append(ktype)
    if project:
        conds.append("project=?")
        params.append(project)

    where = " AND ".join(conds)

    total = db.execute(
        f"SELECT COUNT(*) FROM knowledge WHERE {where}", params
    ).fetchone()[0]

    offset = (page - 1) * limit
    rows = q(
        db,
        f"""SELECT id, type, project, content, context, tags, confidence,
                   recall_count, created_at, last_confirmed, session_id
            FROM knowledge WHERE {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )

    for row in rows:
        if isinstance(row.get("tags"), str):
            try:
                row["tags"] = json.loads(row["tags"])
            except Exception:
                row["tags"] = []

    return {
        "items": rows,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit),
    }


def api_knowledge_detail(db: sqlite3.Connection, kid: int) -> dict | None:
    """Single knowledge record with full content and version history."""
    record = q1(db, "SELECT * FROM knowledge WHERE id=?", (kid,))
    if not record:
        return None

    if isinstance(record.get("tags"), str):
        try:
            record["tags"] = json.loads(record["tags"])
        except Exception:
            record["tags"] = []

    # Build superseded chain (version history)
    history: list[dict] = []
    # Find records that this one superseded
    predecessors = q(
        db,
        "SELECT id, content, created_at, status FROM knowledge WHERE superseded_by=?",
        (kid,),
    )
    for p in predecessors:
        history.append({**p, "relation": "superseded_by_this"})

    # Find what supersedes this record
    if record.get("superseded_by"):
        successor = q1(
            db,
            "SELECT id, content, created_at, status FROM knowledge WHERE id=?",
            (record["superseded_by"],),
        )
        if successor:
            history.append({**successor, "relation": "supersedes_this"})

    record["version_history"] = history
    return record


def api_sessions(db: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Recent sessions with knowledge count."""
    sessions = q(
        db,
        """SELECT s.*, COUNT(k.id) as knowledge_count
           FROM sessions s
           LEFT JOIN knowledge k ON k.session_id = s.id AND k.status = 'active'
           GROUP BY s.id
           ORDER BY s.started_at DESC
           LIMIT ?""",
        (limit,),
    )
    return sessions


def _knowledge_related(db: sqlite3.Connection, kid: int, limit: int = 10) -> list[dict]:
    """Collect up to `limit` related knowledge records for citation.

    Two sources are merged, preserving order and de-duplicating by id:
      1. Direct knowledge<->knowledge edges in `graph_edges` where the node id
         equals `k-{kid}` (convention used by the graph indexer) — 1-hop in
         either direction.
      2. Peer knowledge that shares a representation (same summary/keywords
         cluster via `knowledge_representations`) — labelled "multi_repr".
    """
    out: list[dict] = []
    seen: set[int] = {kid}

    node_id = f"k-{kid}"
    try:
        edge_rows = db.execute(
            """
            SELECT target_id AS other, relation_type FROM graph_edges
            WHERE source_id = ?
            UNION
            SELECT source_id AS other, relation_type FROM graph_edges
            WHERE target_id = ?
            LIMIT ?
            """,
            (node_id, node_id, limit * 4),
        ).fetchall()
    except sqlite3.Error:
        edge_rows = []

    for r in edge_rows:
        other = r["other"] if isinstance(r, sqlite3.Row) else r[0]
        if not other or not isinstance(other, str) or not other.startswith("k-"):
            continue
        try:
            peer_id = int(other[2:])
        except ValueError:
            continue
        if peer_id in seen:
            continue
        peer = q1(
            db,
            "SELECT id, type, substr(content, 1, 200) AS title "
            "FROM knowledge WHERE id = ? AND status = 'active'",
            (peer_id,),
        )
        if not peer:
            continue
        seen.add(peer_id)
        out.append({"id": peer["id"], "title": peer["title"] or "", "via": "graph_edge"})
        if len(out) >= limit:
            return out

    # Fallback / augmentation: peers sharing a representation.
    try:
        rep_rows = db.execute(
            """
            SELECT DISTINCT r2.knowledge_id AS peer_id
            FROM knowledge_representations r1
            JOIN knowledge_representations r2
              ON r1.representation = r2.representation
             AND r2.knowledge_id != r1.knowledge_id
            WHERE r1.knowledge_id = ?
            LIMIT ?
            """,
            (kid, limit * 2),
        ).fetchall()
    except sqlite3.Error:
        rep_rows = []

    for r in rep_rows:
        peer_id = int(r["peer_id"] if isinstance(r, sqlite3.Row) else r[0])
        if peer_id in seen:
            continue
        peer = q1(
            db,
            "SELECT id, type, substr(content, 1, 200) AS title "
            "FROM knowledge WHERE id = ? AND status = 'active'",
            (peer_id,),
        )
        if not peer:
            continue
        seen.add(peer_id)
        out.append({"id": peer["id"], "title": peer["title"] or "", "via": "multi_repr"})
        if len(out) >= limit:
            break
    return out


def api_knowledge_citation(db: sqlite3.Connection, kid: int) -> dict | None:
    """Flat citation payload for /api/knowledge/{id} used by IDE integrations.

    Shape is intentionally stable and self-contained — consumers paste the
    URL into docs and expect the same keys every time.
    """
    record = q1(
        db,
        "SELECT id, type, content, context, project, tags, created_at, session_id "
        "FROM knowledge WHERE id = ?",
        (kid,),
    )
    if not record:
        return None
    tags = record.get("tags") or "[]"
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    record["tags"] = tags
    record["related"] = _knowledge_related(db, kid, limit=10)
    return record


def api_session_citation(db: sqlite3.Connection, sid: str) -> dict | None:
    """Citation payload for /api/session/{id}.

    Merges the immutable `sessions` row (started_at, summary) with the latest
    `session_summaries` continuity blob (which stores next_steps and
    pitfalls). Knowledge linked to the session is attached as a light list.
    """
    session = q1(
        db,
        "SELECT id AS session_id, summary, started_at AS created_at, project, status "
        "FROM sessions WHERE id = ?",
        (sid,),
    )

    continuity = q1(
        db,
        "SELECT summary, next_steps, pitfalls, ended_at "
        "FROM session_summaries WHERE session_id = ? "
        "ORDER BY datetime(ended_at) DESC LIMIT 1",
        (sid,),
    )

    # Either source being present means the session is citeable.
    if not session and not continuity:
        return None

    def _decode_json_list(raw: object) -> list:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                val = json.loads(raw)
                return val if isinstance(val, list) else []
            except Exception:
                return []
        return []

    summary = ""
    next_steps: list = []
    pitfalls: list = []
    created_at = ""
    if continuity:
        summary = continuity.get("summary") or ""
        next_steps = _decode_json_list(continuity.get("next_steps"))
        pitfalls = _decode_json_list(continuity.get("pitfalls"))
        created_at = continuity.get("ended_at") or ""
    if session:
        if not summary:
            summary = session.get("summary") or ""
        if not created_at:
            created_at = session.get("created_at") or ""

    knowledge = q(
        db,
        "SELECT id, type, substr(content, 1, 160) AS title "
        "FROM knowledge WHERE session_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 100",
        (sid,),
    )

    return {
        "session_id": sid,
        "summary": summary,
        "next_steps": next_steps,
        "pitfalls": pitfalls,
        "knowledge": knowledge,
        "created_at": created_at,
    }


def api_graph(db: sqlite3.Connection, limit: int = 1600) -> dict:
    """Nodes and edges for the graph visualization with auto-generated edges."""
    nodes = q(
        db,
        """SELECT k.id, k.type, k.project, substr(k.content, 1, 120) as label,
                  k.recall_count, k.confidence, k.tags, k.session_id
           FROM knowledge k
           WHERE k.status='active'
           ORDER BY recall_count DESC, created_at DESC
           LIMIT ?""",
        (limit,),
    )

    node_ids = {n["id"] for n in nodes}

    # Explicit relations
    edges_raw = q(db, "SELECT from_id, to_id, type FROM relations")
    edges = [
        e for e in edges_raw
        if e["from_id"] in node_ids and e["to_id"] in node_ids
    ]
    seen_pairs: set[tuple[int, int]] = {(e["from_id"], e["to_id"]) for e in edges}

    # Auto-generate edges: same session = co-created
    from collections import defaultdict
    session_groups: dict[str, list[int]] = defaultdict(list)
    tag_groups: dict[str, list[int]] = defaultdict(list)

    for n in nodes:
        if n["session_id"]:
            session_groups[n["session_id"]].append(n["id"])
        if n["tags"]:
            try:
                import json as _json
                tags = _json.loads(n["tags"]) if isinstance(n["tags"], str) else n["tags"]
                for t in tags:
                    if t and t not in ("reusable", "session-summary", "session-autosave",
                                       "context-recovery", "2026", "self-reflection"):
                        tag_groups[t].append(n["id"])
            except Exception:
                pass

    def add_edge(a: int, b: int, etype: str) -> None:
        pair = (min(a, b), max(a, b))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            edges.append({"from_id": a, "to_id": b, "type": etype})

    # Session co-occurrence (max 3 edges per session to avoid clutter)
    for sid, ids in session_groups.items():
        if 2 <= len(ids) <= 8:
            for i in range(min(len(ids) - 1, 3)):
                add_edge(ids[i], ids[i + 1], "co-session")

    # Tag co-occurrence (nodes sharing rare tags)
    for tag, ids in tag_groups.items():
        if 2 <= len(ids) <= 15:
            for i in range(min(len(ids) - 1, 4)):
                add_edge(ids[i], ids[i + 1], "shared-tag:" + tag)

    # Same project links (connect top nodes within each project)
    project_groups: dict[str, list[int]] = defaultdict(list)
    for n in nodes:
        project_groups[n["project"]].append(n["id"])
    for proj, ids in project_groups.items():
        if 2 <= len(ids):
            for i in range(min(len(ids) - 1, 5)):
                add_edge(ids[i], ids[i + 1], "same-project")

    return {"nodes": nodes, "edges": edges}


def api_errors(
    db: sqlite3.Connection,
    category: str | None = None,
    project: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """Paginated error listing with optional filters."""
    try:
        conds: list[str] = []
        params: list = []

        if category:
            conds.append("category=?")
            params.append(category)
        if project:
            conds.append("project=?")
            params.append(project)

        where = (" WHERE " + " AND ".join(conds)) if conds else ""

        total = db.execute(
            f"SELECT COUNT(*) FROM errors{where}", params
        ).fetchone()[0]

        offset = (page - 1) * limit
        rows = q(
            db,
            f"""SELECT id, category, severity, description, context,
                       fix, status, project, tags, created_at
                FROM errors{where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )

        return {
            "items": rows,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
        }
    except Exception:
        return {"items": [], "total": 0, "page": 1, "limit": limit, "pages": 1}


def api_insights(
    db: sqlite3.Connection,
    project: str | None = None,
) -> list[dict]:
    """Active insights ordered by importance."""
    try:
        conds = ["status='active'"]
        params: list = []

        if project:
            conds.append("project=?")
            params.append(project)

        where = " AND ".join(conds)

        rows = q(
            db,
            f"""SELECT id, content, category, importance, confidence,
                       source_error_ids, status, project, created_at
                FROM insights
                WHERE {where}
                ORDER BY importance DESC""",
            tuple(params),
        )

        for row in rows:
            row["promotion_eligible"] = (
                (row.get("importance") or 0) >= 5
                and (row.get("confidence") or 0) >= 0.8
            )

        return rows
    except Exception:
        return []


def api_rules(
    db: sqlite3.Connection,
    project: str | None = None,
) -> list[dict]:
    """Active and suspended rules ordered by priority."""
    try:
        conds = ["status != 'retired'"]
        params: list = []

        if project:
            conds.append("(scope='global' OR project=?)")
            params.append(project)

        where = " AND ".join(conds)

        rows = q(
            db,
            f"""SELECT id, content, category, scope, priority,
                       fire_count, success_rate, status, project, created_at
                FROM rules
                WHERE {where}
                ORDER BY priority DESC, success_rate DESC""",
            tuple(params),
        )

        return rows
    except Exception:
        return []


def api_self_improvement(db: sqlite3.Connection) -> dict:
    """Summary stats for the self-improving agent feature."""
    result: dict = {
        "error_count": 0,
        "errors_by_category": {},
        "insight_count": 0,
        "rule_count": 0,
        "avg_success_rate": 0.0,
    }

    try:
        result["error_count"] = db.execute(
            "SELECT COUNT(*) FROM errors"
        ).fetchone()[0]
    except Exception:
        pass

    try:
        rows = db.execute(
            "SELECT category, COUNT(*) FROM errors GROUP BY category"
        ).fetchall()
        result["errors_by_category"] = dict(rows)
    except Exception:
        pass

    try:
        result["insight_count"] = db.execute(
            "SELECT COUNT(*) FROM insights WHERE status='active'"
        ).fetchone()[0]
    except Exception:
        pass

    try:
        result["rule_count"] = db.execute(
            "SELECT COUNT(*) FROM rules WHERE status='active'"
        ).fetchone()[0]
    except Exception:
        pass

    try:
        row = db.execute(
            "SELECT AVG(success_rate) FROM rules WHERE status='active'"
        ).fetchone()
        result["avg_success_rate"] = round(row[0] or 0.0, 2)
    except Exception:
        pass

    return result


def api_observations(
    db: sqlite3.Connection,
    project: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """Paginated observations listing."""
    try:
        conds: list[str] = []
        params: list = []
        if project:
            conds.append("project=?")
            params.append(project)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = db.execute(f"SELECT COUNT(*) FROM observations{where}", params).fetchone()[0]
        offset = (page - 1) * limit
        rows = q(
            db,
            f"""SELECT id, session_id, tool_name, observation_type, summary,
                       files_affected, project, branch, created_at
                FROM observations{where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        for row in rows:
            if isinstance(row.get("files_affected"), str):
                try:
                    row["files_affected"] = json.loads(row["files_affected"])
                except Exception:
                    row["files_affected"] = []
        return {"items": rows, "total": total, "page": page, "limit": limit,
                "pages": max(1, (total + limit - 1) // limit)}
    except Exception:
        return {"items": [], "total": 0, "page": 1, "limit": limit, "pages": 1}


def api_branches(db: sqlite3.Connection) -> list[str]:
    """Get distinct branches from sessions and knowledge."""
    branches = set()
    try:
        for r in db.execute("SELECT DISTINCT branch FROM sessions WHERE branch != ''").fetchall():
            branches.add(r[0])
    except Exception:
        pass
    try:
        for r in db.execute("SELECT DISTINCT branch FROM knowledge WHERE branch != '' AND status='active'").fetchall():
            branches.add(r[0])
    except Exception:
        pass
    return sorted(branches)


def api_graph_stats(db: sqlite3.Connection) -> dict:
    """Graph statistics for Super Memory v5 dashboard."""
    try:
        nodes_by_type = dict(db.execute(
            "SELECT type, COUNT(*) FROM graph_nodes WHERE status='active' GROUP BY type"
        ).fetchall())
        edges_by_type = dict(db.execute(
            "SELECT relation_type, COUNT(*) FROM graph_edges GROUP BY relation_type"
        ).fetchall())
        total_nodes = sum(nodes_by_type.values()) if nodes_by_type else 0
        total_edges = sum(edges_by_type.values()) if edges_by_type else 0
        top_nodes = [dict(r) for r in db.execute(
            "SELECT name, type, importance, mention_count FROM graph_nodes "
            "WHERE status='active' ORDER BY importance DESC LIMIT 20"
        ).fetchall()]
        return {"total_nodes": total_nodes, "total_edges": total_edges,
                "nodes_by_type": nodes_by_type, "edges_by_type": edges_by_type,
                "top_nodes": top_nodes}
    except Exception:
        return {"total_nodes": 0, "total_edges": 0,
                "nodes_by_type": {}, "edges_by_type": {},
                "top_nodes": []}


def api_graph_visual(db: sqlite3.Connection, limit: int = 100) -> dict:
    """Graph nodes and edges from graph_nodes/graph_edges tables for vis.js visualization."""
    try:
        nodes = [dict(r) for r in db.execute(
            """SELECT id, type, name, content, importance, mention_count, status
               FROM graph_nodes
               WHERE status='active'
               ORDER BY importance DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()]

        node_ids = {n["id"] for n in nodes}

        all_edges = [dict(r) for r in db.execute(
            """SELECT id, source_id, target_id, relation_type, weight, context
               FROM graph_edges
               ORDER BY weight DESC"""
        ).fetchall()]

        edges = [
            e for e in all_edges
            if e["source_id"] in node_ids and e["target_id"] in node_ids
        ]

        return {"nodes": nodes, "edges": edges}
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}


def api_graph_node_detail(db: sqlite3.Connection, node_id: str) -> dict | None:
    """Get details for a single graph node including connected knowledge."""
    try:
        node = db.execute(
            "SELECT * FROM graph_nodes WHERE id=?", (node_id,)
        ).fetchone()
        if not node:
            return None
        node = dict(node)

        # Connected edges (both directions)
        outgoing = [dict(r) for r in db.execute(
            """SELECT e.relation_type, e.weight, e.context, n.name, n.type, n.id as target_id
               FROM graph_edges e
               JOIN graph_nodes n ON n.id = e.target_id
               WHERE e.source_id = ?
               ORDER BY e.weight DESC LIMIT 50""",
            (node_id,),
        ).fetchall()]

        incoming = [dict(r) for r in db.execute(
            """SELECT e.relation_type, e.weight, e.context, n.name, n.type, n.id as source_id
               FROM graph_edges e
               JOIN graph_nodes n ON n.id = e.source_id
               WHERE e.target_id = ?
               ORDER BY e.weight DESC LIMIT 50""",
            (node_id,),
        ).fetchall()]

        return {
            "node": node,
            "outgoing": outgoing,
            "incoming": incoming,
        }
    except Exception:
        return None


def api_system_status() -> dict:
    """System status: LaunchAgents, memory stats, disk usage."""
    # LaunchAgent statuses
    agents: list[dict] = []
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "claude.memory" in line or "claude-memory" in line:
                parts = line.split("\t")
                if len(parts) >= 3:
                    agents.append({
                        "pid": parts[0] if parts[0] != "-" else None,
                        "exit_code": int(parts[1]) if parts[1] != "-" else None,
                        "label": parts[2],
                    })
    except Exception as exc:
        agents.append({"error": str(exc)})

    # Memory stats
    stats: dict = {}
    try:
        db = get_db()
        if db:
            stats["knowledge_count"] = db.execute(
                "SELECT COUNT(*) FROM knowledge WHERE status='active'"
            ).fetchone()[0]
            try:
                stats["graph_nodes"] = db.execute(
                    "SELECT COUNT(*) FROM graph_nodes WHERE status='active'"
                ).fetchone()[0]
                stats["graph_edges"] = db.execute(
                    "SELECT COUNT(*) FROM graph_edges"
                ).fetchone()[0]
            except Exception:
                stats["graph_nodes"] = 0
                stats["graph_edges"] = 0
            try:
                stats["last_reflection"] = db.execute(
                    "SELECT MAX(created_at) FROM insights"
                ).fetchone()[0]
            except Exception:
                stats["last_reflection"] = None
            db.close()
    except Exception:
        pass

    # Disk usage
    disk: dict = {}
    if DB_PATH.exists():
        disk["db_mb"] = round(DB_PATH.stat().st_size / 1048576, 2)
    chroma_dir = MEMORY_DIR / "chroma"
    if chroma_dir.exists():
        disk["chroma_mb"] = round(
            sum(f.stat().st_size for f in chroma_dir.rglob("*") if f.is_file()) / 1048576, 2
        )
    disk["total_mb"] = round(sum(disk.values()), 2)

    return {
        "status": "running",
        "port": DASHBOARD_PORT,
        "launch_agents": agents,
        "memory": stats,
        "disk": disk,
        "uptime_info": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def api_episodes(db: sqlite3.Connection) -> dict:
    """Recent episodes for Super Memory v5 dashboard."""
    try:
        episodes = [dict(r) for r in db.execute(
            "SELECT * FROM episodes ORDER BY created_at DESC LIMIT 50"
        ).fetchall()]
        by_outcome = dict(db.execute(
            "SELECT outcome, COUNT(*) FROM episodes GROUP BY outcome"
        ).fetchall())
        avg_impact = db.execute(
            "SELECT AVG(impact_score) FROM episodes"
        ).fetchone()[0] or 0
        return {"episodes": episodes, "stats": {
            "total": len(episodes),
            "by_outcome": by_outcome,
            "avg_impact": round(avg_impact, 2),
        }}
    except Exception:
        return {"episodes": [], "stats": {"total": 0, "by_outcome": {}, "avg_impact": 0}}


def api_skills(db: sqlite3.Connection) -> dict:
    """All skills for Super Memory v5 dashboard."""
    try:
        skills = [dict(r) for r in db.execute(
            "SELECT * FROM skills ORDER BY times_used DESC"
        ).fetchall()]
        # Enrich: extract description from projects JSON, parse stack
        for sk in skills:
            if sk.get("projects"):
                try:
                    proj = json.loads(sk["projects"]) if isinstance(sk["projects"], str) else sk["projects"]
                    sk["description"] = proj.get("description", "")
                    sk["source"] = proj.get("source", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            if sk.get("stack") and isinstance(sk["stack"], str):
                try:
                    sk["stack_list"] = json.loads(sk["stack"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return {"skills": skills, "total": len(skills)}
    except Exception:
        return {"skills": [], "total": 0}


def api_self_model(db: sqlite3.Connection) -> dict:
    """Self model data for Super Memory v5 dashboard."""
    try:
        competencies = [dict(r) for r in db.execute(
            "SELECT * FROM competencies ORDER BY level DESC"
        ).fetchall()]
    except Exception:
        competencies = []

    try:
        blind_spots = [dict(r) for r in db.execute(
            "SELECT * FROM blind_spots WHERE status IN ('active', 'monitoring')"
        ).fetchall()]
    except Exception:
        blind_spots = []

    try:
        user_model = {r["key"]: {"value": r["value"], "confidence": r["confidence"]}
                      for r in db.execute("SELECT * FROM user_model").fetchall()}
    except Exception:
        user_model = {}

    return {"competencies": competencies, "blind_spots": blind_spots,
            "user_model": user_model}


def api_reflection(db: sqlite3.Connection) -> dict:
    """Recent reflection reports for Super Memory v5 dashboard."""
    try:
        reports = [dict(r) for r in db.execute(
            "SELECT * FROM reflection_reports ORDER BY created_at DESC LIMIT 20"
        ).fetchall()]
    except Exception:
        reports = []

    try:
        proposals = [dict(r) for r in db.execute(
            "SELECT * FROM pending_proposals WHERE status='pending' ORDER BY created_at DESC"
        ).fetchall()]
    except Exception:
        proposals = []

    return {"reports": reports, "proposals": proposals}


# ============================================================
# HTML Template
# ============================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Total Memory</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
    --bg: #0f172a;
    --card: #1e293b;
    --card-hover: #253349;
    --border: #334155;
    --text: #e2e8f0;
    --text-dim: #94a3b8;
    --accent: #3b82f6;
    --accent-hover: #2563eb;

    --decision: #f59e0b;
    --fact: #3b82f6;
    --solution: #22c55e;
    --lesson: #ef4444;
    --convention: #8b5cf6;

    --error-low: #94a3b8;
    --error-medium: #f59e0b;
    --error-high: #ef4444;
    --error-critical: #dc2626;
    --insight-color: #8b5cf6;
    --rule-color: #06b6d4;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    min-height: 100vh;
}

.container { max-width: 1400px; margin: 0 auto; padding: 20px; }

/* Header */
header {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
}
header h1 { font-size: 24px; font-weight: 700; }
header h1 span { color: var(--accent); }
header .subtitle { color: var(--text-dim); font-size: 14px; }

/* Stats cards */
.stats-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
}
.stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
}
.stat-card .label { color: var(--text-dim); font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em; }
.stat-card .value { font-size: 32px; font-weight: 700; margin-top: 4px; }
.stat-card .value.accent { color: var(--accent); }
.stat-card .value.green { color: var(--solution); }
.stat-card .value.amber { color: var(--decision); }

/* Tabs */
.tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 20px;
    border-bottom: 2px solid var(--border);
}
.tab {
    padding: 10px 20px;
    cursor: pointer;
    border: none;
    background: none;
    color: var(--text-dim);
    font-size: 14px;
    font-weight: 500;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    transition: color 0.2s, border-color 0.2s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }

.tab-content { display: none; }
.tab-content.active { display: block; }

/* Filters */
.filters {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
    align-items: center;
}
.filters input, .filters select {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
}
.filters input:focus, .filters select:focus { border-color: var(--accent); }
.filters input { flex: 1; min-width: 200px; }
.filters select { min-width: 140px; }

/* Table */
.table-wrap { overflow-x: auto; }
table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
}
thead th {
    text-align: left;
    padding: 10px 12px;
    color: var(--text-dim);
    font-weight: 500;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
}
tbody tr {
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    transition: background 0.15s;
}
tbody tr:hover { background: var(--card-hover); }
td { padding: 10px 12px; vertical-align: top; }
td.id-col { color: var(--text-dim); font-size: 12px; font-family: monospace; }
td.content-col { max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td.num-col { text-align: right; font-family: monospace; }
td.date-col { white-space: nowrap; color: var(--text-dim); font-size: 13px; }

/* Badge */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}
.badge-decision  { background: rgba(245,158,11,0.15); color: var(--decision); }
.badge-fact      { background: rgba(59,130,246,0.15); color: var(--fact); }
.badge-solution  { background: rgba(34,197,94,0.15); color: var(--solution); }
.badge-lesson    { background: rgba(239,68,68,0.15); color: var(--lesson); }
.badge-convention{ background: rgba(139,92,246,0.15); color: var(--convention); }

/* Pagination */
.pagination {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-top: 16px;
}
.pagination button {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    transition: background 0.15s, border-color 0.15s;
}
.pagination button:hover:not(:disabled) { border-color: var(--accent); }
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination .page-info { color: var(--text-dim); font-size: 13px; }

/* Modal */
.modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 1000;
    justify-content: center;
    align-items: flex-start;
    padding: 40px 20px;
    overflow-y: auto;
}
.modal-overlay.open { display: flex; }
.modal {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    max-width: 800px;
    width: 100%;
    padding: 28px;
    position: relative;
}
.modal-close {
    position: absolute;
    top: 16px;
    right: 16px;
    background: none;
    border: none;
    color: var(--text-dim);
    font-size: 20px;
    cursor: pointer;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 8px;
    transition: background 0.15s;
}
.modal-close:hover { background: rgba(255,255,255,0.1); }
.modal h2 { margin-bottom: 16px; font-size: 18px; }
.modal-section { margin-bottom: 16px; }
.modal-section .section-label {
    font-size: 12px;
    text-transform: uppercase;
    color: var(--text-dim);
    letter-spacing: 0.05em;
    margin-bottom: 6px;
}
.modal-section .section-body {
    background: var(--bg);
    border-radius: 8px;
    padding: 12px;
    font-size: 14px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
}
.modal-meta {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
}
.modal-meta .meta-item .meta-label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; }
.modal-meta .meta-item .meta-value { font-size: 14px; margin-top: 2px; }
.tag {
    display: inline-block;
    background: rgba(59,130,246,0.15);
    color: var(--accent);
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 12px;
    margin: 2px 4px 2px 0;
}
.version-item {
    background: var(--bg);
    border-radius: 8px;
    padding: 10px 12px;
    margin-top: 8px;
    font-size: 13px;
}
.version-item .version-relation {
    font-size: 11px;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 4px;
}

/* Severity badges */
.badge-low      { background: rgba(148,163,184,0.15); color: var(--error-low); }
.badge-medium   { background: rgba(245,158,11,0.15); color: var(--error-medium); }
.badge-high     { background: rgba(239,68,68,0.15); color: var(--error-high); }
.badge-critical { background: rgba(220,38,38,0.2); color: var(--error-critical); font-weight: 700; }

/* Status badges for self-improvement */
.badge-active    { background: rgba(6,182,212,0.15); color: var(--rule-color); }
.badge-suspended { background: rgba(245,158,11,0.15); color: var(--error-medium); }
.badge-retired   { background: rgba(148,163,184,0.15); color: var(--error-low); }
.badge-resolved  { background: rgba(34,197,94,0.15); color: var(--solution); }
.badge-pending   { background: rgba(59,130,246,0.15); color: var(--fact); }

/* Importance badge */
.badge-importance {
    background: rgba(139,92,246,0.15);
    color: var(--insight-color);
    min-width: 28px;
    text-align: center;
}

/* Priority badge */
.badge-priority {
    background: rgba(6,182,212,0.15);
    color: var(--rule-color);
    min-width: 28px;
    text-align: center;
}
.badge-priority.high-priority {
    background: rgba(239,68,68,0.15);
    color: var(--error-high);
}

/* Progress bar for success rate */
.progress-bar {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-width: 120px;
}
.progress-bar .bar {
    flex: 1;
    height: 6px;
    background: rgba(255,255,255,0.1);
    border-radius: 3px;
    overflow: hidden;
    min-width: 60px;
}
.progress-bar .bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s;
}
.progress-bar .bar-label {
    font-size: 12px;
    font-family: monospace;
    color: var(--text-dim);
    min-width: 36px;
    text-align: right;
}

/* Promotion eligible indicator */
.promo-eligible {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--solution);
    box-shadow: 0 0 6px rgba(34,197,94,0.5);
}
.promo-not-eligible {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--border);
}

/* Section divider within a tab */
.section-divider {
    margin: 32px 0 20px 0;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
    font-size: 16px;
    font-weight: 600;
    color: var(--text);
}
.section-divider .section-icon {
    color: var(--insight-color);
    margin-right: 8px;
}

/* Graph */
#graph-canvas {
    width: 100%;
    height: 700px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    cursor: grab;
}
#graph-canvas:active { cursor: grabbing; }
#graph-controls {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
    align-items: center;
    flex-wrap: wrap;
}
#graph-controls button {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    transition: background 0.2s;
}
#graph-controls button:hover { background: var(--border); }
#graph-controls select {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 8px;
    font-size: 13px;
}
#graph-controls .graph-info {
    margin-left: auto;
    font-size: 12px;
    color: var(--text-dim);
}
#graph-controls-row2 {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
    align-items: center;
    flex-wrap: wrap;
}
#graph-controls-row2 button {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 12px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 12px;
    transition: background 0.2s, border-color 0.2s;
}
#graph-controls-row2 button:hover { background: var(--border); }
#graph-controls-row2 button.cluster-active {
    background: var(--accent);
    color: #000;
    border-color: var(--accent);
    font-weight: 600;
}
#graph-search {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 12px;
    border-radius: 8px;
    font-size: 12px;
    width: 200px;
    outline: none;
}
#graph-search:focus { border-color: var(--accent); }
#graph-search::placeholder { color: var(--text-dim); }
#graph-controls-row2 select {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 10px;
    border-radius: 8px;
    font-size: 12px;
}
#graph-cluster-legend {
    display: flex;
    gap: 12px;
    margin-bottom: 6px;
    flex-wrap: wrap;
    font-size: 11px;
    color: var(--text-dim);
    min-height: 20px;
}
#graph-cluster-legend .cl-item {
    display: flex;
    align-items: center;
    gap: 4px;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 4px;
    transition: background 0.15s;
}
#graph-cluster-legend .cl-item:hover { background: var(--border); }
#graph-cluster-legend .cl-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
}
#graph-tooltip {
    display: none;
    position: absolute;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    max-width: 350px;
    pointer-events: none;
    z-index: 500;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
}
#graph-tooltip .tt-type { font-size: 11px; text-transform: uppercase; margin-bottom: 4px; }
#graph-tooltip .tt-project { font-size: 11px; color: var(--text-dim); margin-bottom: 4px; }
#graph-tooltip .tt-content { line-height: 1.5; }
#graph-tooltip .tt-edges { font-size: 11px; color: var(--text-dim); margin-top: 6px; }

/* Loading / Error */
.loading {
    text-align: center;
    padding: 60px 20px;
    color: var(--text-dim);
}
.error-box {
    background: rgba(239,68,68,0.1);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: 12px;
    padding: 24px;
    text-align: center;
    color: var(--lesson);
}

/* Responsive */
@media (max-width: 768px) {
    .stats-row { grid-template-columns: repeat(2, 1fr); }
    .filters { flex-direction: column; }
    .filters input, .filters select { width: 100%; min-width: unset; }
}
</style>
</head>
<body>

<div class="container">
    <header>
        <div>
            <h1><span>Claude</span> Total Memory</h1>
            <div class="subtitle" style="display:flex;align-items:center;gap:10px;">
                <span>Read-only dashboard &mdash; memory.db</span>
                <span id="header-feed-status" title="Live feed status (SSE)"
                      style="display:inline-flex;align-items:center;gap:6px;
                             padding:3px 8px;border-radius:10px;background:rgba(255,255,255,0.05);
                             border:1px solid #333;font-size:11px;">
                    <span id="header-feed-dot" style="width:8px;height:8px;border-radius:50%;
                                                       background:#ef4444;display:inline-block;"></span>
                    <span id="header-feed-text" style="color:var(--text-dim);">Disconnected</span>
                </span>
            </div>
        </div>
    </header>

    <div id="error-container"></div>

    <div class="stats-row" id="stats-row">
        <div class="stat-card"><div class="label">Total Knowledge</div><div class="value accent" id="stat-total">--</div></div>
        <div class="stat-card"><div class="label">Sessions</div><div class="value" id="stat-sessions">--</div></div>
        <div class="stat-card"><div class="label">Projects</div><div class="value" id="stat-projects">--</div></div>
        <div class="stat-card"><div class="label">Health Score</div><div class="value green" id="stat-health">--</div></div>
        <div class="stat-card"><div class="label">Storage</div><div class="value" id="stat-storage">--</div></div>
        <div class="stat-card">
            <div class="label">Self-Improvement</div>
            <div class="value" id="stat-si" style="color: var(--rule-color)">--</div>
            <div class="label" id="stat-si-detail">-- errors, -- insights, -- rules</div>
        </div>
        <div class="stat-card"><div class="label">Observations</div><div class="value" id="stat-observations" style="color:var(--insight-color)">--</div></div>
        <div class="stat-card">
            <div class="label">Graph</div>
            <div class="value" id="stat-graph" style="color: var(--accent)">--</div>
            <div class="label" id="stat-graph-detail">-- nodes, -- edges</div>
        </div>
        <div class="stat-card">
            <div class="label">Episodes</div>
            <div class="value" id="stat-episodes" style="color: var(--solution)">--</div>
            <div class="label" id="stat-episodes-detail">-- breakthroughs, -- failures</div>
        </div>
        <div class="stat-card">
            <div class="label">Skills</div>
            <div class="value" id="stat-skills" style="color: var(--decision)">--</div>
            <div class="label" id="stat-skills-detail">avg success: --%</div>
        </div>
        <div class="stat-card"><div class="label">Blind Spots</div><div class="value" id="stat-blind-spots" style="color: var(--lesson)">--</div></div>
    </div>

    <!-- V6_PANELS_HERE -->

    <div class="tabs">
        <button class="tab active" data-tab="knowledge">Knowledge</button>
        <button class="tab" data-tab="sessions">Sessions</button>
        <button class="tab" data-tab="graph">Graph</button>
        <a class="tab" href="/graph/live" style="text-decoration:none;">Graph Live 🔴</a>
        <button class="tab" data-tab="self-improvement">Self-Improvement</button>
        <button class="tab" data-tab="rules">Rules (SOUL)</button>
        <button class="tab" data-tab="v5-graph">Graph v5</button>
        <button class="tab" data-tab="v5-episodes">Episodes</button>
        <button class="tab" data-tab="v5-skills">Skills</button>
        <button class="tab" data-tab="v5-self">Self Model</button>
        <button class="tab" data-tab="v5-reflection">Reflection</button>
        <button class="tab" data-tab="live-feed">Live Feed</button>
    </div>

    <!-- Knowledge Tab -->
    <div class="tab-content active" id="tab-knowledge">
        <div class="filters">
            <input type="text" id="search-input" placeholder="Search knowledge...">
            <select id="type-filter"><option value="">All types</option></select>
            <select id="project-filter"><option value="">All projects</option></select>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Type</th>
                        <th>Project</th>
                        <th>Content</th>
                        <th>Score</th>
                        <th>Recalls</th>
                        <th>Tokens</th>
                        <th>Created</th>
                    </tr>
                </thead>
                <tbody id="knowledge-body"></tbody>
            </table>
        </div>
        <div class="pagination" id="pagination">
            <button id="prev-btn" disabled>&laquo; Prev</button>
            <span class="page-info" id="page-info">Page 1</span>
            <button id="next-btn">&raquo; Next</button>
        </div>
    </div>

    <!-- Sessions Tab -->
    <div class="tab-content" id="tab-sessions">
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Session ID</th>
                        <th>Started</th>
                        <th>Project</th>
                        <th>Status</th>
                        <th>Knowledge Count</th>
                    </tr>
                </thead>
                <tbody id="sessions-body"></tbody>
            </table>
        </div>
    </div>

    <!-- Graph Tab -->
    <div class="tab-content" id="tab-graph">
        <div id="graph-controls">
            <button onclick="graphZoom(1.3)" title="Zoom In">&#x1F50D;+ Zoom In</button>
            <button onclick="graphZoom(0.7)" title="Zoom Out">&#x1F50D;- Zoom Out</button>
            <button onclick="graphFitAll()" title="Fit All">Fit All</button>
            <button onclick="graphRestart()" title="Re-layout">Re-layout</button>
            <select id="graph-filter-project" onchange="graphFilterProject(this.value)">
                <option value="">All Projects</option>
            </select>
            <label style="font-size:13px;color:var(--text-dim);display:flex;align-items:center;gap:4px;">
                <input type="checkbox" id="graph-show-labels" checked onchange="graphToggleLabels(this.checked)"> Labels
            </label>
            <label style="font-size:13px;color:var(--text-dim);display:flex;align-items:center;gap:4px;">
                <input type="checkbox" id="graph-show-edges" checked onchange="graphToggleEdges(this.checked)"> Edges
            </label>
            <span class="graph-info" id="graph-info">Loading...</span>
        </div>
        <div id="graph-controls-row2">
            <span style="font-size:12px;color:var(--text-dim);font-weight:600;">Cluster:</span>
            <button class="cluster-active" onclick="graphSetCluster('none')" id="cluster-btn-none">No Clustering</button>
            <button onclick="graphSetCluster('project')" id="cluster-btn-project">By Project</button>
            <button onclick="graphSetCluster('type')" id="cluster-btn-type">By Type</button>
            <span style="width:1px;height:20px;background:var(--border);margin:0 4px;"></span>
            <input type="text" id="graph-search" placeholder="Search nodes..." oninput="graphSearch(this.value)">
            <select id="graph-filter-type" onchange="graphFilterType(this.value)">
                <option value="">All Types</option>
                <option value="fact">fact</option>
                <option value="solution">solution</option>
                <option value="decision">decision</option>
                <option value="lesson">lesson</option>
                <option value="convention">convention</option>
            </select>
            <label style="font-size:12px;color:var(--text-dim);display:flex;align-items:center;gap:4px;">
                <input type="checkbox" id="graph-show-hulls" checked onchange="graphToggleHulls(this.checked)"> Cluster Hulls
            </label>
        </div>
        <div id="graph-cluster-legend"></div>
        <canvas id="graph-canvas"></canvas>
        <div id="graph-tooltip">
            <div class="tt-type"></div>
            <div class="tt-project"></div>
            <div class="tt-content"></div>
            <div class="tt-edges"></div>
        </div>
    </div>

    <!-- Self-Improvement Tab -->
    <div class="tab-content" id="tab-self-improvement">
        <div class="filters">
            <select id="si-category-filter"><option value="">All categories</option></select>
            <select id="si-project-filter"><option value="">All projects</option></select>
        </div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Category</th>
                        <th>Severity</th>
                        <th>Description</th>
                        <th>Fix</th>
                        <th>Status</th>
                        <th>Created</th>
                    </tr>
                </thead>
                <tbody id="errors-body"></tbody>
            </table>
        </div>
        <div class="pagination" id="errors-pagination">
            <button id="errors-prev-btn" disabled>&laquo; Prev</button>
            <span class="page-info" id="errors-page-info">Page 1</span>
            <button id="errors-next-btn">&raquo; Next</button>
        </div>

        <div class="section-divider"><span class="section-icon">&#9670;</span>Insights</div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Content</th>
                        <th>Category</th>
                        <th>Importance</th>
                        <th>Confidence</th>
                        <th>Status</th>
                        <th>Promotion</th>
                    </tr>
                </thead>
                <tbody id="insights-body"></tbody>
            </table>
        </div>
    </div>

    <!-- Rules Tab -->
    <div class="tab-content" id="tab-rules">
        <div class="filters">
            <select id="rules-project-filter"><option value="">All projects</option></select>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Content</th>
                        <th>Category</th>
                        <th>Scope</th>
                        <th>Priority</th>
                        <th>Fire Count</th>
                        <th>Success Rate</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="rules-body"></tbody>
            </table>
        </div>
    </div>

    <!-- Graph v5 Tab -->
    <div class="tab-content" id="tab-v5-graph">
        <h3 style="margin-bottom:16px;color:var(--accent)">Knowledge Graph</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
            <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px">
                <h4 style="color:var(--text-dim);margin-bottom:12px;font-size:13px;text-transform:uppercase">Nodes by Type</h4>
                <div id="v5-nodes-by-type"></div>
            </div>
            <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px">
                <h4 style="color:var(--text-dim);margin-bottom:12px;font-size:13px;text-transform:uppercase">Edges by Type</h4>
                <div id="v5-edges-by-type"></div>
            </div>
        </div>
        <h4 style="color:var(--text-dim);margin-bottom:12px;font-size:13px;text-transform:uppercase">Top Nodes by Importance</h4>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr><th>Name</th><th>Type</th><th>Importance</th><th>Mentions</th></tr>
                </thead>
                <tbody id="v5-top-nodes-body"></tbody>
            </table>
        </div>
    </div>

    <!-- Episodes Tab -->
    <div class="tab-content" id="tab-v5-episodes">
        <h3 style="margin-bottom:16px;color:var(--solution)">Episodes Timeline</h3>
        <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap" id="v5-episode-stats"></div>
        <div id="v5-episodes-list" style="display:flex;flex-direction:column;gap:12px"></div>
    </div>

    <!-- Skills Tab -->
    <div class="tab-content" id="tab-v5-skills">
        <h3 style="margin-bottom:16px;color:var(--decision)">Skills Registry</h3>
        <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;align-items:center">
            <input type="text" id="v5-skills-search" placeholder="Search skills..." style="flex:1;min-width:200px;padding:8px 14px;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px">
            <select id="v5-skills-filter" style="padding:8px 14px;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px">
                <option value="">All types</option>
                <option value="cmd:">Commands</option>
                <option value="agent:">Agents</option>
                <option value="rules:">Rules</option>
            </select>
            <span id="v5-skills-count" style="color:var(--text-dim);font-size:13px"></span>
        </div>
        <div id="v5-skills-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:16px"></div>
    </div>

    <!-- Self Model Tab -->
    <div class="tab-content" id="tab-v5-self">
        <h3 style="margin-bottom:16px;color:var(--convention)">Self Model</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
            <div>
                <h4 style="color:var(--text-dim);margin-bottom:12px;font-size:13px;text-transform:uppercase">Competencies</h4>
                <div id="v5-competencies"></div>
            </div>
            <div>
                <h4 style="color:var(--text-dim);margin-bottom:12px;font-size:13px;text-transform:uppercase">Blind Spots</h4>
                <div id="v5-blind-spots"></div>
                <h4 style="color:var(--text-dim);margin:20px 0 12px;font-size:13px;text-transform:uppercase">User Model</h4>
                <div id="v5-user-model"></div>
            </div>
        </div>
    </div>

    <!-- Reflection Tab -->
    <div class="tab-content" id="tab-v5-reflection">
        <h3 style="margin-bottom:16px;color:var(--rule-color)">Reflection Reports</h3>
        <div id="v5-reports" style="display:flex;flex-direction:column;gap:16px;margin-bottom:24px"></div>
        <h4 style="color:var(--text-dim);margin-bottom:12px;font-size:13px;text-transform:uppercase">Pending Proposals</h4>
        <div id="v5-proposals" style="display:flex;flex-direction:column;gap:12px"></div>
    </div>

    <!-- Live Feed Tab -->
    <div class="tab-content" id="tab-live-feed">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
            <div id="feed-status" style="width:10px;height:10px;border-radius:50%;background:#ef4444"></div>
            <span id="feed-status-text" style="color:var(--text-dim);font-size:13px">Disconnected</span>
        </div>
        <div id="feed-list" style="display:flex;flex-direction:column;gap:8px;max-height:70vh;overflow-y:auto"></div>
    </div>
</div>

<!-- Detail Modal -->
<div class="modal-overlay" id="detail-modal">
    <div class="modal">
        <button class="modal-close" id="modal-close">&times;</button>
        <h2 id="modal-title">Knowledge Detail</h2>
        <div class="modal-meta" id="modal-meta"></div>
        <div class="modal-section">
            <div class="section-label">Content</div>
            <div class="section-body" id="modal-content"></div>
        </div>
        <div class="modal-section" id="modal-context-section">
            <div class="section-label">Context</div>
            <div class="section-body" id="modal-context"></div>
        </div>
        <div class="modal-section" id="modal-tags-section">
            <div class="section-label">Tags</div>
            <div id="modal-tags"></div>
        </div>
        <div class="modal-section" id="modal-history-section">
            <div class="section-label">Version History</div>
            <div id="modal-history"></div>
        </div>
    </div>
</div>

<script>
// ============================================================
// State
// ============================================================
let currentPage = 1;
const pageLimit = 50;
let totalPages = 1;
let graphData = null;
let graphLoaded = false;

const typeColors = {
    decision: '#f59e0b',
    fact: '#3b82f6',
    solution: '#22c55e',
    lesson: '#ef4444',
    convention: '#8b5cf6',
};

// ============================================================
// API helpers
// ============================================================
async function api(path) {
    const resp = await fetch(path);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

function escapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function truncate(s, n) {
    if (!s) return '';
    return s.length > n ? s.slice(0, n) + '...' : s;
}

function formatDate(iso) {
    if (!iso) return '--';
    try {
        const d = new Date(iso);
        return d.toLocaleDateString('en-GB', { day:'2-digit', month:'short', year:'numeric' })
             + ' ' + d.toLocaleTimeString('en-GB', { hour:'2-digit', minute:'2-digit' });
    } catch { return iso; }
}

function badgeClass(type) {
    return 'badge badge-' + (type || 'fact');
}

// ============================================================
// Stats
// ============================================================
async function loadStats() {
    try {
        const s = await api('/api/stats');
        document.getElementById('stat-total').textContent = s.total_knowledge;
        document.getElementById('stat-sessions').textContent = s.sessions_count;
        document.getElementById('stat-projects').textContent = Object.keys(s.by_project).length;
        document.getElementById('stat-health').textContent = (s.health_score * 100).toFixed(0) + '%';
        document.getElementById('stat-storage').textContent = s.storage_mb.toFixed(1) + ' MB';
        document.getElementById('stat-observations').textContent = s.observations_count || 0;

        // Populate filters
        const typeSelect = document.getElementById('type-filter');
        for (const t of Object.keys(s.by_type).sort()) {
            const opt = document.createElement('option');
            opt.value = t;
            opt.textContent = t + ' (' + s.by_type[t] + ')';
            typeSelect.appendChild(opt);
        }
        const projSelect = document.getElementById('project-filter');
        for (const p of Object.keys(s.by_project).sort()) {
            const opt = document.createElement('option');
            opt.value = p;
            opt.textContent = p + ' (' + s.by_project[p] + ')';
            projSelect.appendChild(opt);
        }
    } catch (e) {
        document.getElementById('error-container').innerHTML =
            '<div class="error-box">Cannot connect to memory database: ' + escapeHtml(e.message) + '</div>';
    }
}

// ============================================================
// Knowledge table
// ============================================================
async function loadKnowledge() {
    const search = document.getElementById('search-input').value;
    const type = document.getElementById('type-filter').value;
    const project = document.getElementById('project-filter').value;

    const params = new URLSearchParams();
    if (search) params.set('q', search);
    if (type) params.set('type', type);
    if (project) params.set('project', project);
    params.set('page', currentPage);
    params.set('limit', pageLimit);

    try {
        const data = await api('/api/knowledge?' + params.toString());
        totalPages = data.pages;

        const tbody = document.getElementById('knowledge-body');
        tbody.innerHTML = '';

        for (const row of data.items) {
            const tr = document.createElement('tr');
            tr.onclick = () => openDetail(row.id);
            tr.innerHTML =
                '<td class="id-col">' + row.id + '</td>' +
                '<td><span class="' + badgeClass(row.type) + '">' + escapeHtml(row.type) + '</span></td>' +
                '<td>' + escapeHtml(row.project) + '</td>' +
                '<td class="content-col">' + escapeHtml(truncate(row.content, 100)) + '</td>' +
                '<td class="num-col">' + (row.confidence != null ? row.confidence.toFixed(2) : '--') + '</td>' +
                '<td class="num-col">' + (row.recall_count || 0) + '</td>' +
                '<td class="num-col">' + Math.round((row.content||'').length / 4) + '</td>' +
                '<td class="date-col">' + formatDate(row.created_at) + '</td>';
            tbody.appendChild(tr);
        }

        if (data.items.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-dim);padding:40px">No knowledge found</td></tr>';
        }

        document.getElementById('page-info').textContent = 'Page ' + currentPage + ' of ' + totalPages;
        document.getElementById('prev-btn').disabled = currentPage <= 1;
        document.getElementById('next-btn').disabled = currentPage >= totalPages;
    } catch (e) {
        document.getElementById('knowledge-body').innerHTML =
            '<tr><td colspan="8" class="loading">Error loading data</td></tr>';
    }
}

// ============================================================
// Detail modal
// ============================================================
async function openDetail(id) {
    try {
        const r = await api('/api/knowledge/' + id);
        if (!r) return;

        document.getElementById('modal-title').textContent = 'Knowledge #' + r.id;

        const meta = document.getElementById('modal-meta');
        meta.innerHTML = [
            metaItem('Type', '<span class="' + badgeClass(r.type) + '">' + escapeHtml(r.type) + '</span>'),
            metaItem('Project', escapeHtml(r.project)),
            metaItem('Confidence', r.confidence != null ? r.confidence.toFixed(2) : '--'),
            metaItem('Recalls', r.recall_count || 0),
            metaItem('Created', formatDate(r.created_at)),
            metaItem('Last Confirmed', formatDate(r.last_confirmed)),
            metaItem('Session', '<span style="font-family:monospace;font-size:12px">' + escapeHtml(truncate(r.session_id, 30)) + '</span>'),
            metaItem('Status', escapeHtml(r.status)),
        ].join('');

        document.getElementById('modal-content').textContent = r.content || '';

        const ctxSection = document.getElementById('modal-context-section');
        const ctxBody = document.getElementById('modal-context');
        if (r.context) {
            ctxSection.style.display = '';
            ctxBody.textContent = r.context;
        } else {
            ctxSection.style.display = 'none';
        }

        const tagsSection = document.getElementById('modal-tags-section');
        const tagsEl = document.getElementById('modal-tags');
        const tags = Array.isArray(r.tags) ? r.tags : [];
        if (tags.length > 0) {
            tagsSection.style.display = '';
            tagsEl.innerHTML = tags.map(t => '<span class="tag">' + escapeHtml(t) + '</span>').join('');
        } else {
            tagsSection.style.display = 'none';
        }

        const histSection = document.getElementById('modal-history-section');
        const histEl = document.getElementById('modal-history');
        const hist = r.version_history || [];
        if (hist.length > 0) {
            histSection.style.display = '';
            histEl.innerHTML = hist.map(h =>
                '<div class="version-item">' +
                '<div class="version-relation">' + escapeHtml(h.relation) + ' &mdash; #' + h.id + ' (' + escapeHtml(h.status) + ')</div>' +
                '<div>' + escapeHtml(truncate(h.content, 200)) + '</div>' +
                '<div style="color:var(--text-dim);font-size:12px;margin-top:4px">' + formatDate(h.created_at) + '</div>' +
                '</div>'
            ).join('');
        } else {
            histSection.style.display = 'none';
        }

        document.getElementById('detail-modal').classList.add('open');
    } catch (e) {
        console.error('Failed to load detail:', e);
    }
}

function metaItem(label, value) {
    return '<div class="meta-item"><div class="meta-label">' + label + '</div><div class="meta-value">' + value + '</div></div>';
}

function closeModal() {
    document.getElementById('detail-modal').classList.remove('open');
}

// ============================================================
// Sessions
// ============================================================
async function loadSessions() {
    try {
        const sessions = await api('/api/sessions?limit=50');
        const tbody = document.getElementById('sessions-body');
        tbody.innerHTML = '';
        for (const s of sessions) {
            const tr = document.createElement('tr');
            tr.innerHTML =
                '<td style="font-family:monospace;font-size:12px;color:var(--text-dim)">' + escapeHtml(truncate(s.id, 36)) + '</td>' +
                '<td class="date-col">' + formatDate(s.started_at) + '</td>' +
                '<td>' + escapeHtml(s.project || 'general') + '</td>' +
                '<td><span class="badge badge-fact">' + escapeHtml(s.status || 'open') + '</span></td>' +
                '<td class="num-col">' + (s.knowledge_count || 0) + '</td>';
            tbody.appendChild(tr);
        }
        if (sessions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-dim);padding:40px">No sessions found</td></tr>';
        }
    } catch (e) {
        document.getElementById('sessions-body').innerHTML =
            '<tr><td colspan="5" class="loading">Error loading sessions</td></tr>';
    }
}

// ============================================================
// Self-Improvement: Errors + Insights
// ============================================================
let errorsPage = 1;
let errorsTotalPages = 1;
const errorsLimit = 50;
let siLoaded = false;

function severityBadge(severity) {
    const s = (severity || 'low').toLowerCase();
    return '<span class="badge badge-' + s + '">' + escapeHtml(s) + '</span>';
}

function statusBadge(status) {
    const s = (status || 'pending').toLowerCase();
    const cls = {active:'active', suspended:'suspended', retired:'retired',
                 resolved:'resolved', pending:'pending', promoted:'active'}[s] || 'fact';
    return '<span class="badge badge-' + cls + '">' + escapeHtml(s) + '</span>';
}

function importanceBadge(val) {
    const v = val || 0;
    const cls = v >= 7 ? 'high-priority' : '';
    return '<span class="badge badge-importance ' + cls + '">' + v + '</span>';
}

function priorityBadge(val) {
    const v = val || 0;
    const cls = v >= 8 ? 'high-priority' : '';
    return '<span class="badge badge-priority ' + cls + '">' + v + '</span>';
}

function successRateBar(rate) {
    const pct = Math.round((rate || 0) * 100);
    const color = pct >= 80 ? 'var(--solution)' : pct >= 50 ? 'var(--decision)' : 'var(--error-high)';
    return '<div class="progress-bar">' +
        '<div class="bar"><div class="bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
        '<span class="bar-label">' + pct + '%</span>' +
        '</div>';
}

async function loadErrors() {
    const category = document.getElementById('si-category-filter').value;
    const project = document.getElementById('si-project-filter').value;

    const params = new URLSearchParams();
    if (category) params.set('category', category);
    if (project) params.set('project', project);
    params.set('page', errorsPage);
    params.set('limit', errorsLimit);

    try {
        const data = await api('/api/errors?' + params.toString());
        errorsTotalPages = data.pages;

        const tbody = document.getElementById('errors-body');
        tbody.innerHTML = '';

        for (const row of data.items) {
            const tr = document.createElement('tr');
            tr.innerHTML =
                '<td class="id-col">' + row.id + '</td>' +
                '<td>' + escapeHtml(row.category || '') + '</td>' +
                '<td>' + severityBadge(row.severity) + '</td>' +
                '<td class="content-col">' + escapeHtml(truncate(row.description, 80)) + '</td>' +
                '<td class="content-col">' + escapeHtml(truncate(row.fix || '', 60)) + '</td>' +
                '<td>' + statusBadge(row.status) + '</td>' +
                '<td class="date-col">' + formatDate(row.created_at) + '</td>';
            tbody.appendChild(tr);
        }

        if (data.items.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);padding:40px">No errors recorded</td></tr>';
        }

        document.getElementById('errors-page-info').textContent = 'Page ' + errorsPage + ' of ' + errorsTotalPages;
        document.getElementById('errors-prev-btn').disabled = errorsPage <= 1;
        document.getElementById('errors-next-btn').disabled = errorsPage >= errorsTotalPages;

        // Populate category filter if not done
        if (document.getElementById('si-category-filter').options.length <= 1 && data.items.length > 0) {
            const cats = new Set(data.items.map(i => i.category).filter(Boolean));
            const sel = document.getElementById('si-category-filter');
            for (const c of [...cats].sort()) {
                const opt = document.createElement('option');
                opt.value = c;
                opt.textContent = c;
                sel.appendChild(opt);
            }
        }
    } catch (e) {
        document.getElementById('errors-body').innerHTML =
            '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);padding:40px">Errors table not available</td></tr>';
    }
}

async function loadInsights() {
    const project = document.getElementById('si-project-filter').value;
    const params = new URLSearchParams();
    if (project) params.set('project', project);

    try {
        const data = await api('/api/insights?' + params.toString());
        const tbody = document.getElementById('insights-body');
        tbody.innerHTML = '';

        for (const row of data) {
            const tr = document.createElement('tr');
            const confPct = Math.round((row.confidence || 0) * 100);
            tr.innerHTML =
                '<td class="id-col">' + row.id + '</td>' +
                '<td class="content-col">' + escapeHtml(truncate(row.content, 100)) + '</td>' +
                '<td>' + escapeHtml(row.category || '') + '</td>' +
                '<td>' + importanceBadge(row.importance) + '</td>' +
                '<td class="num-col">' + confPct + '%</td>' +
                '<td>' + statusBadge(row.status) + '</td>' +
                '<td style="text-align:center">' +
                    (row.promotion_eligible
                        ? '<span class="promo-eligible" title="Eligible for promotion"></span>'
                        : '<span class="promo-not-eligible" title="Not eligible"></span>') +
                '</td>';
            tbody.appendChild(tr);
        }

        if (data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);padding:40px">No insights generated</td></tr>';
        }
    } catch (e) {
        document.getElementById('insights-body').innerHTML =
            '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);padding:40px">Insights table not available</td></tr>';
    }
}

async function loadSelfImprovement() {
    siLoaded = true;
    await Promise.all([loadErrors(), loadInsights()]);
}

// ============================================================
// Rules (SOUL)
// ============================================================
let rulesLoaded = false;

async function loadRules() {
    rulesLoaded = true;
    const project = document.getElementById('rules-project-filter').value;
    const params = new URLSearchParams();
    if (project) params.set('project', project);

    try {
        const data = await api('/api/rules?' + params.toString());
        const tbody = document.getElementById('rules-body');
        tbody.innerHTML = '';

        for (const row of data) {
            const tr = document.createElement('tr');
            const statusCls = (row.status || 'active').toLowerCase();
            tr.style.opacity = statusCls === 'suspended' ? '0.7' : '1';
            tr.innerHTML =
                '<td class="id-col">' + row.id + '</td>' +
                '<td class="content-col">' + escapeHtml(truncate(row.content, 100)) + '</td>' +
                '<td>' + escapeHtml(row.category || '') + '</td>' +
                '<td>' + escapeHtml(row.scope || 'global') + '</td>' +
                '<td>' + priorityBadge(row.priority) + '</td>' +
                '<td class="num-col">' + (row.fire_count || 0) + '</td>' +
                '<td>' + successRateBar(row.success_rate) + '</td>' +
                '<td>' + statusBadge(row.status) + '</td>';
            tbody.appendChild(tr);
        }

        if (data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-dim);padding:40px">No rules defined</td></tr>';
        }
    } catch (e) {
        document.getElementById('rules-body').innerHTML =
            '<tr><td colspan="8" style="text-align:center;color:var(--text-dim);padding:40px">Rules table not available</td></tr>';
    }
}

// ============================================================
// Self-Improvement stats
// ============================================================
async function loadSIStats() {
    try {
        const si = await api('/api/self-improvement');
        const total = (si.error_count || 0) + (si.insight_count || 0) + (si.rule_count || 0);
        document.getElementById('stat-si').textContent = total;
        document.getElementById('stat-si-detail').textContent =
            (si.error_count || 0) + ' errors, ' +
            (si.insight_count || 0) + ' insights, ' +
            (si.rule_count || 0) + ' rules';

        // Populate project filters for SI tabs from stats
        try {
            const stats = await api('/api/stats');
            const projects = Object.keys(stats.by_project || {}).sort();
            for (const selId of ['si-project-filter', 'rules-project-filter']) {
                const sel = document.getElementById(selId);
                if (sel && sel.options.length <= 1) {
                    for (const p of projects) {
                        const opt = document.createElement('option');
                        opt.value = p;
                        opt.textContent = p;
                        sel.appendChild(opt);
                    }
                }
            }
        } catch (_) {}
    } catch (e) {
        document.getElementById('stat-si').textContent = 'N/A';
        document.getElementById('stat-si-detail').textContent = 'tables not available';
    }
}

// ============================================================
// Graph (force-directed with zoom/pan, clustering, pure JS + Canvas)
// ============================================================
let G = {};  // global graph state for control functions

function graphZoom(factor) {
    if (!G.canvas) return;
    const cx = G.canvas.clientWidth / 2;
    const cy = G.canvas.clientHeight / 2;
    G.panX = cx - (cx - G.panX) * factor;
    G.panY = cy - (cy - G.panY) * factor;
    G.zoom *= factor;
    G.drawGraph();
}
function graphFitAll() {
    if (!G.nodes || !G.nodes.length) return;
    let minX=Infinity, maxX=-Infinity, minY=Infinity, maxY=-Infinity;
    for (const n of G.visibleNodes()) {
        minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
        minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    }
    const pad = 60;
    const W = G.canvas.clientWidth;
    const H = G.canvas.clientHeight;
    const dx = maxX - minX || 1;
    const dy = maxY - minY || 1;
    G.zoom = Math.min((W - pad*2) / dx, (H - pad*2) / dy, 3);
    G.panX = W/2 - (minX + dx/2) * G.zoom;
    G.panY = H/2 - (minY + dy/2) * G.zoom;
    G.drawGraph();
}
function graphRestart() {
    if (!G.nodes) return;
    G.clusterMode = 'none';
    document.querySelectorAll('#graph-controls-row2 button[id^="cluster-btn"]').forEach(b => b.classList.remove('cluster-active'));
    document.getElementById('cluster-btn-none').classList.add('cluster-active');
    const spread = Math.sqrt(G.nodes.length) * 80;
    for (const n of G.nodes) {
        n.x = (Math.random() - 0.5) * spread;
        n.y = (Math.random() - 0.5) * spread;
        n.vx = 0; n.vy = 0;
    }
    G.runSimulation();
}
function graphFilterProject(project) {
    G.filterProject = project || '';
    G.drawGraph();
    graphUpdateInfo();
}
function graphFilterType(type) {
    G.filterType = type || '';
    G.drawGraph();
    graphUpdateInfo();
}
function graphToggleLabels(on) { G.showLabels = on; G.drawGraph(); }
function graphToggleEdges(on) { G.showEdges = on; G.drawGraph(); }
function graphToggleHulls(on) { G.showHulls = on; G.drawGraph(); }

function graphSearch(query) {
    G.searchQuery = (query || '').toLowerCase().trim();
    G.drawGraph();
    graphUpdateInfo();
}

function graphSetCluster(mode) {
    if (!G.nodes) return;
    G.clusterMode = mode;
    // Update button states
    document.querySelectorAll('#graph-controls-row2 button[id^="cluster-btn"]').forEach(b => b.classList.remove('cluster-active'));
    document.getElementById('cluster-btn-' + mode).classList.add('cluster-active');

    if (mode === 'none') {
        // Reset cluster centers, re-run simulation
        graphUpdateLegend([]);
        G.runSimulation();
        return;
    }

    // Compute cluster groups and arrange nodes radially
    const groups = {};
    for (const n of G.nodes) {
        const key = mode === 'project' ? (n.project || 'unknown') : (n.type || 'unknown');
        if (!groups[key]) groups[key] = [];
        groups[key].push(n);
    }

    const keys = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);
    const clusterColorMap = {};
    const clPalette = [
        '#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899',
        '#06b6d4','#84cc16','#f97316','#6366f1','#14b8a6','#e11d48',
        '#a855f7','#22c55e','#eab308','#0ea5e9','#d946ef','#64748b'
    ];

    // Arrange clusters in a circle
    const totalNodes = G.nodes.length;
    const baseRadius = Math.sqrt(totalNodes) * 18;
    const angleStep = (2 * Math.PI) / keys.length;

    keys.forEach((key, ki) => {
        const angle = ki * angleStep - Math.PI / 2;
        const cx = Math.cos(angle) * baseRadius;
        const cy = Math.sin(angle) * baseRadius;
        const nodesInGroup = groups[key];
        const groupRadius = Math.sqrt(nodesInGroup.length) * 12;
        clusterColorMap[key] = clPalette[ki % clPalette.length];

        // Place nodes in this cluster around its center
        nodesInGroup.forEach((n, ni) => {
            const a2 = (ni / nodesInGroup.length) * 2 * Math.PI;
            const r2 = groupRadius * Math.sqrt(ni / nodesInGroup.length);
            n.x = cx + Math.cos(a2) * r2;
            n.y = cy + Math.sin(a2) * r2;
            n.vx = 0; n.vy = 0;
            n.clusterKey = key;
            n.clusterColor = clusterColorMap[key];
        });
    });

    G.clusterColorMap = clusterColorMap;
    G.clusterGroups = groups;

    // Build legend
    const legendItems = keys.map(k => ({
        key: k,
        color: clusterColorMap[k],
        count: groups[k].length
    }));
    graphUpdateLegend(legendItems);

    // Run a short simulation with cluster gravity
    G.runClusteredSimulation(groups, clusterColorMap);
}

function graphUpdateLegend(items) {
    const el = document.getElementById('graph-cluster-legend');
    if (!items || !items.length) { el.innerHTML = ''; return; }
    el.innerHTML = items.map(it =>
        '<div class="cl-item" onclick="graphFocusCluster(\'' + it.key.replace(/'/g, "\\'") + '\')">' +
        '<span class="cl-dot" style="background:' + it.color + '"></span>' +
        '<span>' + it.key + ' (' + it.count + ')</span></div>'
    ).join('');
}

function graphFocusCluster(key) {
    if (!G.clusterGroups || !G.clusterGroups[key]) return;
    const nodes = G.clusterGroups[key];
    let minX=Infinity, maxX=-Infinity, minY=Infinity, maxY=-Infinity;
    for (const n of nodes) {
        minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
        minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    }
    const pad = 80;
    const W = G.canvas.clientWidth;
    const H = G.canvas.clientHeight;
    const dx = maxX - minX || 1;
    const dy = maxY - minY || 1;
    G.zoom = Math.min((W - pad*2) / dx, (H - pad*2) / dy, 5);
    G.panX = W/2 - (minX + dx/2) * G.zoom;
    G.panY = H/2 - (minY + dy/2) * G.zoom;
    G.drawGraph();
}

function graphUpdateInfo() {
    const vis = G.visibleNodes();
    let text = vis.length + ' nodes, ' + G.visibleEdges().length + ' edges';
    if (G.filterProject) text += ' [' + G.filterProject + ']';
    if (G.filterType) text += ' [' + G.filterType + ']';
    if (G.searchQuery) text += ' (search: "' + G.searchQuery + '")';
    document.getElementById('graph-info').textContent = text;
}

function initGraph() {
    if (graphLoaded) return;
    graphLoaded = true;

    const canvas = document.getElementById('graph-canvas');
    const ctx = canvas.getContext('2d');
    const tooltip = document.getElementById('graph-tooltip');
    const dpr = window.devicePixelRatio || 1;

    G.canvas = canvas;
    G.ctx = ctx;
    G.zoom = 1;
    G.panX = 0;
    G.panY = 0;
    G.nodes = [];
    G.edges = [];
    G.showLabels = true;
    G.showEdges = true;
    G.showHulls = true;
    G.filterProject = '';
    G.filterType = '';
    G.searchQuery = '';
    G.clusterMode = 'none';
    G.clusterColorMap = {};
    G.clusterGroups = {};
    G.hoveredNode = null;
    G.dragNode = null;
    G.isPanning = false;
    G.lastMouse = {x:0, y:0};

    function resize() {
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        if (G.drawGraph) G.drawGraph();
    }
    resize();
    window.addEventListener('resize', resize);

    // Project color palette
    const projectColors = {};
    const projectPalette = [
        '#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899',
        '#06b6d4','#84cc16','#f97316','#6366f1','#14b8a6','#e11d48',
        '#a855f7','#22c55e','#eab308','#0ea5e9','#d946ef','#64748b'
    ];
    let pIdx = 0;
    function getProjectColor(proj) {
        if (!projectColors[proj]) {
            projectColors[proj] = projectPalette[pIdx % projectPalette.length];
            pIdx++;
        }
        return projectColors[proj];
    }

    // Screen <-> world transforms
    function toScreen(wx, wy) {
        return { x: wx * G.zoom + G.panX, y: wy * G.zoom + G.panY };
    }
    function toWorld(sx, sy) {
        return { x: (sx - G.panX) / G.zoom, y: (sy - G.panY) / G.zoom };
    }

    G.visibleNodes = function() {
        let nodes = G.nodes;
        if (G.filterProject) nodes = nodes.filter(n => n.project === G.filterProject);
        if (G.filterType) nodes = nodes.filter(n => n.type === G.filterType);
        if (G.searchQuery) nodes = nodes.filter(n =>
            n.label.toLowerCase().includes(G.searchQuery) ||
            (n.project || '').toLowerCase().includes(G.searchQuery) ||
            (n.type || '').toLowerCase().includes(G.searchQuery)
        );
        return nodes;
    };
    G.visibleEdges = function() {
        const vis = new Set(G.visibleNodes().map(n => n.id));
        return G.edges.filter(e => vis.has(e.source.id) && vis.has(e.target.id));
    };

    // Build adjacency for tooltip
    function getNodeEdges(node) {
        return G.edges.filter(e => e.source.id === node.id || e.target.id === node.id);
    }

    // Convex hull (Graham scan) for cluster boundaries
    function convexHull(points) {
        if (points.length < 3) return points;
        const pts = points.slice().sort((a, b) => a.x - b.x || a.y - b.y);
        const cross = (O, A, B) => (A.x - O.x) * (B.y - O.y) - (A.y - O.y) * (B.x - O.x);
        const lower = [];
        for (const p of pts) {
            while (lower.length >= 2 && cross(lower[lower.length-2], lower[lower.length-1], p) <= 0) lower.pop();
            lower.push(p);
        }
        const upper = [];
        for (let i = pts.length - 1; i >= 0; i--) {
            while (upper.length >= 2 && cross(upper[upper.length-2], upper[upper.length-1], pts[i]) <= 0) upper.pop();
            upper.push(pts[i]);
        }
        upper.pop(); lower.pop();
        return lower.concat(upper);
    }

    api('/api/graph').then(data => {
        graphData = data;
        const idMap = {};
        const projects = new Set();
        const types = new Set();
        const spread = Math.sqrt(data.nodes.length) * 100;

        G.nodes = data.nodes.map((n) => {
            projects.add(n.project);
            types.add(n.type);
            const node = {
                id: n.id,
                x: (Math.random() - 0.5) * spread,
                y: (Math.random() - 0.5) * spread,
                vx: 0, vy: 0,
                type: n.type,
                project: n.project || 'unknown',
                label: n.label,
                recall_count: n.recall_count || 0,
                confidence: n.confidence || 0,
                radius: Math.max(4, Math.min(14, 4 + Math.sqrt(n.recall_count || 0) * 2.5)),
                color: typeColors[n.type] || '#64748b',
                projectColor: getProjectColor(n.project || 'unknown'),
                clusterKey: '',
                clusterColor: '',
            };
            idMap[n.id] = node;
            return node;
        });

        G.edges = data.edges.map(e => ({
            source: idMap[e.from_id],
            target: idMap[e.to_id],
            type: e.type,
        })).filter(e => e.source && e.target);

        // Populate project filter
        const sel = document.getElementById('graph-filter-project');
        [...projects].sort().forEach(p => {
            const opt = document.createElement('option');
            opt.value = p; opt.textContent = p;
            sel.appendChild(opt);
        });

        // Populate type filter
        const tsel = document.getElementById('graph-filter-type');
        // Clear existing options except first
        while (tsel.options.length > 1) tsel.remove(1);
        [...types].sort().forEach(t => {
            const opt = document.createElement('option');
            opt.value = t; opt.textContent = t;
            tsel.appendChild(opt);
        });

        graphUpdateInfo();
        G.runSimulation();
    }).catch(err => {
        document.getElementById('graph-info').textContent = 'Error loading graph: ' + err.message;
    });

    // Barnes-Hut quad-tree for O(n log n) repulsion
    function buildQuadTree(nodes) {
        if (!nodes.length) return null;
        let minX=Infinity, maxX=-Infinity, minY=Infinity, maxY=-Infinity;
        for (const n of nodes) {
            minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
            minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
        }
        const pad = 10;
        const size = Math.max(maxX - minX, maxY - minY) + pad * 2;
        const cx = (minX + maxX) / 2;
        const cy = (minY + maxY) / 2;

        function makeNode(x, y, s) { return {x, y, size: s, mass: 0, comX: 0, comY: 0, children: null, body: null}; }

        function insert(tree, node) {
            if (tree.mass === 0 && !tree.body) {
                tree.body = node;
                tree.mass = 1;
                tree.comX = node.x;
                tree.comY = node.y;
                return;
            }
            if (tree.body) {
                if (!tree.children) subdivide(tree);
                const old = tree.body;
                tree.body = null;
                insertToChild(tree, old);
            }
            insertToChild(tree, node);
            // Update center of mass
            const newMass = tree.mass + 1;
            tree.comX = (tree.comX * tree.mass + node.x) / newMass;
            tree.comY = (tree.comY * tree.mass + node.y) / newMass;
            tree.mass = newMass;
        }

        function subdivide(tree) {
            const hs = tree.size / 2;
            tree.children = [
                makeNode(tree.x - hs/2, tree.y - hs/2, hs), // NW
                makeNode(tree.x + hs/2, tree.y - hs/2, hs), // NE
                makeNode(tree.x - hs/2, tree.y + hs/2, hs), // SW
                makeNode(tree.x + hs/2, tree.y + hs/2, hs), // SE
            ];
        }

        function insertToChild(tree, node) {
            const idx = (node.x > tree.x ? 1 : 0) + (node.y > tree.y ? 2 : 0);
            insert(tree.children[idx], node);
        }

        const root = makeNode(cx, cy, size);
        for (const n of nodes) insert(root, n);
        return root;
    }

    function quadTreeForce(tree, node, theta, k) {
        if (!tree || tree.mass === 0) return {fx: 0, fy: 0};
        if (tree.body && tree.body !== node) {
            const dx = node.x - tree.comX;
            const dy = node.y - tree.comY;
            const dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
            if (dist > k * 8) return {fx: 0, fy: 0};
            const force = (k * k) / dist;
            return {fx: (dx / dist) * force, fy: (dy / dist) * force};
        }
        if (!tree.children) return {fx: 0, fy: 0};
        const dx = node.x - tree.comX;
        const dy = node.y - tree.comY;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
        if (tree.size / dist < theta) {
            // Treat as single body
            if (dist > k * 8) return {fx: 0, fy: 0};
            const force = (k * k * tree.mass) / dist;
            return {fx: (dx / dist) * force, fy: (dy / dist) * force};
        }
        let fx = 0, fy = 0;
        for (const child of tree.children) {
            const f = quadTreeForce(child, node, theta, k);
            fx += f.fx; fy += f.fy;
        }
        return {fx, fy};
    }

    G.runSimulation = function() {
        const nodes = G.nodes;
        const edges = G.edges;
        let iterations = 0;
        const maxIter = 400;
        const k = 180; // ideal spring length (was 60 — too dense)

        function tick() {
            if (iterations > maxIter) {
                G.drawGraph();
                setTimeout(() => graphFitAll(), 50);
                return;
            }
            iterations++;
            const cooling = 1 - (iterations / maxIter);

            // Barnes-Hut repulsion O(n log n)
            const tree = buildQuadTree(nodes);
            for (const n of nodes) {
                const f = quadTreeForce(tree, n, 0.7, k);
                // Same-project nodes get less repulsion (applied in clustered sim only)
                n.vx += f.fx * 2.0;
                n.vy += f.fy * 2.0;
            }

            // Attraction along edges
            for (const e of edges) {
                let dx = e.target.x - e.source.x;
                let dy = e.target.y - e.source.y;
                let dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
                let force = (dist - k * 1.2) * 0.02;
                let fx = (dx / dist) * force;
                let fy = (dy / dist) * force;
                e.source.vx += fx;
                e.source.vy += fy;
                e.target.vx -= fx;
                e.target.vy -= fy;
            }

            // Weak center gravity
            for (const n of nodes) {
                n.vx -= n.x * 0.0003;
                n.vy -= n.y * 0.0003;
            }

            // Apply velocity with cooling
            for (const n of nodes) {
                if (n === G.dragNode) continue;
                let speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
                let maxSpeed = 20 * cooling;
                if (speed > maxSpeed && speed > 0) {
                    n.vx = (n.vx / speed) * maxSpeed;
                    n.vy = (n.vy / speed) * maxSpeed;
                }
                n.x += n.vx * cooling;
                n.y += n.vy * cooling;
                n.vx *= 0.82;
                n.vy *= 0.82;
            }

            if (iterations % 4 === 0) G.drawGraph();
            requestAnimationFrame(tick);
        }
        tick();
    };

    // Clustered simulation: keeps nodes near their cluster center
    G.runClusteredSimulation = function(groups, colorMap) {
        const nodes = G.nodes;
        const edges = G.edges;
        let iterations = 0;
        const maxIter = 250;

        // Compute cluster centers
        const centers = {};
        for (const key of Object.keys(groups)) {
            const ns = groups[key];
            let sx = 0, sy = 0;
            for (const n of ns) { sx += n.x; sy += n.y; }
            centers[key] = {x: sx / ns.length, y: sy / ns.length};
        }

        function tick() {
            if (iterations > maxIter) {
                G.drawGraph();
                setTimeout(() => graphFitAll(), 50);
                return;
            }
            iterations++;
            const cooling = 1 - (iterations / maxIter);
            const k = 30;

            // Intra-cluster repulsion (only between nodes in same cluster)
            for (const key of Object.keys(groups)) {
                const ns = groups[key];
                if (ns.length > 500) {
                    // Use quad-tree for large clusters
                    const tree = buildQuadTree(ns);
                    for (const n of ns) {
                        const f = quadTreeForce(tree, n, 0.7, k);
                        n.vx += f.fx * 0.5;
                        n.vy += f.fy * 0.5;
                    }
                } else {
                    for (let i = 0; i < ns.length; i++) {
                        for (let j = i + 1; j < ns.length; j++) {
                            let dx = ns[j].x - ns[i].x;
                            let dy = ns[j].y - ns[i].y;
                            let dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
                            if (dist > k * 5) continue;
                            let force = (k * k) / dist * 0.4;
                            let fx = (dx / dist) * force;
                            let fy = (dy / dist) * force;
                            ns[i].vx -= fx; ns[i].vy -= fy;
                            ns[j].vx += fx; ns[j].vy += fy;
                        }
                    }
                }
            }

            // Attraction to cluster center (strong)
            for (const key of Object.keys(groups)) {
                const c = centers[key];
                for (const n of groups[key]) {
                    const dx = c.x - n.x;
                    const dy = c.y - n.y;
                    n.vx += dx * 0.008;
                    n.vy += dy * 0.008;
                }
            }

            // Intra-cluster edge attraction
            for (const e of edges) {
                if (e.source.clusterKey !== e.target.clusterKey) continue;
                let dx = e.target.x - e.source.x;
                let dy = e.target.y - e.source.y;
                let dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
                let force = (dist - k * 0.6) * 0.03;
                let fx = (dx / dist) * force;
                let fy = (dy / dist) * force;
                e.source.vx += fx; e.source.vy += fy;
                e.target.vx -= fx; e.target.vy -= fy;
            }

            // Apply velocity
            for (const n of nodes) {
                if (n === G.dragNode) continue;
                let speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
                let maxSpeed = 10 * cooling;
                if (speed > maxSpeed && speed > 0) {
                    n.vx = (n.vx / speed) * maxSpeed;
                    n.vy = (n.vy / speed) * maxSpeed;
                }
                n.x += n.vx * cooling;
                n.y += n.vy * cooling;
                n.vx *= 0.8;
                n.vy *= 0.8;
            }

            if (iterations % 4 === 0) G.drawGraph();
            requestAnimationFrame(tick);
        }
        tick();
    };

    G.drawGraph = function() {
        const W = canvas.clientWidth;
        const H = canvas.clientHeight;
        ctx.clearRect(0, 0, W, H);

        const visNodes = G.visibleNodes();
        const visNodeIds = new Set(visNodes.map(n => n.id));
        let visEdges = G.showEdges ? G.edges.filter(e => visNodeIds.has(e.source.id) && visNodeIds.has(e.target.id)) : [];
        // Limit edges to prevent visual clutter (keep strongest connections)
        const MAX_VIS_EDGES = 1500;
        if (visEdges.length > MAX_VIS_EDGES) {
            visEdges.sort((a, b) => (b.weight || 1) - (a.weight || 1));
            visEdges = visEdges.slice(0, MAX_VIS_EDGES);
        }

        // Highlight matched search nodes
        const searchSet = new Set();
        if (G.searchQuery) {
            for (const n of visNodes) searchSet.add(n.id);
        }

        // Draw cluster hulls
        if (G.showHulls && G.clusterMode !== 'none' && G.clusterGroups) {
            for (const key of Object.keys(G.clusterGroups)) {
                const clNodes = G.clusterGroups[key].filter(n => visNodeIds.has(n.id));
                if (clNodes.length < 3) continue;
                const points = clNodes.map(n => {
                    const s = toScreen(n.x, n.y);
                    return {x: s.x, y: s.y};
                });
                const hull = convexHull(points);
                if (hull.length < 3) continue;

                const color = G.clusterColorMap[key] || '#64748b';
                // Draw expanded hull with padding
                ctx.beginPath();
                // Smooth hull with rounded corners
                const pad = 20 * G.zoom;
                for (let i = 0; i < hull.length; i++) {
                    const p = hull[i];
                    // Offset points outward from centroid
                    let cx2 = 0, cy2 = 0;
                    for (const h of hull) { cx2 += h.x; cy2 += h.y; }
                    cx2 /= hull.length; cy2 /= hull.length;
                    const dx = p.x - cx2;
                    const dy = p.y - cy2;
                    const dist = Math.sqrt(dx*dx + dy*dy) || 1;
                    const ox = p.x + (dx / dist) * pad;
                    const oy = p.y + (dy / dist) * pad;
                    if (i === 0) ctx.moveTo(ox, oy);
                    else ctx.lineTo(ox, oy);
                }
                ctx.closePath();
                ctx.fillStyle = color + '12';
                ctx.fill();
                ctx.strokeStyle = color + '30';
                ctx.lineWidth = 1.5;
                ctx.setLineDash([4, 4]);
                ctx.stroke();
                ctx.setLineDash([]);

                // Cluster label at centroid
                let lcx = 0, lcy = 0;
                for (const n of clNodes) { const s = toScreen(n.x, n.y); lcx += s.x; lcy += s.y; }
                lcx /= clNodes.length; lcy /= clNodes.length;
                const fontSize = Math.max(11, Math.min(16, 13 * G.zoom));
                ctx.font = 'bold ' + fontSize + 'px system-ui';
                ctx.textAlign = 'center';
                ctx.fillStyle = color + 'aa';
                ctx.fillText(key + ' (' + clNodes.length + ')', lcx, lcy - (Math.sqrt(clNodes.length) * 3 * G.zoom));
            }
        }

        // Draw edges (batch by style for performance)
        if (visEdges.length > 0) {
            // Non-hovered edges
            ctx.beginPath();
            ctx.strokeStyle = 'rgba(100,116,139,0.06)';
            ctx.lineWidth = Math.max(0.2, 0.4 * G.zoom);
            for (const e of visEdges) {
                if (G.hoveredNode && (e.source.id === G.hoveredNode.id || e.target.id === G.hoveredNode.id)) continue;
                const s = toScreen(e.source.x, e.source.y);
                const t = toScreen(e.target.x, e.target.y);
                // Cull off-screen edges
                if ((s.x < 0 && t.x < 0) || (s.x > W && t.x > W)) continue;
                if ((s.y < 0 && t.y < 0) || (s.y > H && t.y > H)) continue;
                ctx.moveTo(s.x, s.y);
                ctx.lineTo(t.x, t.y);
            }
            ctx.stroke();

            // Hovered node edges
            if (G.hoveredNode) {
                ctx.beginPath();
                ctx.strokeStyle = 'rgba(96,165,250,0.8)';
                ctx.lineWidth = 2 * G.zoom;
                for (const e of visEdges) {
                    if (e.source.id !== G.hoveredNode.id && e.target.id !== G.hoveredNode.id) continue;
                    const s = toScreen(e.source.x, e.source.y);
                    const t = toScreen(e.target.x, e.target.y);
                    ctx.moveTo(s.x, s.y);
                    ctx.lineTo(t.x, t.y);
                }
                ctx.stroke();

                // Edge labels when zoomed
                if (G.showLabels && G.zoom > 1.5) {
                    for (const e of visEdges) {
                        if (e.source.id !== G.hoveredNode.id && e.target.id !== G.hoveredNode.id) continue;
                        const s = toScreen(e.source.x, e.source.y);
                        const t = toScreen(e.target.x, e.target.y);
                        const mx = (s.x + t.x) / 2;
                        const my = (s.y + t.y) / 2;
                        ctx.font = Math.max(9, 10 * G.zoom) + 'px system-ui';
                        ctx.fillStyle = 'rgba(148,163,184,0.8)';
                        ctx.textAlign = 'center';
                        ctx.fillText(e.type, mx, my - 3);
                    }
                }
            }
        }

        // Build hovered adjacency set for dimming
        let hoveredAdj = null;
        if (G.hoveredNode) {
            hoveredAdj = new Set();
            hoveredAdj.add(G.hoveredNode.id);
            for (const e of G.edges) {
                if (e.source.id === G.hoveredNode.id) hoveredAdj.add(e.target.id);
                if (e.target.id === G.hoveredNode.id) hoveredAdj.add(e.source.id);
            }
        }

        // Draw nodes
        for (const n of visNodes) {
            const s = toScreen(n.x, n.y);
            const r = n.radius * G.zoom;
            if (s.x + r < -5 || s.x - r > W + 5 || s.y + r < -5 || s.y - r > H + 5) continue;

            const dimmed = hoveredAdj && !hoveredAdj.has(n.id);
            const isSearchMatch = G.searchQuery && searchSet.has(n.id);
            const nodeColor = (G.clusterMode !== 'none' && n.clusterColor) ? n.clusterColor : n.color;

            // Node glow for hovered or search match
            if (n === G.hoveredNode || isSearchMatch) {
                ctx.beginPath();
                ctx.arc(s.x, s.y, r + 5, 0, Math.PI * 2);
                ctx.fillStyle = (isSearchMatch ? '#fbbf24' : nodeColor) + '44';
                ctx.fill();
            }

            // Node circle
            ctx.beginPath();
            ctx.arc(s.x, s.y, r, 0, Math.PI * 2);
            if (dimmed) {
                ctx.fillStyle = nodeColor + '25';
            } else if (n === G.hoveredNode) {
                ctx.fillStyle = nodeColor;
            } else {
                ctx.fillStyle = nodeColor + 'bb';
            }
            ctx.fill();

            // Border: cluster color or project color
            if (n === G.hoveredNode) {
                ctx.strokeStyle = '#ffffff';
                ctx.lineWidth = 2.5;
            } else if (isSearchMatch) {
                ctx.strokeStyle = '#fbbf24';
                ctx.lineWidth = 2;
            } else {
                ctx.strokeStyle = (G.clusterMode !== 'none' && n.clusterColor) ? n.clusterColor + '55' : n.projectColor + '55';
                ctx.lineWidth = 0.8;
            }
            ctx.stroke();

            // Labels
            if (G.showLabels && G.zoom > 0.6 && !dimmed) {
                const fontSize = Math.max(8, Math.min(12, 10 * G.zoom));
                ctx.font = fontSize + 'px system-ui';
                ctx.textAlign = 'center';

                if (G.zoom > 1.2 || n === G.hoveredNode) {
                    const lbl = n.label.length > 40 ? n.label.slice(0, 37) + '...' : n.label;
                    ctx.fillStyle = 'rgba(226,232,240,0.9)';
                    ctx.fillText(lbl, s.x, s.y + r + fontSize + 2);
                } else if (G.zoom > 0.6) {
                    ctx.fillStyle = 'rgba(148,163,184,0.5)';
                    ctx.fillText(n.project, s.x, s.y + r + fontSize + 2);
                }
            }
        }

        // Legend (bottom-left) — only when not clustered (cluster legend is in HTML)
        if (G.clusterMode === 'none') {
            const ltypes = [...new Set(visNodes.map(n => n.type))];
            ctx.font = '11px system-ui';
            ctx.textAlign = 'left';
            ltypes.forEach((t, i) => {
                const ly = H - 16 - (ltypes.length - 1 - i) * 18;
                ctx.beginPath();
                ctx.arc(16, ly, 5, 0, Math.PI * 2);
                ctx.fillStyle = typeColors[t] || '#64748b';
                ctx.fill();
                ctx.fillStyle = 'rgba(148,163,184,0.7)';
                ctx.fillText(t, 28, ly + 4);
            });
        }
    };

    // ---- Mouse interaction with zoom/pan ----
    canvas.addEventListener('wheel', e => {
        e.preventDefault();
        const br = canvas.getBoundingClientRect();
        const mx = e.clientX - br.left;
        const my = e.clientY - br.top;
        const factor = e.deltaY < 0 ? 1.15 : 0.87;
        G.panX = mx - (mx - G.panX) * factor;
        G.panY = my - (my - G.panY) * factor;
        G.zoom *= factor;
        G.zoom = Math.max(0.05, Math.min(15, G.zoom));
        G.drawGraph();
    }, {passive: false});

    canvas.addEventListener('mousemove', e => {
        const br = canvas.getBoundingClientRect();
        const mx = e.clientX - br.left;
        const my = e.clientY - br.top;

        // Drag node
        if (G.dragNode) {
            const w = toWorld(mx, my);
            G.dragNode.x = w.x;
            G.dragNode.y = w.y;
            G.dragNode.vx = 0;
            G.dragNode.vy = 0;
            G.drawGraph();
            return;
        }

        // Pan
        if (G.isPanning) {
            G.panX += mx - G.lastMouse.x;
            G.panY += my - G.lastMouse.y;
            G.lastMouse = {x: mx, y: my};
            G.drawGraph();
            return;
        }

        // Hover detection in world coords
        const w = toWorld(mx, my);
        let found = null;
        const visNodes = G.visibleNodes();
        for (const n of visNodes) {
            const dx = n.x - w.x;
            const dy = n.y - w.y;
            const hitR = n.radius + 4 / G.zoom;
            if (dx * dx + dy * dy < hitR * hitR) {
                found = n;
                break;
            }
        }

        if (found !== G.hoveredNode) {
            G.hoveredNode = found;
            G.drawGraph();
        }

        if (G.hoveredNode) {
            canvas.style.cursor = 'pointer';
            tooltip.style.display = 'block';
            tooltip.style.left = (e.clientX + 14) + 'px';
            tooltip.style.top = (e.clientY + 14) + 'px';
            tooltip.querySelector('.tt-type').textContent = G.hoveredNode.type;
            tooltip.querySelector('.tt-type').style.color = G.hoveredNode.color;
            tooltip.querySelector('.tt-project').textContent = G.hoveredNode.project;
            tooltip.querySelector('.tt-content').textContent = G.hoveredNode.label;
            const ne = getNodeEdges(G.hoveredNode);
            tooltip.querySelector('.tt-edges').textContent = ne.length ?
                ne.length + ' connection(s): ' + [...new Set(ne.map(x=>x.type))].join(', ') : 'No connections';
        } else {
            canvas.style.cursor = 'grab';
            tooltip.style.display = 'none';
        }
    });

    canvas.addEventListener('mousedown', e => {
        const br = canvas.getBoundingClientRect();
        const mx = e.clientX - br.left;
        const my = e.clientY - br.top;

        if (G.hoveredNode) {
            G.dragNode = G.hoveredNode;
            canvas.style.cursor = 'grabbing';
        } else {
            G.isPanning = true;
            G.lastMouse = {x: mx, y: my};
            canvas.style.cursor = 'grabbing';
        }
    });

    canvas.addEventListener('mouseup', () => {
        G.dragNode = null;
        G.isPanning = false;
        canvas.style.cursor = G.hoveredNode ? 'pointer' : 'grab';
    });

    canvas.addEventListener('mouseleave', () => {
        tooltip.style.display = 'none';
        G.hoveredNode = null;
        G.dragNode = null;
        G.isPanning = false;
        G.drawGraph();
    });

    // Double-click to zoom into node
    canvas.addEventListener('dblclick', e => {
        const br = canvas.getBoundingClientRect();
        const mx = e.clientX - br.left;
        const my = e.clientY - br.top;
        if (G.hoveredNode) {
            const targetZoom = 2.5;
            G.zoom = targetZoom;
            G.panX = mx - G.hoveredNode.x * targetZoom;
            G.panY = my - G.hoveredNode.y * targetZoom;
            G.drawGraph();
        }
    });
}

// ============================================================
// Tabs
// ============================================================
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        const target = tab.dataset.tab;
        document.getElementById('tab-' + target).classList.add('active');

        if (target === 'sessions') loadSessions();
        if (target === 'graph') initGraph();
        if (target === 'self-improvement') loadSelfImprovement();
        if (target === 'rules') loadRules();
        if (target === 'v5-graph') loadV5Graph();
        if (target === 'v5-episodes') loadV5Episodes();
        if (target === 'v5-skills') loadV5Skills();
        if (target === 'v5-self') { loadV5SelfModel(); if (!window._selfModelInterval) window._selfModelInterval = setInterval(loadV5SelfModel, 60000); }
        if (target === 'v5-reflection') loadV5Reflection();
        if (target === 'live-feed') initLiveFeed();
    });
});

// ============================================================
// Pagination
// ============================================================
document.getElementById('prev-btn').addEventListener('click', () => {
    if (currentPage > 1) { currentPage--; loadKnowledge(); }
});
document.getElementById('next-btn').addEventListener('click', () => {
    if (currentPage < totalPages) { currentPage++; loadKnowledge(); }
});

// ============================================================
// Errors pagination
// ============================================================
document.getElementById('errors-prev-btn').addEventListener('click', () => {
    if (errorsPage > 1) { errorsPage--; loadErrors(); }
});
document.getElementById('errors-next-btn').addEventListener('click', () => {
    if (errorsPage < errorsTotalPages) { errorsPage++; loadErrors(); }
});

// ============================================================
// Self-Improvement filters
// ============================================================
document.getElementById('si-category-filter').addEventListener('change', () => { errorsPage = 1; loadErrors(); });
document.getElementById('si-project-filter').addEventListener('change', () => { errorsPage = 1; loadErrors(); loadInsights(); });
document.getElementById('rules-project-filter').addEventListener('change', () => { loadRules(); });

// ============================================================
// Search / filters
// ============================================================
let searchTimeout;
document.getElementById('search-input').addEventListener('input', () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => { currentPage = 1; loadKnowledge(); }, 300);
});
document.getElementById('type-filter').addEventListener('change', () => { currentPage = 1; loadKnowledge(); });
document.getElementById('project-filter').addEventListener('change', () => { currentPage = 1; loadKnowledge(); });

// ============================================================
// Modal
// ============================================================
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('detail-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ============================================================
// Init
// ============================================================
loadStats();
// Connect Live Feed SSE on page load (independent from loadStats).
// Defer one tick so a slow loadStats doesn't block the connection.
setTimeout(() => {
    try { initLiveFeed(); }
    catch (e) { console.error('initLiveFeed failed:', e); }
}, 0);
loadKnowledge();
loadSIStats();
loadV5Stats();

// ============================================================
// Live Feed (SSE)
// ============================================================
let feedSource = null;
let feedInitialized = false;
const MAX_FEED_ITEMS = 100;

function initLiveFeed() {
    if (feedInitialized) return;
    feedInitialized = true;
    connectSSE();
}

function _setFeedStatus(color, text) {
    // Tab pane indicators (when Live Feed tab visible)
    const dot = document.getElementById('feed-status');
    const lbl = document.getElementById('feed-status-text');
    if (dot) dot.style.background = color;
    if (lbl) lbl.textContent = text;
    // Header pill (always visible)
    const hdot = document.getElementById('header-feed-dot');
    const hlbl = document.getElementById('header-feed-text');
    if (hdot) hdot.style.background = color;
    if (hlbl) hlbl.textContent = text;
}

function connectSSE() {
    if (feedSource) { try { feedSource.close(); } catch(e) {} }

    _setFeedStatus('#facc15', 'Connecting…');
    feedSource = new EventSource('/api/events');

    feedSource.onopen = () => _setFeedStatus('#22c55e', 'Connected');
    feedSource.onerror = () => _setFeedStatus('#ef4444', 'Reconnecting…');

    feedSource.addEventListener('knowledge', e => {
        addFeedItem('knowledge', JSON.parse(e.data));
    });
    feedSource.addEventListener('error_log', e => {
        addFeedItem('error', JSON.parse(e.data));
    });
    feedSource.addEventListener('observation', e => {
        addFeedItem('observation', JSON.parse(e.data));
    });
}

function addFeedItem(type, data) {
    const list = document.getElementById('feed-list');
    const item = document.createElement('div');
    item.style.cssText = 'padding:12px;background:var(--card);border-radius:8px;border:1px solid var(--border);animation:fadeIn 0.3s ease';
    const colors = { knowledge: 'var(--accent)', error: 'var(--error-high)', observation: 'var(--insight-color)' };
    const color = colors[type] || 'var(--text-dim)';
    let label = data.summary || data.content || data.description || '';
    if (label.length > 150) label = label.substring(0, 150) + '...';
    item.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
        '<span style="color:' + color + ';font-weight:600;text-transform:uppercase;font-size:12px">' + escapeHtml(type) + '</span>' +
        '<span style="color:var(--text-dim);font-size:12px">' + new Date().toLocaleTimeString() + '</span>' +
        '</div>' +
        '<div style="color:var(--text);font-size:14px">' + escapeHtml(label) + '</div>';
    list.prepend(item);
    while (list.children.length > MAX_FEED_ITEMS) list.lastChild.remove();
}

// ============================================================
// Super Memory v5 — Stats loading
// ============================================================
async function loadV5Stats() {
    try {
        const gs = await api('/api/graph-stats');
        document.getElementById('stat-graph').textContent = gs.total_nodes;
        document.getElementById('stat-graph-detail').textContent = gs.total_nodes + ' nodes, ' + gs.total_edges + ' edges';
    } catch (_) {
        document.getElementById('stat-graph').textContent = 'N/A';
        document.getElementById('stat-graph-detail').textContent = 'tables not available';
    }

    try {
        const ep = await api('/api/episodes');
        const st = ep.stats || {};
        document.getElementById('stat-episodes').textContent = st.total || 0;
        const b = (st.by_outcome || {}).breakthrough || 0;
        const f = (st.by_outcome || {}).failure || 0;
        document.getElementById('stat-episodes-detail').textContent = b + ' breakthroughs, ' + f + ' failures';
    } catch (_) {
        document.getElementById('stat-episodes').textContent = 'N/A';
        document.getElementById('stat-episodes-detail').textContent = 'tables not available';
    }

    try {
        const sk = await api('/api/skills');
        document.getElementById('stat-skills').textContent = sk.total || 0;
        if (sk.skills && sk.skills.length > 0) {
            const avgSr = sk.skills.reduce((a, s) => a + (s.success_rate || 0), 0) / sk.skills.length;
            document.getElementById('stat-skills-detail').textContent = 'avg success: ' + (avgSr * 100).toFixed(0) + '%';
        }
    } catch (_) {
        document.getElementById('stat-skills').textContent = 'N/A';
        document.getElementById('stat-skills-detail').textContent = 'tables not available';
    }

    try {
        const sm = await api('/api/self-model');
        document.getElementById('stat-blind-spots').textContent = (sm.blind_spots || []).length;
    } catch (_) {
        document.getElementById('stat-blind-spots').textContent = 'N/A';
    }
}

// ============================================================
// Super Memory v5 — Graph Tab
// ============================================================
async function loadV5Graph() {
    try {
        const data = await api('/api/graph-stats');

        // Nodes by type
        const nodesDiv = document.getElementById('v5-nodes-by-type');
        nodesDiv.innerHTML = Object.entries(data.nodes_by_type || {}).map(([type, count]) =>
            '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">' +
            '<span class="badge badge-' + type + '">' + escapeHtml(type) + '</span>' +
            '<span style="font-weight:600;color:var(--accent)">' + count + '</span></div>'
        ).join('') || '<div style="color:var(--text-dim)">No graph nodes yet</div>';

        // Edges by type
        const edgesDiv = document.getElementById('v5-edges-by-type');
        edgesDiv.innerHTML = Object.entries(data.edges_by_type || {}).map(([type, count]) =>
            '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">' +
            '<span style="color:var(--text)">' + escapeHtml(type) + '</span>' +
            '<span style="font-weight:600;color:var(--decision)">' + count + '</span></div>'
        ).join('') || '<div style="color:var(--text-dim)">No graph edges yet</div>';

        // Top nodes table
        const tbody = document.getElementById('v5-top-nodes-body');
        tbody.innerHTML = (data.top_nodes || []).map(n =>
            '<tr>' +
            '<td>' + escapeHtml(n.name) + '</td>' +
            '<td><span class="badge badge-' + (n.type || 'fact') + '">' + escapeHtml(n.type) + '</span></td>' +
            '<td style="font-weight:600;color:var(--accent)">' + (n.importance || 0).toFixed(1) + '</td>' +
            '<td>' + (n.mention_count || 0) + '</td>' +
            '</tr>'
        ).join('') || '<tr><td colspan="4" style="color:var(--text-dim)">No graph nodes yet</td></tr>';
    } catch (e) {
        document.getElementById('v5-top-nodes-body').innerHTML =
            '<tr><td colspan="4" style="color:var(--text-dim)">Graph tables not available: ' + escapeHtml(e.message) + '</td></tr>';
    }
}

// ============================================================
// Super Memory v5 — Episodes Tab
// ============================================================
const outcomeColors = { breakthrough: 'var(--solution)', failure: 'var(--lesson)', routine: 'var(--text-dim)', discovery: 'var(--accent)', success: 'var(--solution)' };

async function loadV5Episodes() {
    try {
        const data = await api('/api/episodes');
        const stats = data.stats || {};

        // Stats badges
        const statsDiv = document.getElementById('v5-episode-stats');
        const badges = [
            { label: 'Total', value: stats.total || 0, color: 'var(--text)' },
            { label: 'Avg Impact', value: (stats.avg_impact || 0).toFixed(1), color: 'var(--accent)' },
        ];
        Object.entries(stats.by_outcome || {}).forEach(([k, v]) => {
            badges.push({ label: k, value: v, color: outcomeColors[k] || 'var(--text-dim)' });
        });
        statsDiv.innerHTML = badges.map(b =>
            '<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 20px;text-align:center">' +
            '<div style="color:var(--text-dim);font-size:11px;text-transform:uppercase;letter-spacing:0.05em">' + escapeHtml(b.label) + '</div>' +
            '<div style="font-size:24px;font-weight:700;color:' + b.color + '">' + b.value + '</div></div>'
        ).join('');

        // Episodes timeline
        const listDiv = document.getElementById('v5-episodes-list');
        listDiv.innerHTML = (data.episodes || []).map(ep => {
            const oc = ep.outcome || 'routine';
            const color = outcomeColors[oc] || 'var(--text-dim)';
            return '<div style="background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;border-left:4px solid ' + color + '">' +
                '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
                '<span style="color:' + color + ';font-weight:600;text-transform:uppercase;font-size:12px;padding:2px 8px;border:1px solid ' + color + ';border-radius:4px">' + escapeHtml(oc) + '</span>' +
                '<span style="color:var(--text-dim);font-size:12px">' + formatDate(ep.created_at) + '</span></div>' +
                '<div style="color:var(--text);font-size:14px;margin-bottom:6px">' + escapeHtml(truncate(ep.summary || ep.description || '', 200)) + '</div>' +
                '<div style="display:flex;gap:12px;color:var(--text-dim);font-size:12px">' +
                (ep.impact_score != null ? '<span>Impact: <strong style="color:var(--accent)">' + ep.impact_score + '</strong></span>' : '') +
                (ep.project ? '<span>Project: ' + escapeHtml(ep.project) + '</span>' : '') +
                '</div></div>';
        }).join('') || '<div style="color:var(--text-dim);padding:20px;text-align:center">No episodes recorded yet</div>';
    } catch (e) {
        document.getElementById('v5-episodes-list').innerHTML =
            '<div style="color:var(--text-dim);padding:20px">Episodes table not available: ' + escapeHtml(e.message) + '</div>';
    }
}

// ============================================================
// Super Memory v5 — Skills Tab
// ============================================================
let _allSkills = [];

function renderSkillCard(sk) {
    const sr = (sk.success_rate || 0);
    const srPct = (sr * 100).toFixed(0);
    const barColor = sr >= 0.8 ? 'var(--solution)' : sr >= 0.5 ? 'var(--decision)' : 'var(--lesson)';

    let stepsHtml = '';
    if (sk.steps) {
        try {
            const steps = typeof sk.steps === 'string' ? JSON.parse(sk.steps) : sk.steps;
            if (Array.isArray(steps) && steps.length > 0 && steps[0] !== 'See full definition for details') {
                stepsHtml = '<details style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">' +
                    '<summary style="color:var(--text-dim);font-size:11px;text-transform:uppercase;cursor:pointer">Steps (' + steps.length + ')</summary>' +
                    '<div style="margin-top:4px">' +
                    steps.map((s, i) => '<div style="color:var(--text);font-size:13px;padding:2px 0">' + (i+1) + '. ' + escapeHtml(s) + '</div>').join('') +
                    '</div></details>';
            }
        } catch (_) {}
    }

    let antiHtml = '';
    if (sk.anti_patterns) {
        try {
            const anti = typeof sk.anti_patterns === 'string' ? JSON.parse(sk.anti_patterns) : sk.anti_patterns;
            if (Array.isArray(anti) && anti.length > 0) {
                antiHtml = '<details style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">' +
                    '<summary style="color:var(--lesson);font-size:11px;text-transform:uppercase;cursor:pointer">Anti-patterns (' + anti.length + ')</summary>' +
                    '<div style="margin-top:4px">' +
                    anti.map(a => '<div style="color:var(--text-dim);font-size:13px;padding:2px 0">\u2718 ' + escapeHtml(a) + '</div>').join('') +
                    '</div></details>';
            }
        } catch (_) {}
    }

    const src = sk.source || '';
    const badgeColor = src === 'claude-commands' ? 'var(--solution)' : src === 'claude-agents' ? 'var(--convention)' : src === 'claude-rules' ? 'var(--decision)' : 'var(--text-dim)';
    const badgeLabel = src === 'claude-commands' ? 'Command' : src === 'claude-agents' ? 'Agent' : src === 'claude-rules' ? 'Rules' : 'Skill';

    let stackHtml = '';
    const stackList = sk.stack_list || (sk.stack ? (() => { try { return JSON.parse(sk.stack); } catch(_) { return []; } })() : []);
    if (stackList.length > 0) {
        stackHtml = '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px">' +
            stackList.map(t => '<span style="background:var(--border);color:var(--text-dim);font-size:11px;padding:2px 8px;border-radius:10px">' + escapeHtml(t) + '</span>').join('') +
            '</div>';
    }

    return '<div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
        '<div style="display:flex;align-items:center;gap:8px">' +
        '<span style="background:' + badgeColor + ';color:#000;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;text-transform:uppercase">' + badgeLabel + '</span>' +
        '<h4 style="color:var(--text);font-size:15px;margin:0">' + escapeHtml((sk.name || '').replace(/^(cmd:|agent:|rules:)/, '')) + '</h4></div>' +
        '<span style="color:var(--text-dim);font-size:12px">' + (sk.times_used ? 'Used ' + sk.times_used + 'x' : sk.status || '') + '</span></div>' +
        (sk.description ? '<div style="color:var(--text-dim);font-size:13px;margin-bottom:6px">' + escapeHtml(truncate(sk.description, 200)) + '</div>' : '') +
        (sk.trigger_pattern ? '<div style="color:var(--text-dim);font-size:12px;font-family:monospace;margin-bottom:10px;opacity:0.7">' + escapeHtml(sk.trigger_pattern) + '</div>' : '') +
        (sk.times_used > 0 ? '<div style="margin-bottom:4px;display:flex;justify-content:space-between;font-size:12px">' +
            '<span style="color:var(--text-dim)">Success Rate</span><span style="color:' + barColor + ';font-weight:600">' + srPct + '%</span></div>' +
            '<div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden">' +
            '<div style="height:100%;width:' + srPct + '%;background:' + barColor + ';border-radius:3px;transition:width 0.5s"></div></div>' : '') +
        stackHtml + stepsHtml + antiHtml + '</div>';
}

function filterAndRenderSkills() {
    const search = (document.getElementById('v5-skills-search').value || '').toLowerCase();
    const typeFilter = document.getElementById('v5-skills-filter').value;
    const listDiv = document.getElementById('v5-skills-list');

    let filtered = _allSkills;
    if (typeFilter) filtered = filtered.filter(sk => (sk.name || '').startsWith(typeFilter));
    if (search) filtered = filtered.filter(sk =>
        (sk.name || '').toLowerCase().includes(search) ||
        (sk.description || '').toLowerCase().includes(search) ||
        (sk.trigger_pattern || '').toLowerCase().includes(search)
    );

    document.getElementById('v5-skills-count').textContent = filtered.length + ' of ' + _allSkills.length;
    listDiv.innerHTML = filtered.map(renderSkillCard).join('') ||
        '<div style="color:var(--text-dim);padding:20px;text-align:center">No skills match filter</div>';
}

async function loadV5Skills() {
    try {
        const data = await api('/api/skills');
        _allSkills = data.skills || [];
        filterAndRenderSkills();

        // Attach filter listeners once
        const searchEl = document.getElementById('v5-skills-search');
        const filterEl = document.getElementById('v5-skills-filter');
        if (!searchEl._bound) {
            searchEl.addEventListener('input', filterAndRenderSkills);
            filterEl.addEventListener('change', filterAndRenderSkills);
            searchEl._bound = true;
        }
    } catch (e) {
        document.getElementById('v5-skills-list').innerHTML =
            '<div style="color:var(--text-dim);padding:20px">Skills table not available: ' + escapeHtml(e.message) + '</div>';
    }
}

// ============================================================
// Super Memory v5 — Self Model Tab
// ============================================================
async function loadV5SelfModel() {
    try {
        const data = await api('/api/self-model');

        // Competencies as horizontal bars
        const compDiv = document.getElementById('v5-competencies');
        compDiv.innerHTML = (data.competencies || []).map(c => {
            const level = c.level || 0;
            const pct = Math.min(100, level * 100).toFixed(0);
            const color = level >= 0.85 ? 'var(--solution)' : level >= 0.7 ? 'var(--accent)' : level >= 0.5 ? 'var(--decision)' : 'var(--lesson)';
            const trend = c.trend === 'improving' ? ' ↑' : c.trend === 'stable_low' ? ' ⚠' : '';
            return '<div style="margin-bottom:12px">' +
                '<div style="display:flex;justify-content:space-between;margin-bottom:4px">' +
                '<span style="color:var(--text);font-size:14px">' + escapeHtml(c.domain || c.name || 'Unknown') + '</span>' +
                '<span style="color:' + color + ';font-weight:600;font-size:14px">' + pct + '%' + trend + '</span></div>' +
                '<div style="height:8px;background:var(--border);border-radius:4px;overflow:hidden">' +
                '<div style="height:100%;width:' + pct + '%;background:' + color + ';border-radius:4px;transition:width 0.5s"></div></div>' +
                (c.based_on ? '<div style="color:var(--text-dim);font-size:11px;margin-top:2px">' + c.based_on + ' evidence · ' + (c.trend || '') + '</div>' : '') +
                '</div>';
        }).join('') || '<div style="color:var(--text-dim)">No competency data yet</div>';

        // Blind spots
        const bsDiv = document.getElementById('v5-blind-spots');
        bsDiv.innerHTML = (data.blind_spots || []).map(bs => {
            const isMonitoring = bs.status === 'monitoring';
            const borderColor = isMonitoring ? 'var(--accent)' : 'var(--lesson)';
            const statusLabel = isMonitoring ? '📚 Learning' : '⚠️ Unknown area';
            const statusColor = isMonitoring ? 'var(--accent)' : 'var(--lesson)';
            return '<div style="background:var(--card);border:1px solid var(--border);border-left:3px solid ' + borderColor + ';border-radius:8px;padding:12px;margin-bottom:8px">' +
            '<div style="color:' + statusColor + ';font-weight:600;font-size:13px;margin-bottom:4px">' + statusLabel + '</div>' +
            '<div style="color:var(--text);font-size:13px">' + escapeHtml(bs.description || '') + '</div>' +
            '</div>';
        }).join('') || '<div style="color:var(--text-dim)">No blind spots detected</div>';

        // User model
        const umDiv = document.getElementById('v5-user-model');
        const entries = Object.entries(data.user_model || {});
        umDiv.innerHTML = entries.map(([key, val]) =>
            '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">' +
            '<span style="color:var(--text);font-size:13px">' + escapeHtml(key) + '</span>' +
            '<div style="display:flex;align-items:center;gap:8px">' +
            '<span style="color:var(--accent);font-size:13px">' + escapeHtml(String(val.value || '')) + '</span>' +
            '<span style="color:var(--text-dim);font-size:11px">(' + ((val.confidence || 0) * 100).toFixed(0) + '%)</span></div></div>'
        ).join('') || '<div style="color:var(--text-dim)">No user model data yet</div>';
    } catch (e) {
        document.getElementById('v5-competencies').innerHTML =
            '<div style="color:var(--text-dim)">Self model tables not available: ' + escapeHtml(e.message) + '</div>';
    }
}

// ============================================================
// Super Memory v5 — Reflection Tab
// ============================================================
async function loadV5Reflection() {
    try {
        const data = await api('/api/reflection');

        // Reports
        const reportsDiv = document.getElementById('v5-reports');
        reportsDiv.innerHTML = (data.reports || []).map(r =>
            '<div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px">' +
            '<div style="display:flex;justify-content:space-between;margin-bottom:8px">' +
            '<span style="color:var(--rule-color);font-weight:600;font-size:14px">' + escapeHtml(r.title || r.type || 'Report') + '</span>' +
            '<span style="color:var(--text-dim);font-size:12px">' + formatDate(r.created_at) + '</span></div>' +
            '<div style="color:var(--text);font-size:14px;white-space:pre-wrap">' + escapeHtml(truncate(r.content || r.summary || '', 500)) + '</div>' +
            (r.recommendations ? '<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);color:var(--text-dim);font-size:13px">' +
            '<strong>Recommendations:</strong> ' + escapeHtml(truncate(r.recommendations, 200)) + '</div>' : '') +
            '</div>'
        ).join('') || '<div style="color:var(--text-dim);padding:20px;text-align:center">No reflection reports yet</div>';

        // Proposals
        const proposalsDiv = document.getElementById('v5-proposals');
        proposalsDiv.innerHTML = (data.proposals || []).map(p =>
            '<div style="background:var(--card);border:1px solid var(--border);border-left:3px solid var(--decision);border-radius:8px;padding:16px">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
            '<span style="color:var(--decision);font-weight:600;font-size:13px">' + escapeHtml(p.type || 'Proposal') + '</span>' +
            '<span style="color:var(--text-dim);font-size:12px">' + formatDate(p.created_at) + '</span></div>' +
            '<div style="color:var(--text);font-size:14px;margin-bottom:8px">' + escapeHtml(truncate(p.content || p.description || '', 300)) + '</div>' +
            '<div style="display:flex;gap:8px">' +
            '<span style="color:var(--text-dim);font-size:12px;padding:4px 12px;border:1px solid var(--border);border-radius:4px">Status: ' + escapeHtml(p.status || 'pending') + '</span>' +
            '</div></div>'
        ).join('') || '<div style="color:var(--text-dim)">No pending proposals</div>';
    } catch (e) {
        document.getElementById('v5-reports').innerHTML =
            '<div style="color:var(--text-dim)">Reflection tables not available: ' + escapeHtml(e.message) + '</div>';
    }
}

</script>
</body>
</html>
"""


GRAPH_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Knowledge Graph — Claude Memory</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0f172a; color: #e2e8f0; height: 100vh; overflow: hidden;
    display: flex; flex-direction: column;
}
.toolbar {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 20px; background: #1e293b; border-bottom: 1px solid #334155;
    flex-shrink: 0; z-index: 10;
}
.toolbar h1 { font-size: 16px; font-weight: 600; white-space: nowrap; }
.toolbar a { color: #3b82f6; text-decoration: none; font-size: 13px; }
.toolbar a:hover { text-decoration: underline; }
.toolbar select, .toolbar input {
    background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
    border-radius: 6px; padding: 5px 10px; font-size: 13px;
}
.toolbar label { font-size: 13px; color: #94a3b8; }
#graph-container { width: 100%; height: calc(100vh - 80px); min-height: 600px; border: 1px solid #334155; }
#detail-panel {
    position: absolute; top: 0; right: 0; width: 380px; height: 100%;
    background: #1e293bee; border-left: 1px solid #334155;
    overflow-y: auto; padding: 20px; display: none; z-index: 5;
}
#detail-panel.open { display: block; }
#detail-panel h2 { font-size: 15px; margin-bottom: 8px; color: #3b82f6; }
#detail-panel .close-btn {
    position: absolute; top: 10px; right: 14px; cursor: pointer;
    color: #94a3b8; font-size: 20px; background: none; border: none;
}
#detail-panel .close-btn:hover { color: #e2e8f0; }
.detail-section { margin-bottom: 14px; }
.detail-section h3 { font-size: 13px; color: #94a3b8; margin-bottom: 4px; text-transform: uppercase; }
.detail-section p, .detail-section li { font-size: 13px; line-height: 1.5; }
.detail-section ul { list-style: none; padding: 0; }
.detail-section li { padding: 3px 0; border-bottom: 1px solid #33415544; }
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600; margin-right: 4px;
}
.stats-bar {
    display: flex; gap: 16px; font-size: 12px; color: #94a3b8; margin-left: auto;
}
.stats-bar span { white-space: nowrap; }
#loading {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    font-size: 16px; color: #94a3b8;
}
.legend {
    position: absolute; bottom: 12px; left: 12px; background: #1e293bee;
    border: 1px solid #334155; border-radius: 8px; padding: 10px 14px;
    font-size: 11px; z-index: 5; display: flex; flex-wrap: wrap; gap: 8px;
}
.legend-item { display: flex; align-items: center; gap: 4px; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; }
</style>
</head>
<body>

<div class="toolbar">
    <h1>Knowledge Graph</h1>
    <a href="/">&larr; Dashboard</a>
    <label>Limit:
        <select id="node-limit" onchange="loadGraph()">
            <option value="50">50</option>
            <option value="100" selected>100</option>
            <option value="200">200</option>
            <option value="500">500</option>
        </select>
    </label>
    <label>Filter type:
        <select id="type-filter" onchange="filterGraph()">
            <option value="">All</option>
        </select>
    </label>
    <label>Search: <input id="search-input" type="text" placeholder="node name..." onkeyup="searchNode()" /></label>
    <div class="stats-bar">
        <span id="stat-nodes">Nodes: ...</span>
        <span id="stat-edges">Edges: ...</span>
    </div>
</div>

<div id="loading">Loading graph data...</div>
<div id="graph-container"></div>

<div class="legend" id="legend"></div>

<div id="detail-panel">
    <button class="close-btn" onclick="closeDetail()">&times;</button>
    <div id="detail-content"></div>
</div>

<script>
const TYPE_COLORS = {
    concept:    '#3b82f6',
    technology: '#22c55e',
    project:    '#f97316',
    person:     '#a855f7',
    company:    '#ec4899',
    pattern:    '#06b6d4',
    skill:      '#14b8a6',
    fact:       '#eab308',
    solution:   '#84cc16',
    decision:   '#f59e0b',
    lesson:     '#ef4444',
    convention: '#8b5cf6',
    rule:       '#6366f1',
    episode:    '#f43f5e',
    repo:       '#64748b',
    article:    '#94a3b8',
    doc:        '#78716c',
    preference: '#d946ef',
    competency: '#2dd4bf',
    blindspot:  '#fb7185',
    prohibition:'#dc2626',
    procedure:  '#0ea5e9',
};
const DEFAULT_COLOR = '#64748b';

let network = null;
let allNodes = [];
let allEdges = [];
let nodesDataset, edgesDataset;

function getColor(type) { return TYPE_COLORS[type] || DEFAULT_COLOR; }

function buildLegend(types) {
    const el = document.getElementById('legend');
    el.innerHTML = types.map(t =>
        `<div class="legend-item"><div class="legend-dot" style="background:${getColor(t)}"></div>${t}</div>`
    ).join('');
}

async function loadGraph() {
    const limit = document.getElementById('node-limit').value;
    document.getElementById('loading').style.display = 'block';

    const resp = await fetch(`/api/graph-visual?limit=${limit}`);
    const data = await resp.json();

    allNodes = data.nodes || [];
    allEdges = data.edges || [];

    const types = [...new Set(allNodes.map(n => n.type))].sort();
    const filterSel = document.getElementById('type-filter');
    const curVal = filterSel.value;
    filterSel.innerHTML = '<option value="">All</option>' +
        types.map(t => `<option value="${t}">${t} (${allNodes.filter(n=>n.type===t).length})</option>`).join('');
    filterSel.value = curVal;

    buildLegend(types);
    renderGraph();
    document.getElementById('loading').style.display = 'none';
}

function renderGraph(filterType) {
    let nodes = allNodes;
    if (filterType) nodes = nodes.filter(n => n.type === filterType);
    const nodeIds = new Set(nodes.map(n => n.id));

    const visNodes = nodes.map(n => ({
        id: n.id,
        label: n.name.length > 30 ? n.name.slice(0, 28) + '..' : n.name,
        title: `${n.name}\nType: ${n.type}\nImportance: ${(n.importance || 0).toFixed(2)}\nMentions: ${n.mention_count || 0}`,
        color: { background: getColor(n.type), border: getColor(n.type), highlight: { background: '#fff', border: getColor(n.type) } },
        font: { color: '#e2e8f0', size: Math.max(10, Math.min(16, 8 + (n.importance || 0.5) * 14)) },
        size: Math.max(8, Math.min(30, 5 + (n.importance || 0.5) * 40)),
        _data: n,
    }));

    const visEdges = allEdges
        .filter(e => nodeIds.has(e.source_id) && nodeIds.has(e.target_id))
        .map(e => ({
            from: e.source_id,
            to: e.target_id,
            label: e.relation_type.replace(/_/g, ' '),
            title: `${e.relation_type} (weight: ${(e.weight || 1).toFixed(1)})${e.context ? '\n' + e.context : ''}`,
            arrows: 'to',
            color: { color: '#475569', highlight: '#3b82f6', opacity: 0.6 },
            font: { color: '#64748b', size: 9, strokeWidth: 0 },
            width: Math.max(0.5, Math.min(3, (e.weight || 1))),
            smooth: { type: 'continuous' },
        }));

    document.getElementById('stat-nodes').textContent = `Nodes: ${visNodes.length}`;
    document.getElementById('stat-edges').textContent = `Edges: ${visEdges.length}`;

    nodesDataset = new vis.DataSet(visNodes);
    edgesDataset = new vis.DataSet(visEdges);

    const container = document.getElementById('graph-container');
    const options = {
        physics: {
            solver: 'forceAtlas2Based',
            forceAtlas2Based: { gravitationalConstant: -80, centralGravity: 0.01, springLength: 120, springConstant: 0.04 },
            stabilization: { iterations: 80, updateInterval: 25 },
        },
        interaction: { hover: true, tooltipDelay: 200, zoomView: true, dragView: true },
        edges: { smooth: { type: 'continuous' } },
    };

    if (network) network.destroy();
    container.style.height = (window.innerHeight - 80) + 'px';
    network = new vis.Network(container, { nodes: nodesDataset, edges: edgesDataset }, options);

    network.once('stabilizationIterationsDone', () => {
        network.setOptions({ physics: false });
        network.fit({ animation: { duration: 500 } });
    });

    network.on('click', async (params) => {
        if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            await showDetail(nodeId);
        } else {
            closeDetail();
        }
    });
}

function filterGraph() {
    const filterType = document.getElementById('type-filter').value;
    renderGraph(filterType || undefined);
}

function searchNode() {
    const q = document.getElementById('search-input').value.toLowerCase();
    if (!q || !network) return;
    const found = allNodes.find(n => n.name.toLowerCase().includes(q));
    if (found) {
        network.selectNodes([found.id]);
        network.focus(found.id, { scale: 1.5, animation: true });
    }
}

async function showDetail(nodeId) {
    const resp = await fetch(`/api/graph-node/${encodeURIComponent(nodeId)}`);
    if (!resp.ok) { closeDetail(); return; }
    const data = await resp.json();
    const n = data.node;

    let html = `<h2>${escHtml(n.name)}</h2>`;
    html += `<div class="detail-section">
        <span class="badge" style="background:${getColor(n.type)}44;color:${getColor(n.type)}">${n.type}</span>
        <span class="badge" style="background:#33415588;color:#94a3b8">importance: ${(n.importance||0).toFixed(2)}</span>
        <span class="badge" style="background:#33415588;color:#94a3b8">mentions: ${n.mention_count||0}</span>
    </div>`;

    if (n.content) {
        html += `<div class="detail-section"><h3>Content</h3><p>${escHtml(n.content).slice(0, 500)}</p></div>`;
    }

    if (data.outgoing && data.outgoing.length) {
        html += `<div class="detail-section"><h3>Outgoing (${data.outgoing.length})</h3><ul>`;
        data.outgoing.forEach(e => {
            html += `<li><span style="color:${getColor(e.type)}">${escHtml(e.name)}</span> <span style="color:#64748b;font-size:11px">[${e.relation_type}]</span></li>`;
        });
        html += `</ul></div>`;
    }

    if (data.incoming && data.incoming.length) {
        html += `<div class="detail-section"><h3>Incoming (${data.incoming.length})</h3><ul>`;
        data.incoming.forEach(e => {
            html += `<li><span style="color:${getColor(e.type)}">${escHtml(e.name)}</span> <span style="color:#64748b;font-size:11px">[${e.relation_type}]</span></li>`;
        });
        html += `</ul></div>`;
    }

    if (n.properties) {
        try {
            const props = typeof n.properties === 'string' ? JSON.parse(n.properties) : n.properties;
            if (Object.keys(props).length) {
                html += `<div class="detail-section"><h3>Properties</h3><ul>`;
                Object.entries(props).forEach(([k, v]) => {
                    html += `<li><strong>${escHtml(k)}:</strong> ${escHtml(String(v))}</li>`;
                });
                html += `</ul></div>`;
            }
        } catch(e) {}
    }

    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-panel').classList.add('open');
}

function closeDetail() {
    document.getElementById('detail-panel').classList.remove('open');
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

loadGraph();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# Citation HTML views — /knowledge/{id}, /session/{id}
# Both pages fetch the matching /api/... JSON in-browser and render it as
# readable markdown-ish blocks. Kept intentionally simple (no framework).
# ─────────────────────────────────────────────


_KNOWLEDGE_VIEW_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Knowledge __KID__ — Claude Memory</title>
<style>
  body { margin:0; background:#0a0a14; color:#ddd;
         font-family:-apple-system,Segoe UI,sans-serif; }
  .wrap { max-width:860px; margin:40px auto; padding:0 20px; }
  h1 { font-size:18px; color:#8ad; margin:0 0 4px; }
  .meta { color:#888; font-size:12px; margin-bottom:20px; }
  .meta b { color:#aaa; }
  pre { background:#111; padding:14px; border:1px solid #222;
        border-radius:6px; white-space:pre-wrap; font-size:13px;
        line-height:1.5; color:#d0d0d0; }
  .section { margin-top:24px; }
  .section h2 { font-size:13px; color:#8ad; text-transform:uppercase;
                letter-spacing:.05em; margin:0 0 8px; }
  .tag { display:inline-block; background:#1a2a3a; color:#8ad;
         padding:2px 8px; margin:2px; border-radius:12px; font-size:11px; }
  .related a { color:#8ad; text-decoration:none; }
  .related li { margin:6px 0; }
  .via { color:#666; font-size:11px; margin-left:6px; }
  .err { color:#f55; padding:20px; }
  a.back { color:#888; font-size:12px; text-decoration:none; }
  a.back:hover { color:#8ad; }
</style>
</head>
<body>
<div class="wrap">
  <a href="/" class="back">← Dashboard</a>
  <div id="body"><p style="color:#888">Loading knowledge __KID__…</p></div>
</div>
<script>
const KID = "__KID__";
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
fetch("/api/knowledge/" + KID)
  .then(r => r.json().then(j => ({ok: r.ok, data: j})))
  .then(({ok, data}) => {
    const b = document.getElementById("body");
    if (!ok) {
      b.innerHTML = '<div class="err">Error: ' + esc(data.message || data.error) + '</div>';
      return;
    }
    const tags = (data.tags || []).map(t =>
      '<span class="tag">' + esc(t) + '</span>').join("");
    const related = (data.related || []).map(r =>
      '<li class="related"><a href="/knowledge/' + r.id + '">#' + r.id +
      ' ' + esc(r.title) + '</a><span class="via">via ' + esc(r.via) + '</span></li>'
    ).join("") || '<li style="color:#666">No related records</li>';
    b.innerHTML =
      '<h1>Knowledge #' + esc(data.id) + ' — ' + esc(data.type) + '</h1>' +
      '<div class="meta">' +
        '<b>Project:</b> ' + esc(data.project || "–") + ' · ' +
        '<b>Session:</b> ' + esc(data.session_id || "–") + ' · ' +
        '<b>Created:</b> ' + esc(data.created_at || "–") +
      '</div>' +
      '<div class="section"><h2>Content</h2><pre>' + esc(data.content) + '</pre></div>' +
      (data.context ? '<div class="section"><h2>Context</h2><pre>' +
        esc(data.context) + '</pre></div>' : "") +
      (tags ? '<div class="section"><h2>Tags</h2>' + tags + '</div>' : "") +
      '<div class="section"><h2>Related</h2><ul>' + related + '</ul></div>';
  })
  .catch(e => {
    document.getElementById("body").innerHTML =
      '<div class="err">Fetch failed: ' + esc(e.message) + '</div>';
  });
</script>
</body>
</html>
"""


_SESSION_VIEW_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Session __SID__ — Claude Memory</title>
<style>
  body { margin:0; background:#0a0a14; color:#ddd;
         font-family:-apple-system,Segoe UI,sans-serif; }
  .wrap { max-width:860px; margin:40px auto; padding:0 20px; }
  h1 { font-size:18px; color:#8ad; margin:0 0 4px; }
  .meta { color:#888; font-size:12px; margin-bottom:20px; }
  pre { background:#111; padding:14px; border:1px solid #222;
        border-radius:6px; white-space:pre-wrap; font-size:13px;
        line-height:1.5; color:#d0d0d0; }
  .section { margin-top:24px; }
  .section h2 { font-size:13px; color:#8ad; text-transform:uppercase;
                letter-spacing:.05em; margin:0 0 8px; }
  li { margin:4px 0; color:#ccc; }
  .k-link { color:#8ad; text-decoration:none; }
  .k-link:hover { text-decoration:underline; }
  .err { color:#f55; padding:20px; }
  a.back { color:#888; font-size:12px; text-decoration:none; }
  a.back:hover { color:#8ad; }
</style>
</head>
<body>
<div class="wrap">
  <a href="/" class="back">← Dashboard</a>
  <div id="body"><p style="color:#888">Loading session…</p></div>
</div>
<script>
const SID = "__SID__";
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
fetch("/api/session/" + encodeURIComponent(SID))
  .then(r => r.json().then(j => ({ok: r.ok, data: j})))
  .then(({ok, data}) => {
    const b = document.getElementById("body");
    if (!ok) {
      b.innerHTML = '<div class="err">Error: ' + esc(data.message || data.error) + '</div>';
      return;
    }
    const list = arr => (arr && arr.length)
      ? '<ul>' + arr.map(x => '<li>' + esc(x) + '</li>').join("") + '</ul>'
      : '<p style="color:#666">–</p>';
    const know = (data.knowledge || []).map(k =>
      '<li><a class="k-link" href="/knowledge/' + k.id + '">#' + k.id + ' ' +
      esc(k.type) + '</a> — ' + esc(k.title) + '</li>').join("")
      || '<li style="color:#666">No knowledge linked</li>';
    b.innerHTML =
      '<h1>Session ' + esc(data.session_id) + '</h1>' +
      '<div class="meta"><b>Created:</b> ' + esc(data.created_at || "–") + '</div>' +
      '<div class="section"><h2>Summary</h2><pre>' + esc(data.summary || "") + '</pre></div>' +
      '<div class="section"><h2>Next steps</h2>' + list(data.next_steps) + '</div>' +
      '<div class="section"><h2>Pitfalls</h2>' + list(data.pitfalls) + '</div>' +
      '<div class="section"><h2>Knowledge</h2><ul>' + know + '</ul></div>';
  })
  .catch(e => {
    document.getElementById("body").innerHTML =
      '<div class="err">Fetch failed: ' + esc(e.message) + '</div>';
  });
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the memory dashboard."""

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default access log noise; print only errors."""
        pass

    def _send_json(self, data: object, status: int = 200) -> None:
        """Send a JSON response."""
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        """Send an HTML response, injecting v6 panels when the marker is present."""
        if "<!-- V6_PANELS_HERE -->" in html:
            try:
                from dashboard_v6 import V6_PANELS_HTML
                html = html.replace("<!-- V6_PANELS_HERE -->", V6_PANELS_HTML)
            except Exception:
                # Marker left in place if import fails — harmless
                pass
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        """Send an error JSON response."""
        self._send_json({"error": message}, status)

    def _send_error_citation(self, status: int, code: str, message: str) -> None:
        """Citation-API error: {"error": "<code>", "message": "<details>"}.

        Used by the /api/knowledge/{id} and /api/session/{id} endpoints to
        give IDE integrations a stable machine-parseable error shape.
        """
        self._send_json({"error": code, "message": message}, status)

    def _get_db(self) -> sqlite3.Connection | None:
        """Open a read-only DB connection. Returns None if DB is missing."""
        return get_db()

    def _handle_sse(self) -> None:
        """Server-Sent Events endpoint: polls DB every 2s for new records."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        db = get_db()
        if not db:
            self.wfile.write(b"event: error\ndata: {\"error\": \"no database\"}\n\n")
            self.wfile.flush()
            return

        try:
            # Get current max IDs as watermarks
            last_k = db.execute("SELECT MAX(id) FROM knowledge").fetchone()[0] or 0
            last_e = 0
            last_o = 0
            try:
                last_e = db.execute("SELECT MAX(id) FROM errors").fetchone()[0] or 0
            except Exception:
                pass
            try:
                last_o = db.execute("SELECT MAX(id) FROM observations").fetchone()[0] or 0
            except Exception:
                pass

            while True:
                time.sleep(2)
                # Heartbeat
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break

                try:
                    # New knowledge
                    rows = db.execute(
                        "SELECT id, type, project, substr(content,1,120) as summary, created_at "
                        "FROM knowledge WHERE id > ? AND status='active' ORDER BY id",
                        (last_k,)
                    ).fetchall()
                    for r in rows:
                        d = dict(r)
                        last_k = d["id"]
                        payload = json.dumps(d, default=str)
                        self.wfile.write(f"event: knowledge\ndata: {payload}\n\n".encode())

                    # New errors
                    try:
                        rows = db.execute(
                            "SELECT id, category, severity, substr(description,1,120) as summary, created_at "
                            "FROM errors WHERE id > ? ORDER BY id",
                            (last_e,)
                        ).fetchall()
                        for r in rows:
                            d = dict(r)
                            last_e = d["id"]
                            payload = json.dumps(d, default=str)
                            self.wfile.write(f"event: error_log\ndata: {payload}\n\n".encode())
                    except Exception:
                        pass

                    # New observations
                    try:
                        rows = db.execute(
                            "SELECT id, tool_name, observation_type, summary, created_at "
                            "FROM observations WHERE id > ? ORDER BY id",
                            (last_o,)
                        ).fetchall()
                        for r in rows:
                            d = dict(r)
                            last_o = d["id"]
                            payload = json.dumps(d, default=str)
                            self.wfile.write(f"event: observation\ndata: {payload}\n\n".encode())
                    except Exception:
                        pass

                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                except Exception:
                    pass
        finally:
            db.close()

    def do_GET(self) -> None:
        """Route GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # Helper to get single param values
        def p(key: str, default: str = "") -> str:
            vals = params.get(key, [default])
            return vals[0] if vals else default

        # --- Main page ---
        if path in ("", "/"):
            self._send_html(HTML_PAGE)
            return

        # --- Graph visualization page ---
        if path == "/graph":
            self._send_html(GRAPH_PAGE)
            return

        # --- Live graph visualization (v6) ---
        if path == "/graph/live":
            try:
                from dashboard_v6 import GRAPH_LIVE_HTML
                self._send_html(GRAPH_LIVE_HTML)
            except Exception as e:
                self._send_error(500, f"graph live error: {e}")
            return
        if path == "/graph/hive":
            try:
                from dashboard_v6 import GRAPH_HIVE_HTML
                self._send_html(GRAPH_HIVE_HTML)
            except Exception as e:
                self._send_error(500, f"graph hive error: {e}")
            return
        if path == "/graph/matrix":
            try:
                from dashboard_v6 import GRAPH_MATRIX_HTML
                self._send_html(GRAPH_MATRIX_HTML)
            except Exception as e:
                self._send_error(500, f"graph matrix error: {e}")
            return

        # --- Citation HTML view for /knowledge/{id} ---
        # Renders a minimal dark-theme page that consumes the same JSON as
        # /api/knowledge/{id}. Used when pasting a URL into docs / chat.
        if path.startswith("/knowledge/"):
            parts = path.split("/")
            if len(parts) == 3 and parts[2].isdigit():
                self._send_html(_KNOWLEDGE_VIEW_HTML.replace("__KID__", parts[2]))
                return
            self._send_error(404, "not_found")
            return

        # --- Citation HTML view for /session/{id} ---
        if path.startswith("/session/"):
            parts = path.split("/")
            if len(parts) == 3 and parts[2]:
                sid_html = parts[2].replace('"', "").replace("<", "").replace(">", "")
                self._send_html(_SESSION_VIEW_HTML.replace("__SID__", sid_html))
                return
            self._send_error(404, "not_found")
            return

        # --- System status (no DB required) ---
        if path == "/status":
            self._send_json(api_system_status())
            return

        # --- SSE endpoint (no DB close, long-lived) ---
        if path == "/api/events":
            self._handle_sse()
            return

        # --- API routes require DB ---
        db = self._get_db()
        if db is None:
            self._send_error(503, "Database not found at " + str(DB_PATH))
            return

        try:
            if path == "/api/stats":
                self._send_json(api_stats(db))

            # v6.0 endpoints (savings, queues, coverage, graph delta)
            elif path == "/api/v6/savings":
                from dashboard_v6 import api_v6_savings
                self._send_json(api_v6_savings(db))
            elif path == "/api/v6/queues":
                from dashboard_v6 import api_v6_queues
                self._send_json(api_v6_queues(db))
            elif path == "/api/v6/coverage":
                from dashboard_v6 import api_v6_coverage
                self._send_json(api_v6_coverage(db))
            # v10.1 — async enrichment worker health
            elif path == "/api/v10/enrichment-queue":
                from dashboard_v6 import api_v10_enrichment_queue
                self._send_json(api_v10_enrichment_queue(db))
            elif path == "/api/graph/delta":
                from dashboard_v6 import api_graph_delta
                since = p("since") or None
                limit_n = min(5000, max(1, int(p("limit", "200"))))
                offset_n = max(0, int(p("offset", "0")))
                min_m = max(0, int(p("min_mentions", "0")))
                min_w = max(0.0, float(p("min_edge_weight", "0")))
                self._send_json(api_graph_delta(db, since, limit_n, offset_n, min_m, min_w))
            elif path == "/api/graph/by_type":
                from dashboard_v6 import api_graph_by_type
                self._send_json(api_graph_by_type(
                    db,
                    max(0, int(p("min_mentions", "3"))),
                    min(200, max(10, int(p("limit_per_type", "80")))),
                ))
            elif path == "/api/graph/matrix":
                from dashboard_v6 import api_graph_matrix
                self._send_json(api_graph_matrix(
                    db,
                    max(1, int(p("min_mentions", "5"))),
                    min(500, max(20, int(p("limit", "200")))),
                ))

            elif path == "/api/knowledge":
                search = p("q") or None
                ktype = p("type") or None
                project = p("project") or None
                page = max(1, int(p("page", "1")))
                limit = min(200, max(1, int(p("limit", "50"))))
                self._send_json(api_knowledge(db, search, ktype, project, page, limit))

            elif path.startswith("/api/knowledge/"):
                # Extract ID from path — citation API: stable JSON shape with
                # related edges / multi-repr peers for IDE linking.
                parts = path.split("/")
                if len(parts) == 4 and parts[3].isdigit():
                    kid = int(parts[3])
                    result = api_knowledge_citation(db, kid)
                    if result:
                        self._send_json(result)
                    else:
                        self._send_error_citation(
                            404, "not_found",
                            f"knowledge record {kid} not found",
                        )
                else:
                    self._send_error_citation(
                        400, "bad_request", "invalid knowledge id",
                    )

            elif path.startswith("/api/session/") and path != "/api/sessions":
                # /api/session/{id} — citation payload (summary + next_steps + pitfalls)
                parts = path.split("/")
                if len(parts) == 4 and parts[3]:
                    from urllib.parse import unquote
                    sid = unquote(parts[3])
                    result = api_session_citation(db, sid)
                    if result:
                        self._send_json(result)
                    else:
                        self._send_error_citation(
                            404, "not_found",
                            f"session {sid} not found",
                        )
                else:
                    self._send_error_citation(
                        400, "bad_request", "invalid session id",
                    )

            elif path == "/api/sessions":
                limit = min(200, max(1, int(p("limit", "20"))))
                self._send_json(api_sessions(db, limit))

            elif path == "/api/graph":
                limit = min(2000, max(50, int(p("limit", "1600"))))
                self._send_json(api_graph(db, limit))

            elif path == "/api/errors":
                category = p("category") or None
                project = p("project") or None
                page = max(1, int(p("page", "1")))
                limit = min(200, max(1, int(p("limit", "50"))))
                self._send_json(api_errors(db, category, project, page, limit))

            elif path == "/api/insights":
                project = p("project") or None
                self._send_json(api_insights(db, project))

            elif path == "/api/rules":
                project = p("project") or None
                self._send_json(api_rules(db, project))

            elif path == "/api/self-improvement":
                self._send_json(api_self_improvement(db))

            elif path == "/api/observations":
                project = p("project") or None
                page = max(1, int(p("page", "1")))
                limit = min(200, max(1, int(p("limit", "50"))))
                self._send_json(api_observations(db, project, page, limit))

            elif path == "/api/branches":
                self._send_json(api_branches(db))

            elif path == "/api/graph-stats":
                self._send_json(api_graph_stats(db))

            elif path == "/api/episodes":
                self._send_json(api_episodes(db))

            elif path == "/api/skills":
                self._send_json(api_skills(db))

            elif path == "/api/self-model":
                self._send_json(api_self_model(db))

            elif path == "/api/reflection":
                self._send_json(api_reflection(db))

            elif path == "/api/graph-visual":
                limit = min(500, max(10, int(p("limit", "100"))))
                self._send_json(api_graph_visual(db, limit))

            elif path.startswith("/api/graph-node/"):
                from urllib.parse import unquote
                node_id = unquote(path[len("/api/graph-node/"):])
                if node_id:
                    result = api_graph_node_detail(db, node_id)
                    if result:
                        self._send_json(result)
                    else:
                        self._send_error(404, "Graph node not found")
                else:
                    self._send_error(400, "Missing node ID")

            else:
                self._send_error(404, "Not found")
        except Exception as e:
            self._send_error(500, str(e))
        finally:
            db.close()


def main() -> None:
    """Start the dashboard HTTP server."""
    print(f"Memory dir: {MEMORY_DIR}")
    print(f"Database:   {DB_PATH} ({'exists' if DB_PATH.exists() else 'NOT FOUND'})")
    print(f"Dashboard running at http://localhost:{DASHBOARD_PORT}")

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        allow_reuse_port = True

        def server_bind(self) -> None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            super().server_bind()

    # Bind to loopback by default; opt-in LAN via DASHBOARD_BIND.
    # Never expose DB / graph / filter contents to any non-127.0.0.1 host.
    bind_addr = os.environ.get("DASHBOARD_BIND", "127.0.0.1").strip() or "127.0.0.1"
    server = ThreadingHTTPServer((bind_addr, DASHBOARD_PORT), DashboardHandler)
    print(f"Dashboard at http://{bind_addr}:{DASHBOARD_PORT}  "
          f"(DASHBOARD_BIND to override)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard.")
        server.server_close()


if __name__ == "__main__":
    main()
