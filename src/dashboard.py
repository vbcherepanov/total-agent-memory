#!/usr/bin/env python3
"""
Claude Total Memory — Web Dashboard

Standalone HTTP server using only Python stdlib.
Provides a read-only web interface for browsing memory data.

Usage:
    python src/dashboard.py

Environment:
    DASHBOARD_PORT      — HTTP port (default: 37737)
    CLAUDE_MEMORY_DIR   — Path to memory storage (default: ~/.claude-memory)
"""

import json
import os
import sqlite3
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "37737"))
MEMORY_DIR = Path(os.environ.get("CLAUDE_MEMORY_DIR", os.path.expanduser("~/.claude-memory")))
DB_PATH = MEMORY_DIR / "memory.db"


def get_db():
    """Open a read-only SQLite connection with WAL mode."""
    if not DB_PATH.exists():
        return None
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def q(db, sql, params=()):
    """Execute a query and return a list of dicts."""
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    except Exception:
        return []


def q1(db, sql, params=()):
    """Execute a query and return a single dict or None."""
    try:
        r = db.execute(sql, params).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def api_stats(db):
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

    return {
        "total_knowledge": total_knowledge,
        "by_type": by_type,
        "by_project": by_project,
        "sessions_count": sessions_count,
        "health_score": health_score,
        "storage_mb": storage_mb,
        "stale_90d": stale,
        "never_recalled": never_recalled,
    }


def api_knowledge(db, search=None, ktype=None, project=None, page=1, limit=50):
    """Paginated knowledge listing with optional filters."""
    conds = ["status='active'"]
    params = []

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


def api_knowledge_detail(db, kid):
    """Single knowledge record with full content and version history."""
    record = q1(db, "SELECT * FROM knowledge WHERE id=?", (kid,))
    if not record:
        return None

    if isinstance(record.get("tags"), str):
        try:
            record["tags"] = json.loads(record["tags"])
        except Exception:
            record["tags"] = []

    history = []
    predecessors = q(
        db,
        "SELECT id, content, created_at, status FROM knowledge WHERE superseded_by=?",
        (kid,),
    )
    for p in predecessors:
        history.append({**p, "relation": "superseded_by_this"})

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


def api_sessions(db, limit=20):
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


def api_graph(db):
    """Nodes and edges for the graph visualization."""
    nodes = q(
        db,
        """SELECT id, type, project, substr(content, 1, 80) as label,
                  recall_count, confidence
           FROM knowledge
           WHERE status='active'
           ORDER BY recall_count DESC, created_at DESC
           LIMIT 200""",
    )

    node_ids = {n["id"] for n in nodes}

    edges_raw = q(db, "SELECT from_id, to_id, type FROM relations")
    edges = [
        e for e in edges_raw
        if e["from_id"] in node_ids and e["to_id"] in node_ids
    ]

    return {"nodes": nodes, "edges": edges}


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
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    min-height: 100vh;
}

.container { max-width: 1400px; margin: 0 auto; padding: 20px; }

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

#graph-canvas {
    width: 100%;
    height: 600px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    cursor: grab;
}
#graph-canvas:active { cursor: grabbing; }
#graph-tooltip {
    display: none;
    position: absolute;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    max-width: 300px;
    pointer-events: none;
    z-index: 500;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
}
#graph-tooltip .tt-type { font-size: 11px; text-transform: uppercase; margin-bottom: 4px; }
#graph-tooltip .tt-content { line-height: 1.5; }

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
            <div class="subtitle">Read-only dashboard &mdash; memory.db</div>
        </div>
    </header>

    <div id="error-container"></div>

    <div class="stats-row" id="stats-row">
        <div class="stat-card"><div class="label">Total Knowledge</div><div class="value accent" id="stat-total">--</div></div>
        <div class="stat-card"><div class="label">Sessions</div><div class="value" id="stat-sessions">--</div></div>
        <div class="stat-card"><div class="label">Projects</div><div class="value" id="stat-projects">--</div></div>
        <div class="stat-card"><div class="label">Health Score</div><div class="value green" id="stat-health">--</div></div>
        <div class="stat-card"><div class="label">Storage</div><div class="value" id="stat-storage">--</div></div>
    </div>

    <div class="tabs">
        <button class="tab active" data-tab="knowledge">Knowledge</button>
        <button class="tab" data-tab="sessions">Sessions</button>
        <button class="tab" data-tab="graph">Graph</button>
    </div>

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

    <div class="tab-content" id="tab-graph">
        <canvas id="graph-canvas"></canvas>
        <div id="graph-tooltip">
            <div class="tt-type"></div>
            <div class="tt-content"></div>
        </div>
    </div>
</div>

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

function badgeClass(type) { return 'badge badge-' + (type || 'fact'); }

async function loadStats() {
    try {
        const s = await api('/api/stats');
        document.getElementById('stat-total').textContent = s.total_knowledge;
        document.getElementById('stat-sessions').textContent = s.sessions_count;
        document.getElementById('stat-projects').textContent = Object.keys(s.by_project).length;
        document.getElementById('stat-health').textContent = (s.health_score * 100).toFixed(0) + '%';
        document.getElementById('stat-storage').textContent = s.storage_mb.toFixed(1) + ' MB';

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
                '<td class="date-col">' + formatDate(row.created_at) + '</td>';
            tbody.appendChild(tr);
        }

        if (data.items.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-dim);padding:40px">No knowledge found</td></tr>';
        }

        document.getElementById('page-info').textContent = 'Page ' + currentPage + ' of ' + totalPages;
        document.getElementById('prev-btn').disabled = currentPage <= 1;
        document.getElementById('next-btn').disabled = currentPage >= totalPages;
    } catch (e) {
        document.getElementById('knowledge-body').innerHTML =
            '<tr><td colspan="7" class="loading">Error loading data</td></tr>';
    }
}

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
        if (r.context) { ctxSection.style.display = ''; ctxBody.textContent = r.context; }
        else { ctxSection.style.display = 'none'; }

        const tagsSection = document.getElementById('modal-tags-section');
        const tagsEl = document.getElementById('modal-tags');
        const tags = Array.isArray(r.tags) ? r.tags : [];
        if (tags.length > 0) {
            tagsSection.style.display = '';
            tagsEl.innerHTML = tags.map(t => '<span class="tag">' + escapeHtml(t) + '</span>').join('');
        } else { tagsSection.style.display = 'none'; }

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
        } else { histSection.style.display = 'none'; }

        document.getElementById('detail-modal').classList.add('open');
    } catch (e) { console.error('Failed to load detail:', e); }
}

function metaItem(label, value) {
    return '<div class="meta-item"><div class="meta-label">' + label + '</div><div class="meta-value">' + value + '</div></div>';
}

function closeModal() { document.getElementById('detail-modal').classList.remove('open'); }

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

function initGraph() {
    if (graphLoaded) return;
    graphLoaded = true;

    const canvas = document.getElementById('graph-canvas');
    const ctx = canvas.getContext('2d');
    const tooltip = document.getElementById('graph-tooltip');
    const dpr = window.devicePixelRatio || 1;

    function resize() {
        const w = canvas.clientWidth;
        const h = canvas.clientHeight;
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    window.addEventListener('resize', resize);

    let nodes = [];
    let edges = [];
    let hoveredNode = null;
    let dragNode = null;

    api('/api/graph').then(data => {
        graphData = data;
        const W = canvas.clientWidth;
        const H = canvas.clientHeight;
        const idMap = {};

        nodes = data.nodes.map((n, i) => {
            const angle = (2 * Math.PI * i) / data.nodes.length;
            const r = Math.min(W, H) * 0.35;
            const node = {
                id: n.id,
                x: W / 2 + r * Math.cos(angle) + (Math.random() - 0.5) * 60,
                y: H / 2 + r * Math.sin(angle) + (Math.random() - 0.5) * 60,
                vx: 0, vy: 0,
                type: n.type,
                label: n.label,
                recall_count: n.recall_count || 0,
                radius: Math.max(4, Math.min(12, 4 + (n.recall_count || 0))),
                color: typeColors[n.type] || '#64748b',
            };
            idMap[n.id] = node;
            return node;
        });

        edges = data.edges.map(e => ({
            source: idMap[e.from_id],
            target: idMap[e.to_id],
            type: e.type,
        })).filter(e => e.source && e.target);

        simulate();
    }).catch(() => {});

    function simulate() {
        let iterations = 0;
        const maxIter = 300;

        function tick() {
            if (iterations > maxIter) { draw(); return; }
            iterations++;

            const W = canvas.clientWidth;
            const H = canvas.clientHeight;
            const k = Math.sqrt((W * H) / Math.max(nodes.length, 1));
            const cooling = 1 - iterations / maxIter;

            for (let i = 0; i < nodes.length; i++) {
                for (let j = i + 1; j < nodes.length; j++) {
                    let dx = nodes[j].x - nodes[i].x;
                    let dy = nodes[j].y - nodes[i].y;
                    let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                    let force = (k * k) / dist * 0.5;
                    let fx = (dx / dist) * force;
                    let fy = (dy / dist) * force;
                    nodes[i].vx -= fx; nodes[i].vy -= fy;
                    nodes[j].vx += fx; nodes[j].vy += fy;
                }
            }

            for (const e of edges) {
                let dx = e.target.x - e.source.x;
                let dy = e.target.y - e.source.y;
                let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                let force = (dist * dist) / k * 0.02;
                let fx = (dx / dist) * force;
                let fy = (dy / dist) * force;
                e.source.vx += fx; e.source.vy += fy;
                e.target.vx -= fx; e.target.vy -= fy;
            }

            for (const n of nodes) {
                let dx = W / 2 - n.x;
                let dy = H / 2 - n.y;
                n.vx += dx * 0.001; n.vy += dy * 0.001;
            }

            for (const n of nodes) {
                if (n === dragNode) continue;
                let speed = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
                let maxSpeed = 10 * cooling;
                if (speed > maxSpeed && speed > 0) {
                    n.vx = (n.vx / speed) * maxSpeed;
                    n.vy = (n.vy / speed) * maxSpeed;
                }
                n.x += n.vx; n.y += n.vy;
                n.vx *= 0.9; n.vy *= 0.9;
                n.x = Math.max(n.radius, Math.min(W - n.radius, n.x));
                n.y = Math.max(n.radius, Math.min(H - n.radius, n.y));
            }

            draw();
            requestAnimationFrame(tick);
        }
        tick();
    }

    function draw() {
        const W = canvas.clientWidth;
        const H = canvas.clientHeight;
        ctx.clearRect(0, 0, W, H);

        ctx.lineWidth = 0.5;
        ctx.strokeStyle = 'rgba(100,116,139,0.3)';
        for (const e of edges) {
            ctx.beginPath();
            ctx.moveTo(e.source.x, e.source.y);
            ctx.lineTo(e.target.x, e.target.y);
            ctx.stroke();
        }

        for (const n of nodes) {
            ctx.beginPath();
            ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
            ctx.fillStyle = n === hoveredNode ? n.color : n.color + '99';
            ctx.fill();
            if (n === hoveredNode) {
                ctx.strokeStyle = '#ffffff';
                ctx.lineWidth = 2;
                ctx.stroke();
            }
        }
    }

    canvas.addEventListener('mousemove', e => {
        const br = canvas.getBoundingClientRect();
        const mx = e.clientX - br.left;
        const my = e.clientY - br.top;

        if (dragNode) {
            dragNode.x = mx; dragNode.y = my;
            dragNode.vx = 0; dragNode.vy = 0;
            draw();
            return;
        }

        let found = null;
        for (const n of nodes) {
            const dx = n.x - mx; const dy = n.y - my;
            if (dx * dx + dy * dy < (n.radius + 4) * (n.radius + 4)) { found = n; break; }
        }

        if (found !== hoveredNode) { hoveredNode = found; draw(); }

        if (hoveredNode) {
            tooltip.style.display = 'block';
            tooltip.style.left = (e.clientX + 12) + 'px';
            tooltip.style.top = (e.clientY + 12) + 'px';
            tooltip.querySelector('.tt-type').textContent = hoveredNode.type;
            tooltip.querySelector('.tt-type').style.color = hoveredNode.color;
            tooltip.querySelector('.tt-content').textContent = hoveredNode.label;
        } else { tooltip.style.display = 'none'; }
    });

    canvas.addEventListener('mousedown', e => { if (hoveredNode) { dragNode = hoveredNode; canvas.style.cursor = 'grabbing'; } });
    canvas.addEventListener('mouseup', () => { dragNode = null; canvas.style.cursor = 'grab'; });
    canvas.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; hoveredNode = null; dragNode = null; draw(); });
}

document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        const target = tab.dataset.tab;
        document.getElementById('tab-' + target).classList.add('active');
        if (target === 'sessions') loadSessions();
        if (target === 'graph') initGraph();
    });
});

document.getElementById('prev-btn').addEventListener('click', () => { if (currentPage > 1) { currentPage--; loadKnowledge(); } });
document.getElementById('next-btn').addEventListener('click', () => { if (currentPage < totalPages) { currentPage++; loadKnowledge(); } });

let searchTimeout;
document.getElementById('search-input').addEventListener('input', () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => { currentPage = 1; loadKnowledge(); }, 300);
});
document.getElementById('type-filter').addEventListener('change', () => { currentPage = 1; loadKnowledge(); });
document.getElementById('project-filter').addEventListener('change', () => { currentPage = 1; loadKnowledge(); });

document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('detail-modal').addEventListener('click', e => { if (e.target === e.currentTarget) closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

loadStats();
loadKnowledge();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the memory dashboard."""

    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        def p(key, default=""):
            vals = params.get(key, [default])
            return vals[0] if vals else default

        if path in ("", "/"):
            self._send_html(HTML_PAGE)
            return

        db = get_db()
        if db is None:
            self._send_error(503, "Database not found at " + str(DB_PATH))
            return

        try:
            if path == "/api/stats":
                self._send_json(api_stats(db))

            elif path == "/api/knowledge":
                search = p("q") or None
                ktype = p("type") or None
                project = p("project") or None
                page = max(1, int(p("page", "1")))
                limit = min(200, max(1, int(p("limit", "50"))))
                self._send_json(api_knowledge(db, search, ktype, project, page, limit))

            elif path.startswith("/api/knowledge/"):
                parts = path.split("/")
                if len(parts) == 4 and parts[3].isdigit():
                    kid = int(parts[3])
                    result = api_knowledge_detail(db, kid)
                    if result:
                        self._send_json(result)
                    else:
                        self._send_error(404, "Knowledge record not found")
                else:
                    self._send_error(400, "Invalid knowledge ID")

            elif path == "/api/sessions":
                limit = min(200, max(1, int(p("limit", "20"))))
                self._send_json(api_sessions(db, limit))

            elif path == "/api/graph":
                self._send_json(api_graph(db))

            else:
                self._send_error(404, "Not found")
        except Exception as e:
            self._send_error(500, str(e))
        finally:
            db.close()


def main():
    """Start the dashboard HTTP server."""
    print(f"Memory dir: {MEMORY_DIR}")
    print(f"Database:   {DB_PATH} ({'exists' if DB_PATH.exists() else 'NOT FOUND'})")
    print(f"Dashboard running at http://localhost:{DASHBOARD_PORT}")

    server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard.")
        server.server_close()


if __name__ == "__main__":
    main()
