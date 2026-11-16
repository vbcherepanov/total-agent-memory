"""Progressive-disclosure response modes for memory_recall.

Two transforms on top of the standard ``Recall.search`` result:

* :func:`index_response` — strip every item to an ultra-compact set of
  metadata fields (id + title + score + type + project + created_at). No
  content, no context, no cognitive expansion. ~40-60 tokens per hit.
* :func:`timeline_response` — flatten grouped hits into a chronological
  list and pad each hit with ±N neighbours from the same session so the
  caller can see what happened around the match.

Designed for a 3-layer workflow: ``recall(mode='index')`` → pick ids →
``memory_get(ids=[...])`` for full content, saving 80-90 %% of the tokens
versus ``detail='full'`` on the same ``limit``.
"""

from __future__ import annotations

from typing import Any

from memory_core.retrieval import SearchScope

# ── index mode ────────────────────────────────────────────────

_TITLE_MAX = 80


def _first_line(content: str, limit: int = _TITLE_MAX) -> str:
    """Return the first non-empty line of ``content``, truncated to ``limit``.

    Used to build a stable "title" for compact index entries without loading
    any body text into context.
    """
    if not content:
        return ""
    # Title = first non-empty line; fall back to whole string if none.
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            content = stripped
            break
    if len(content) > limit:
        return content[:limit] + "..."
    return content


def _index_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Build a single compact index entry from a standard search item."""
    return {
        "id": item.get("id"),
        "title": _first_line(item.get("content", "") or "", _TITLE_MAX),
        "score": item.get("score", 0.0),
        "type": item.get("type", ""),
        "project": item.get("project", ""),
        "created_at": item.get("created_at", ""),
    }


def index_response(search_result: dict[str, Any]) -> dict[str, Any]:
    """Transform a ``Recall.search`` result into index-only mode.

    Accepts the grouped ``{"results": {type: [items]}}`` shape produced by
    ``Recall.search`` (any detail level) and returns a flat ``results`` list
    of minimal metadata. ``total`` is recomputed to reflect the flattened
    list. ``mode`` is set to ``"index"``. Heavy keys such as ``cognitive``,
    ``expansion`` and ``tiers_used`` are preserved as-is if the caller
    already attached them, but the per-item payload is stripped.
    """
    flat: list[dict[str, Any]] = []
    grouped = search_result.get("results") or {}
    if isinstance(grouped, dict):
        for kind, group in grouped.items():
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict):
                    continue
                item = {**item, "type": item.get("type") or kind}
                # Items from detail="compact" store the title under "title"
                # rather than "content". Preserve that when available so we
                # don't re-truncate.
                if "content" not in item and "title" in item:
                    entry = {
                        "id": item.get("id"),
                        "title": item.get("title", ""),
                        "score": item.get("score", 0.0),
                        "type": item.get("type", ""),
                        "project": item.get("project", ""),
                        "created_at": item.get("created_at", ""),
                    }
                else:
                    entry = _index_entry(item)
                flat.append(entry)
    # Rank by score desc — gives a stable order independent of type grouping.
    flat.sort(key=lambda e: e.get("score", 0.0), reverse=True)
    out = {
        "query": search_result.get("query"),
        "mode": "index",
        "total": len(flat),
        "results": flat,
    }
    # Carry forward useful top-level metadata when present.
    for key in ("fusion", "tiers_used", "auto_detail"):
        if key in search_result:
            out[key] = search_result[key]
    return out


# ── timeline mode ─────────────────────────────────────────────


def _flatten(search_result: dict[str, Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    grouped = search_result.get("results") or {}
    if isinstance(grouped, dict):
        for kind, group in grouped.items():
            if isinstance(group, list):
                flat.extend({**i, "type": i.get("type") or kind}
                            for i in group if isinstance(i, dict))
    return flat


def _compact_neighbor(row: Any) -> dict[str, Any]:
    """Render a DB row as a compact timeline neighbour entry."""
    # sqlite3.Row supports dict-like access
    content = row["content"] if "content" in row.keys() else ""
    return {
        "id": row["id"],
        "title": _first_line(content or "", _TITLE_MAX),
        "type": row["type"] if "type" in row.keys() else "",
        "project": row["project"] if "project" in row.keys() else "",
        "created_at": row["created_at"] if "created_at" in row.keys() else "",
        "session_id": row["session_id"] if "session_id" in row.keys() else "",
        "via": ["timeline_neighbor"],
    }


def _fetch_neighbors(
    store: Any,
    *,
    session_id: str,
    created_at: str,
    exclude_ids: set[int],
    neighbors: int,
    scope: SearchScope | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``neighbors`` before and ``neighbors`` after the anchor.

    Preference: records in the same ``session_id``. When there aren't enough
    session peers (or session_id is empty), fall back to global chronology
    by ``created_at``. ``exclude_ids`` is mutated to prevent duplicates
    bubbling up across anchors.
    """
    if neighbors <= 0:
        return []

    collected: list[dict[str, Any]] = []
    db = store.db
    predicates, scope_params = (scope or SearchScope()).sql()
    where = " AND ".join(predicates)

    def _rows_to_entries(rows: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in rows:
            kid = r["id"]
            if kid in exclude_ids:
                continue
            exclude_ids.add(kid)
            out.append(_compact_neighbor(r))
        return out

    # Same-session before/after by created_at
    if session_id:
        before = db.execute(
            "SELECT id, session_id, type, content, project, created_at "
            f"FROM knowledge k WHERE session_id=? AND {where} "
            "AND created_at < ? ORDER BY created_at DESC LIMIT ?",
            [session_id, *scope_params, created_at, neighbors],
        ).fetchall()
        after = db.execute(
            "SELECT id, session_id, type, content, project, created_at "
            f"FROM knowledge k WHERE session_id=? AND {where} "
            "AND created_at > ? ORDER BY created_at ASC LIMIT ?",
            [session_id, *scope_params, created_at, neighbors],
        ).fetchall()
        collected.extend(_rows_to_entries(list(before)))
        collected.extend(_rows_to_entries(list(after)))

    # Fallback — global chronology when session peers are sparse or absent.
    need_before = neighbors - sum(
        1 for e in collected if e.get("created_at", "") < created_at
    )
    need_after = neighbors - sum(
        1 for e in collected if e.get("created_at", "") > created_at
    )
    if need_before > 0:
        rows = db.execute(
            "SELECT id, session_id, type, content, project, created_at "
            f"FROM knowledge k WHERE {where} AND created_at < ? "
            "ORDER BY created_at DESC LIMIT ?",
            [*scope_params, created_at, need_before * 3],
        ).fetchall()
        added = 0
        for r in rows:
            if added >= need_before:
                break
            if r["id"] in exclude_ids:
                continue
            exclude_ids.add(r["id"])
            collected.append(_compact_neighbor(r))
            added += 1
    if need_after > 0:
        rows = db.execute(
            "SELECT id, session_id, type, content, project, created_at "
            f"FROM knowledge k WHERE {where} AND created_at > ? "
            "ORDER BY created_at ASC LIMIT ?",
            [*scope_params, created_at, need_after * 3],
        ).fetchall()
        added = 0
        for r in rows:
            if added >= need_after:
                break
            if r["id"] in exclude_ids:
                continue
            exclude_ids.add(r["id"])
            collected.append(_compact_neighbor(r))
            added += 1
    return collected


def timeline_response(
    search_result: dict[str, Any],
    store: Any,
    neighbors: int = 2,
    limit: int = 5,
    scope: SearchScope | None = None,
) -> dict[str, Any]:
    """Expand top-K search hits with ±neighbours and return chronological list.

    Each anchor hit keeps its full payload (as returned by the underlying
    search) and is marked with ``role='hit'``. Neighbours get ``role=
    'neighbor'`` with compact fields. The final list is sorted by
    ``created_at`` ascending so the caller reads it like a session diary.
    """
    flat = _flatten(search_result)
    # Respect limit: only expand top-K hits.
    hits = flat[: max(0, int(limit))]

    seen: set[int] = {int(h["id"]) for h in hits if isinstance(h.get("id"), int)}
    timeline_items: list[dict[str, Any]] = []

    for hit in hits:
        anchor = dict(hit)
        anchor["role"] = "hit"
        timeline_items.append(anchor)

        session_id = hit.get("session_id", "") or ""
        created_at = hit.get("created_at", "") or ""
        if not created_at:
            continue
        nbrs = _fetch_neighbors(
            store,
            session_id=session_id,
            created_at=created_at,
            exclude_ids=seen,
            neighbors=neighbors,
            scope=scope,
        )
        for n in nbrs:
            n["role"] = "neighbor"
            timeline_items.append(n)

    timeline_items.sort(key=lambda e: e.get("created_at", "") or "")

    if scope is not None:
        visible = []
        for item in timeline_items:
            row = store.db.execute('SELECT * FROM knowledge WHERE id=?', (item['id'],)).fetchone()
            if row is not None and scope.allows(dict(row), store.db):
                visible.append(item)
        timeline_items = visible

    out = {
        "query": search_result.get("query"),
        "mode": "timeline",
        "total": len(timeline_items),
        "hits": len(hits),
        "neighbors": neighbors,
        "results": timeline_items,
    }
    for key in ("fusion", "tiers_used"):
        if key in search_result:
            out[key] = search_result[key]
    return out
