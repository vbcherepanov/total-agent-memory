"""v10.1 — Async enrichment worker (inbox/outbox style).

Sync hot-path of `save_knowledge` is fast: privacy → canonical_tags →
INSERT → embed → graph link → enqueue → return.

This worker picks rows out of `enrichment_queue` and runs the heavy
LLM-bound stages: quality gate, entity-dedup audit, contradiction
detector, episodic event linking, wiki auto-refresh.

The worker is **opt-in** by default; the sync path runs the legacy
inline pipeline unless `MEMORY_ASYNC_ENRICHMENT=true`. A daemon
thread is started by `Store.__init__` when the flag is on; tests can
also drive it deterministically through `run_pending(db, max_rows=N)`.

Idempotency
-----------
Each stage is idempotent and writes its own audit row. A row may be
processed more than once if the worker is killed mid-stage; downstream
log tables tolerate duplicate inserts (UNIQUE on knowledge_id+stage
where applicable, or a "skip if already present" guard).

Failure isolation
-----------------
A single failing stage does NOT block subsequent rows: each is wrapped
in try/except and logged to `last_error`. Rows that fail >3 times are
marked `failed` and stay out of the active queue until manually retried.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from paths import memory_dir

LOG = lambda msg: sys.stderr.write(f"[enrich-worker] {msg}\n")


# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────


def _enabled() -> bool:
    return os.environ.get("MEMORY_ASYNC_ENRICHMENT", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _tick_interval() -> float:
    raw = os.environ.get("MEMORY_ENRICH_TICK_SEC")
    if not raw:
        return 0.1
    try:
        v = float(raw)
        return max(0.01, min(5.0, v))
    except ValueError:
        return 0.1


def _batch_size() -> int:
    raw = os.environ.get("MEMORY_ENRICH_BATCH")
    if not raw:
        return 5
    try:
        return max(1, min(50, int(raw)))
    except ValueError:
        return 5


def _max_attempts() -> int:
    raw = os.environ.get("MEMORY_ENRICH_MAX_ATTEMPTS")
    if not raw:
        return 3
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _stale_after_sec() -> int:
    """How long a row may sit in 'processing' before we assume the
    worker that claimed it died. Default 60s — well above any healthy
    stage latency, well below any retention concern.
    """
    raw = os.environ.get("MEMORY_ENRICH_STALE_AFTER_SEC")
    if not raw:
        return 60
    try:
        return max(5, int(raw))
    except ValueError:
        return 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ──────────────────────────────────────────────
# Enqueue
# ──────────────────────────────────────────────


@dataclass
class EnrichmentTask:
    id: int
    knowledge_id: int
    session_id: str | None
    project: str
    ktype: str
    content_snapshot: str
    tags_snapshot: list[str]
    importance: str
    skip_quality: bool
    attempts: int


def enqueue(
    db,
    *,
    knowledge_id: int,
    session_id: str | None,
    project: str,
    ktype: str,
    content_snapshot: str,
    tags_snapshot: list[str] | None,
    importance: str = "medium",
    skip_quality: bool = False,
) -> int:
    """Insert a pending row into the enrichment queue. Returns row id."""
    cur = db.execute(
        """INSERT INTO enrichment_queue
             (knowledge_id, session_id, project, ktype,
              content_snapshot, tags_snapshot, importance, skip_quality,
              status, enqueued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            knowledge_id, session_id, project, ktype,
            content_snapshot, json.dumps(list(tags_snapshot or [])),
            importance, 1 if skip_quality else 0,
            _now(),
        ),
    )
    db.commit()
    return cur.lastrowid


# ──────────────────────────────────────────────
# Claim & finalise
# ──────────────────────────────────────────────


def reclaim_stale(db) -> int:
    """Flip 'processing' rows older than MEMORY_ENRICH_STALE_AFTER_SEC
    back to 'pending' so a healthy worker picks them up. Returns the
    number of rows reclaimed.

    Called once at worker start and on every tick — cheap (single
    UPDATE with index on `status`).
    """
    cutoff_sec = _stale_after_sec()
    cutoff_ts = (
        datetime.now(timezone.utc).timestamp() - cutoff_sec
    )
    cutoff_iso = (
        datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    cur = db.execute(
        """UPDATE enrichment_queue
              SET status='pending',
                  last_error='reclaimed: previous worker did not finish in time'
            WHERE status='processing'
              AND started_at IS NOT NULL
              AND started_at < ?""",
        (cutoff_iso,),
    )
    db.commit()
    return cur.rowcount or 0


def _claim_pending(db, limit: int) -> list[EnrichmentTask]:
    """Atomically move up to `limit` pending rows to 'processing'.

    Uses one UPDATE statement to avoid two workers grabbing the same row
    when running multi-threaded (defensive — by default there is only
    one daemon thread per Store).
    """
    now = _now()
    cur = db.execute(
        """UPDATE enrichment_queue
              SET status='processing',
                  started_at=?,
                  attempts = attempts + 1
            WHERE id IN (
                SELECT id FROM enrichment_queue
                 WHERE status='pending'
              ORDER BY enqueued_at ASC
                 LIMIT ?
            )
        RETURNING id, knowledge_id, session_id, project, ktype,
                  content_snapshot, tags_snapshot, importance, skip_quality,
                  attempts""",
        (now, limit),
    )
    rows = cur.fetchall()
    db.commit()
    out: list[EnrichmentTask] = []
    for row in rows:
        try:
            tags = json.loads(row[6]) if row[6] else []
        except Exception:
            tags = []
        out.append(EnrichmentTask(
            id=row[0],
            knowledge_id=row[1],
            session_id=row[2],
            project=row[3] or "general",
            ktype=row[4] or "fact",
            content_snapshot=row[5] or "",
            tags_snapshot=tags,
            importance=row[7] or "medium",
            skip_quality=bool(row[8]),
            attempts=row[9] or 0,
        ))
    return out


def _mark_done(db, task_id: int) -> None:
    db.execute(
        "UPDATE enrichment_queue SET status='done', finished_at=?, last_error=NULL WHERE id=?",
        (_now(), task_id),
    )
    db.commit()


def _mark_failed_or_retry(db, task_id: int, attempts: int, error: str) -> None:
    next_status = "failed" if attempts >= _max_attempts() else "pending"
    db.execute(
        "UPDATE enrichment_queue SET status=?, last_error=?, finished_at=? WHERE id=?",
        (next_status, error[:500], _now() if next_status == "failed" else None, task_id),
    )
    db.commit()


# ──────────────────────────────────────────────
# Stage runners (each idempotent, each fail-isolated)
# ──────────────────────────────────────────────


def _run_quality_gate(db, task: EnrichmentTask, store=None) -> None:
    """Score the record async. On 'drop' verdict, mark the knowledge row
    as `quality_dropped` rather than physically removing it (we already
    committed an INSERT in the sync path).
    """
    if task.skip_quality:
        return
    try:
        from quality_gate import score_quality, log_decision
    except Exception as e:
        LOG(f"quality_gate import failed: {e}")
        return
    score = score_quality(task.content_snapshot, ktype=task.ktype, project=task.project)
    log_decision(
        db, score, project=task.project, ktype=task.ktype,
        content=task.content_snapshot, knowledge_id=task.knowledge_id,
    )
    if score.decision == "drop":
        db.execute(
            "UPDATE knowledge SET status='quality_dropped' WHERE id=? AND status='active'",
            (task.knowledge_id,),
        )
        db.commit()
        LOG(
            f"async quality-gate dropped id={task.knowledge_id} "
            f"(score={score.total:.2f}<{score.threshold}, reason={score.reason!r})"
        )


def _run_entity_dedup_audit(db, task: EnrichmentTask, store=None) -> None:
    """Walk the canonicalised tags and persist audit-log rows.

    The actual canonicalisation already happened in the sync path
    (`canonical_tags` is cheap). What's deferred here is the embed-based
    second-pass dedup — comparing leftover free-form tags against
    `graph_nodes` via cosine similarity, which is the FastEmbed-heavy
    part. We re-run the cheap path here too so audit log rows reflect
    the ground truth at processing time.
    """
    try:
        import entity_dedup as _ed
    except Exception as e:
        LOG(f"entity_dedup import failed: {e}")
        return
    if not _ed._enabled() or not task.tags_snapshot:
        return
    cand_pool = _ed.production_candidates_query(db, project=task.project)
    if not cand_pool:
        return
    if store is not None:
        embed_fn = lambda texts: store.embed(texts) or None
    else:
        return  # cannot embed without store — skip
    _, decisions = _ed.canonicalize_entity_tags(
        task.tags_snapshot, candidates=cand_pool, embed_fn=embed_fn,
    )
    if decisions:
        _ed.log_decisions(
            db, decisions,
            knowledge_id=task.knowledge_id, project=task.project,
        )


def _run_contradiction_detector(db, task: EnrichmentTask, store=None) -> None:
    """Re-run the contradiction sweep against existing same-type records."""
    try:
        from contradiction_detector import (
            should_run as _cd_should_run,
            detect_contradictions as _cd_detect,
            production_candidates_query as _cd_fetch,
            production_llm_call as _cd_llm,
            apply_and_log as _cd_apply,
        )
    except Exception as e:
        LOG(f"contradiction_detector import failed: {e}")
        return
    ok, reason = _cd_should_run(task.ktype)
    if not ok or store is None:
        return
    embs = store.embed([task.content_snapshot])
    if not embs:
        return
    cand_pool = store._binary_search(
        embs[0], n_candidates=50, project=task.project, n_results=10
    )
    cand_pool = [(cid, cos) for cid, cos in cand_pool if cid != task.knowledge_id]
    if not cand_pool:
        return
    verdicts = _cd_detect(
        task.content_snapshot,
        ktype=task.ktype, project=task.project,
        candidate_pool=cand_pool,
        fetch_candidates=lambda ids: _cd_fetch(
            db, project=task.project, ktype=task.ktype, candidate_ids=ids
        ),
        llm_fn=_cd_llm,
    )
    if verdicts:
        counts = _cd_apply(db, verdicts, new_id=task.knowledge_id)
        if counts.get("superseded"):
            LOG(
                f"async contradiction superseded {counts['superseded']} "
                f"record(s) on save id={task.knowledge_id}"
            )


def _run_episodic_event(db, task: EnrichmentTask, store=None) -> None:
    """Spawn an Event node + MENTIONED_IN edges to entity nodes."""
    try:
        import episodic as _ep
    except Exception as e:
        LOG(f"episodic import failed: {e}")
        return
    _ep.record_save_event(
        db, knowledge_id=task.knowledge_id,
        project=task.project, session_id=task.session_id or "",
    )


def _run_wiki_refresh(db, task: EnrichmentTask, store=None) -> None:
    """Maybe regenerate <MEMORY_DIR>/wikis/<project>.md.

    Off by default (`MEMORY_WIKI_AUTO_REFRESH_EVERY_N=0`), so this is
    typically a cheap no-op. When enabled, runs only every N saves.
    """
    try:
        import project_wiki as _pw
    except Exception as e:
        LOG(f"project_wiki import failed: {e}")
        return
    output_dir = str(memory_dir())
    _pw.maybe_auto_refresh(
        db, project=task.project, save_count=task.knowledge_id,
        output_dir=os.path.join(output_dir, "wikis"),
    )


# Stages run in deterministic order. quality_gate first so a 'drop' is
# visible to a follow-up read before contradiction_detector spends LLM
# budget on a record we are about to soft-drop anyway.
_STAGES: list[tuple[str, Callable[..., None]]] = [
    ("quality_gate", _run_quality_gate),
    ("entity_dedup", _run_entity_dedup_audit),
    ("contradiction", _run_contradiction_detector),
    ("episodic", _run_episodic_event),
    ("wiki", _run_wiki_refresh),
]


def process_task(db, task: EnrichmentTask, *, store=None) -> tuple[bool, str | None]:
    """Run all stages for one task. Returns (ok, error_or_None).

    Per-stage errors are collected and reported as a single string so the
    queue row can be retried; one bad stage does not nuke the others.
    """
    errors: list[str] = []
    for label, runner in _STAGES:
        try:
            runner(db, task, store=store)
        except Exception as e:
            LOG(f"stage {label!r} failed for kid={task.knowledge_id}: {e}")
            errors.append(f"{label}: {e}")
    if errors:
        return False, "; ".join(errors)
    return True, None


def run_pending(db, *, max_rows: int | None = None, store=None) -> dict[str, int]:
    """Drain the queue once. Returns counts {claimed,done,retried,failed,reclaimed}.

    Tests call this directly to bypass the daemon thread.
    """
    # Reclaim rows whose previous worker died mid-stage. Cheap: indexed
    # UPDATE that touches only stale 'processing' rows.
    reclaimed = reclaim_stale(db)
    limit = max_rows if max_rows is not None else _batch_size()
    tasks = _claim_pending(db, limit)
    counts = {
        "claimed": len(tasks), "done": 0, "retried": 0,
        "failed": 0, "reclaimed": reclaimed,
    }
    for task in tasks:
        ok, err = process_task(db, task, store=store)
        if ok:
            _mark_done(db, task.id)
            counts["done"] += 1
        else:
            _mark_failed_or_retry(db, task.id, task.attempts, err or "unknown")
            if task.attempts >= _max_attempts():
                counts["failed"] += 1
            else:
                counts["retried"] += 1
    return counts


# ──────────────────────────────────────────────
# Daemon thread
# ──────────────────────────────────────────────


class _WorkerThread(threading.Thread):
    def __init__(self, store):
        super().__init__(daemon=True, name="enrich-worker")
        self._store = store
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        tick = _tick_interval()
        LOG(f"started (tick={tick}s, batch={_batch_size()})")
        while not self._stop.is_set():
            try:
                run_pending(self._store.db, store=self._store)
            except Exception as e:
                LOG(f"tick error: {e}")
            self._stop.wait(tick)


def start_worker(store) -> _WorkerThread | None:
    """Launch the daemon thread if async enrichment is enabled."""
    if not _enabled():
        return None
    t = _WorkerThread(store)
    t.start()
    return t
