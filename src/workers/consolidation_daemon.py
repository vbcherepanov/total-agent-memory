"""v11 W2-G — Idle-project consolidation daemon.

Background worker that runs forever picking the OLDEST idle project
(>30 min since last touch) and applying five consolidation phases:

  1. Transitive-fact materialization (2-hop graph inference).
  2. Episode materialization for old sessions missing episodes_v11 rows.
  3. Cosine-based dedup of near-identical knowledge facts.
  4. Decay (multiplicative confidence reduction for stale facts).
  5. Optional summary compression for long sessions (LLM, off-hot-path).

Hard rules
----------

* Never consolidates the project the user is currently using
  (see :func:`workers.project_activity.is_active`).
* Pause-checks between every phase AND inside long phases — if the
  project becomes active mid-run, the daemon exits with paused=True
  immediately, leaving the lock cleared so a future run can resume.
* Advisory lock is a TTL stored in ``consolidation_state.locked_until``.
  Stale locks (TTL in the past) are claimable — this is what lets a
  killed daemon recover on restart without manual intervention.
* SQLite must be in WAL mode so reads (recall) keep flowing while the
  daemon writes. WAL is set on every connection the daemon opens; the
  production server already enables WAL too.

Layer separation
----------------

* MAY import: ``memory_core.*``, stdlib, numpy.
* MUST NOT import: ``ai_layer.*`` at module top-level.  An optional
  ``llm_summarizer`` callable can be passed in by the daemon
  entrypoint, keeping the deterministic core LLM-free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

# Make src/ importable when this module is loaded as workers.consolidation_daemon.
_SRC = str(Path(__file__).resolve().parent.parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from memory_core.episodes.extractor import extract_episodes_from_session  # noqa: E402
from workers.project_activity import is_active as _is_active_fn  # noqa: E402

log = logging.getLogger("workers.consolidation")


# ─── tunables ──────────────────────────────────────────────────────────

# Bound the per-run cost of expensive phases.
TRANSITIVE_INFERENCE_LIMIT = 100      # max new facts per run
DEDUP_WINDOW_SIZE = 200               # rolling pairwise window per (project,type)
DEDUP_COSINE_THRESHOLD = 0.95         # merge floor
DECAY_FACTOR = 0.95                   # multiplicative
DECAY_MIN_CONFIDENCE = 0.05           # floor — never zero out
DECAY_AGE_DAYS = 30
SESSION_SUMMARY_TURN_THRESHOLD = 30   # log_count threshold for compression
EPISODE_MIN_FACTS = 3                 # session needs ≥3 facts to materialize
EPISODE_MIN_AGE_HOURS = 24            # session must be ≥24h old


# ─── dataclasses ───────────────────────────────────────────────────────


@dataclass
class ConsolidationStats:
    """Per-run summary stored in ``consolidation_state.stats_json``."""

    project: str
    started_at: datetime
    ended_at: datetime
    transitive_facts_added: int = 0
    episodes_materialized: int = 0
    duplicates_merged: int = 0
    decay_applied: int = 0
    summaries_compressed: int = 0
    paused: bool = False
    error: str | None = None
    phases: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat()
        d["ended_at"] = self.ended_at.isoformat()
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


# ─── time helpers ──────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ─── lock management ───────────────────────────────────────────────────


def _try_acquire_lock(
    conn: sqlite3.Connection,
    project: str,
    *,
    ttl_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Atomically claim the advisory lock for ``project``.

    Strategy:

    * UPSERT the row, but *condition* the locked_until update on the old
      lock being NULL or in the past. SQLite supports this via the
      ``WHERE`` clause of a ``DO UPDATE``.
    * Return True only if the row's ``locked_until`` matches the value
      we just tried to write — this proves WE won the race, not somebody
      else who upserted concurrently.

    The pattern is robust against a killed daemon (stale lock TTL in
    the past becomes claimable on next run).
    """
    when = now or _utcnow()
    until = _iso(when + timedelta(seconds=ttl_seconds))
    now_iso = _iso(when)

    conn.execute(
        """
        INSERT INTO consolidation_state (project, locked_until, last_status, updated_at)
        VALUES (?, ?, 'in_progress', ?)
        ON CONFLICT(project) DO UPDATE SET
            locked_until = excluded.locked_until,
            last_status  = 'in_progress',
            updated_at   = excluded.updated_at
        WHERE consolidation_state.locked_until IS NULL
           OR consolidation_state.locked_until < ?
        """,
        (project, until, now_iso, now_iso),
    )
    conn.commit()

    row = conn.execute(
        "SELECT locked_until FROM consolidation_state WHERE project = ?",
        (project,),
    ).fetchone()
    return row is not None and str(row[0]) == until


def _release_lock(
    conn: sqlite3.Connection,
    project: str,
    *,
    status: str,
    stats: ConsolidationStats | None,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Clear the advisory lock and persist run outcome."""
    when = now or _utcnow()
    last_consolidated = _iso(when) if status == "ok" else None
    stats_json = stats.to_json() if stats is not None else None
    conn.execute(
        """
        INSERT INTO consolidation_state (
            project, last_consolidated_at, last_status, last_error,
            locked_until, stats_json, updated_at
        ) VALUES (?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            last_consolidated_at = COALESCE(excluded.last_consolidated_at,
                                            consolidation_state.last_consolidated_at),
            last_status          = excluded.last_status,
            last_error           = excluded.last_error,
            locked_until         = NULL,
            stats_json           = excluded.stats_json,
            updated_at           = excluded.updated_at
        """,
        (project, last_consolidated, status, error, stats_json, _iso(when)),
    )
    conn.commit()


# ─── phase 1: transitive fact materialization ──────────────────────────


def _materialize_transitive_facts(
    conn: sqlite3.Connection,
    project: str,
    *,
    pause_check: Callable[[], bool],
    limit: int = TRANSITIVE_INFERENCE_LIMIT,
) -> int:
    """Infer 2-hop facts: (A, rel, B) + (B, rel, C) → (A, rel, C).

    Bounded by ``limit`` per run. Inferred facts are stored as new
    ``knowledge`` rows tagged ``inferred,transitive``. We use a content
    hash to keep the operation idempotent across runs — re-running on
    the same graph produces zero additional rows.
    """
    if pause_check():
        return 0

    # Pull edges projected through nodes that touch this project. The
    # graph is project-agnostic at the schema level; we filter by joining
    # on `knowledge_nodes` → `knowledge.project` so only edges that
    # connect to facts owned by this project participate.
    rows = conn.execute(
        """
        SELECT DISTINCT
            ge.source_id, ge.relation_type, ge.target_id,
            n_src.name AS src_name, n_tgt.name AS tgt_name
        FROM graph_edges ge
        JOIN graph_nodes n_src ON n_src.id = ge.source_id
        JOIN graph_nodes n_tgt ON n_tgt.id = ge.target_id
        WHERE EXISTS (
            SELECT 1 FROM knowledge_nodes kn
            JOIN knowledge k ON k.id = kn.knowledge_id
            WHERE k.project = ?
              AND (kn.node_id = ge.source_id OR kn.node_id = ge.target_id)
        )
        """,
        (project,),
    ).fetchall()

    # Index edges by source for cheap 2-hop walk.
    by_source: dict[str, list[tuple[str, str, str, str]]] = {}
    name_of: dict[str, str] = {}
    for r in rows:
        src, rel, tgt, src_name, tgt_name = (str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]))
        by_source.setdefault(src, []).append((rel, tgt, src_name, tgt_name))
        name_of[src] = src_name
        name_of[tgt] = tgt_name

    inferred = 0
    seen_hashes: set[str] = set()

    for a_id, edges in by_source.items():
        if pause_check() or inferred >= limit:
            break
        for rel1, b_id, _a_name, _b_name in edges:
            for rel2, c_id, _bn, _c_name in by_source.get(b_id, ()):
                if c_id == a_id:
                    continue  # ignore trivial cycles
                if rel1 != rel2:
                    continue  # only same-relation chains compose cleanly
                if pause_check() or inferred >= limit:
                    break
                a_name = name_of.get(a_id, a_id)
                c_name = name_of.get(c_id, c_id)
                content = f"{a_name} {rel1} {c_name} (transitive via {name_of.get(b_id, b_id)})"
                content_hash = hashlib.sha256(
                    f"{project}|transitive|{a_id}|{rel1}|{c_id}".encode("utf-8")
                ).hexdigest()
                if content_hash in seen_hashes:
                    continue
                seen_hashes.add(content_hash)

                # Skip if an inferred row with the same hash exists (idempotency).
                exists = conn.execute(
                    """
                    SELECT 1 FROM knowledge
                    WHERE project = ? AND type = 'fact'
                      AND status = 'active'
                      AND context = ?
                    LIMIT 1
                    """,
                    (project, f"hash:{content_hash}"),
                ).fetchone()
                if exists is not None:
                    continue

                now_iso = _iso(_utcnow())
                conn.execute(
                    """
                    INSERT INTO knowledge (
                        session_id, type, content, context, project, tags,
                        status, confidence, source, created_at
                    ) VALUES (?, 'fact', ?, ?, ?, ?, 'active', 0.6, 'transitive', ?)
                    """,
                    (
                        f"consolidation:{project}",
                        content,
                        f"hash:{content_hash}",
                        project,
                        json.dumps(["inferred", "transitive"]),
                        now_iso,
                    ),
                )
                inferred += 1

    if inferred:
        conn.commit()
    return inferred


# ─── phase 2: episode materialization ──────────────────────────────────


def _materialize_old_episodes(
    conn: sqlite3.Connection,
    project: str,
    *,
    pause_check: Callable[[], bool],
    embed_fn: Callable[[str], Sequence[float]] | None,
    llm_summarizer: Callable[[str], str] | None,
    now: datetime | None = None,
) -> int:
    """Run extract_episodes_from_session on old sessions missing episodes."""
    if pause_check():
        return 0

    when = now or _utcnow()
    cutoff_iso = _iso(when - timedelta(hours=EPISODE_MIN_AGE_HOURS))

    # Sessions older than threshold AND with ≥ EPISODE_MIN_FACTS facts AND
    # no row in episodes_v11 yet.
    rows = conn.execute(
        """
        SELECT k.session_id, COUNT(*) AS n
        FROM knowledge k
        WHERE k.project = ?
          AND k.session_id IS NOT NULL
          AND k.session_id <> ''
          AND k.created_at < ?
          AND COALESCE(k.status, 'active') = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM episodes_v11 e
              WHERE e.project = k.project AND e.session_id = k.session_id
          )
        GROUP BY k.session_id
        HAVING n >= ?
        ORDER BY MIN(k.created_at) ASC
        """,
        (project, cutoff_iso, EPISODE_MIN_FACTS),
    ).fetchall()

    materialized = 0
    for r in rows:
        if pause_check():
            break
        session_id = str(r[0])
        try:
            new_eps = extract_episodes_from_session(
                conn,
                project=project,
                session_id=session_id,
                llm_summarizer=llm_summarizer,
                embed_fn=embed_fn,
            )
        except Exception as exc:  # noqa: BLE001 — phase isolation
            log.warning(
                "episode_materialization_failed",
                extra={"project": project, "session_id": session_id, "error": str(exc)},
            )
            continue
        materialized += len(new_eps)

    return materialized


# ─── phase 3: cosine dedup ─────────────────────────────────────────────


def _cosine_dedup(
    conn: sqlite3.Connection,
    project: str,
    *,
    pause_check: Callable[[], bool],
    threshold: float = DEDUP_COSINE_THRESHOLD,
    window: int = DEDUP_WINDOW_SIZE,
) -> int:
    """Greedy pairwise cosine dedup within (project, type), rolling window.

    For each (project, type) bucket we pull up to ``window`` most recent
    active facts that have a stored embedding. We compare each new fact
    against the already-kept set in O(window). If similarity ≥ threshold
    we merge OLDER into NEWER:

      * older.status = 'merged'
      * older.superseded_by = newer.id

    O(window²) but ``window`` is bounded — true n² is avoided.
    """
    if pause_check():
        return 0

    # Lazy import — keep numpy out of module-load path for non-numpy tests.
    import numpy as np

    types = [
        str(r[0]) for r in conn.execute(
            "SELECT DISTINCT type FROM knowledge WHERE project = ? AND COALESCE(status,'active') = 'active'",
            (project,),
        ).fetchall()
    ]

    merged_total = 0
    for ktype in types:
        if pause_check():
            break
        rows = conn.execute(
            """
            SELECT k.id, e.float32_vector
            FROM knowledge k
            JOIN embeddings e ON e.knowledge_id = k.id
            WHERE k.project = ? AND k.type = ?
              AND COALESCE(k.status, 'active') = 'active'
            ORDER BY k.created_at DESC, k.id DESC
            LIMIT ?
            """,
            (project, ktype, window),
        ).fetchall()

        if len(rows) < 2:
            continue

        kept: list[tuple[int, "np.ndarray"]] = []
        merges: list[tuple[int, int]] = []  # (older_id, newer_id)

        for kid_raw, blob in rows:
            kid = int(kid_raw)
            if not blob:
                continue
            vec = np.frombuffer(blob, dtype=np.float32)
            n = float(np.linalg.norm(vec))
            if n <= 0:
                continue
            unit = vec / n

            duplicate_of: int | None = None
            for keep_id, keep_unit in kept:
                if pause_check():
                    duplicate_of = None
                    break
                sim = float(np.dot(unit, keep_unit))
                if sim >= threshold:
                    duplicate_of = keep_id
                    break

            if duplicate_of is not None:
                # rows are ordered created_at DESC → kept entries are NEWER.
                # current `kid` is older → merge older(kid) into newer(duplicate_of).
                merges.append((kid, duplicate_of))
            else:
                kept.append((kid, unit))

        if pause_check():
            break

        for older_id, newer_id in merges:
            conn.execute(
                """
                UPDATE knowledge
                SET status = 'merged', superseded_by = ?
                WHERE id = ? AND COALESCE(status, 'active') = 'active'
                """,
                (newer_id, older_id),
            )
        if merges:
            conn.commit()
            merged_total += len(merges)

    return merged_total


# ─── phase 4: decay ────────────────────────────────────────────────────


def _apply_decay(
    conn: sqlite3.Connection,
    project: str,
    *,
    pause_check: Callable[[], bool],
    factor: float = DECAY_FACTOR,
    floor: float = DECAY_MIN_CONFIDENCE,
    age_days: int = DECAY_AGE_DAYS,
    now: datetime | None = None,
) -> int:
    """Multiply confidence by ``factor`` for facts not recalled in ``age_days``.

    Facts at or below ``floor`` are left alone — we never zero out a
    long-tail truth, just reduce ranking weight.

    Note on the column choice: the v5 schema has no `priority` field;
    `confidence` is the closest semantic ranking signal and is what the
    retriever already uses for boosting. Decay updates `confidence` so
    stale facts naturally sink in recall results.
    """
    if pause_check():
        return 0

    when = now or _utcnow()
    cutoff_iso = _iso(when - timedelta(days=age_days))

    # Eligible: active rows, owned by project, last_recalled IS NULL OR < cutoff,
    # AND confidence already above the floor (else decay would do nothing).
    cur = conn.execute(
        """
        UPDATE knowledge
        SET confidence = MAX(?, confidence * ?)
        WHERE project = ?
          AND COALESCE(status, 'active') = 'active'
          AND (last_recalled IS NULL OR last_recalled < ?)
          AND confidence > ?
        """,
        (floor, factor, project, cutoff_iso, floor),
    )
    n = int(cur.rowcount or 0)
    if n:
        conn.commit()
    return n


# ─── phase 5: summary compression ──────────────────────────────────────


def _compress_long_session_summaries(
    conn: sqlite3.Connection,
    project: str,
    *,
    pause_check: Callable[[], bool],
    llm_summarizer: Callable[[str], str] | None,
    turn_threshold: int = SESSION_SUMMARY_TURN_THRESHOLD,
) -> int:
    """For sessions with > N turns and no summary, write one via the LLM.

    No-op if ``llm_summarizer`` is None — the deterministic core works
    without LLM, and the daemon entrypoint is free to leave summarization
    off in environments without a configured provider.
    """
    if llm_summarizer is None:
        return 0
    if pause_check():
        return 0

    rows = conn.execute(
        """
        SELECT id, log_count FROM sessions
        WHERE project = ?
          AND log_count > ?
          AND (summary IS NULL OR summary = '')
        ORDER BY log_count DESC
        LIMIT 50
        """,
        (project, turn_threshold),
    ).fetchall()

    compressed = 0
    for r in rows:
        if pause_check():
            break
        sess_id = str(r[0])
        # Pull facts contents joined as input; cap to avoid runaway prompts.
        facts = conn.execute(
            """
            SELECT content FROM knowledge
            WHERE session_id = ? AND project = ?
              AND COALESCE(status, 'active') = 'active'
            ORDER BY created_at ASC
            LIMIT 200
            """,
            (sess_id, project),
        ).fetchall()
        if not facts:
            continue
        joined = "\n".join(str(f[0]) for f in facts if f[0])
        try:
            summary_text = (llm_summarizer(joined) or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "summary_llm_failed",
                extra={"project": project, "session_id": sess_id, "error": str(exc)},
            )
            continue
        if not summary_text:
            continue
        conn.execute(
            "UPDATE sessions SET summary = ? WHERE id = ? AND (summary IS NULL OR summary = '')",
            (summary_text, sess_id),
        )
        compressed += 1

    if compressed:
        conn.commit()
    return compressed


# ─── public: consolidate_project ───────────────────────────────────────


def consolidate_project(
    conn: sqlite3.Connection,
    project: str,
    *,
    budget_seconds: int = 600,
    pause_check: Callable[[], bool] | None = None,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    llm_summarizer: Callable[[str], str] | None = None,
) -> ConsolidationStats:
    """One pass over a single project.

    Honors the budget via the `pause_check` callable AND a hard wall
    clock. Returns a stats record describing what happened. On lock
    contention returns ``paused=True`` immediately.
    """
    if not project:
        raise ValueError("consolidate_project: project is required")

    started = _utcnow()
    deadline = started + timedelta(seconds=budget_seconds)
    stats = ConsolidationStats(project=project, started_at=started, ended_at=started)

    # Lock TTL = budget + 30s grace. If the daemon is killed mid-run the
    # lock just expires; the next daemon claim sees an "in past" TTL and
    # legitimately reclaims it.
    if not _try_acquire_lock(conn, project, ttl_seconds=budget_seconds + 30, now=started):
        stats.paused = True
        stats.ended_at = _utcnow()
        return stats

    def _check_pause() -> bool:
        if pause_check is not None and pause_check():
            return True
        if _utcnow() >= deadline:
            return True
        return False

    try:
        # Phase 1
        if _check_pause():
            stats.paused = True
        else:
            stats.transitive_facts_added = _materialize_transitive_facts(
                conn, project, pause_check=_check_pause
            )
            stats.phases.append("transitive")

        # Phase 2
        if not stats.paused and not _check_pause():
            stats.episodes_materialized = _materialize_old_episodes(
                conn, project,
                pause_check=_check_pause,
                embed_fn=embed_fn,
                llm_summarizer=llm_summarizer,
            )
            stats.phases.append("episodes")
        elif _check_pause():
            stats.paused = True

        # Phase 3
        if not stats.paused and not _check_pause():
            stats.duplicates_merged = _cosine_dedup(
                conn, project, pause_check=_check_pause
            )
            stats.phases.append("dedup")
        elif _check_pause():
            stats.paused = True

        # Phase 4
        if not stats.paused and not _check_pause():
            stats.decay_applied = _apply_decay(
                conn, project, pause_check=_check_pause
            )
            stats.phases.append("decay")
        elif _check_pause():
            stats.paused = True

        # Phase 5
        if not stats.paused and not _check_pause():
            stats.summaries_compressed = _compress_long_session_summaries(
                conn, project,
                pause_check=_check_pause,
                llm_summarizer=llm_summarizer,
            )
            stats.phases.append("summaries")
        elif _check_pause():
            stats.paused = True

    except Exception as exc:  # noqa: BLE001 — daemon must never crash the process
        stats.error = f"{type(exc).__name__}: {exc}"
        log.exception("consolidation_failed", extra={"project": project})
        stats.ended_at = _utcnow()
        _release_lock(conn, project, status="failed", stats=stats, error=stats.error)
        return stats

    stats.ended_at = _utcnow()
    final_status = "paused" if stats.paused else "ok"
    _release_lock(conn, project, status=final_status, stats=stats)
    return stats


# ─── public: run_daemon ────────────────────────────────────────────────


def list_idle_projects_by_knowledge(
    conn: sqlite3.Connection,
    *,
    idle_seconds: int = 1800,
    consolidate_cooldown_seconds: int = 21600,
    now: datetime | None = None,
) -> list[str]:
    """Idle projects judged by their newest knowledge row, oldest first.

    The personal server never writes ``project_activity`` (only the team
    daemon does), so the hourly reflection job decides idleness from the
    knowledge table itself: a project whose newest active record is older
    than ``idle_seconds`` and that was not consolidated within the cooldown.
    """
    when = now or _utcnow()
    idle_cutoff = _iso(when - timedelta(seconds=idle_seconds))
    cooldown_cutoff = _iso(when - timedelta(seconds=consolidate_cooldown_seconds))
    rows = conn.execute(
        """
        SELECT k.project, MAX(k.created_at) AS newest
        FROM knowledge k
        LEFT JOIN consolidation_state cs ON cs.project = k.project
        WHERE COALESCE(k.status, 'active') = 'active'
          AND k.project IS NOT NULL AND k.project <> ''
          AND (cs.last_consolidated_at IS NULL OR cs.last_consolidated_at < ?)
        GROUP BY k.project
        HAVING newest < ?
        ORDER BY newest ASC, k.project ASC
        """,
        (cooldown_cutoff, idle_cutoff),
    ).fetchall()
    return [str(row[0]) for row in rows]


def consolidate_idle_projects(
    conn: sqlite3.Connection,
    *,
    budget_seconds: int = 120,
    max_projects: int = 3,
    idle_seconds: int = 1800,
    consolidate_cooldown_seconds: int = 21600,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    llm_summarizer: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> list[ConsolidationStats]:
    """One bounded consolidation sweep for a scheduled job (the reflection runner).

    Takes up to ``max_projects`` idle projects, oldest first, and shares
    ``budget_seconds`` of wall clock between them. Each project gets the
    remaining budget; the sweep stops when it is used up.
    """
    started = _utcnow()
    deadline = started + timedelta(seconds=budget_seconds)
    results: list[ConsolidationStats] = []
    candidates = list_idle_projects_by_knowledge(
        conn, idle_seconds=idle_seconds, consolidate_cooldown_seconds=consolidate_cooldown_seconds, now=now)
    for project in candidates[:max_projects]:
        remaining = int((deadline - _utcnow()).total_seconds())
        if remaining <= 0:
            break
        stats = consolidate_project(
            conn, project,
            budget_seconds=remaining,
            pause_check=None,
            embed_fn=embed_fn,
            llm_summarizer=llm_summarizer,
        )
        results.append(stats)
        log.info(
            "consolidation_sweep_project",
            extra={"project": project, "paused": stats.paused, "error": stats.error,
                   "episodes": stats.episodes_materialized, "duplicates": stats.duplicates_merged,
                   "transitive": stats.transitive_facts_added, "decay": stats.decay_applied},
        )
    return results


def _open_conn(db_path: str) -> sqlite3.Connection:
    """Open a daemon-private connection with WAL + busy timeout.

    WAL mode is what lets recall (read-only) keep flowing while the
    daemon writes. SQLite WAL allows N readers + 1 writer concurrently
    without blocking reads.
    """
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_daemon(
    db_path: str,
    *,
    poll_interval_seconds: int = 60,
    idle_seconds: int = 1800,
    consolidate_cooldown_seconds: int = 21600,
    budget_seconds: int = 600,
    stop_event: threading.Event | None = None,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    llm_summarizer: Callable[[str], str] | None = None,
    on_run: Callable[[ConsolidationStats], None] | None = None,
) -> None:
    """Forever loop: pick oldest idle project, consolidate, sleep, repeat.

    Run-loop semantics:

    * No idle candidates → sleep ``poll_interval_seconds`` and retry.
    * Picks the oldest-touched idle project not in cooldown.
    * Excludes the currently-active project (last touch < 5 min).
    * The pause_check passed to consolidate_project re-checks
      ``is_active`` AND the global stop_event each phase, so an arriving
      user immediately bumps the daemon off their project.
    """
    if stop_event is None:
        stop_event = threading.Event()

    # Re-import locally so test injection of the activity module works.
    from workers import project_activity  # noqa: WPS433

    conn = _open_conn(db_path)
    try:
        while not stop_event.is_set():
            try:
                active = project_activity.get_active_project(conn)
                exclude = [active] if active else []
                candidates = project_activity.list_idle_projects(
                    conn,
                    idle_seconds=idle_seconds,
                    consolidate_cooldown_seconds=consolidate_cooldown_seconds,
                    exclude=exclude,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("daemon_pick_failed")
                _interruptible_sleep(stop_event, poll_interval_seconds)
                continue

            if not candidates:
                _interruptible_sleep(stop_event, poll_interval_seconds)
                continue

            project = candidates[0]

            def _pause() -> bool:
                if stop_event.is_set():
                    return True
                # If the user starts touching this project, abort.
                return _is_active_fn(conn, project, threshold_seconds=300)

            stats = consolidate_project(
                conn, project,
                budget_seconds=budget_seconds,
                pause_check=_pause,
                embed_fn=embed_fn,
                llm_summarizer=llm_summarizer,
            )
            log.info(
                "consolidation_run_complete",
                extra={
                    "project": project,
                    "paused": stats.paused,
                    "error": stats.error,
                    "transitive": stats.transitive_facts_added,
                    "episodes": stats.episodes_materialized,
                    "duplicates": stats.duplicates_merged,
                    "decay": stats.decay_applied,
                    "summaries": stats.summaries_compressed,
                },
            )
            if on_run is not None:
                try:
                    on_run(stats)
                except Exception:  # noqa: BLE001
                    log.exception("on_run_callback_failed")

            # Short pause between projects so the loop never spins hot.
            _interruptible_sleep(stop_event, max(1, poll_interval_seconds // 6))
    finally:
        conn.close()


def _interruptible_sleep(stop_event: threading.Event, seconds: float) -> None:
    """Wait up to ``seconds`` but return immediately when stop_event is set."""
    if seconds <= 0:
        return
    stop_event.wait(timeout=seconds)


__all__ = [
    "ConsolidationStats",
    "consolidate_project",
    "run_daemon",
    "DECAY_FACTOR",
    "DECAY_MIN_CONFIDENCE",
    "DECAY_AGE_DAYS",
    "DEDUP_COSINE_THRESHOLD",
    "DEDUP_WINDOW_SIZE",
    "TRANSITIVE_INFERENCE_LIMIT",
    "EPISODE_MIN_FACTS",
    "EPISODE_MIN_AGE_HOURS",
    "SESSION_SUMMARY_TURN_THRESHOLD",
]
