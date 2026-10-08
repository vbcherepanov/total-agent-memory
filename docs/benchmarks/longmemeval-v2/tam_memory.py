"""total-agent-memory (TAM) as a LongMemEval-V2 memory backend (memory_type "tam").

insert(trajectory) turns one web-agent trajectory into text fragments and adds them to a
private TAM store (tam_bench_common.tam_worker, one worker process per memory):

- one fragment per state: URL, the agent's thought and action, a short goal line, then
  the page's accessibility tree. TAM indexes the whole text (FTS5 sees every page token;
  the local embedding model sees the leading part, i.e. URL/thought/action/goal);
- one overview fragment per trajectory: goal, outcome, start URL and the ordered list of
  (URL, action, short thought) for every step.

Screenshots are not indexed and question images are not used for retrieval: this backend
is text-only. query() runs TAM's recall on the question text (FTS5 + vectors + RRF, and
the local cross-encoder when cross_rerank is "on") and, with fill_budget (default), TAM's
budget fill: up to fill_pool ranked hits, each with the context_radius states before and
after it in the same trajectory, each state counted at most per_hit_max_chars, are kept
whole in rank order while they fit into max_context_chars. It returns them as text items,
best first. A state hit longer than its share of the context budget is cut to the window
of accessibility-tree lines that shares the most terms with the question.

No LLM runs inside TAM; the reader and the judge are the harness's.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import unquote

_BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCH_ROOT))

from memory_modules.memory import Memory, MemoryContextItem, register_memory
from tam_bench_common.tam_worker import TamStoreProcess, TamWorkerSettings

PROJECT = "lmev2"
INDEX_MODES = frozenset({"state", "chunk"})
# memory_params that shape the stored index; a saved memory can be reloaded with different
# values for every other (query-time) parameter.
OVERVIEW_EMBED_CHARS = 16000
INDEX_PARAMS = ("tam_src", "index_mode", "chunk_chars", "embed_chars", "embed_provider", "embed_env",
                "embed_batch", "goal_chars", "overview_thought_chars", "tree_clean")
SAVED_STORE_DIR = "tam_store"
SAVED_STATE_FILE = "tam_adapter_state.json"
MAX_CONTEXT_RADIUS = 3
RECORD_SEPARATOR = "\n\n"
TERM = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    ["the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her", "was", "one", "our", "out", "has", "have", "how", "its", "may", "new", "now", "see", "two", "way", "who", "did", "get", "let", "put", "say", "she", "too", "use", "what", "when", "where", "which", "while", "with", "this", "that", "from", "they", "them", "then", "than", "there", "their", "these", "those", "into", "your", "about", "after", "before", "would", "could", "should", "will", "been", "being", "does", "done", "each", "some", "such", "only", "other", "more", "most", "very", "also", "just", "over", "under", "again", "once", "here", "why", "whom", "mark", "final", "answer", "boxed"])


@dataclass(frozen=True)
class TamLmeSettings:
    tam_src: str
    python_executable: str | None = None
    top_k: int = 10
    fill_budget: bool = True
    fill_pool: int = 100
    context_radius: int = 1
    max_context_chars: int = 48000
    per_hit_max_chars: int = 8000
    goal_chars: int = 300
    overview_thought_chars: int = 200
    embed_batch: int = 64
    embed_provider: str = "fastembed"
    cross_rerank: str = "on"
    worker_timeout_s: float = 3600.0
    work_root: str | None = None
    index_mode: str = "state"
    chunk_chars: int = 1200
    chunk_pool: int = 200
    state_full_chars: int = 6000
    state_max_chars: int = 12000
    per_page_cap: int = 2
    clean_query: bool = False
    embed_chars: int = 600
    embed_env: dict[str, str] | None = None
    embed_key_file: str | None = None
    screenshots: int = 0
    screenshot_root: str | None = None
    tree_clean: str = "v1"

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> TamLmeSettings:
        known = {field.name for field in fields(cls)}
        unknown = set(params) - known
        if unknown:
            raise ValueError(f"unknown TAM memory_params: {sorted(unknown)}")
        values = dict(params)
        values.setdefault("tam_src", os.environ.get("TAM_SRC_DIR"))
        if not values["tam_src"]:
            raise ValueError("memory_params.tam_src (or TAM_SRC_DIR) is required")
        settings = cls(**values)
        for name in ("top_k", "fill_pool", "max_context_chars", "per_hit_max_chars", "goal_chars",
                     "overview_thought_chars", "embed_batch", "chunk_chars", "chunk_pool", "state_full_chars",
                     "state_max_chars", "per_page_cap", "embed_chars"):
            if int(getattr(settings, name)) < 1:
                raise ValueError(f"{name} must be >= 1")
        if int(settings.screenshots) < 0:
            raise ValueError("screenshots must be >= 0")
        if settings.screenshots and not settings.screenshot_root:
            raise ValueError("screenshots needs screenshot_root (the directory holding screenshots/<trajectory>/)")
        if settings.tree_clean not in TREE_CLEAN_VERSIONS:
            raise ValueError(f"tree_clean must be one of {sorted(TREE_CLEAN_VERSIONS)}")
        if settings.index_mode not in INDEX_MODES:
            raise ValueError(f"index_mode must be one of {sorted(INDEX_MODES)}")
        if not 0 <= int(settings.context_radius) <= MAX_CONTEXT_RADIUS:
            raise ValueError(f"context_radius must be between 0 and {MAX_CONTEXT_RADIUS}")
        return settings

    def worker_settings(self) -> TamWorkerSettings:
        return TamWorkerSettings(tam_src=self.tam_src, project=PROJECT, top_k=self.top_k,
                                 embed_batch=self.embed_batch, embed_provider=self.embed_provider,
                                 cross_rerank=self.cross_rerank, worker_timeout_s=self.worker_timeout_s,
                                 work_root=self.work_root, python_executable=self.python_executable,
                                 extra_env=self.embed_env, embed_key_file=self.embed_key_file)


# ---------------------------------------------------------------------------
# Trajectory -> fragments (pure functions)
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def state_fragment(trajectory: dict[str, Any], state: dict[str, Any], state_count: int,
                   settings: TamLmeSettings) -> dict[str, Any]:
    trajectory_id = _text(trajectory.get("id"))
    header_lines = [
        (f"Trajectory {trajectory_id} ({_text(trajectory.get('environment'))}, outcome: "
         f"{_text(trajectory.get('outcome')) or 'unknown'}) - step {state.get('state_index')} of {state_count}"),
        f"URL: {_text(state.get('url'))}",
    ]
    if _text(state.get("thought")):
        header_lines.append(f"Thought: {_text(state.get('thought'))}")
    header_lines.append(f"Action: {_text(state.get('action')) or '(none: initial state)'}")
    header_lines.append(f"Goal: {_clip(' '.join(_text(trajectory.get('goal')).split()), settings.goal_chars)}")
    header = "\n".join(header_lines)
    page = _text(state.get("accessibility_tree"))
    content = f"{header}\nPage (accessibility tree):\n{page}" if page else header
    return {
        "index_text": content,
        "content": content,
        "session": trajectory_id,
        "meta": {"kind": "state", "trajectory_id": trajectory_id, "state_index": state.get("state_index"),
                 "page_offset": len(header) + len("\nPage (accessibility tree):\n") if page else len(content)},
    }


def overview_fragment(trajectory: dict[str, Any], states: Sequence[dict[str, Any]],
                      settings: TamLmeSettings) -> dict[str, Any]:
    trajectory_id = _text(trajectory.get("id"))
    lines = [
        (f"Trajectory {trajectory_id} overview ({_text(trajectory.get('environment'))}, outcome: "
         f"{_text(trajectory.get('outcome')) or 'unknown'}, {len(states)} states)"),
        f"Goal: {_text(trajectory.get('goal'))}",
        f"Start URL: {_text(trajectory.get('start_url'))}",
        "Steps:",
    ]
    for state in states:
        action = _text(state.get("action"))
        if not action:
            continue
        thought = _clip(" ".join(_text(state.get("thought")).split()), settings.overview_thought_chars)
        line = f"{state.get('state_index')}. [{_text(state.get('url'))}] {action}"
        lines.append(f"{line} - {thought}" if thought else line)
    content = "\n".join(lines)
    # The overview lists every step, so a long trajectory can pass an embedding model's input
    # window (bge-m3: 8192 tokens); only the embedding input is capped, FTS keeps the whole text.
    return {"index_text": content, "content": content, "session": trajectory_id,
            "embed_text": content[:OVERVIEW_EMBED_CHARS],
            "meta": {"kind": "overview", "trajectory_id": trajectory_id, "page_offset": len(content)}}


def trajectory_fragments(trajectory: dict[str, Any], settings: TamLmeSettings) -> list[dict[str, Any]]:
    states = [state for state in trajectory.get("states") or [] if isinstance(state, dict)]
    fragments = [overview_fragment(trajectory, states, settings)]
    fragments.extend(state_fragment(trajectory, state, len(states), settings) for state in states)
    return fragments


# ---------------------------------------------------------------------------
# Page-chunk index (index_mode "chunk", pure functions)
#
# Every state's accessibility tree is cleaned (element ids, the visible/clickable flags
# and nameless layout containers removed; nesting kept as one space per level) and cut
# into chunks of whole lines. Each chunk is indexed with its page title and URL, so both
# TAM's FTS5 and its embedding model see a page region instead of the first lines of a
# whole page. Identical chunks (menus, headers repeated on many pages) are indexed once
# and remember every state they occur in. Each state also gets a short "step" fragment
# (page title, URL, thought, action, goal) and each trajectory an overview fragment.
# At query time chunk/step/overview hits are folded into states (reciprocal-rank sum),
# near-duplicate pages are capped, and each state is shown with its matching regions.
# ---------------------------------------------------------------------------

AXTREE_ID = re.compile(r"^\[\d+\]\s*")
AXTREE_FRAME_ID = re.compile(r"^\[[a-z]+\d+\]\s*")
# v1: the cleaning the 2026-09-29 configuration was fixed with. v2 also strips frame-scoped ids
# ("[a193]", elements inside iframes) and keeps icon-glyph text, which can carry state
# (ServiceNow marks a mandatory field with a glyph in front of its label).
TREE_CLEAN_VERSIONS = frozenset({"v1", "v2"})
AXTREE_NOISE_FLAGS = re.compile(r",\s*(?:visible|clickable)\b")
AXTREE_EMPTY_CONTAINER = re.compile(
    r"^(?:generic|none|group|list|listitem|Section|LayoutTable|LayoutTableRow|LayoutTableCell|paragraph|region|"
    r"navigation|banner|main|contentinfo|complementary|rowgroup|table|row|gridcell|cell|Iframe|article|separator|"
    r"image|img|StaticText|DescriptionList|DescriptionListTerm|DescriptionListDetail|strong|emphasis|figure|"
    r"toolbar|menubar|tablist|tabpanel|form|search|dialog|alert|status|log|note|document|application)"
    r"\s*''\s*$")
AXTREE_ICON_TEXT = re.compile(r"^StaticText\s+'(?:\\u[0-9a-fA-F]{4}|[-]|\s)*'$")
ROOT_TITLE = re.compile(r"^RootWebArea\s+'(.*?)'")
FORMAT_SENTENCE = re.compile(r"\\boxed|boxed\{|final answer", re.IGNORECASE)
SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+|\n+")
RANK_OFFSET = 10


def clean_tree(tree: str, version: str = "v1") -> list[str]:
    lines: list[str] = []
    for raw in tree.split("\n"):
        stripped = raw.lstrip("\t")
        depth = len(raw) - len(stripped)
        text = AXTREE_ID.sub("", stripped.strip())
        if version == "v2":
            text = AXTREE_FRAME_ID.sub("", text)
        text = AXTREE_NOISE_FLAGS.sub("", text)
        if not text or AXTREE_EMPTY_CONTAINER.match(text) or (version == "v1" and AXTREE_ICON_TEXT.match(text)):
            continue
        lines.append(" " * depth + text)
    return lines


def page_title(lines: Sequence[str]) -> str:
    for line in lines[:3]:
        match = ROOT_TITLE.match(line.strip())
        if match:
            return match.group(1)
    return ""


def chunk_lines(lines: Sequence[str], chunk_chars: int) -> list[tuple[int, int]]:
    """[start, end) line ranges of consecutive whole lines, each at most chunk_chars long
    (a single longer line is its own chunk)."""
    ranges: list[tuple[int, int]] = []
    start = 0
    size = 0
    for index, line in enumerate(lines):
        cost = len(line) + 1
        if index > start and size + cost > chunk_chars:
            ranges.append((start, index))
            start, size = index, 0
        size += cost
    if start < len(lines):
        ranges.append((start, len(lines)))
    return ranges


def clean_query_text(query: str) -> str:
    """The question without its answer-format instructions (sentences about \\boxed{} / the final answer)."""
    kept = [part for part in SENTENCE_SPLIT.split(query) if part.strip() and not FORMAT_SENTENCE.search(part)]
    return " ".join(kept).strip() or query


@dataclass
class StateView:
    trajectory_id: str
    state_index: int
    state_count: int
    url: str
    title: str
    thought: str
    action: str
    next_action: str
    lines: list[str]
    chunks: list[tuple[int, int]]
    screenshot: str = ""

    @property
    def signature(self) -> tuple[str, str]:
        # Percent-decoded first: some apps (ServiceNow) wrap the real page and its query in an
        # encoded path segment, which would otherwise make every record URL a distinct page.
        return self.title, unquote(self.url).split("?", 1)[0].split("#", 1)[0]


def trajectory_views(trajectory: dict[str, Any], settings: TamLmeSettings) -> list[StateView]:
    trajectory_id = _text(trajectory.get("id"))
    states = [state for state in trajectory.get("states") or [] if isinstance(state, dict)]
    views: list[StateView] = []
    for position, state in enumerate(states):
        lines = clean_tree(_text(state.get("accessibility_tree")), settings.tree_clean)
        next_action = _text(states[position + 1].get("action")) if position + 1 < len(states) else ""
        views.append(StateView(
            trajectory_id=trajectory_id, state_index=position, state_count=len(states), url=_text(state.get("url")),
            title=page_title(lines), thought=" ".join(_text(state.get("thought")).split()),
            action=_text(state.get("action")), next_action=next_action, lines=lines,
            chunks=chunk_lines(lines, settings.chunk_chars), screenshot=_text(state.get("screenshot"))))
    return views


def chunk_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def view_fragments(trajectory: dict[str, Any], views: Sequence[StateView],
                   settings: TamLmeSettings) -> list[dict[str, Any]]:
    """Overview, step and chunk fragments for one trajectory (chunk keys let the caller skip
    chunks already indexed from another state)."""
    trajectory_id = _text(trajectory.get("id"))
    goal = _clip(" ".join(_text(trajectory.get("goal")).split()), settings.goal_chars)
    raw_states = [state for state in trajectory.get("states") or [] if isinstance(state, dict)]
    fragments = [overview_fragment(trajectory, raw_states, settings)]
    for view in views:
        place = f"{view.title} | {view.url}" if view.title else view.url
        step_lines = [place]
        if view.thought:
            step_lines.append(f"Thought: {view.thought}")
        step_lines.append(f"Action that led here: {view.action or '(none: initial state)'}")
        if view.next_action:
            step_lines.append(f"Next action: {view.next_action}")
        step_lines.append(f"Goal: {goal}")
        step_text = "\n".join(step_lines)
        fragments.append({"index_text": step_text, "content": step_text, "session": trajectory_id,
                          "embed_text": step_text[:settings.embed_chars],
                          "meta": {"kind": "step", "trajectory_id": trajectory_id,
                                   "state_index": view.state_index}})
        short_place = f"{view.title} | {view.signature[1]}" if view.title else view.signature[1]
        for chunk_index, (start, end) in enumerate(view.chunks):
            text = f"{short_place}\n" + "\n".join(view.lines[start:end])
            fragments.append({"index_text": text, "content": text, "session": trajectory_id,
                              "embed_text": text[:settings.embed_chars],
                              "meta": {"kind": "chunk", "trajectory_id": trajectory_id,
                                       "state_index": view.state_index, "chunk_index": chunk_index,
                                       "key": chunk_key(text)}})
    return fragments


@dataclass
class Candidate:
    kind: str
    trajectory_id: str
    state_index: int
    score: float = 0.0
    first_rank: int = 0
    chunk_indexes: set = None

    def __post_init__(self) -> None:
        if self.chunk_indexes is None:
            self.chunk_indexes = set()


def rank_candidates(hits: Sequence[dict[str, Any]],
                    occurrences: dict[str, list[tuple[str, int, int]]]) -> list[Candidate]:
    """Fold ranked fragment hits into states and trajectory overviews: each hit adds
    1/(RANK_OFFSET + rank) to every state it occurs in; best total first."""
    candidates: dict[tuple[str, str, int], Candidate] = {}

    def bump(kind: str, trajectory_id: str, state_index: int, rank: int, chunk_index: int | None) -> None:
        key = (kind, trajectory_id, state_index)
        candidate = candidates.get(key)
        if candidate is None:
            candidate = candidates[key] = Candidate(kind, trajectory_id, state_index, first_rank=rank)
        candidate.score += 1.0 / (RANK_OFFSET + rank)
        if chunk_index is not None:
            candidate.chunk_indexes.add(chunk_index)

    for hit in hits:
        meta = hit["meta"]
        rank = int(hit["rank"])
        kind = meta.get("kind")
        if kind == "overview":
            bump("overview", meta["trajectory_id"], -1, rank, None)
        elif kind == "step":
            bump("state", meta["trajectory_id"], int(meta["state_index"]), rank, None)
        elif kind == "chunk":
            for trajectory_id, state_index, chunk_index in occurrences.get(meta["key"], []):
                bump("state", trajectory_id, state_index, rank, chunk_index)
    return sorted(candidates.values(), key=lambda candidate: (-candidate.score, candidate.first_rank))


def render_state(view: StateView, chunk_indexes: set, goal: str, outcome: str, environment: str,
                 settings: TamLmeSettings, budget: int) -> str:
    header = [(f"Trajectory {view.trajectory_id} ({environment}, outcome: {outcome or 'unknown'}) - "
               f"state {view.state_index} of {view.state_count}"),
              f"Goal: {goal}", f"URL: {view.url}"]
    if view.thought:
        header.append(f"Agent thought on this page: {view.thought}")
    header.append(f"Action that led to this page: {view.action or '(none: initial state)'}")
    if view.next_action:
        header.append(f"Next action taken on this page: {view.next_action}")
    header.append("Page (accessibility tree):")
    head = "\n".join(header) + "\n"
    page = "\n".join(view.lines)
    limit = min(budget, settings.state_max_chars)
    if len(head) + len(page) <= min(limit, len(head) + settings.state_full_chars):
        return head + page
    wanted = sorted(chunk_indexes) or [0]
    shown: list[int] = []
    for index in wanted:
        for neighbour in (index - 1, index, index + 1):
            if 0 <= neighbour < len(view.chunks) and neighbour not in shown:
                shown.append(neighbour)
    shown.sort()
    parts: list[str] = []
    size = len(head)
    previous = -1
    for index in shown:
        start, end = view.chunks[index]
        text = "\n".join(view.lines[start:end])
        gap = "[...]\n" if index != previous + 1 else ""
        if size + len(gap) + len(text) + 1 > limit:
            if index in chunk_indexes and size + len(gap) + 200 < limit:
                parts.append(gap + text[:limit - size - len(gap) - 1])
                size = limit
            break
        parts.append(gap + text)
        size += len(gap) + len(text) + 1
        previous = index
    if previous != len(view.chunks) - 1:
        parts.append("[...]")
    return head + "\n".join(parts)


# ---------------------------------------------------------------------------
# Query-time presentation (pure functions)
# ---------------------------------------------------------------------------

def query_terms(query: str) -> frozenset:
    return frozenset(term for term in TERM.findall(query.lower()) if len(term) >= 3 and term not in STOPWORDS)


def best_window(lines: Sequence[str], terms: frozenset, budget: int) -> tuple[int, int]:
    """[start, end) of the contiguous line window within `budget` chars that covers the most term hits."""
    scores = [len(terms.intersection(TERM.findall(line.lower()))) for line in lines]
    best = (0, 0, -1)
    start = 0
    size = 0
    score = 0
    for end, line in enumerate(lines):
        size += len(line) + 1
        score += scores[end]
        while size > budget and start <= end:
            size -= len(lines[start]) + 1
            score -= scores[start]
            start += 1
        if start <= end and score > best[2]:
            best = (start, end + 1, score)
    if best[2] <= 0:
        end = 0
        size = 0
        while end < len(lines) and size + len(lines[end]) + 1 <= budget:
            size += len(lines[end]) + 1
            end += 1
        return 0, end
    return best[0], best[1]


def render_hit(hit: dict[str, Any], terms: frozenset, budget: int) -> str:
    content = hit["content"]
    if len(content) <= budget:
        return content
    offset = int(hit["meta"].get("page_offset", len(content)))
    head, page = content[:offset], content[offset:]
    room = budget - len(head) - len("[...]\n") * 2
    if room <= 0:
        return _clip(content, budget)
    lines = page.split("\n")
    start, end = best_window(lines, terms, room)
    if end <= start:
        return _clip(content, budget)
    excerpt = "\n".join(lines[start:end])
    if len(excerpt) > room:
        excerpt = excerpt[:room]
    prefix = "[...]\n" if start > 0 else ""
    suffix = "\n[...]" if end < len(lines) else ""
    return f"{head}{prefix}{excerpt}{suffix}"


def item_label(position: int) -> str:
    return f"[Memory {position}]\n"


def hit_groups(hits: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """A ranked hit and the neighbours the worker put after it (records with an `anchor`)."""
    groups: list[list[dict[str, Any]]] = []
    for hit in hits:
        if hit.get("anchor") is None or not groups:
            groups.append([hit])
        else:
            groups[-1].append(hit)
    return groups


def assemble_items(query: str, hits: list[dict[str, Any]], settings: TamLmeSettings) -> list[MemoryContextItem]:
    """One text item per ranked hit, best first; a hit's neighbouring states are shown with
    it in trajectory order. Each record is cut to at most per_hit_max_chars."""
    terms = query_terms(query)
    items: list[MemoryContextItem] = []
    remaining = settings.max_context_chars
    for position, group in enumerate(hit_groups(hits), start=1):
        label = item_label(position)
        remaining -= len(label)
        parts: list[str] = []
        for record in sorted(group, key=lambda record: record.get("position", 0)):
            budget = min(settings.per_hit_max_chars, remaining)
            if budget <= 0:
                break
            text = render_hit(record, terms, budget)
            parts.append(text)
            remaining -= len(text) + len(RECORD_SEPARATOR)
        if not parts:
            break
        items.append({"type": "text", "value": label + RECORD_SEPARATOR.join(parts)})
    return items


# ---------------------------------------------------------------------------
# Harness backend
# ---------------------------------------------------------------------------

@register_memory
class TamMemory(Memory):
    """LongMemEval-V2 backend backed by a private total-agent-memory store."""

    memory_type = "tam"

    def __init__(self, memory_params: dict[str, object]) -> None:
        super().__init__(memory_params)
        self.settings = TamLmeSettings.from_params(dict(memory_params))
        self._store: TamStoreProcess | None = None
        self._store_lock = threading.Lock()
        self._fragment_count = 0
        self.insert_stats: dict[str, float] = {"embed_seconds": 0.0, "save_seconds": 0.0, "trajectories": 0}
        self._build_started: float | None = None
        self._build_wall_seconds: float | None = None
        self._last_hits = threading.local()
        self._views: dict[tuple[str, int], StateView] = {}
        self._trajectories: dict[str, dict[str, str]] = {}
        self._overviews: dict[str, str] = {}
        self._occurrences: dict[str, list[tuple[str, int, int]]] = {}
        self._seed_dir: Path | None = None

    def _ensure_store(self) -> TamStoreProcess:
        with self._store_lock:
            if self._store is None:
                self._store = TamStoreProcess(self.settings.worker_settings(), prefix="lmev2-tam-",
                                              seed_dir=self._seed_dir)
            return self._store

    @classmethod
    def reconcile_loaded_memory_config(cls, saved_config: dict[str, Any],
                                       requested_config: dict[str, Any] | None) -> dict[str, Any]:
        """A saved TAM index can serve other query-time settings; index parameters must match."""
        if saved_config.get("memory_type") != cls.memory_type:
            raise RuntimeError(f"saved memory is {saved_config.get('memory_type')!r}, not {cls.memory_type!r}")
        if requested_config is None:
            return json.loads(json.dumps(saved_config))
        if requested_config.get("memory_type") != cls.memory_type:
            raise RuntimeError(f"requested memory is {requested_config.get('memory_type')!r}, not {cls.memory_type!r}")
        saved = saved_config["memory_params"]
        requested = requested_config["memory_params"]
        defaults = {field.name: field.default for field in fields(TamLmeSettings)}
        differing = [name for name in INDEX_PARAMS
                     if saved.get(name, defaults.get(name)) != requested.get(name, defaults.get(name))]
        if differing:
            raise RuntimeError(f"saved TAM index was built with different {differing}")
        return json.loads(json.dumps(requested_config))

    def _save_backend(self, output_dir: Path) -> None:
        self._ensure_store().snapshot(output_dir / SAVED_STORE_DIR)
        state = {
            "fragment_count": self._fragment_count,
            "insert_stats": self.insert_stats,
            "build_wall_seconds": (time.perf_counter() - self._build_started) if self._build_started else None,
            "views": [dataclasses.asdict(view) for view in self._views.values()],
            "trajectories": self._trajectories,
            "overviews": self._overviews,
            "occurrences": self._occurrences,
        }
        (output_dir / SAVED_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    def _load_backend(self, input_dir: Path) -> None:
        store_dir = input_dir / SAVED_STORE_DIR
        if not store_dir.is_dir():
            raise RuntimeError(f"no saved TAM store in {input_dir}")
        state = json.loads((input_dir / SAVED_STATE_FILE).read_text(encoding="utf-8"))
        self._seed_dir = store_dir
        self._fragment_count = int(state["fragment_count"])
        self.insert_stats = dict(state["insert_stats"])
        self._build_wall_seconds = state["build_wall_seconds"]
        for record in state["views"]:
            view = StateView(**{**record, "chunks": [tuple(chunk) for chunk in record["chunks"]]})
            self._views[(view.trajectory_id, view.state_index)] = view
        self._trajectories = dict(state["trajectories"])
        self._overviews = dict(state["overviews"])
        self._occurrences = {key: [tuple(item) for item in value] for key, value in state["occurrences"].items()}
        # Start the worker now, so copying the store is part of loading, not of the first query.
        self._ensure_store()

    def insert(self, trajectory: dict[str, object]) -> None:
        if self._build_started is None:
            self._build_started = time.perf_counter()
        self._build_wall_seconds = None
        if self.settings.index_mode == "chunk":
            fragments = self._chunk_mode_fragments(dict(trajectory))
        else:
            fragments = trajectory_fragments(dict(trajectory), self.settings)
        stats = self._ensure_store().add(fragments)
        self._fragment_count += int(stats.get("fragments", 0))
        self.insert_stats["embed_seconds"] += float(stats.get("embed_seconds", 0.0))
        self.insert_stats["save_seconds"] += float(stats.get("save_seconds", 0.0))
        self.insert_stats["trajectories"] += 1

    def query(self, query: str, query_image: str | None = None) -> list[MemoryContextItem]:
        if self._build_wall_seconds is None and self._build_started is not None:
            self._build_wall_seconds = time.perf_counter() - self._build_started
        if self._fragment_count == 0 or not query.strip():
            self._last_hits.value = []
            return []
        if self.settings.index_mode == "chunk":
            return self._chunk_mode_query(query)
        if self.settings.fill_budget:
            hits = self._ensure_store().search(query, limit=self.settings.fill_pool,
                                               radius=self.settings.context_radius,
                                               fill_chars=self.settings.max_context_chars,
                                               fill_overhead=len(item_label(self.settings.fill_pool)),
                                               fill_record_cap=self.settings.per_hit_max_chars)
        else:
            hits = self._ensure_store().search(query)
        self._last_hits.value = [{"rank": hit["rank"], **hit["meta"]} for hit in hits]
        return assemble_items(query, hits, self.settings)

    def _chunk_mode_fragments(self, trajectory: dict[str, Any]) -> list[dict[str, Any]]:
        """Fragments of one trajectory for the chunk index; chunks already indexed from
        another state are not indexed again, only their new occurrence is recorded."""
        trajectory_id = _text(trajectory.get("id"))
        views = trajectory_views(trajectory, self.settings)
        self._trajectories[trajectory_id] = {
            "goal": " ".join(_text(trajectory.get("goal")).split()),
            "outcome": _text(trajectory.get("outcome")),
            "environment": _text(trajectory.get("environment")),
        }
        for view in views:
            self._views[(trajectory_id, view.state_index)] = view
        fragments: list[dict[str, Any]] = []
        for fragment in view_fragments(trajectory, views, self.settings):
            meta = fragment["meta"]
            if meta["kind"] == "overview":
                self._overviews[trajectory_id] = fragment["content"]
            if meta["kind"] != "chunk":
                fragments.append(fragment)
                continue
            occurrence = (trajectory_id, int(meta["state_index"]), int(meta["chunk_index"]))
            seen = self._occurrences.get(meta["key"])
            if seen is None:
                self._occurrences[meta["key"]] = [occurrence]
                fragments.append(fragment)
            else:
                seen.append(occurrence)
        return fragments

    def _chunk_mode_query(self, query: str) -> list[MemoryContextItem]:
        search_text = clean_query_text(query) if self.settings.clean_query else query
        hits = self._ensure_store().search(search_text, limit=self.settings.chunk_pool)
        candidates = rank_candidates(hits, self._occurrences)
        items: list[MemoryContextItem] = []
        shown: list[dict[str, Any]] = []
        per_page: dict[tuple[str, str], int] = {}
        remaining = self.settings.max_context_chars
        for candidate in candidates:
            if remaining < 400:
                break
            label = item_label(len(items) + 1)
            if candidate.kind == "overview":
                text = _clip(self._overviews[candidate.trajectory_id], min(self.settings.state_max_chars,
                                                                           remaining - len(label)))
            else:
                view = self._views[(candidate.trajectory_id, candidate.state_index)]
                if per_page.get(view.signature, 0) >= self.settings.per_page_cap:
                    continue
                per_page[view.signature] = per_page.get(view.signature, 0) + 1
                info = self._trajectories[candidate.trajectory_id]
                text = render_state(view, candidate.chunk_indexes, _clip(info["goal"], self.settings.goal_chars),
                                    info["outcome"], info["environment"], self.settings, remaining - len(label))
            items.append({"type": "text", "value": label + text})
            image = self._screenshot_path(candidate, sum(item["type"] == "image" for item in items))
            if image is not None:
                items.append({"type": "image", "value": image})
            shown.append({"kind": candidate.kind, "trajectory_id": candidate.trajectory_id,
                          "state_index": candidate.state_index, "score": round(candidate.score, 4),
                          "chunks": sorted(candidate.chunk_indexes)})
            remaining -= len(label) + len(text) + len(RECORD_SEPARATOR)
        self._last_hits.value = shown
        return items

    def _screenshot_path(self, candidate: Candidate, attached: int) -> str | None:
        """The stored screenshot of a shown state while fewer than `screenshots` are attached."""
        if candidate.kind != "state" or attached >= self.settings.screenshots:
            return None
        view = self._views[(candidate.trajectory_id, candidate.state_index)]
        if not view.screenshot:
            return None
        path = Path(self.settings.screenshot_root) / view.screenshot
        return str(path) if path.is_file() else None

    def post_query_hook(self, *, query: str, query_image: str | None,
                        memory_context: list[MemoryContextItem]) -> dict[str, object]:
        return {
            "tam_hits": getattr(self._last_hits, "value", []),
            "tam_fragments_indexed": self._fragment_count,
            "tam_cross_rerank": self.settings.cross_rerank,
            "tam_index_build": {**self.insert_stats, "wall_seconds_until_first_query": self._build_wall_seconds},
            "query_image_used": False,
        }

    def close(self) -> None:
        with self._store_lock:
            if self._store is not None:
                self._store.close()
                self._store = None
