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
in try/except and logged to `last_error`. A failed row goes back to
`pending` parked behind an exponential backoff (`next_attempt_at`), so a
short provider outage does not burn through its attempts. Failures caused
by an unreachable provider ("deferrals") are not charged as attempts at
all. Rows that exhaust `MEMORY_ENRICH_MAX_ATTEMPTS` become `failed`; they
are re-queued automatically after `MEMORY_ENRICH_REQUEUE_AFTER_SEC` (up to
`MAX_REQUEUES` times) or on demand with `retry_failed()` /
`python -m enrichment_worker --retry-failed`.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from memory_core.exit_join import join_at_exit
from memory_core.leases import LeaseHeartbeat, LeaseLost, assert_owned, enqueue_rebuilds
from memory_core.telemetry import counters, op_timer
from llm_provider import ProviderUnavailable
from paths import memory_dir
from queue_retry import MAX_DEFERRALS, ensure_column, next_attempt_iso, now_iso

LOG = lambda msg: sys.stderr.write(f"[enrich-worker] {msg}\n")


# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────


def _enabled() -> bool:
    return os.environ.get("MEMORY_ASYNC_ENRICHMENT", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


class AsyncEnrichmentUnsupported(RuntimeError):
    """MEMORY_ASYNC_ENRICHMENT is on for a PostgreSQL workspace store."""


def reject_postgres() -> None:
    """Refuse async enrichment on PostgreSQL.

    The worker thread shares the Store's connection, and a psycopg connection
    shared across threads shares its transaction state; the SQLite path opens a
    second connection to memory.db, which a PostgreSQL workspace does not have.
    """
    if _enabled():
        raise AsyncEnrichmentUnsupported(
            "MEMORY_ASYNC_ENRICHMENT=true is not supported on a PostgreSQL workspace; set it to false"
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


def _requeue_after_sec() -> int:
    """Seconds a `failed` row waits before it is re-queued automatically (0 disables)."""
    raw = os.environ.get("MEMORY_ENRICH_REQUEUE_AFTER_SEC")
    if not raw:
        return DEFAULT_REQUEUE_AFTER_SEC
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_REQUEUE_AFTER_SEC


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


DEFAULT_REQUEUE_AFTER_SEC = 6 * 3600
MAX_REQUEUES = 3
ERROR_TEXT_LIMIT = 500
BUSY_TIMEOUT_SEC = 30.0

# Errors that mean "the provider is unreachable right now", not "this row is bad".
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ProviderUnavailable, urllib.error.URLError, ConnectionError, TimeoutError,
)


def _ensure_schema(db) -> None:
    """Create the retry columns / stage table on a database that predates migration 045."""
    ensure_column(db, "enrichment_queue", "next_attempt_at", "TEXT")
    ensure_column(db, "enrichment_queue", "deferrals", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "enrichment_queue", "requeues", "INTEGER NOT NULL DEFAULT 0")
    db.execute(
        "CREATE TABLE IF NOT EXISTS enrichment_stage_done ("
        "queue_id INTEGER NOT NULL, stage TEXT NOT NULL, done_at TEXT NOT NULL, "
        "PRIMARY KEY (queue_id, stage))"
    )
    db.commit()


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
    lease_token: str = ""
    atomic_only: bool = False
    deferred: bool = False  # set by process_task when only provider outages caused the failure
    outcome: str = ""  # 'pending' | 'failed', set by _mark_failed_or_retry


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
    _ensure_schema(db)
    # A row that kills the process never reaches _mark_failed_or_retry, so its
    # claim already counted as an attempt: past the limit it must stop here
    # instead of crash-looping the server on every restart.
    cur = db.execute(
        """UPDATE enrichment_queue
              SET status=CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END,
                  finished_at=CASE WHEN attempts >= ? THEN ? ELSE finished_at END,
                  lease_token=NULL, heartbeat_at=NULL,
                  last_error='reclaimed: previous worker did not finish in time'
            WHERE status='processing'
              AND started_at IS NOT NULL
              AND COALESCE(heartbeat_at, started_at) < ?""",
        (_max_attempts(), _max_attempts(), _now(), cutoff_iso),
    )
    db.commit()
    return cur.rowcount or 0


def fail_exhausted_pending(db) -> int:
    """Move `pending` rows already at the attempt limit to `failed`.

    Lowering MEMORY_ENRICH_MAX_ATTEMPTS leaves such rows unclaimable; as `failed`
    they follow the normal cool-down / requeue path instead of waiting forever.
    """
    _ensure_schema(db)
    cur = db.execute(
        """UPDATE enrichment_queue
              SET status='failed', finished_at=?, next_attempt_at=NULL,
                  last_error=COALESCE(last_error, 'attempt limit lowered below recorded attempts')
            WHERE status='pending' AND attempts >= ?""",
        (_now(), _max_attempts()),
    )
    db.commit()
    return cur.rowcount or 0


def requeue_failed_due(db) -> int:
    """Re-queue `failed` rows whose cool-down elapsed (bounded by MAX_REQUEUES)."""
    delay = _requeue_after_sec()
    if delay <= 0:
        return 0
    _ensure_schema(db)
    cutoff = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - delay, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    cur = db.execute(
        """UPDATE enrichment_queue
              SET status='pending', attempts=0, deferrals=0, requeues=requeues + 1,
                  next_attempt_at=NULL, finished_at=NULL
            WHERE status='failed' AND requeues < ? AND finished_at IS NOT NULL
              AND finished_at <= ?""",
        (MAX_REQUEUES, cutoff),
    )
    db.commit()
    return cur.rowcount or 0


def retry_failed(db, *, project: str | None = None, limit: int | None = None) -> int:
    """Put `failed` rows back in the queue right now. Returns the number re-queued."""
    _ensure_schema(db)
    sql = ("SELECT id FROM enrichment_queue WHERE status='failed'"
           + (" AND project=?" if project else "") + " ORDER BY id"
           + (" LIMIT ?" if limit else ""))
    params = [p for p in (project, limit) if p]
    ids = [row[0] for row in db.execute(sql, params).fetchall()]
    for task_id in ids:
        db.execute(
            "UPDATE enrichment_queue SET status='pending', attempts=0, deferrals=0, "
            "next_attempt_at=NULL, finished_at=NULL WHERE id=? AND status='failed'",
            (task_id,),
        )
    db.commit()
    return len(ids)


def _claim_pending(db, limit: int) -> list[EnrichmentTask]:
    """Atomically move up to `limit` pending rows to 'processing'.

    Uses one UPDATE statement to avoid two workers grabbing the same row
    when running multi-threaded (defensive — by default there is only
    one daemon thread per Store).
    """
    _ensure_schema(db)
    now = _now()
    due = now_iso()
    token = uuid.uuid4().hex
    cur = db.execute(
        """UPDATE enrichment_queue
              SET status='processing',
                  started_at=?, heartbeat_at=?, lease_token=?,
                  attempts = attempts + 1
            WHERE id IN (
                SELECT id FROM enrichment_queue
                 WHERE status='pending'
                   AND attempts < ?
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              ORDER BY enqueued_at ASC
                 LIMIT ?
            )
        RETURNING id, knowledge_id, session_id, project, ktype,
                  content_snapshot, tags_snapshot, importance, skip_quality,
                  attempts, lease_token, atomic_only""",
        (now, now, token, _max_attempts(), due, limit),
    )
    rows = cur.fetchall()
    db.commit()
    out: list[EnrichmentTask] = []
    for row in rows:
        try:
            tags = json.loads(row[6]) if row[6] else []
        except (json.JSONDecodeError, TypeError) as error:
            LOG(f"Invalid queue tags for task {row[0]}: {error}")
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
            lease_token=row[10],
            atomic_only=bool(row[11]),
        ))
    return out


def _mark_done(db, task: EnrichmentTask) -> bool:
    cursor = db.execute(
        "UPDATE enrichment_queue SET status='done', finished_at=?, last_error=NULL, "
        "lease_token=NULL WHERE id=? AND lease_token=? AND status='processing'",
        (_now(), task.id, task.lease_token),
    )
    if cursor.rowcount == 1:
        db.execute("DELETE FROM enrichment_stage_done WHERE queue_id=?", (task.id,))
    db.commit()
    return cursor.rowcount == 1


def _deferral_count(db, task_id: int) -> int:
    row = db.execute("SELECT deferrals FROM enrichment_queue WHERE id=?", (task_id,)).fetchone()
    return (row[0] if row else 0) or 0


def _mark_failed_or_retry(db, task: EnrichmentTask, error: str) -> bool:
    """Record a failed run: park the row behind a backoff, or fail it for good.

    A provider outage (`task.deferred`) is not the row's fault: it does not use
    up an attempt, only a bounded number of deferrals.
    """
    attempts = task.attempts
    deferrals = _deferral_count(db, task.id)
    if task.deferred and deferrals + 1 < MAX_DEFERRALS:
        attempts = max(0, attempts - 1)
        deferrals += 1
        backoff_step = deferrals
    else:
        backoff_step = attempts
    next_status = "failed" if attempts >= _max_attempts() else "pending"
    due = next_attempt_iso(backoff_step) if next_status == "pending" else None
    cursor = db.execute(
        "UPDATE enrichment_queue SET status=?, last_error=?, finished_at=?, lease_token=NULL, "
        "attempts=?, deferrals=?, next_attempt_at=? "
        "WHERE id=? AND lease_token=? AND status='processing'",
        (next_status, error[:ERROR_TEXT_LIMIT], _now() if next_status == "failed" else None,
         attempts, deferrals, due, task.id, task.lease_token),
    )
    db.commit()
    task.outcome = next_status
    return cursor.rowcount == 1


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
        from quality_gate import log_decision, score_quality
    except Exception as e:
        LOG(f"quality_gate import failed: {e}")
        return
    score = score_quality(task.content_snapshot, ktype=task.ktype, project=task.project)
    if score.retryable:
        # An LLM failure is not a verdict on the content: re-run the gate later.
        raise ProviderUnavailable(f"quality gate could not score: {score.reason}")
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
            apply_and_log as _cd_apply,
        )
        from contradiction_detector import (
            detect_contradictions as _cd_detect,
        )
        from contradiction_detector import (
            production_candidates_query as _cd_fetch,
        )
        from contradiction_detector import (
            production_llm_call as _cd_llm,
        )
        from contradiction_detector import (
            should_run as _cd_should_run,
        )
        from contradiction_detector import LLM_UNAVAILABLE_REASON
    except Exception as e:
        LOG(f"contradiction_detector import failed: {e}")
        return
    ok, reason = _cd_should_run(task.ktype)
    if not ok and reason == LLM_UNAVAILABLE_REASON:
        raise ProviderUnavailable(reason)
    if not ok or store is None:
        return
    embs = store.embed([task.content_snapshot])
    if not embs:
        return
    if getattr(store, "thread_bound", False):
        from memory_core.vector_search import VectorScope, VectorSearch
        cand_pool = VectorSearch(db, max_cache_bytes=0).search(
            embs[0], VectorScope(dimension=len(embs[0]), project=task.project), candidates=50, limit=10)
    else:
        cand_pool = store._binary_search(
            embs[0], n_candidates=50, project=task.project, n_results=10)
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
        failed = [v for v in verdicts if v.retryable]
        if failed:
            raise ProviderUnavailable(
                f"contradiction check failed for {len(failed)} candidate(s): {failed[0].reason}"
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
def _run_atomic_facts(db, task: EnrichmentTask, store=None) -> None:
    if os.environ.get("MEMORY_ATOMIC_FACTS_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return
    import config
    from ai_layer.atomic_fact_extractor import FactExtractor
    from llm_provider import make_provider
    from memory_core.atomic_facts import FactRepository

    FactExtractor(FactRepository(db), make_provider(config.get_phase_provider("enrich")),
                  config.get_phase_model("enrich"),
                  before_commit=(lambda: assert_owned(db, task.id, task.lease_token))
                  if getattr(task, "lease_token", "") else None).extract(task.knowledge_id)


def _run_passage_index(db, task: EnrichmentTask, store=None) -> None:
    if os.environ.get('MEMORY_PASSAGE_INDEX_ENABLED', 'false').lower() not in ('true', '1', 'yes'):
        return
    from memory_core.embeddings import EmbeddingProvider
    from memory_core.passage_index import PASSAGE_CHARS, PassageIndex
    row = db.execute("SELECT * FROM knowledge WHERE id=? AND status='active'", (task.knowledge_id,)).fetchone()
    if row is None or len(row['content']) <= PASSAGE_CHARS:
        return
    provider = store.evidence_embedder if store is not None else EmbeddingProvider()
    PassageIndex(db, provider.embed_texts, provider.active_model()).ensure([dict(row)])


_STAGES: list[tuple[str, Callable[..., None]]] = [
    ("quality_gate", _run_quality_gate),
    ("entity_dedup", _run_entity_dedup_audit),
    ("contradiction", _run_contradiction_detector),
    ("atomic_facts", _run_atomic_facts),
    ("passage_index", _run_passage_index),
    ("episodic", _run_episodic_event),
    ("wiki", _run_wiki_refresh),
]


def process_task(db, task: EnrichmentTask, *, store=None) -> tuple[bool, str | None]:
    """Run all stages for one task. Returns (ok, error_or_None).

    Per-stage errors are collected and reported as a single string so the
    queue row can be retried; one bad stage does not nuke the others.
    """
    errors: list[str] = []
    transient_only = True
    stages = [("atomic_facts", _run_atomic_facts)] if task.atomic_only else _STAGES
    _ensure_schema(db)
    done = {row[0] for row in db.execute(
        "SELECT stage FROM enrichment_stage_done WHERE queue_id=?", (task.id,)).fetchall()}
    for label, runner in stages:
        if label in done:
            continue
        try:
            if task.lease_token:
                assert_owned(db, task.id, task.lease_token)
            with op_timer(f"enrichment_{label}_ms"):
                runner(db, task, store=store)
            db.execute(
                "INSERT OR IGNORE INTO enrichment_stage_done (queue_id, stage, done_at) VALUES (?, ?, ?)",
                (task.id, label, _now()),
            )
            db.commit()
        except LeaseLost:
            raise
        except Exception as e:
            LOG(f"stage {label!r} failed for kid={task.knowledge_id}: {e}")
            errors.append(f"{label}: {e}")
            transient_only = transient_only and isinstance(e, TRANSIENT_ERRORS)
    task.deferred = bool(errors) and transient_only
    if errors:
        return False, "; ".join(errors)
    return True, None


def run_pending(db, *, max_rows: int | None = None, store=None) -> dict[str, int]:
    """Drain the queue once. Returns counts {claimed,done,retried,failed,reclaimed}.

    Tests call this directly to bypass the daemon thread.
    """
    # Reclaim rows whose previous worker died mid-stage. Cheap: indexed
    # UPDATE that touches only stale 'processing' rows.
    if os.environ.get("MEMORY_ATOMIC_FACTS_ENABLED", "false").lower() in ("true", "1", "yes"):
        enqueue_rebuilds(db, max_rows if max_rows is not None else _batch_size(), _now)
    reclaimed = reclaim_stale(db)
    fail_exhausted_pending(db)
    requeue_failed_due(db)
    limit = max_rows if max_rows is not None else _batch_size()
    tasks = _claim_pending(db, limit)
    counts = {
        "claimed": len(tasks), "done": 0, "retried": 0,
        "failed": 0, "reclaimed": reclaimed,
    }
    with LeaseHeartbeat(db, [(task.id, task.lease_token) for task in tasks],
                        _stale_after_sec() / 3, logging.getLogger(__name__)):
        for task in tasks:
            try:
                ok, err = process_task(db, task, store=store)
                if ok:
                    updated = _mark_done(db, task)
                    if updated:
                        counts["done"] += 1
                else:
                    updated = _mark_failed_or_retry(db, task, err or "unknown")
                    if updated:
                        counts["failed" if task.outcome == "failed" else "retried"] += 1
                if not updated:
                    counters.bump("enrichment_lease_lost")
            except LeaseLost as error:
                logging.getLogger(__name__).warning("Enrichment ownership changed", extra={
                    "task_id": task.id, "error": str(error)})
    return counts


# ──────────────────────────────────────────────
# Daemon thread
# ──────────────────────────────────────────────


class _WorkerThread(threading.Thread):
    def __init__(self, store):
        super().__init__(daemon=True, name="enrich-worker")
        self._store = store
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _open_db(self):
        """Private connection to the same database file.

        Sharing the Store's Connection across threads corrupts sqlite3's
        implicit transaction state — the worker's writes and the main thread's
        writes interleave into "cannot start a transaction within a
        transaction". WAL mode is what makes two connections to one file the
        right answer here.

        Falls back to the shared connection when the Store predates `db_path`
        (or is a test double), preserving previous behaviour rather than
        refusing to start.
        """
        db_path = getattr(self._store, "db_path", None)
        if db_path is None:
            return self._store.db, False
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn, True

    def run(self) -> None:
        tick = _tick_interval()
        try:
            db, owned = self._open_db()
        except Exception as e:
            LOG(f"could not open worker connection: {e}")
            return
        LOG(f"started (tick={tick}s, batch={_batch_size()})")
        try:
            while not self._stop_event.is_set():
                try:
                    run_pending(db, store=self._store)
                except Exception as e:
                    LOG(f"tick error: {e}")
                self._stop_event.wait(tick)
        finally:
            if owned:
                try:
                    db.close()
                except Exception:
                    pass


def start_worker(store) -> _WorkerThread | None:
    """Launch the daemon thread if async enrichment is enabled."""
    if not _enabled():
        return None
    t = _WorkerThread(store)
    t.start()
    join_at_exit(t, stop=t.stop)  # its tasks embed through onnxruntime; see memory_core/exit_join.py
    return t


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m enrichment_worker --retry-failed [--project NAME]``."""
    import argparse

    from paths import memory_db

    parser = argparse.ArgumentParser(prog="enrichment_worker", description=__doc__.splitlines()[0])
    parser.add_argument("--retry-failed", action="store_true",
                        help="put every failed enrichment row back in the queue")
    parser.add_argument("--project", default=None, help="limit --retry-failed to one project")
    parser.add_argument("--limit", type=int, default=None, help="maximum rows to re-queue")
    args = parser.parse_args(argv)
    if not args.retry_failed:
        parser.print_usage(sys.stderr)
        return 2
    db = sqlite3.connect(str(memory_db()), timeout=BUSY_TIMEOUT_SEC)
    try:
        count = retry_failed(db, project=args.project, limit=args.limit)
    finally:
        db.close()
    sys.stdout.write(f"re-queued {count} failed enrichment row(s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
