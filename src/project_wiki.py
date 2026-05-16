"""Project wiki — v10 P2.8.

Beever Atlas's "wiki-first RAG" idea: distil noisy chat into a clean,
auto-maintained per-channel wiki BEFORE any query is issued, so
retrieval at query time hits curated prose instead of fragmented chat
turns. We do the equivalent for each project tracked by
`memory_recall`: a per-project markdown digest that surfaces top
decisions, active solutions, conventions, and recent changes.

The wiki is **regenerated on demand** (cheap deterministic SQL — no
LLM calls) and lives at
`<memory-dir>/wikis/<project>.md`. A small MCP tool
`memory_wiki_generate(project)` lets the agent or a hook trigger a
refresh; the same function is used by the auto-refresh path that fires
every Nth save (gated on `MEMORY_WIKI_AUTO_REFRESH_EVERY_N`).

Sections, in order:

  1. **Top Decisions** — `type='decision'`, importance ∈ {critical, high},
     active. Newest 25 by `created_at`.
  2. **Active Solutions** — `type='solution'`, active. 25 by
     created_at desc.
  3. **Conventions** — `type='convention'`, active. All of them; rarely
     numerous, easy on the page.
  4. **Recent Changes** — last 14 days, any active type. 30 rows,
     newest first.

Each entry renders as:

    - [#<id>] <title> *(YYYY-MM-DD, importance)*
      tags: tag1, tag2

The title is the first non-empty line of the content, capped at 110
chars. The body is intentionally NOT included — the wiki is a *catalog*,
not a knowledge dump; readers click through to `/knowledge/<id>` for
details.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from paths import memory_dir

LOG = lambda msg: sys.stderr.write(f"[project-wiki] {msg}\n")

_TITLE_MAX = 110
_DEFAULT_RECENT_DAYS = 14
_DEFAULT_TOP_DECISIONS = 25
_DEFAULT_ACTIVE_SOLUTIONS = 25
_DEFAULT_RECENT_CHANGES = 30


# ──────────────────────────────────────────────
# Env knobs
# ──────────────────────────────────────────────


def _enabled() -> bool:
    raw = os.environ.get("MEMORY_WIKI_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _output_dir() -> Path:
    """Resolve the wikis directory. Defaults to `<MEMORY_DIR>/wikis`."""
    override = os.environ.get("MEMORY_WIKI_DIR")
    if override:
        return Path(override).expanduser()
    return memory_dir() / "wikis"


def _recent_days() -> int:
    raw = os.environ.get("MEMORY_WIKI_RECENT_DAYS")
    if not raw:
        return _DEFAULT_RECENT_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_RECENT_DAYS


def _auto_refresh_every_n() -> int:
    """0 disables the auto-refresh path. Default 0 — explicit calls only."""
    raw = os.environ.get("MEMORY_WIKI_AUTO_REFRESH_EVERY_N")
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _first_line(content: str | None, limit: int = _TITLE_MAX) -> str:
    if not content:
        return ""
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(line) > limit:
            return line[: limit - 1] + "…"
        return line
    return ""


def _short_date(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    try:
        return iso_ts[:10]
    except Exception:
        return ""


def _parse_tags(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str):
        # Stored as JSON array text by save_knowledge.
        import json
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
        except Exception:
            pass
    return []


# ──────────────────────────────────────────────
# Section queries
# ──────────────────────────────────────────────


def _top_decisions(db, project: str, limit: int = _DEFAULT_TOP_DECISIONS) -> list[dict]:
    rows = db.execute(
        """SELECT id, content, tags, importance, created_at
             FROM knowledge
            WHERE project = ? AND status = 'active' AND type = 'decision'
              AND importance IN ('critical', 'high')
            ORDER BY
                CASE importance
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    ELSE 2
                END,
                created_at DESC
            LIMIT ?""",
        (project, limit),
    ).fetchall()
    return [dict(r) if hasattr(r, "keys") else dict(zip(
        ("id", "content", "tags", "importance", "created_at"), r)) for r in rows]


def _active_solutions(db, project: str, limit: int = _DEFAULT_ACTIVE_SOLUTIONS) -> list[dict]:
    rows = db.execute(
        """SELECT id, content, tags, importance, created_at
             FROM knowledge
            WHERE project = ? AND status = 'active' AND type = 'solution'
            ORDER BY created_at DESC
            LIMIT ?""",
        (project, limit),
    ).fetchall()
    return [dict(r) if hasattr(r, "keys") else dict(zip(
        ("id", "content", "tags", "importance", "created_at"), r)) for r in rows]


def _conventions(db, project: str) -> list[dict]:
    rows = db.execute(
        """SELECT id, content, tags, importance, created_at
             FROM knowledge
            WHERE project = ? AND status = 'active' AND type = 'convention'
            ORDER BY created_at DESC""",
        (project,),
    ).fetchall()
    return [dict(r) if hasattr(r, "keys") else dict(zip(
        ("id", "content", "tags", "importance", "created_at"), r)) for r in rows]


def _recent_changes(db, project: str, days: int, limit: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    rows = db.execute(
        """SELECT id, type, content, tags, importance, created_at
             FROM knowledge
            WHERE project = ? AND status = 'active' AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?""",
        (project, cutoff, limit),
    ).fetchall()
    return [dict(r) if hasattr(r, "keys") else dict(zip(
        ("id", "type", "content", "tags", "importance", "created_at"), r)) for r in rows]


# ──────────────────────────────────────────────
# Markdown rendering
# ──────────────────────────────────────────────


def _render_entry(row: dict, *, with_type: bool = False) -> str:
    title = _first_line(row.get("content"))
    when = _short_date(row.get("created_at"))
    importance = (row.get("importance") or "medium").lower()
    rid = row.get("id")
    extras: list[str] = []
    if when:
        extras.append(when)
    if importance and importance != "medium":
        extras.append(importance)
    if with_type:
        t = row.get("type") or ""
        if t:
            extras.append(t)
    suffix = f" *({', '.join(extras)})*" if extras else ""
    head = f"- [#{rid}] {title}{suffix}"
    tags = _parse_tags(row.get("tags"))
    if tags:
        head += "\n  tags: " + ", ".join(tags[:6])
    return head


def render_wiki(
    db,
    project: str,
    *,
    recent_days: int | None = None,
    top_decisions: int = _DEFAULT_TOP_DECISIONS,
    active_solutions: int = _DEFAULT_ACTIVE_SOLUTIONS,
    recent_changes: int = _DEFAULT_RECENT_CHANGES,
) -> str:
    """Build the markdown body for a single project. Pure function — no I/O.

    Intended for tests and for the on-demand rendering path. The
    persistence wrapper (`generate_wiki`) writes the result to disk and
    returns the path.
    """
    days = _recent_days() if recent_days is None else recent_days
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    decisions = _top_decisions(db, project, limit=top_decisions)
    solutions = _active_solutions(db, project, limit=active_solutions)
    conventions = _conventions(db, project)
    changes = _recent_changes(db, project, days=days, limit=recent_changes)

    parts: list[str] = []
    parts.append(f"# Project: {project}")
    parts.append("")
    parts.append(f"_Last updated: {now}_")
    parts.append("")
    parts.append(
        f"_Auto-generated by total-agent-memory v10 — "
        f"{len(decisions)} top decisions, {len(solutions)} active solutions, "
        f"{len(conventions)} conventions, {len(changes)} recent changes_"
    )
    parts.append("")

    parts.append("## Top Decisions (critical / high)")
    parts.append("")
    if decisions:
        for row in decisions:
            parts.append(_render_entry(row))
    else:
        parts.append("_No critical or high-importance decisions yet._")
    parts.append("")

    parts.append("## Active Solutions")
    parts.append("")
    if solutions:
        for row in solutions:
            parts.append(_render_entry(row))
    else:
        parts.append("_No active solutions recorded._")
    parts.append("")

    parts.append("## Conventions")
    parts.append("")
    if conventions:
        for row in conventions:
            parts.append(_render_entry(row))
    else:
        parts.append("_No conventions recorded._")
    parts.append("")

    parts.append(f"## Recent Changes (last {days} days)")
    parts.append("")
    if changes:
        for row in changes:
            parts.append(_render_entry(row, with_type=True))
    else:
        parts.append("_No changes in the recent window._")
    parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


@dataclass
class WikiGenerateResult:
    project: str
    path: str
    chars: int
    decisions: int
    solutions: int
    conventions: int
    recent_changes: int


def generate_wiki(
    db,
    project: str,
    *,
    output_dir: Path | str | None = None,
) -> WikiGenerateResult | None:
    """Render and persist the wiki for a single project. Returns None when
    the wiki is disabled by env, or the project has zero active records
    (no point creating an empty file)."""
    if not _enabled():
        return None
    if not project:
        return None

    # Quick check: any active records at all?
    try:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge WHERE project=? AND status='active'",
            (project,),
        ).fetchone()
    except Exception as exc:
        LOG(f"generate_wiki count failed for {project}: {exc}")
        return None
    if (row["c"] if hasattr(row, "keys") else row[0]) == 0:
        return None

    body = render_wiki(db, project)

    out_dir = Path(output_dir).expanduser() if output_dir else _output_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        LOG(f"wiki dir create failed: {exc}")
        return None

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", project)
    target = out_dir / f"{safe}.md"
    try:
        target.write_text(body)
    except Exception as exc:
        LOG(f"wiki write failed for {project}: {exc}")
        return None

    return WikiGenerateResult(
        project=project,
        path=str(target),
        chars=len(body),
        decisions=body.count("## Top Decisions"),
        solutions=body.count("## Active Solutions"),
        conventions=body.count("## Conventions"),
        recent_changes=body.count("## Recent Changes"),
    )


def list_projects(db) -> list[str]:
    """Distinct projects with at least one active record. Sorted."""
    try:
        rows = db.execute(
            "SELECT DISTINCT project FROM knowledge "
            "WHERE status='active' AND project IS NOT NULL AND project != '' "
            "ORDER BY project"
        ).fetchall()
    except Exception:
        return []
    return [
        (r[0] if not hasattr(r, "keys") else r["project"])
        for r in rows
    ]


def generate_all(db, *, output_dir: Path | str | None = None) -> list[WikiGenerateResult]:
    """Refresh wikis for every project that has active records. Returns
    successful results only."""
    out: list[WikiGenerateResult] = []
    for project in list_projects(db):
        res = generate_wiki(db, project, output_dir=output_dir)
        if res:
            out.append(res)
    return out


# ──────────────────────────────────────────────
# Auto-refresh hook
# ──────────────────────────────────────────────


def maybe_auto_refresh(
    db,
    *,
    project: str,
    save_count: int,
    output_dir: Path | str | None = None,
) -> WikiGenerateResult | None:
    """Called by `save_knowledge` after a successful insert. Triggers a
    regeneration every Nth save (gated on
    `MEMORY_WIKI_AUTO_REFRESH_EVERY_N`, default 0 = off).

    `save_count` is the row's freshly-minted id — a deterministic
    monotonic integer that fires the refresh on multiples of N without
    needing a separate counter.
    """
    n = _auto_refresh_every_n()
    if n <= 0 or not project or save_count <= 0:
        return None
    if save_count % n != 0:
        return None
    return generate_wiki(db, project, output_dir=output_dir)
