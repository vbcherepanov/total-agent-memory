"""v11 W1-A — Episode retriever.

Hybrid lookup over the :mod:`extractor`-built episode rows. Two
channels run in parallel:

* **BM25** over the FTS5 mirror ``episodes_v11_fts`` (summary +
  participants + outcome). Falls back to a ``LIKE`` scan when FTS5 is
  unavailable so the call still returns sensible results in stripped
  test environments.
* **Cosine** between the query embedding and each episode's stored
  ``embedding_blob``.

Results are fused with Reciprocal Rank Fusion (k = 60). RRF is rank-based
and ignores raw magnitude differences between BM25 and cosine, which is
the right call here: BM25 produces unbounded negative log scores while
cosine sits in [-1, 1].
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

# memory_core/episodes/retriever.py → memory_core → src
_SRC = str(Path(__file__).resolve().parent.parent.parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from memory_core.episodes.schema import EpisodeHit  # noqa: E402
from memory_core.retrieval import SearchScope  # noqa: E402
from memory_core.telemetry import counters, op_timer  # noqa: E402
from tam_db.contracts import Backend  # noqa: E402


RRF_K: int = 60
DEFAULT_K: int = 5
CANDIDATE_MULTIPLIER: int = 5    # pull this many per channel before fusion


# ─── public API ─────────────────────────────────────────────────────────


@op_timer("retrieval_episodes_ms")
def retrieve_episodes(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    *,
    k: int = DEFAULT_K,
    embed_fn: Callable[[str], Sequence[float]],
    scope: SearchScope | None = None,
) -> list[EpisodeHit]:
    """Return the top-k episodes for ``query``, fused across BM25 and cosine.

    Args:
        conn: SQLite connection with migration 023 applied.
        query: free-text question or topic.
        project: optional project filter. None = global search across
            all projects.
        k: maximum results to return (default 5).
        embed_fn: function that turns ``query`` into a vector. Required
            so the retriever stays decoupled from the embedding stack;
            the recall ladder injects the production provider, tests
            inject deterministic stubs.

    Returns:
        list of :class:`EpisodeHit` ordered by fused score, descending.
        Empty when no episodes match or the query is blank.
    """
    if not query or not query.strip():
        return []
    if k <= 0:
        return []
    candidate_n = max(k * CANDIDATE_MULTIPLIER, 25)

    bm25_ranked = _bm25_search(conn, query, project, candidate_n, scope=scope)
    cosine_ranked = _cosine_search(conn, query, project, candidate_n, embed_fn, scope=scope)

    fused = _rrf_fuse(
        {"bm25": bm25_ranked, "cosine": cosine_ranked},
        k=RRF_K,
    )

    if not fused:
        return []

    bm25_rank = {eid: idx + 1 for idx, (eid, _s) in enumerate(bm25_ranked)}
    cos_rank = {eid: idx + 1 for idx, (eid, _s) in enumerate(cosine_ranked)}

    hit_ids = [eid for eid, _ in fused[:k]]
    rows = _hydrate(conn, hit_ids)
    fact_map = _load_fact_links(conn, hit_ids)

    out: list[EpisodeHit] = []
    fused_score = dict(fused)
    for eid in hit_ids:
        row = rows.get(eid)
        if row is None:
            continue
        out.append(
            EpisodeHit(
                episode_id=eid,
                score=float(fused_score.get(eid, 0.0)),
                summary=str(row["summary"]),
                started_at=str(row["started_at"]),
                ended_at=str(row["ended_at"]),
                fact_ids=tuple(fact_map.get(eid, ())),
                project=str(row["project"]),
                session_id=row["session_id"],
                bm25_rank=bm25_rank.get(eid),
                cosine_rank=cos_rank.get(eid),
            )
        )
    counters.bump("retrieval_episode_hits", len(out))
    return out


def _episode_scope(scope: SearchScope | None) -> tuple[str, list[str]]:
    if scope is None:
        return "1=1", []
    predicates, params = scope.sql()
    sql = ("EXISTS (SELECT 1 FROM episode_facts ef WHERE ef.episode_id=e.id) "
           "AND NOT EXISTS (SELECT 1 FROM episode_facts ef WHERE ef.episode_id=e.id "
           "AND NOT EXISTS (SELECT 1 FROM knowledge k WHERE k.id=ef.knowledge_id AND "
           + " AND ".join(predicates) + "))")
    if scope.as_of is not None:
        from memory_core.snapshot import Snapshot
        sql += " AND julianday(e.ended_at)<=julianday(?)"
        params.append(Snapshot.at(scope.as_of).instant)
    return sql, params


# ─── BM25 channel ───────────────────────────────────────────────────────


def _bm25_search(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    n: int,
    *, scope: SearchScope | None = None,
) -> list[tuple[int, float]]:
    if _fts_available(conn):
        return _bm25_fts(conn, query, project, n, scope=scope)
    return _bm25_like(conn, query, project, n, scope=scope)


def _bm25_fts(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    n: int,
    *, scope: SearchScope | None = None,
) -> list[tuple[int, float]]:
    fts_query = _sanitize_fts(query)
    if not fts_query:
        return _bm25_like(conn, query, project, n, scope=scope)
    if getattr(conn, "backend", None) is Backend.POSTGRES:
        return _bm25_pg(conn, fts_query, project, n, scope=scope)
    sql = (
        "SELECT f.rowid AS eid, bm25(episodes_v11_fts) AS score "
        "FROM episodes_v11_fts AS f "
        "JOIN episodes_v11 AS e ON e.id = f.rowid "
        "WHERE episodes_v11_fts MATCH ? "
    )
    params: list[Any] = [fts_query]
    if project:
        sql += "AND e.project = ? "
        params.append(project)
    predicate, scope_params = _episode_scope(scope)
    sql += f"AND {predicate} "
    params.extend(scope_params)
    sql += "ORDER BY score ASC LIMIT ?"
    params.append(int(n))
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        # Malformed FTS expression even after sanitize — degrade.
        return _bm25_like(conn, query, project, n, scope=scope)
    # bm25() returns lower=better; convert to higher=better so RRF gets a
    # consistent "first row is best" ordering.
    rows = cur.fetchall()
    return [(int(r[0]), -float(r[1])) for r in rows]


def _bm25_pg(
    conn: sqlite3.Connection,
    fts_query: str,
    project: str | None,
    n: int,
    *, scope: SearchScope | None = None,
) -> list[tuple[int, float]]:
    from memory_core.pg_fts import match_source

    source, params = match_source("episodes_v11_fts", fts_query)
    sql = (
        f"SELECT f.id AS eid, f.rank AS score FROM ({source}) AS f "
        "JOIN episodes_v11 AS e ON e.id = f.id "
    )
    predicate, scope_params = _episode_scope(scope)
    sql += f"WHERE {predicate} "
    params = [*params, *scope_params]
    if project:
        sql += "AND e.project = ? "
        params = [*params, project]
    sql += "ORDER BY score ASC, eid ASC LIMIT ?"
    rows = conn.execute(sql, [*params, int(n)]).fetchall()
    # Same sign convention as FTS5 bm25(): lower is better, negated for RRF.
    return [(int(r[0]), -float(r[1])) for r in rows]


def _bm25_like(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    n: int,
    *, scope: SearchScope | None = None,
) -> list[tuple[int, float]]:
    """Fallback when FTS5 is missing — naive substring scoring on tokens.

    Score = count of distinct query tokens that appear in summary +
    participants. Deterministic, side-effect free; good enough to keep
    smoke tests sensible.
    """
    tokens = [t for t in _tokenize(query) if t]
    if not tokens:
        return []
    sql = (
        "SELECT id, summary, COALESCE(participants,'') AS participants "
        "FROM episodes_v11 e"
    )
    predicate, params = _episode_scope(scope)
    sql += f" WHERE {predicate}"
    if project:
        sql += " AND e.project = ?"
        params.append(project)
    cur = conn.execute(sql, params)
    scored: list[tuple[int, float]] = []
    for row in cur.fetchall():
        eid = int(row[0])
        haystack = (str(row[1]) + " " + str(row[2])).lower()
        score = sum(1 for tok in tokens if tok in haystack)
        if score > 0:
            scored.append((eid, float(score)))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:n]


MIN_FTS_TOKEN_LEN = 2


def _sanitize_fts(query: str) -> str:
    """Convert free text into an FTS5-safe MATCH expression.

    Strategy: Russian terms become stem prefixes, others exact quoted
    phrases (memory_core.query_terms), OR-ed together. Never
    raises — empty input ⇒ empty output ⇒ caller falls back to LIKE.
    """
    from memory_core.query_terms import fts_match_query

    if not any(len(token) >= MIN_FTS_TOKEN_LEN for token in _tokenize(query)):
        return ""
    return fts_match_query(query)


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for ch in (text or "").lower():
        if ch.isalnum() or ch == "_":
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


# ─── Cosine channel ─────────────────────────────────────────────────────


def _cosine_search(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    n: int,
    embed_fn: Callable[[str], Sequence[float]],
    *, scope: SearchScope | None = None,
) -> list[tuple[int, float]]:
    qvec = list(embed_fn(query) or ())
    if not qvec:
        return []
    qnorm = _l2(qvec)
    if qnorm <= 0.0:
        return []

    predicate, params = _episode_scope(scope)
    sql = f"SELECT id, embedding_blob FROM episodes_v11 e WHERE embedding_blob IS NOT NULL AND {predicate}"
    if project:
        sql += " AND project = ?"
        params.append(project)
    cur = conn.execute(sql, params)
    scored: list[tuple[int, float]] = []
    for row in cur.fetchall():
        eid = int(row[0])
        blob = row[1]
        vec = _blob_to_vec(blob)
        if not vec or len(vec) != len(qvec):
            continue
        sim = _cosine(qvec, vec, a_norm=qnorm)
        if sim is None:
            continue
        scored.append((eid, sim))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:n]


def _blob_to_vec(blob: bytes | None) -> list[float]:
    if blob is None:
        return []
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return []
    raw = bytes(blob)
    if len(raw) % 4 != 0:
        return []
    n = len(raw) // 4
    if n == 0:
        return []
    return list(struct.unpack(f"{n}f", raw))


def _cosine(
    a: Sequence[float],
    b: Sequence[float],
    *,
    a_norm: float | None = None,
) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = 0.0
    nb = 0.0
    na_sq = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        nb += fy * fy
        if a_norm is None:
            na_sq += fx * fx
    if a_norm is None:
        if na_sq <= 0.0:
            return None
        a_norm = na_sq ** 0.5
    if nb <= 0.0 or a_norm <= 0.0:
        return None
    return dot / (a_norm * (nb ** 0.5))


def _l2(vec: Sequence[float]) -> float:
    return sum(float(x) * float(x) for x in vec) ** 0.5


# ─── RRF fusion ─────────────────────────────────────────────────────────


def _rrf_fuse(
    channels: dict[str, list[tuple[int, float]]],
    *,
    k: int,
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for _name, ranked in channels.items():
        for rank, (eid, _raw) in enumerate(ranked):
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


# ─── hydration ──────────────────────────────────────────────────────────


def _hydrate(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"""
        SELECT id, project, session_id, started_at, ended_at, summary
        FROM episodes_v11
        WHERE id IN ({placeholders})
        """,
        list(ids),
    )
    out: dict[int, sqlite3.Row] = {}
    for row in cur.fetchall():
        # Support both Row and tuple — wrap in a dict for stable access.
        d = {
            "id": int(row[0]),
            "project": row[1],
            "session_id": row[2],
            "started_at": row[3],
            "ended_at": row[4],
            "summary": row[5],
        }
        out[d["id"]] = d  # type: ignore[assignment]
    return out


def _load_fact_links(
    conn: sqlite3.Connection,
    episode_ids: list[int],
) -> dict[int, list[int]]:
    if not episode_ids:
        return {}
    placeholders = ",".join("?" for _ in episode_ids)
    cur = conn.execute(
        f"""
        SELECT episode_id, knowledge_id
        FROM episode_facts
        WHERE episode_id IN ({placeholders})
        ORDER BY episode_id, knowledge_id
        """,
        list(episode_ids),
    )
    out: dict[int, list[int]] = {}
    for row in cur.fetchall():
        out.setdefault(int(row[0]), []).append(int(row[1]))
    return out


# ─── helpers ────────────────────────────────────────────────────────────


def _fts_available(conn: sqlite3.Connection) -> bool:
    if getattr(conn, "backend", None) is Backend.POSTGRES:
        # The workspace baseline always carries the episodes lexical index.
        return True
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = 'episodes_v11_fts'"
    )
    return cur.fetchone() is not None


__all__ = ["retrieve_episodes", "RRF_K"]
