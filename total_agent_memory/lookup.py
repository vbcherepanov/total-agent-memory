"""`tam-lookup` / `lookup-memory` CLI: bash-friendly memory search for sub-agents.

This module ships in the pip package so any client that did
`pip install total-agent-memory` (or `uv pip install …`) gets working
`tam-lookup` and `lookup-memory` binaries on PATH.

Design goals:
  * **Zero extra deps** — uses only what total_agent_memory.server already
    pulls in (mcp[cli], chromadb, sentence-transformers, sqlite3 stdlib).
  * **No Ollama / RAG / LLM call** — pure retrieval. Sub-agents that need
    a chat model wrap us with their own.
  * **Same DB the running MCP server uses** — reads the configured memory
    dir (TAM_MEMORY_DIR, falls back to ~/.tam, then legacy ~/.claude-memory),
    so results match what the parent agent sees through MCP tools.

Usage:
    tam-lookup "query"
    tam-lookup --project myproj "query"
    tam-lookup --limit 5 --json "query"
    tam-lookup --tag reusable --type solution "query"

Output: human-readable bullets by default; `--json` for structured stdout
(stable schema for machine consumption by agents).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_LIMIT = 8
DEFAULT_PROJECT_FILTER = None


def _resolve_db_path() -> Path:
    """Resolve memory.db via the shared `paths.memory_dir()` helper.

    Honors TAM_MEMORY_DIR / legacy CLAUDE_MEMORY_DIR / ~/.tam / migrated
    ~/.claude-memory in the order encoded by `total_agent_memory.paths`.
    """
    # The runtime lives in src/, which is on sys.path because pyproject
    # includes it as a package. Defer the import so the CLI starts fast.
    _src = Path(__file__).resolve().parent.parent / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
    from paths import memory_db  # noqa: WPS433
    return memory_db()


def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.stderr.write(
            f"[tam-lookup] memory.db not found at {db_path}\n"
            "  Run the MCP server once (or set TAM_MEMORY_DIR) so the DB initialises.\n"
        )
        sys.exit(2)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _has_fts(conn: sqlite3.Connection) -> bool:
    """v8+ ships an FTS5 virtual table `knowledge_fts`. Fall back to LIKE if absent."""
    try:
        conn.execute("SELECT 1 FROM knowledge_fts LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    project: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Hybrid BM25 (FTS5) + LIKE fallback. Filters: project, type, tags."""
    use_fts = _has_fts(conn)

    where: list[str] = ["k.status = 'active'"]
    params: list = []

    if project:
        where.append("k.project = ?")
        params.append(project)
    if types:
        placeholders = ",".join("?" * len(types))
        where.append(f"k.type IN ({placeholders})")
        params.extend(types)
    if tags:
        # tags is JSON in DB; LIKE on substring is sufficient for the lookup CLI.
        for t in tags:
            where.append("k.tags LIKE ?")
            params.append(f'%"{t}"%')

    if use_fts:
        # FTS5 BM25 ranking — uses the virtual table on `content`.
        sql = (
            "SELECT k.id, k.project, k.type, k.content, k.tags, k.created_at, "
            "       bm25(knowledge_fts) AS score "
            "FROM knowledge_fts "
            "JOIN knowledge k ON k.id = knowledge_fts.rowid "
            "WHERE knowledge_fts MATCH ? "
            f"AND {' AND '.join(where)} "
            "ORDER BY score LIMIT ?"
        )
        rows = conn.execute(sql, (_fts_query(query), *params, limit)).fetchall()
    else:
        sql = (
            "SELECT k.id, k.project, k.type, k.content, k.tags, k.created_at, "
            "       0.0 AS score "
            "FROM knowledge k "
            f"WHERE {' AND '.join(where)} AND k.content LIKE ? "
            "ORDER BY k.created_at DESC LIMIT ?"
        )
        rows = conn.execute(sql, (*params, f"%{query}%", limit)).fetchall()

    return [dict(r) for r in rows]


def _fts_query(q: str) -> str:
    """Quote bare tokens for FTS5 so punctuation in user input doesn't break parsing."""
    parts = []
    for tok in q.split():
        tok = tok.replace('"', '""')
        parts.append(f'"{tok}"')
    return " ".join(parts) or '""'


def render_human(rows: list[dict]) -> str:
    if not rows:
        return "(no matches)"
    out: list[str] = []
    for i, r in enumerate(rows, 1):
        content = (r.get("content") or "").strip()
        snippet = content if len(content) <= 280 else content[:277] + "..."
        meta = f"[{r.get('type', '?')}|{r.get('project', '?')}]"
        out.append(f"{i}. {meta} {snippet}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tam-lookup",
        description="Search the total-agent-memory DB from the shell.",
    )
    p.add_argument("query", nargs="+", help="Search query (free text).")
    p.add_argument("-p", "--project", default=DEFAULT_PROJECT_FILTER,
                   help="Restrict to a single project.")
    p.add_argument("-l", "--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"Max results (default {DEFAULT_LIMIT}).")
    p.add_argument("-t", "--type", action="append",
                   help="Filter by knowledge type. Repeat for multiple.")
    p.add_argument("--tag", action="append",
                   help="Require this tag. Repeat for multiple (AND).")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Emit machine-readable JSON instead of bullets.")
    p.add_argument("--db", type=Path, default=None,
                   help="Path to memory.db (default: $CLAUDE_MEMORY_DIR/memory.db).")
    args = p.parse_args(argv)

    db_path = args.db or _resolve_db_path()
    conn = _open_db(db_path)
    try:
        rows = search(
            conn,
            " ".join(args.query),
            project=args.project,
            types=args.type,
            tags=args.tag,
            limit=args.limit,
        )
    finally:
        conn.close()

    if args.json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_human(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
