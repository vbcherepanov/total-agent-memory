"""Contract of ``POST /internal/hook/recall``: warm per-prompt recall for client hooks.

Request (JSON object, ``Content-Type: application/json``, at most
``MAX_BODY_BYTES``; unknown keys are rejected so typos surface)::

    {
      "query":        string, required, non-empty. Longer than
                      MAX_QUERY_CHARS is cut to that length.
      "project":      string | null, optional (<= MAX_ID_CHARS). Restricts
                      recall to one project; null/absent searches all.
      "session_id":   string | null, optional (<= MAX_ID_CHARS). The client
                      session. With "dedupe", records already returned to the
                      same session are not repeated.
      "budget_chars": integer, optional, BUDGET_MIN..BUDGET_MAX,
                      default DEFAULT_BUDGET_CHARS. Upper bound on len(text).
      "limit":        integer, optional, 1..MAX_LIMIT, default DEFAULT_LIMIT.
                      Records requested from memory_recall.
      "dedupe":       boolean, optional, default true.
    }

Response 200::

    {
      "ok":           true,
      "text":         string. Compact block ready to inject into a prompt;
                      "" when nothing relevant (or new) was found.
      "count":        integer, records included in "text".
      "ids":          [integer], their memory ids, in rank order.
      "skipped_seen": integer, records dropped because this session_id
                      already received them.
      "dropped_floor": integer, records below the relevance floor.
      "truncated":    boolean, true when the budget cut records or content.
      "elapsed_ms":   integer, server-side recall time.
    }

Errors use ``{"ok": false, "error": string}``: 400 invalid request, 401
missing/wrong bearer token, 413 body too large, 503 the store is still
warming up past ``HOOK_READY_TIMEOUT_SEC``, 500 recall failed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_CHARS = 4000
MAX_ID_CHARS = 200
BUDGET_MIN = 200
BUDGET_MAX = 20000
DEFAULT_BUDGET_CHARS = 2000
DEFAULT_LIMIT = 8
MAX_LIMIT = 20
# One record never eats the whole budget: long contents are cut here first.
MAX_RECORD_CHARS = 600
# Remembered sessions for dedupe, and remembered ids per session (both LRU).
MAX_TRACKED_SESSIONS = 256
MAX_SEEN_IDS_PER_SESSION = 1000
ELLIPSIS = "…"
# A record is only worth including with at least this much of its content.
MIN_CONTENT_CHARS = 40
ALLOWED_KEYS = frozenset({"query", "project", "session_id", "budget_chars", "limit", "dedupe"})
DEFAULT_MIN_SCORE = 0.2
DEFAULT_CROSS_PROJECT_MIN_SCORE = 0.6
AUTOMATION_BLOCK = re.compile(
    r'<(task-notification|local-command-stdout|command-name)\b[^>]*>.*?(?:</\1\s*>|$)',
    re.IGNORECASE | re.DOTALL,
)


def user_prompt_text(prompt: str) -> str:
    return AUTOMATION_BLOCK.sub(' ', prompt).strip()


def _score_floor(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
        if math.isfinite(value) and value >= 0:
            return value
    except ValueError:
        logging.getLogger(__name__).warning('Invalid recall score floor', extra={'setting': name})
        return default
    logging.getLogger(__name__).warning('Invalid recall score floor', extra={'setting': name})
    return default


def _score(record: Mapping[str, Any]) -> float:
    try:
        value = float(record.get('score') or 0)
    except (ValueError, TypeError):
        return -math.inf
    return value if math.isfinite(value) else -math.inf


class HookRequestError(ValueError):
    """The request body violates the contract above (HTTP 400)."""


@dataclass(frozen=True)
class HookRecallRequest:
    query: str
    project: str | None
    session_id: str | None
    budget_chars: int
    limit: int
    dedupe: bool

    def recall_arguments(self) -> dict[str, Any]:
        args: dict[str, Any] = {"query": self.query, "limit": self.limit, "detail": "full"}
        if self.project:
            args["project"] = self.project
        return args


def _optional_id(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HookRequestError(f"'{key}' must be a string or null")
    value = value.strip()
    if len(value) > MAX_ID_CHARS:
        raise HookRequestError(f"'{key}' must be at most {MAX_ID_CHARS} characters")
    return value or None


def _bounded_int(data: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HookRequestError(f"'{key}' must be an integer")
    if not low <= value <= high:
        raise HookRequestError(f"'{key}' must be between {low} and {high}")
    return value


def parse_request(body: bytes) -> HookRecallRequest:
    if len(body) > MAX_BODY_BYTES:
        raise HookRequestError(f"request body exceeds {MAX_BODY_BYTES} bytes")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise HookRequestError("request body must be a JSON object") from None
    if not isinstance(data, dict):
        raise HookRequestError("request body must be a JSON object")
    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise HookRequestError(f"unknown field(s): {', '.join(unknown)}")
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HookRequestError("'query' must be a non-empty string")
    dedupe = data.get("dedupe", True)
    if not isinstance(dedupe, bool):
        raise HookRequestError("'dedupe' must be a boolean")
    return HookRecallRequest(
        query=user_prompt_text(query)[:MAX_QUERY_CHARS],
        project=_optional_id(data, "project"),
        session_id=_optional_id(data, "session_id"),
        budget_chars=_bounded_int(data, "budget_chars", DEFAULT_BUDGET_CHARS, BUDGET_MIN, BUDGET_MAX),
        limit=_bounded_int(data, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
        dedupe=dedupe,
    )


class SeenTracker:
    """Which record ids each session already received (bounded, thread-safe)."""

    def __init__(self, max_sessions: int = MAX_TRACKED_SESSIONS, max_ids: int = MAX_SEEN_IDS_PER_SESSION):
        self._sessions: OrderedDict[str, OrderedDict[int, None]] = OrderedDict()
        self._max_sessions = max_sessions
        self._max_ids = max_ids
        self._lock = threading.Lock()

    def seen(self, session_id: str) -> frozenset[int]:
        with self._lock:
            ids = self._sessions.get(session_id)
            if ids is None:
                return frozenset()
            self._sessions.move_to_end(session_id)
            return frozenset(ids)

    def remember(self, session_id: str, record_ids: Iterable[int]) -> None:
        with self._lock:
            ids = self._sessions.setdefault(session_id, OrderedDict())
            self._sessions.move_to_end(session_id)
            for record_id in record_ids:
                ids[record_id] = None
                ids.move_to_end(record_id)
            while len(ids) > self._max_ids:
                ids.popitem(last=False)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)


@dataclass(frozen=True)
class CompactBlock:
    text: str
    ids: list[int]
    skipped_seen: int
    truncated: bool
    dropped_floor: int = 0


def ranked_records(recall_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """memory_recall groups hits by type; flatten them back into one ranking by score."""
    groups = recall_result.get("results")
    if not isinstance(groups, Mapping):
        return []
    records = [dict(item, type=item.get("type") or group)
               for group, items in groups.items() if isinstance(items, list)
               for item in items if isinstance(item, Mapping) and isinstance(item.get("id"), int)]
    records.sort(key=_score, reverse=True)
    return records


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _record_prefix(record: Mapping[str, Any]) -> str:
    created = str(record.get("created_at") or "")[:10]
    label = ", ".join(part for part in (f"{record.get('type', 'memory')} #{record['id']}",
                                        str(record.get("project") or ""), created) if part)
    return f"- ({label}) "


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars - len(ELLIPSIS)].rstrip() + ELLIPSIS, True


def compact_block(recall_result: Mapping[str, Any], request: HookRecallRequest,
                  seen: frozenset[int] = frozenset()) -> CompactBlock:
    """Render memory_recall output as one prompt-ready block of at most `request.budget_chars`."""
    records = ranked_records(recall_result)
    floor = _score_floor('TAM_HOOK_RECALL_MIN_SCORE', DEFAULT_MIN_SCORE)
    cross_floor = max(floor, _score_floor('TAM_HOOK_RECALL_CROSS_PROJECT_MIN_SCORE',
                                         DEFAULT_CROSS_PROJECT_MIN_SCORE))
    eligible = [r for r in records if _score(r) >= (
        cross_floor if request.project and r.get('project') != request.project else floor)]
    dropped_floor = len(records) - len(eligible)
    records = eligible
    fresh = [r for r in records if r["id"] not in seen] if request.dedupe else records
    skipped = len(records) - len(fresh)
    fresh = fresh[:request.limit]
    if not fresh:
        return CompactBlock("", [], skipped, False, dropped_floor)
    header = "[TAM memory: records relevant to this prompt]"
    lines = [header]
    used = len(header)
    ids: list[int] = []
    truncated = False
    for record in fresh:
        prefix = _record_prefix(record)
        room = request.budget_chars - used - 1 - len(prefix)
        if room < MIN_CONTENT_CHARS:
            truncated = True
            break
        content, cut = _clip(_one_line(record.get("content") or ""), min(MAX_RECORD_CHARS, room))
        truncated = truncated or cut
        line = prefix + content
        lines.append(line)
        used += len(line) + 1
        ids.append(record["id"])
    if not ids:
        return CompactBlock("", [], skipped, True, dropped_floor)
    return CompactBlock("\n".join(lines), ids, skipped, truncated, dropped_floor)
