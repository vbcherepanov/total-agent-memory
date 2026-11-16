"""Context injected with every user prompt (UserPromptSubmit): applicable rules + short background.

Two parts share one budget (``TAM_AMBIENT_TOKEN_BUDGET`` tokens, about
``CHARS_PER_TOKEN`` characters each):

* rules — active rules whose text shares at least ``RULE_MIN_MATCHED`` tokens
  (one of them distinctive) with the prompt, ranked by :mod:`rule_surfaces`
  and recorded as surfaces so the Stop hook can check them. At most
  ``RULES_BUDGET_SHARE`` of the budget;
* background — memory records for the prompt. Warm path: the shared daemon's
  ``POST /internal/hook/recall`` (full hybrid recall, see
  ``tam_daemon/hook_recall.py``). Cold path, when no daemon answers in time:
  FTS5 only over the project's and ``general`` records.

Everything already injected into the session (``ambient.state``) is skipped.
The whole build runs against a deadline (``TAM_AMBIENT_LATENCY_MS``): the
daemon call gets what is left minus ``COLD_RESERVE_SEC``, SQLite queries are
interrupted by a progress handler. Running out of time drops a part, it never
fails the prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import correction_capture
from ambient.actions import table_exists
from ambient.settings import AmbientSettings
from ambient.state import SessionState
from rule_surfaces import STOPWORDS, RankedRule, RuleSurfacer
from tam_daemon import hook_recall
from tam_daemon.registry import DaemonPaths, RegistryError, read_record, read_token

LOGGER = logging.getLogger(__name__)

CHARS_PER_TOKEN: Final = 4
RULES_BUDGET_SHARE: Final = 0.5
MIN_PROMPT_CHARS: Final = 8
# Slash commands (/compact, /clear, ...) are client commands, not a task.
COMMAND_PREFIX: Final = "/"
RULE_LIMIT: Final = 3
RULE_MIN_MATCHED: Final = 2
RULE_PREVIEW_CHARS: Final = 280
RULES_HEADER: Final = "[TAM rules that apply to this prompt — follow them]"
WARM_LIMIT: Final = 6
COLD_LIMIT: Final = 4
# Extra FTS rows fetched so records seen earlier in the session can be skipped.
COLD_FETCH_EXTRA: Final = 16
FTS_MAX_TERMS: Final = 8
FTS_MIN_TERM_CHARS: Final = 3
# Time kept back for the FTS fallback when the daemon call times out.
COLD_RESERVE_SEC: Final = 0.3
# SQLite VM steps between deadline checks.
PROGRESS_STEPS: Final = 2000
SQLITE_MAX_WAIT_SEC: Final = 0.5
MS_PER_SEC: Final = 1000
SOURCE_PROMPT: Final = "ambient_prompt"
# Corrections become draft rows here (src/correction_capture.py).
CORRECTIONS_TABLE: Final = "insights"
PROMPT_KEY_CHARS: Final = 16
GENERAL_PROJECT: Final = "general"
_FTS_WORD: Final = re.compile(r"[^\W_]+", re.UNICODE)

BackgroundPath = Literal["warm", "cold", "none"]
WarmRecall = Callable[[Mapping[str, Any], float], Mapping[str, Any] | None]


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + seconds
        self._start = clock()

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def elapsed_ms(self) -> int:
        return int((self._clock() - self._start) * MS_PER_SEC)


@dataclass(frozen=True)
class PromptContext:
    text: str
    rule_ids: tuple[int, ...]
    record_ids: tuple[int, ...]
    background: BackgroundPath
    elapsed_ms: int
    correction_id: int | None = None
    dropped_floor: int = 0


def wants_context(prompt: str) -> bool:
    stripped = hook_recall.user_prompt_text(prompt)
    return len(stripped) >= MIN_PROMPT_CHARS and not stripped.startswith(COMMAND_PREFIX)


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def rule_line(item: RankedRule, preview: int = RULE_PREVIEW_CHARS) -> str:
    priority = int(item.rule.get("priority") or 0)
    return f"- #{item.rule_id} (P{priority}) {_clip(_one_line(item.rule.get('content')), preview)}"


def strong_matches(ranked: Sequence[RankedRule], min_matched: int = RULE_MIN_MATCHED) -> list[RankedRule]:
    """Lexical matches worth showing: enough shared tokens, at least one of them distinctive."""
    return [item for item in ranked if len(item.matched) >= min_matched and item.distinctive]


def select_rules(db: sqlite3.Connection, prompt: str, project: str, state: SessionState,
                 budget_chars: int) -> tuple[list[RankedRule], str]:
    ranked = RuleSurfacer(db).rank_rules(prompt, project, exclude_rule_ids=state.rules, lexical_only=True)
    lines = [RULES_HEADER]
    used = len(RULES_HEADER)
    kept: list[RankedRule] = []
    for item in strong_matches(ranked)[:RULE_LIMIT]:
        line = rule_line(item)
        if used + 1 + len(line) > budget_chars:
            break
        lines.append(line)
        used += 1 + len(line)
        kept.append(item)
    return kept, "\n".join(lines) if kept else ""


# ── warm path: the shared daemon ───────────────────────────────────────────


def daemon_recall(memory_dir: Path) -> WarmRecall | None:
    """A caller of the memory dir's daemon hook endpoint; None when no daemon is registered."""
    paths = DaemonPaths.for_memory_dir(memory_dir)
    try:
        record = read_record(paths)
        token = read_token(paths) if record is not None else ""
    except RegistryError as exc:
        LOGGER.info(json.dumps({"event": "ambient_daemon_unusable", "error": str(exc)}))
        return None
    if record is None:
        return None
    url = f"{record.base_url}/internal/hook/recall"
    # No proxy handler: loopback traffic and the bearer token must never reach an HTTP(S)_PROXY.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(body: Mapping[str, Any], timeout: float) -> Mapping[str, Any] | None:
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            LOGGER.info(json.dumps({"event": "ambient_daemon_rejected", "status": exc.code}))
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            LOGGER.info(json.dumps({"event": "ambient_daemon_unreachable", "error": str(exc)}))
            return None
        return data if isinstance(data, dict) and data.get("ok") is True else None

    return call


def drop_seen_lines(text: str, ids: Sequence[int], seen: frozenset[int]) -> tuple[str, list[int]]:
    """Remove records the session already got from a daemon block (one line per id after the header)."""
    lines = text.split("\n")
    if not ids or len(lines) != len(ids) + 1:
        return ("", []) if seen.intersection(ids) else (text, list(ids))
    kept = [(line, record_id) for line, record_id in zip(lines[1:], ids, strict=True) if record_id not in seen]
    if not kept:
        return "", []
    return "\n".join([lines[0], *(line for line, _ in kept)]), [record_id for _, record_id in kept]


# ── cold path: FTS5 ────────────────────────────────────────────────────────


def fts_terms(prompt: str, limit: int = FTS_MAX_TERMS) -> list[str]:
    """Distinct content words of the prompt, longest (most specific) first."""
    words = dict.fromkeys(word for word in _FTS_WORD.findall(prompt.casefold())
                          if len(word) >= FTS_MIN_TERM_CHARS and word not in STOPWORDS and not word.isdigit())
    return sorted(words, key=len, reverse=True)[:limit]


def fts_query(terms: Sequence[str]) -> str:
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def cold_recall(db: sqlite3.Connection, prompt: str, project: str, session_id: str,
                seen: frozenset[int]) -> dict[str, Any]:
    """memory_recall-shaped result ({"results": {type: [record]}}) from FTS5 alone."""
    terms = fts_terms(prompt)
    if not terms:
        return {"results": {}}
    rows = db.execute(
        "SELECT k.id, k.type, k.content, k.project, k.created_at, bm25(knowledge_fts) AS rank "
        "FROM knowledge_fts JOIN knowledge k ON k.id = knowledge_fts.rowid "
        "WHERE knowledge_fts MATCH ? AND k.status = 'active' AND k.project IN (?, ?) AND k.session_id != ? "
        "ORDER BY rank LIMIT ?",
        (fts_query(terms), project, GENERAL_PROJECT, session_id, COLD_LIMIT + COLD_FETCH_EXTRA)).fetchall()
    groups: dict[str, list[dict[str, Any]]] = {}
    for record_id, kind, content, record_project, created_at, rank in rows:
        if int(record_id) in seen:
            continue
        groups.setdefault(str(kind or "memory"), []).append({
            "id": int(record_id), "type": kind, "content": content, "project": record_project,
            "created_at": created_at,
            "score": len(set(terms) & set(_FTS_WORD.findall(content.casefold()))) / len(terms)})
    return {"results": groups}


# ── assembly ───────────────────────────────────────────────────────────────


def _connect(db_path: Path, deadline: Deadline) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path), timeout=min(SQLITE_MAX_WAIT_SEC, max(deadline.remaining(), 0.01)))
    db.set_progress_handler(lambda: 1 if deadline.expired() else 0, PROGRESS_STEPS)
    return db


def build_prompt_context(*, db_path: Path, memory_dir: Path, prompt: str, project: str, session_id: str,
                         settings: AmbientSettings, warm: WarmRecall | None = None,
                         clock: Callable[[], float] = time.monotonic) -> PromptContext:
    """Correction draft, rules + background for ``prompt``; updates the session state and rule surfaces."""
    deadline = Deadline(settings.latency_ms / MS_PER_SEC, clock)
    prompt = hook_recall.user_prompt_text(prompt)
    capture = settings.correction_capture and is_user_text(prompt)
    recall = settings.ambient_recall and wants_context(prompt)
    if not (capture or recall) or not db_path.is_file():
        return PromptContext("", (), (), "none", deadline.elapsed_ms())
    state = SessionState.load(memory_dir, session_id)
    budget = settings.token_budget * CHARS_PER_TOKEN
    parts: list[str] = []
    kept_rules: list[RankedRule] = []
    record_ids: list[int] = []
    background: BackgroundPath = "none"
    correction_id: int | None = None
    dropped_floor = 0

    db = _connect(db_path, deadline)
    try:
        if capture:
            correction_id = capture_correction(db, prompt, project, session_id, state)
        if recall:
            try:
                kept_rules, rules_text = select_rules(db, prompt, project, state,
                                                      int(budget * RULES_BUDGET_SHARE))
                if kept_rules:
                    RuleSurfacer(db).record_surfaces(kept_rules, prompt, project, session_id, SOURCE_PROMPT)
                    parts.append(rules_text)
            except sqlite3.Error as exc:
                db.rollback()
                kept_rules = []
                LOGGER.warning(json.dumps({"event": "ambient_rules_skipped", "error": str(exc)}))

            room = budget - sum(len(part) + 2 for part in parts)
            if room >= hook_recall.BUDGET_MIN and not deadline.expired():
                seen = frozenset(state.records)
                text, record_ids, background, dropped_floor = _background(db, memory_dir, prompt, project, session_id, room,
                                                           seen, deadline, warm)
                if text:
                    parts.append(text)
    finally:
        db.close()

    state.remember_rules(item.rule_id for item in kept_rules)
    state.remember_records(record_ids)
    if kept_rules or record_ids or correction_id is not None:
        state.save()
    return PromptContext("\n\n".join(parts), tuple(item.rule_id for item in kept_rules), tuple(record_ids),
                         background, deadline.elapsed_ms(), correction_id, dropped_floor)


def capture_correction(db: sqlite3.Connection, prompt: str, project: str, session_id: str,
                       state: SessionState) -> int | None:
    """Draft insight for a user correction in ``prompt`` (``correction_capture``); never raises.

    The same prompt is captured once per session: a resubmitted or replayed prompt
    must not count as the user repeating themselves.
    """
    key = prompt_key(prompt)
    if key in state.corrections or correction_capture.detect(prompt) is None:
        return None
    try:
        if not table_exists(db, CORRECTIONS_TABLE):
            return None
        insight_id = correction_capture.capture(db, prompt, project, session_id)
    except sqlite3.Error as exc:
        db.rollback()
        LOGGER.warning(json.dumps({"event": "ambient_correction_skipped", "error": str(exc)}))
        return None
    state.remember_correction(key)
    return insight_id


def prompt_key(prompt: str) -> str:
    normalised = " ".join(prompt.split()).casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:PROMPT_KEY_CHARS]


def is_user_text(prompt: str) -> bool:
    """Anything the user typed except a client command; short complaints ("WTF") count."""
    stripped = hook_recall.user_prompt_text(prompt)
    return bool(stripped) and not stripped.startswith(COMMAND_PREFIX)


def _background(db: sqlite3.Connection, memory_dir: Path, prompt: str, project: str, session_id: str,
                room: int, seen: frozenset[int], deadline: Deadline,
                warm: WarmRecall | None) -> tuple[str, list[int], BackgroundPath, int]:
    budget = min(room, hook_recall.BUDGET_MAX)
    caller = warm if warm is not None else daemon_recall(memory_dir)
    if caller is not None:
        timeout = deadline.remaining() - COLD_RESERVE_SEC
        if timeout > 0:
            body = {"query": prompt[:hook_recall.MAX_QUERY_CHARS], "project": project or None,
                    "session_id": session_id[:hook_recall.MAX_ID_CHARS] or None, "budget_chars": budget,
                    "limit": WARM_LIMIT, "dedupe": True}
            answer = caller(body, timeout)
            if answer is not None:
                ids = [int(i) for i in answer.get("ids") or [] if isinstance(i, int)]
                text, kept = drop_seen_lines(str(answer.get("text") or ""), ids, seen)
                return text, kept, "warm", int(answer.get("dropped_floor", 0))
    if deadline.expired():
        return "", [], "none", 0
    request = hook_recall.HookRecallRequest(query=prompt, project=project or None, session_id=session_id,
                                            budget_chars=budget, limit=COLD_LIMIT, dedupe=True)
    try:
        result = cold_recall(db, prompt, project, session_id, seen)
    except sqlite3.Error as exc:
        LOGGER.info(json.dumps({"event": "ambient_cold_recall_skipped", "error": str(exc)}))
        return "", [], "none", 0
    block = hook_recall.compact_block(result, request, seen)
    return block.text, block.ids, "cold", block.dropped_floor
