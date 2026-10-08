"""An isolated TAM store that lives in its own worker process.

TAM resolves its memory directory and several caches at import time, so two stores in
one process would share state. Each TamStoreProcess starts a fresh interpreter
(`python -m tam_bench_common.tam_worker`) with TAM_MEMORY_DIR pointing at a private
temporary directory and talks to it over JSON lines on stdin/stdout. Closing it (or
garbage collection) stops the process and deletes the directory.

The worker interpreter is configurable (`python_executable`), so a benchmark harness can
run in its own virtualenv while TAM runs in TAM's; nothing from the harness environment
leaks into the worker's sys.path.

No LLM runs inside TAM here: the worker forces TAM's fast mode (FORCED_TAM_ENV from
aml_adapter: enrichment, query rewriting and LLM hooks off), runs with every API key,
token and secret removed from its environment, and embeds with a local model.

Fragments are stored twice: as TAM knowledge records (what recall ranks) and verbatim in
a side table keyed by the record id, so a hit is returned exactly as inserted (TAM's
save path may redact or normalise content) together with the caller's metadata.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

KNOWLEDGE_TYPE = "fact"
DEFAULT_SESSION = "bench"
MAX_RADIUS = 3
SECRET_ENV = re.compile(r"(API_KEY|APIKEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
SIDE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bench_fragments (
    knowledge_id INTEGER PRIMARY KEY,
    position INTEGER NOT NULL,
    meta TEXT NOT NULL,
    content TEXT NOT NULL
);
"""
SQLITE_MAX_PARAMS = 900
FORBIDDEN_ROOTS = (".tam", ".claude-memory")
# Hosted OpenAI-compatible embedding endpoints the worker may call; each needs embed_key_file.
HOSTED_EMBED_BASES = ("https://api.deepinfra.com/",)
LOCAL_EMBED_BASES = ("http://127.0.0.1", "http://localhost")
BENCH_ROOT = Path(__file__).resolve().parents[1]
SHUTDOWN_GRACE_S = 30.0


@dataclass(frozen=True)
class TamWorkerSettings:
    tam_src: str
    project: str = "bench"
    top_k: int = 10
    embed_batch: int = 64
    embed_provider: str = "fastembed"
    cross_rerank: str = "on"
    worker_timeout_s: float = 1800.0
    work_root: str | None = None
    python_executable: str | None = None
    # Extra variables for the worker, applied after the secret scrub; meant for a local
    # embedding server (e.g. MEMORY_EMBED_PROVIDER=openai with MEMORY_EMBED_API_BASE pointing
    # at a local OpenAI-compatible endpoint, which needs a non-empty placeholder key).
    extra_env: dict[str, str] | None = None
    # File holding only the API key of a hosted embedding endpoint (HOSTED_EMBED_BASES). The key
    # is read when the worker environment is built, so it never appears in these settings, in
    # the harness's memory_config.json or in the parent's environment.
    embed_key_file: str | None = None

    def validate(self) -> TamWorkerSettings:
        if not (Path(self.tam_src) / "server.py").is_file():
            raise ValueError(f"tam_src={self.tam_src!r} does not contain TAM's server.py")
        for name in ("top_k", "embed_batch"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be >= 1")
        if float(self.worker_timeout_s) <= 0:
            raise ValueError("worker_timeout_s must be > 0")
        if self.cross_rerank not in {"on", "off", "auto"}:
            raise ValueError("cross_rerank must be one of on/off/auto")
        if self.work_root is not None:
            check_work_root(self.work_root)
        if self.extra_env is not None:
            if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.extra_env.items()):
                raise ValueError("extra_env must map strings to strings")
            base = self.extra_env.get("MEMORY_EMBED_API_BASE", "")
            if base.startswith(HOSTED_EMBED_BASES):
                if self.embed_key_file is None:
                    raise ValueError("a hosted MEMORY_EMBED_API_BASE needs embed_key_file")
            elif base and not base.startswith(LOCAL_EMBED_BASES):
                raise ValueError("extra_env MEMORY_EMBED_API_BASE must be a local endpoint or one of "
                                 f"{list(HOSTED_EMBED_BASES)}")
        if self.embed_key_file is not None:
            if not Path(self.embed_key_file).expanduser().is_file():
                raise ValueError(f"embed_key_file={self.embed_key_file!r} does not exist")
            if self.extra_env and "MEMORY_EMBED_API_KEY" in self.extra_env:
                raise ValueError("MEMORY_EMBED_API_KEY must come from embed_key_file, not extra_env")
        if self.python_executable is not None and not Path(self.python_executable).is_file():
            raise ValueError(f"python_executable={self.python_executable!r} does not exist")
        return self


def check_work_root(work_root: str) -> Path:
    root = Path(work_root).expanduser().resolve()
    for name in FORBIDDEN_ROOTS:
        forbidden = (Path.home() / name).resolve()
        if root == forbidden or forbidden in root.parents:
            raise ValueError(f"work_root must not be inside {forbidden}")
    return root


def worker_setting_names() -> list[str]:
    return [field.name for field in fields(TamWorkerSettings)]


def worker_environment(base: dict[str, str], memory_dir: str, settings: TamWorkerSettings) -> dict[str, str]:
    env = {name: value for name, value in base.items()
           if not SECRET_ENV.search(name) and name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env.update({
        "TAM_MEMORY_DIR": memory_dir,
        "CLAUDE_MEMORY_DIR": memory_dir,
        "MEMORY_EMBED_PROVIDER": settings.embed_provider,
        # "on" waits for the cross-encoder; TAM's default "auto" skips re-ranking until the
        # model has loaded, which would rank the first queries differently from the rest.
        "MEMORY_CROSS_RERANK": settings.cross_rerank,
        "MEMORY_QUIET": "1",
        "PYTHONPATH": os.pathsep.join([str(BENCH_ROOT), settings.tam_src]),
        "PYTHONUNBUFFERED": "1",
    })
    if settings.extra_env:
        env.update(settings.extra_env)
    if settings.embed_key_file:
        key = Path(settings.embed_key_file).expanduser().read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError(f"embed_key_file={settings.embed_key_file!r} is empty")
        env["MEMORY_EMBED_API_KEY"] = key
    return env


# ---------------------------------------------------------------------------
# Worker process (child side)
# ---------------------------------------------------------------------------

def serve(settings: TamWorkerSettings) -> None:
    # The protocol owns the original stdout; anything TAM prints goes to stderr.
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    from aml_adapter.runtime import FORCED_TAM_ENV, AtomicConnection
    os.environ.update(FORCED_TAM_ENV)
    import server
    from memory_core.retrieval import flatten_results

    store = server.Store(connection_factory=AtomicConnection)
    recall = server.Recall(store)
    db = store.db
    db.executescript(SIDE_SCHEMA)
    db.commit()
    protocol_out.write(json.dumps({"ok": True, "data": {"ready": True}}) + "\n")
    protocol_out.flush()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            request = json.loads(line)
            if request.get("op") == "close":
                break
            try:
                if request["op"] == "add":
                    reply = {"ok": True, "data": _add(store, db, request["fragments"], settings)}
                elif request["op"] == "checkpoint":
                    # Fold the WAL into the main file so a copy of the directory is a complete store.
                    db.commit()
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    reply = {"ok": True, "data": {"checkpointed": True}}
                elif request["op"] == "search":
                    hits = _search(recall, db, flatten_results, request["query"], request.get("limit"), settings)
                    if request.get("radius"):
                        hits = _with_neighbors(db, hits, request["radius"], settings)
                    if request.get("fill_chars") is not None:
                        hits = _fill(hits, request["fill_chars"], request.get("fill_overhead", 0),
                                     request.get("fill_record_cap"))
                    reply = {"ok": True, "data": hits}
                elif request["op"] == "fetch":
                    reply = {"ok": True, "data": _fetch(db, request["positions"])}
                elif request["op"] == "grep":
                    reply = {"ok": True, "data": _grep(db, request["needles"], request["limit"])}
                elif request["op"] == "outline":
                    reply = {"ok": True, "data": _outline(db)}
                else:
                    raise ValueError(f"unknown op {request['op']!r}")
            except Exception as exc:  # noqa: BLE001 - process boundary: the error is sent to the parent, which raises it
                if db.in_transaction:
                    db.rollback()
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            protocol_out.write(json.dumps(reply) + "\n")
            protocol_out.flush()
    finally:
        db.close()
        protocol_out.close()


def _add(store, db, fragments: list[dict[str, Any]], settings: TamWorkerSettings) -> dict[str, Any]:
    started = time.perf_counter()
    # A fragment may carry a shorter `embed_text` (e.g. the part that fits the embedding
    # model's window); FTS and the stored record always use the full `index_text`.
    texts = [fragment.get("embed_text") or fragment["index_text"] for fragment in fragments]
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), settings.embed_batch):
        chunk = texts[offset:offset + settings.embed_batch]
        batch = store.embed(chunk)
        if batch is None or len(batch) != len(chunk) or any(vector is None for vector in batch):
            raise RuntimeError("embedding backend returned no vectors")
        vectors.extend(batch)
    embedded = time.perf_counter()
    (next_position,) = db.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM bench_fragments").fetchone()
    with db.atomic():
        for offset, (fragment, vector) in enumerate(zip(fragments, vectors)):
            record_id, *_ = store.save_knowledge(
                fragment.get("session") or DEFAULT_SESSION, fragment["index_text"], KNOWLEDGE_TYPE, project=settings.project, tags=[],
                skip_dedup=True, skip_quality=True, source_format="conversation",
                repeat="confirm", embedding=vector)
            if record_id is None:
                raise RuntimeError(f"TAM did not store fragment {offset} of this batch")
            db.execute("INSERT INTO bench_fragments(knowledge_id, position, meta, content) VALUES (?, ?, ?, ?)",
                       (record_id, next_position + offset, json.dumps(fragment.get("meta", {})),
                        fragment.get("content", fragment["index_text"])))
    if store.cache is not None:
        store.cache.invalidate()
    if getattr(store, "v9_cache", None) is not None:
        store.v9_cache.invalidate_all()
    finished = time.perf_counter()
    return {"fragments": len(fragments), "embed_mode": store._embed_mode,
            "embed_model": store._active_embed_model_name(),
            "embed_seconds": embedded - started, "save_seconds": finished - embedded}


def _search(recall, db, flatten_results, query: str, limit: int | None,
            settings: TamWorkerSettings) -> list[dict[str, Any]]:
    top_k = int(limit or settings.top_k)
    # record_usage=False: search() otherwise bumps recall_count on every returned row and
    # recall_boost feeds that back into the score, so later queries would be ranked by
    # which records earlier queries happened to retrieve.
    result = recall.search(query, project=settings.project, limit=top_k, record_usage=False)
    if result.get("semantic_diagnostics"):
        raise RuntimeError(f"semantic retrieval unavailable: {result['semantic_diagnostics']}")
    ids: list[int] = []
    for hit in flatten_results(result):
        record_id = int(hit["id"])
        if record_id not in ids:
            ids.append(record_id)
    rows: dict[int, Any] = {}
    for offset in range(0, len(ids), SQLITE_MAX_PARAMS):
        batch = ids[offset:offset + SQLITE_MAX_PARAMS]
        marks = ",".join("?" for _ in batch)
        for row in db.execute(f"SELECT knowledge_id, position, meta, content FROM bench_fragments "
                              f"WHERE knowledge_id IN ({marks})", batch):
            rows[row[0]] = row
    hits = []
    for rank, record_id in enumerate(ids[:top_k]):
        row = rows.get(record_id)
        if row is None:
            raise RuntimeError(f"recall returned record {record_id} that is not a stored fragment")
        hits.append({"rank": rank, "position": row[1], "meta": json.loads(row[2]), "content": row[3],
                     "knowledge_id": record_id})
    return hits


def _fetch(db, positions: list[int]) -> list[dict[str, Any]]:
    """Stored fragments at the given insertion positions, in the order asked for."""
    wanted = [int(position) for position in positions]
    rows: dict[int, Any] = {}
    for offset in range(0, len(wanted), SQLITE_MAX_PARAMS):
        batch = wanted[offset:offset + SQLITE_MAX_PARAMS]
        marks = ",".join("?" for _ in batch)
        for row in db.execute(f"SELECT knowledge_id, position, meta, content FROM bench_fragments "
                              f"WHERE position IN ({marks})", batch):
            rows[row[1]] = row
    return [{"rank": None, "position": rows[position][1], "meta": json.loads(rows[position][2]),
             "content": rows[position][3], "knowledge_id": rows[position][0]}
            for position in dict.fromkeys(wanted) if position in rows]


def _grep(db, needles: list[str], limit: int) -> list[list[int]]:
    """For each needle, the insertion positions (at most `limit`, in order) of the stored
    fragments that contain it verbatim, ignoring case and runs of whitespace."""
    folded = [" ".join(needle.split()).casefold() for needle in needles]
    found: list[list[int]] = [[] for _ in folded]
    for position, content in db.execute("SELECT position, content FROM bench_fragments ORDER BY position"):
        text = " ".join(content.split()).casefold()
        for index, needle in enumerate(folded):
            if needle and len(found[index]) < limit and needle in text:
                found[index].append(position)
    return found


def _outline(db) -> list[dict[str, Any]]:
    """Position and caller metadata of every stored fragment, in insertion order."""
    return [{"position": row[0], "meta": json.loads(row[1])}
            for row in db.execute("SELECT position, meta FROM bench_fragments ORDER BY position")]


def _with_neighbors(db, hits: list[dict[str, Any]], radius: int,
                    settings: TamWorkerSettings) -> list[dict[str, Any]]:
    """TAM's session window around each hit: up to `radius` records before and after it
    in the same session (fragment `session`), in insertion order.

    Returns a flat list: each hit followed by its neighbours (rank None, `anchor` = the
    hit's position). A neighbour that is itself a ranked hit stays a hit.
    """
    from memory_core.evidence_window import MAX_WINDOW_RECORDS, EvidenceWindow
    from memory_core.retrieval import SearchScope

    if not hits:
        return []
    anchors = [{"id": hit["knowledge_id"]} for hit in hits]
    expanded = EvidenceWindow(db).expand(anchors, scope=SearchScope(project=settings.project), radius=radius,
                                         max_neighbors=min(MAX_WINDOW_RECORDS, 2 * radius * len(hits)))
    neighbor_ids = [row["id"] for row in expanded if row.get("anchor_id") is not None]
    rows: dict[int, Any] = {}
    for offset in range(0, len(neighbor_ids), SQLITE_MAX_PARAMS):
        batch = neighbor_ids[offset:offset + SQLITE_MAX_PARAMS]
        marks = ",".join("?" for _ in batch)
        for row in db.execute(f"SELECT knowledge_id, position, meta, content FROM bench_fragments "
                              f"WHERE knowledge_id IN ({marks})", batch):
            rows[row[0]] = row
    by_anchor: dict[int, list[dict[str, Any]]] = {}
    ranked = {hit["knowledge_id"] for hit in hits}
    for record in expanded:
        anchor = record.get("anchor_id")
        if anchor is None or record["id"] in ranked or record["id"] not in rows:
            continue
        row = rows[record["id"]]
        by_anchor.setdefault(anchor, []).append({
            "rank": None, "position": row[1], "meta": json.loads(row[2]), "content": row[3],
            "knowledge_id": record["id"]})
    result: list[dict[str, Any]] = []
    for hit in hits:
        result.append(hit)
        position = hit["position"]
        neighbours = sorted(by_anchor.get(hit["knowledge_id"], []), key=lambda item: item["position"])
        result.extend({**item, "anchor": position} for item in neighbours)
    return result


def _fill(hits: list[dict[str, Any]], fill_chars: int, overhead: int,
          record_cap: int | None) -> list[dict[str, Any]]:
    """TAM's budget fill over ranked hits: whole hits (each with its neighbours, when the
    search asked for them) in rank order while they fit.

    A record costs its content length (capped at record_cap when the caller cuts each
    record to that size) plus a fixed per-record overhead for the caller's labels.
    """
    from memory_core.context_budget import fill_budget

    def cost(hit: dict[str, Any]) -> int:
        size = len(hit["content"])
        return (min(size, record_cap) if record_cap else size) + overhead

    kept = fill_budget(hit_groups([{**hit, "id": hit["position"]} for hit in hits]), fill_chars, cost=cost)
    return [{key: value for key, value in hit.items() if key != "id"} for hit in kept]


def hit_groups(hits: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the flat search result into groups: a ranked hit and the neighbours after it."""
    groups: list[list[dict[str, Any]]] = []
    for hit in hits:
        if hit.get("anchor") is None or not groups:
            groups.append([hit])
        else:
            groups[-1].append(hit)
    return groups


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------

def _shutdown(process: subprocess.Popen, memory_dir: str, slots: threading.Semaphore | None) -> None:
    try:
        if process.poll() is None:
            try:
                process.stdin.write(json.dumps({"op": "close"}) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                process.wait(timeout=SHUTDOWN_GRACE_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    finally:
        shutil.rmtree(memory_dir, ignore_errors=True)
        if slots is not None:
            slots.release()


class TamStoreProcess:
    """One private TAM store owned by one worker process.

    Fragments passed to add() are dicts with `index_text` (what TAM indexes), optional
    `content` (what search returns; defaults to index_text), optional JSON-serialisable
    `meta` and optional `session` (the TAM session the fragment is saved under, e.g. its
    trajectory or conversation session; search(radius=...) returns neighbours from the same
    session only). search() returns hits in TAM's rank order with rank, position
    (insertion order), meta, content and knowledge_id.
    """

    def __init__(self, settings: TamWorkerSettings, slots: threading.Semaphore | None = None,
                 prefix: str = "tam-bench-", seed_dir: str | Path | None = None):
        self.settings = settings.validate()
        self._lock = threading.Lock()
        if slots is not None:
            slots.acquire()
        try:
            if settings.work_root is not None:
                check_work_root(settings.work_root).mkdir(parents=True, exist_ok=True)
            self.memory_dir = tempfile.mkdtemp(prefix=prefix, dir=settings.work_root)
            if seed_dir is not None:
                # Start from a saved store (see snapshot()): the worker opens a copy of it.
                shutil.copytree(seed_dir, self.memory_dir, dirs_exist_ok=True)
            python = settings.python_executable or sys.executable
            self._process = subprocess.Popen(
                [python, "-m", "tam_bench_common.tam_worker", json.dumps(asdict(settings))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8",
                env=worker_environment(dict(os.environ), self.memory_dir, settings), cwd=self.memory_dir)
        except BaseException:
            if slots is not None:
                slots.release()
            raise
        self._finalizer = weakref.finalize(self, _shutdown, self._process, self.memory_dir, slots)
        try:
            self._read_reply("startup")
        except BaseException:
            self.close()
            raise

    def _read_reply(self, op: str) -> Any:
        ready, _, _ = select.select([self._process.stdout], [], [], self.settings.worker_timeout_s)
        if not ready:
            raise TimeoutError(f"TAM worker did not answer {op!r} within {self.settings.worker_timeout_s}s")
        line = self._process.stdout.readline()
        if not line:
            raise RuntimeError(f"TAM worker exited during {op!r} (code {self._process.poll()})")
        reply = json.loads(line)
        if not reply["ok"]:
            raise RuntimeError(f"TAM worker {op} failed: {reply['error']}")
        return reply["data"]

    def call(self, op: str, **payload: Any) -> Any:
        with self._lock:
            if not self._finalizer.alive:
                raise RuntimeError("TAM store is closed")
            self._process.stdin.write(json.dumps({"op": op, **payload}) + "\n")
            self._process.stdin.flush()
            return self._read_reply(op)

    def add(self, fragments: list[dict[str, Any]]) -> dict[str, Any]:
        for index, fragment in enumerate(fragments):
            if not isinstance(fragment.get("index_text"), str) or not fragment["index_text"].strip():
                raise ValueError(f"fragment {index} has no index_text")
        if not fragments:
            return {"fragments": 0}
        return self.call("add", fragments=fragments)

    def search(self, query: str, limit: int | None = None, *, radius: int = 0, fill_chars: int | None = None,
               fill_overhead: int = 0, fill_record_cap: int | None = None) -> list[dict[str, Any]]:
        """Ranked hits. With radius, each hit is followed by up to `radius` records before and
        after it from the same fragment `session` (rank None, `anchor` = the hit's position).
        With fill_chars, TAM's budget fill keeps whole hits (with their neighbours) in rank
        order while their cost (content, capped at fill_record_cap, plus fill_overhead) fits."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if type(radius) is not int or not 0 <= radius <= MAX_RADIUS:
            raise ValueError(f"radius must be an integer between 0 and {MAX_RADIUS}")
        if fill_chars is not None and (type(fill_chars) is not int or fill_chars < 1):
            raise ValueError("fill_chars must be a positive integer")
        if type(fill_overhead) is not int or fill_overhead < 0:
            raise ValueError("fill_overhead must be a non-negative integer")
        if fill_record_cap is not None and (type(fill_record_cap) is not int or fill_record_cap < 1):
            raise ValueError("fill_record_cap must be a positive integer")
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if radius:
            payload["radius"] = radius
        if fill_chars is not None:
            payload.update(fill_chars=fill_chars, fill_overhead=fill_overhead, fill_record_cap=fill_record_cap)
        return self.call("search", **payload)

    def fetch(self, positions: list[int]) -> list[dict[str, Any]]:
        """Stored fragments by insertion position (rank None), in the order asked for;
        positions that hold no fragment are left out."""
        if any(type(position) is not int for position in positions):
            raise ValueError("positions must be integers")
        if not positions:
            return []
        return self.call("fetch", positions=list(positions))

    def grep(self, needles: list[str], limit: int) -> list[list[int]]:
        """Per needle, positions of the fragments that contain it verbatim (case and
        whitespace runs ignored), at most `limit` each, in insertion order."""
        if any(not isinstance(needle, str) or not needle.strip() for needle in needles):
            raise ValueError("needles must be non-empty strings")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        if not needles:
            return []
        return self.call("grep", needles=list(needles), limit=limit)

    def outline(self) -> list[dict[str, Any]]:
        """Position and metadata of every stored fragment, in insertion order."""
        return self.call("outline")

    def snapshot(self, target_dir: str | Path) -> None:
        """Copy the store (checkpointed, WAL folded in) to target_dir, which must not exist;
        a later TamStoreProcess(..., seed_dir=target_dir) serves the same records."""
        self.call("checkpoint")
        with self._lock:
            shutil.copytree(self.memory_dir, target_dir)

    def close(self) -> None:
        self._finalizer()


if __name__ == "__main__":
    serve(TamWorkerSettings(**json.loads(sys.argv[1])))
