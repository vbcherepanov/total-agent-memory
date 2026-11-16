"""Cross-encoder re-ranking of the fused candidate window.

Hybrid retrieval fuses lexical and vector ranks with RRF. The vector tier
embeds short conversational turns poorly ("Jon: Thanks!" sits near any
question about Jon), and RRF lets that tier pull a lexical first place down
to 27th. A cross-encoder reads query and record together and fixes the order;
its rank is fused into the RRF score rather than replacing it, so a weak
cross-encoder verdict cannot bury a strong lexical match.

A conversational turn often means something only next to its neighbours
("What was it about?" / "It was about acceptance"), so the encoder also reads
each candidate together with the turns before and after it in its session, and
both verdicts join the fused rank. Reading the turn alone keeps a precise first
place; reading it in context finds the answer that the question's words only
match in the neighbouring turn.

The model loads in a background thread. Until it is ready, `mode="auto"`
leaves the order unchanged; `mode="on"` waits for it.
"""
from __future__ import annotations

import logging
import threading
import time
import unicodedata
from collections.abc import Callable, Hashable, Sequence
from typing import TypeVar

from memory_core.exit_join import join_at_exit
from memory_core.retrieval import SearchScope, fetch_active_records
from memory_core.telemetry import counters, op_timer

LOGGER = logging.getLogger(__name__)
RankedItem = TypeVar("RankedItem")
RRF_K = 60
MAX_PAIR_CHARS = 2000
LATIN_SHARE_REQUIRED = 0.6
LOAD_TIMEOUT_SECONDS = 120.0
# Records checked for value updates of each other after ranking (O(n^2) token comparisons).
UPDATE_ORDER_WINDOW = 50
# Neighbour lookups joined with UNION ALL into one statement.
NEIGHBOUR_QUERIES_PER_STATEMENT = 100


def latin_share(text: str) -> float:
    """Share of the letters in `text` that belong to the Latin script."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 1.0
    latin = sum(1 for ch in letters if unicodedata.name(ch, "").startswith("LATIN"))
    return latin / len(letters)


class CrossReranker:
    """Lazily loaded fastembed cross-encoder, shared by every search of one process."""

    def __init__(self, model: str, *, multilingual: bool, window: int, weight: float, context_chars: int = 0,
                 loader: Callable[[str], object] | None = None):
        from config import get_cross_rerank_batch_size, get_cross_rerank_cache_scores, get_cross_rerank_threads
        from memory_core.rerank_cache import RerankScoreCache

        self.model = model
        self.multilingual = multilingual
        self.window = window
        self.weight = weight
        self.context_chars = context_chars
        self._threads = get_cross_rerank_threads()
        self._batch_size = get_cross_rerank_batch_size()
        self._loader = loader or (lambda name: self._load_fastembed(name, batch_size=self._batch_size, threads=self._threads))
        self._encoder = None
        self._error: Exception | None = None
        self._ready = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        self._score_cache = RerankScoreCache(get_cross_rerank_cache_scores())

    @staticmethod
    def _load_fastembed(model: str, *, batch_size: int | None = None, threads: int | None = None):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        from config import get_cross_rerank_batch_size, get_cross_rerank_threads
        from memory_core.fastembed_loader import load_model
        from memory_core.rerank_batch import LengthBatchedEncoder
        from memory_core.multilingual_rerank import MODEL, register_model

        batch_size = get_cross_rerank_batch_size() if batch_size is None else batch_size
        threads = get_cross_rerank_threads() if threads is None else threads
        if model == MODEL:
            register_model()
        encoder = load_model(TextCrossEncoder, model, threads=threads)
        return LengthBatchedEncoder(encoder, batch_size) if batch_size else encoder

    def start(self) -> None:
        """Begin loading the model in the background (idempotent)."""
        with self._lock:
            if self._started:
                return
            self._started = True
        thread = threading.Thread(target=self._load, name="cross-rerank-load", daemon=True)
        thread.start()
        join_at_exit(thread)

    def _load(self) -> None:
        started = time.perf_counter()
        try:
            self._encoder = self._loader(self.model)
            counters.bump("cross_rerank_loaded")
            LOGGER.info("Cross-encoder ready", extra={
                "model": self.model, "seconds": round(time.perf_counter() - started, 2)})
        except Exception as error:  # noqa: BLE001 — any load failure disables re-ranking, search continues
            self._error = error
            counters.bump("cross_rerank_load_failed")
            LOGGER.warning("Cross-encoder unavailable; results keep fused order", extra={
                "model": self.model, "error": str(error)})
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float = LOAD_TIMEOUT_SECONDS) -> bool:
        """Block until the model loaded or failed; True when it can re-rank."""
        self.start()
        self._ready.wait(timeout)
        return self._encoder is not None

    @property
    def ready(self) -> bool:
        """True once the model has loaded; False while loading or after a failed load."""
        return self._encoder is not None

    def applies_to(self, query: str) -> bool:
        return self.multilingual or latin_share(query) >= LATIN_SHARE_REQUIRED

    def rerank(self, query: str, items: Sequence[dict], text_of: Callable[[dict], str], *, wait: bool,
               context_of: Callable[[list[dict]], list[str]] | None = None,
               recency_of: Callable[[dict], tuple] | None = None, cache_only: bool = False) -> list[dict]:
        """`items` in fused order -> the window re-ordered, followed by the rest unchanged.

        `context_of(window)` gives each candidate's text with its neighbouring turns; when
        present, the encoder scores both views and the weight is split between them.
        `recency_of(item)` orders records that update each other's value; see
        `keep_updates_below`.
        """
        if len(items) < 2 or not self.applies_to(query):
            counters.bump("cross_rerank_skipped")
            return list(items)
        if cache_only and not self.ready:
            from memory_core.rerank_cache import ScoreCacheMiss
            raise ScoreCacheMiss("Cross-encoder is not ready")
        if wait:
            self.wait_ready()
        else:
            self.start()
        if self._encoder is None:
            counters.bump("cross_rerank_not_ready")
            return list(items)
        window = list(items[:self.window])
        views = [[text_of(item) for item in window]]
        positions = [index for index, text in enumerate(views[0]) if self.applies_to(text[:MAX_PAIR_CHARS])]
        counters.bump("cross_rerank_unsupported_candidates", len(window) - len(positions))
        if len(positions) < 2:
            counters.bump("cross_rerank_skipped")
            return list(items)
        if context_of is not None:
            views.append(context_of(window))
        ranks = []
        with op_timer("cross_rerank_ms"):
            for texts in views:
                if len(texts) != len(window):
                    raise RuntimeError(f"cross-encoder view has {len(texts)} texts for {len(window)} candidates")
                scores = self._score_cache.score(query, [texts[index][:MAX_PAIR_CHARS] for index in positions],
                                                 self._encoder.rerank, cache_only=cache_only)
                by_score = sorted(range(len(positions)), key=lambda i: (-scores[i], i))
                ranks.append({positions[index]: rank for rank, index in enumerate(by_score)})
        share = self.weight / len(ranks)
        fused = sorted(positions, key=lambda i: (
            -(1 / (RRF_K + i + 1) + sum(share / (RRF_K + rank[i] + 1) for rank in ranks)), i))
        counters.bump("cross_rerank_applied")
        reordered = list(window)
        for position, index in zip(positions, fused):
            reordered[position] = window[index]
        if recency_of is not None:
            reordered = keep_updates_below(reordered, text_of, recency_of)
        return reordered + list(items[self.window:])


def keep_updates_below(ranked: list[RankedItem], text_of: Callable[[RankedItem], str],
                       recency_of: Callable[[RankedItem], tuple], *, older_first: bool = False) -> list[RankedItem]:
    """Put each record below its own later update.

    A web-trained encoder knows the real-world value: it ranks "Windows Vista
    was produced by Microsoft" above the later record "... by Raytheon" that
    replaced it. Records that state different values for the same statement
    (`dedup.updates_value`) form a group; the group keeps the places the
    encoder gave it, filled newest first. Other records keep their places.

    `older_first=True` fills the places oldest first, for a question about the
    earlier value ("what was it before?", `query_shapes.is_historical_value_query`).
    """
    from memory_core.dedup import tokens_update_value, value_tokens
    from memory_core.schedule_updates import schedule_statement, schedule_update

    tokens = [value_tokens(text_of(item)) for item in ranked]
    schedules = [schedule_statement(text_of(item)) for item in ranked]
    parent = list(range(len(ranked)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(ranked)):
        for j in range(i + 1, len(ranked)):
            if root(i) != root(j) and schedule_update(
                schedules[i], schedules[j], tokens_update_value(tokens[i], tokens[j])
            ):
                parent[root(j)] = root(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(ranked)):
        groups.setdefault(root(i), []).append(i)
    out = list(ranked)
    moved = False
    for places in groups.values():
        if len(places) < 2:
            continue
        by_time = sorted(places, key=lambda i: recency_of(ranked[i]), reverse=not older_first)
        for place, source in zip(places, by_time):
            out[place] = ranked[source]
        moved = moved or by_time != places
    if moved:
        counters.bump("update_order_older_first" if older_first else "update_order_restored")
    return out


def order_value_updates(ranked: Sequence[RankedItem], text_of: Callable[[RankedItem], str],
                        recency_of: Callable[[RankedItem], tuple], window: int = UPDATE_ORDER_WINDOW, *,
                        older_first: bool = False) -> list[RankedItem]:
    """`keep_updates_below` over the top `window` records, whatever ranked them; the rest keep their order."""
    return keep_updates_below(list(ranked[:window]), text_of, recency_of, older_first=older_first) + list(
        ranked[window:])


@op_timer("scoped_value_order_ms")
def order_scoped_value_updates(ranked: Sequence[RankedItem], text_of: Callable[[RankedItem], str],
                              recency_of: Callable[[RankedItem], tuple],
                              scope_of: Callable[[RankedItem], Hashable], *,
                              older_first: bool = False) -> list[RankedItem]:
    groups: dict[Hashable, list[int]] = {}
    for index, item in enumerate(ranked):
        groups.setdefault(scope_of(item), []).append(index)
    ordered = list(ranked)
    for places in groups.values():
        values = order_value_updates([ranked[index] for index in places], text_of, recency_of,
                                     older_first=older_first)
        for index, item in zip(places, values):
            ordered[index] = item
    return ordered


def session_window_texts(db, rows: Sequence[dict], *, side_chars: int, scope: SearchScope | None = None) -> dict[int, str]:
    """Record id -> previous turn + record + next turn of the same session."""
    if scope is not None:
        records = fetch_active_records(db, [row["id"] for row in rows], scope=scope)
        rows = [records[row["id"]] for row in rows if row["id"] in records]
    predicates, scope_params = (scope or SearchScope()).sql()
    anchors = [row for row in rows if row.get("session_id") and row.get("created_at")]
    if not anchors:
        return {}
    parts, params = [], []
    for row in anchors:
        # "project = ?" / "IS NULL" rather than "IS ?": PostgreSQL cannot use an index for IS NOT DISTINCT FROM.
        project = row.get("project")
        project_match, project_params = ("k.project IS NULL", []) if project is None else ("k.project=?", [project])
        for side, comparison, direction in ((0, "<", "DESC"), (1, ">", "ASC")):
            parts.append(
                "SELECT * FROM (SELECT ? AS a, ? AS s, k.content FROM knowledge k "
                f"WHERE k.session_id=? AND {project_match} AND {' AND '.join(predicates)} "
                f"AND (k.created_at, k.id) {comparison} (?, ?) ORDER BY k.created_at {direction}, k.id {direction} LIMIT 1)")
            params.append([row["id"], side, row["session_id"], *project_params, *scope_params, row["created_at"], row["id"]])
    around: dict[tuple[int, int], str] = {}
    for offset in range(0, len(parts), NEIGHBOUR_QUERIES_PER_STATEMENT):
        batch = params[offset:offset + NEIGHBOUR_QUERIES_PER_STATEMENT]
        for a, s, content in db.execute(" UNION ALL ".join(parts[offset:offset + NEIGHBOUR_QUERIES_PER_STATEMENT]),
                                        [value for part in batch for value in part]).fetchall():
            around[(a, s)] = content or ""
    out = {}
    for row in anchors:
        before = around.get((row["id"], 0), "")[-side_chars:]
        after = around.get((row["id"], 1), "")[:side_chars]
        out[row["id"]] = "\n".join(t for t in (before, row.get("content", ""), after) if t)
    return out


_shared: CrossReranker | None = None
_shared_lock = threading.Lock()


def shared_reranker() -> CrossReranker | None:
    """Process-wide re-ranker built from config, or None when disabled."""
    global _shared
    from config import (
        MULTILINGUAL_CROSS_RERANK_MODELS,
        get_cross_rerank_batch_size,
        get_cross_rerank_cache_scores,
        get_cross_rerank_context,
        get_cross_rerank_mode,
        get_cross_rerank_model,
        get_cross_rerank_threads,
        get_cross_rerank_weight,
        get_cross_rerank_window,
    )

    if get_cross_rerank_mode() == "off":
        return None
    with _shared_lock:
        model, window, weight = get_cross_rerank_model(), get_cross_rerank_window(), get_cross_rerank_weight()
        context = get_cross_rerank_context()
        capacity = get_cross_rerank_cache_scores()
        batch_size = get_cross_rerank_batch_size()
        threads = get_cross_rerank_threads()
        if _shared is None or (_shared.model, _shared.window, _shared.weight, _shared.context_chars,
                              _shared._score_cache.max_scores, _shared._batch_size, _shared._threads) != (
                                  model, window, weight, context, capacity, batch_size, threads):
            _shared = CrossReranker(model, multilingual=model in MULTILINGUAL_CROSS_RERANK_MODELS,
                                    window=window, weight=weight, context_chars=context)
        return _shared
