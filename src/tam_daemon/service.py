"""The daemon process: TAM's MCP app over streamable HTTP plus token-protected /internal/* endpoints.

Routes (every one requires ``Authorization: Bearer <daemon.token>`` and a
loopback Host/Origin, the same DNS-rebinding rules as MCP_TRANSPORT=http):

    POST/GET/DELETE /mcp               MCP streamable HTTP (stateless sessions)
    GET             /internal/health   liveness + warm-up state, see `health`
    POST            /internal/hook/recall   compact recall for hooks, see hook_recall.py
    POST            /internal/shutdown graceful stop (202), see `shutdown`

Start-up never blocks on the store: the socket is bound and served first,
the Store (which loads the embedding model) is built and pre-warmed on a
background thread, and only requests that need it (tools/call and the hook
endpoint) wait for it. initialize, ping and tools/list answer immediately.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from tam_daemon import hook_recall
from tam_daemon.logs import configure as configure_logging
from tam_daemon.logs import get_logger
from tam_daemon.registry import (
    DaemonPaths,
    DaemonRecord,
    FileLock,
    load_or_create_token,
    remove_record,
    write_record,
)
from tam_daemon.settings import SECONDS_PER_MINUTE, DaemonSettings

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

LOGGER = get_logger("service")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ALREADY_RUNNING = 3
# A starter probing the lock holds it for an instant; wait that out before concluding "already running".
LOCK_ACQUIRE_GRACE_SEC = 2.0
# How long tools/call may wait for the store on a cold daemon (first run may download the model).
STORE_READY_TIMEOUT_SEC = 300.0
# Hooks run on the user's prompt path; they get an answer or a 503 quickly.
HOOK_READY_TIMEOUT_SEC = 10.0
GRACEFUL_SHUTDOWN_SEC = 5
FLUSH_JOIN_TIMEOUT_SEC = 10.0
IDLE_CHECKS_PER_WINDOW = 4
MIN_IDLE_CHECK_SEC = 0.5
MAX_IDLE_CHECK_SEC = 30.0
# Buffering cap for the gate that inspects MCP POST bodies (the SDK's own limit is lower).
MAX_GATED_BODY_BYTES = 16 * 1024 * 1024
MCP_ROUTE = "/mcp"
HEALTH_ROUTE = "/internal/health"
WARMUP_TEXT = "warmup"
STATUS_STARTING = "starting"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
JSONRPC_SERVER_NOT_READY = -32002
# Methods answered without the store; anything else waits for bootstrap.
STORE_FREE_METHODS = frozenset({
    "initialize", "ping", "tools/list", "server/discover",
    "notifications/initialized", "notifications/cancelled", "notifications/roots/list_changed",
})


def _counters() -> Any:
    from memory_core.telemetry import counters
    return counters


def _op_timer(name: str) -> Any:
    from memory_core.telemetry import op_timer
    return op_timer(name)


@dataclass
class DaemonState:
    """Mutable runtime state shared by the routes, the bootstrap thread and the idle watcher."""

    tam: ModuleType
    settings: DaemonSettings
    memory_dir: Path
    version: str
    port: int = 0
    started_wall: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    in_flight: int = 0
    store_ready: bool = False
    embedding_warm: bool = False
    bootstrap_error: str | None = None
    exit_code: int = EXIT_OK
    exit_reason: str = ""
    server: Any = None
    loop: asyncio.AbstractEventLoop | None = None
    ready_event: asyncio.Event | None = None
    bootstrap_thread: threading.Thread | None = None
    seen: hook_recall.SeenTracker = field(default_factory=hook_recall.SeenTracker)

    @property
    def status(self) -> str:
        if self.bootstrap_error is not None:
            return STATUS_FAILED
        return STATUS_READY if self.store_ready else STATUS_STARTING

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def request_exit(self, reason: str, code: int = EXIT_OK) -> None:
        if not self.exit_reason:
            self.exit_reason, self.exit_code = reason, code
        LOGGER.info("daemon stopping", extra={"reason": reason, "exit_code": code})
        if self.server is not None:
            self.server.should_exit = True

    async def wait_ready(self, timeout_sec: float) -> bool:
        if self.store_ready:
            return True
        if self.ready_event is None or self.bootstrap_error is not None:
            return False
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout_sec)
        except TimeoutError:
            return False
        return self.store_ready

    # ── bootstrap (background thread) ─────────────────────────────────

    def start_bootstrap(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.ready_event = asyncio.Event()
        from memory_core.exit_join import join_at_exit

        self.bootstrap_thread = threading.Thread(target=self._bootstrap, name="tam-daemon-bootstrap", daemon=True)
        self.bootstrap_thread.start()
        join_at_exit(self.bootstrap_thread, FLUSH_JOIN_TIMEOUT_SEC)  # prewarm runs onnxruntime

    def _notify_loop(self, callback: Callable[[], None]) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            LOGGER.info("event loop gone before bootstrap finished")
            return
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError as exc:
            LOGGER.info("event loop closed before bootstrap finished", extra={"error": str(exc)})

    def _bootstrap(self) -> None:
        started = time.perf_counter()
        try:
            with _op_timer("daemon_bootstrap_ms"):
                asyncio.run(self.tam._bootstrap_session())
                self._prewarm_embedding()
        except Exception as exc:
            # Any bootstrap failure leaves the daemon unusable: report it and exit.
            self.bootstrap_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("store bootstrap failed", extra={"error": self.bootstrap_error})
            self._notify_loop(self._bootstrap_failed)
            return
        LOGGER.info("store ready", extra={"seconds": round(time.perf_counter() - started, 3),
                                          "embedding_warm": self.embedding_warm})
        self._notify_loop(self._bootstrap_done)

    def _prewarm_embedding(self) -> None:
        """First inference through the store's embedder, so the first recall does not pay for it.

        Runs before the store is published to request handlers: the store's
        SQLite connection is shared, and this keeps it single-threaded.
        """
        with _op_timer("daemon_prewarm_embed_ms"):
            vectors = asyncio.run(self.tam._prewarm_session(WARMUP_TEXT))
        self.embedding_warm = bool(vectors) and vectors[0] is not None
        if not self.embedding_warm:
            LOGGER.warning("embedding prewarm returned no vector; recall falls back to lexical tiers")

    def _bootstrap_done(self) -> None:
        self.store_ready = True
        if self.ready_event is not None:
            self.ready_event.set()

    def _bootstrap_failed(self) -> None:
        if self.ready_event is not None:
            self.ready_event.set()
        self.request_exit("bootstrap-failed", EXIT_FAILED)

    # ── shutdown ──────────────────────────────────────────────────────

    def flush(self) -> None:
        """Stop background work and close the store; called once the HTTP server has drained."""
        thread = self.bootstrap_thread
        if thread is not None and thread.is_alive():
            thread.join(FLUSH_JOIN_TIMEOUT_SEC)
            if thread.is_alive():
                LOGGER.warning("bootstrap still running at shutdown; stopping Store owner")
        try:
            asyncio.run(self.tam._shutdown_session())
        except Exception as exc:  # noqa: BLE001 — backend-specific errors (sqlite3 / psycopg); logged, exit continues
            LOGGER.warning("store close failed", extra={"error": f"{type(exc).__name__}: {exc}"})
            self.exit_code = EXIT_FAILED
            return
        LOGGER.info("background work flushed")

    def health(self) -> dict[str, Any]:
        """Body of GET /internal/health.

        {"status": "starting"|"ready"|"failed", "version": str, "pid": int,
         "port": int, "memory_dir": str, "uptime_sec": float,
         "started_at": float (unix), "idle_exit_min": float (0 = never),
         "in_flight": int, "error": str|null,
         "warm": {"store": bool, "embedding": bool,
                  "reranker": "disabled"|"pending"|"loading"|"ready"}}
        """
        return {
            "status": self.status,
            "version": self.version,
            "pid": os.getpid(),
            "port": self.port,
            "memory_dir": str(self.memory_dir),
            "uptime_sec": round(time.monotonic() - self.started_monotonic, 3),
            "started_at": self.started_wall,
            "idle_exit_min": self.settings.idle_exit_sec / SECONDS_PER_MINUTE,
            "in_flight": self.in_flight,
            "error": self.bootstrap_error,
            "warm": {"store": self.store_ready, "embedding": self.embedding_warm,
                     "reranker": self._reranker_state()},
        }

    def _reranker_state(self) -> str:
        if not self.store_ready:
            return "pending"
        from memory_core.cross_rerank import shared_reranker
        cross = shared_reranker()
        if cross is None:
            return "disabled"
        return "ready" if cross.ready else "loading"

    async def idle_watch(self) -> None:
        idle = self.settings.idle_exit_sec
        if idle <= 0:
            return
        interval = min(MAX_IDLE_CHECK_SEC, max(MIN_IDLE_CHECK_SEC, idle / IDLE_CHECKS_PER_WINDOW))
        while True:
            await asyncio.sleep(interval)
            if self.in_flight == 0 and time.monotonic() - self.last_activity >= idle:
                LOGGER.info("idle timeout reached", extra={"idle_sec": idle})
                self.request_exit("idle")
                return


# ── ASGI layers ──────────────────────────────────────────────────────


async def _send_json(send: Send, status: int, payload: object, headers: list[tuple[bytes, bytes]] = ()) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            *headers]})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    """The whole request body, or None once it exceeds `limit` bytes."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(chunks)
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replaying(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


class BearerAuth:
    """401 unless the request carries the daemon's bearer token (constant-time compare)."""

    def __init__(self, inner: ASGIApp, token: str):
        self.inner = inner
        self.expected = f"Bearer {token}".encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            given = next((v for k, v in scope.get("headers", []) if k == b"authorization"), b"")
            if not hmac.compare_digest(given, self.expected):
                _counters().bump("daemon_auth_rejected")
                await _send_json(send, 401, {"ok": False, "error": "unauthorized"},
                                 [(b"www-authenticate", b"Bearer")])
                return
        await self.inner(scope, receive, send)


class ActivityTracker:
    """Request accounting for the idle timer and metrics; health probes do not keep the daemon alive."""

    def __init__(self, inner: ASGIApp, state: DaemonState):
        self.inner, self.state = inner, state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == HEALTH_ROUTE:
            await self.inner(scope, receive, send)
            return
        state = self.state
        state.in_flight += 1
        state.touch()
        _counters().bump("daemon_http_requests")
        try:
            with _op_timer("daemon_http_ms"):
                await self.inner(scope, receive, send)
        finally:
            state.in_flight -= 1
            state.touch()


class StoreGate:
    """Holds MCP requests that need the store until the background bootstrap has finished."""

    def __init__(self, inner: ASGIApp, state: DaemonState):
        self.inner, self.state = inner, state

    @staticmethod
    def needs_store(body: bytes) -> bool:
        try:
            message = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return False  # the SDK answers the parse error itself
        messages = message if isinstance(message, list) else [message]
        return any(isinstance(m, dict) and isinstance(m.get("method"), str) and m["method"] not in STORE_FREE_METHODS
                   for m in messages)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or self.state.store_ready:
            await self.inner(scope, receive, send)
            return
        body = await _read_body(receive, MAX_GATED_BODY_BYTES)
        if body is None:
            await _send_json(send, 413, {"ok": False, "error": "request body too large"})
            return
        if self.needs_store(body):
            _counters().bump("daemon_requests_gated")
            if not await self.state.wait_ready(STORE_READY_TIMEOUT_SEC):
                request_id = None
                try:
                    parsed = json.loads(body)
                    request_id = parsed.get("id") if isinstance(parsed, dict) else None
                except ValueError:
                    request_id = None
                await _send_json(send, 503, {"jsonrpc": "2.0", "id": request_id, "error": {
                    "code": JSONRPC_SERVER_NOT_READY,
                    "message": f"TAM daemon store unavailable: {self.state.bootstrap_error or 'still warming up'}"}})
                return
        await self.inner(scope, _replaying(body, receive), send)


def build_app(tam: ModuleType, state: DaemonState, token: str) -> ASGIApp:
    """The full ASGI stack: rebinding guard -> bearer auth -> activity -> routes."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    hosts, origins = tam._http_allowed_hosts(state.settings.host, {})
    manager = StreamableHTTPSessionManager(app=tam.app, event_store=None, json_response=False, stateless=True,
                                           **tam._transport_security_kwargs(hosts, origins))

    async def serve_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    async def health(_request: Request) -> Response:
        return JSONResponse(state.health())

    async def shutdown(_request: Request) -> Response:
        """POST /internal/shutdown -> 202 {"ok": true, "pid": int}; the daemon drains, flushes and exits."""
        _counters().bump("daemon_shutdown_requests")
        return JSONResponse({"ok": True, "pid": os.getpid()}, status_code=202,
                            background=BackgroundTask(state.request_exit, "shutdown-endpoint"))

    async def hook_recall_route(request: Request) -> Response:
        body = await _read_body(request.receive, hook_recall.MAX_BODY_BYTES)
        if body is None:
            return JSONResponse({"ok": False, "error": "request body too large"}, status_code=413)
        try:
            req = hook_recall.parse_request(body)
        except hook_recall.HookRequestError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if not req.query:
            _counters().bump("daemon_hook_recall_automation_skipped")
            return JSONResponse({"ok": True, "text": "", "count": 0, "ids": [],
                                 "skipped_seen": 0, "truncated": False, "elapsed_ms": 0,
                                 "dropped_floor": 0})
        if not await state.wait_ready(HOOK_READY_TIMEOUT_SEC):
            return JSONResponse({"ok": False, "error": state.bootstrap_error or "store is warming up"},
                                status_code=503)
        started = time.perf_counter()
        _counters().bump("daemon_hook_recall_requests")
        with _op_timer("daemon_hook_recall_ms"):
            content, is_error = await tam._call_tool_impl("memory_recall", req.recall_arguments())
        text = content[0].text if content else ""
        if is_error:
            _counters().bump("daemon_hook_recall_errors")
            return JSONResponse({"ok": False, "error": text or "memory_recall failed"}, status_code=500)
        try:
            result = json.loads(text)
        except ValueError:
            _counters().bump("daemon_hook_recall_errors")
            LOGGER.error("memory_recall returned non-JSON", extra={"preview": text[:200]})
            return JSONResponse({"ok": False, "error": "memory_recall returned an unexpected payload"},
                                status_code=500)
        track = req.dedupe and req.session_id is not None
        seen = state.seen.seen(req.session_id) if track else frozenset()
        block = hook_recall.compact_block(result if isinstance(result, dict) else {}, req, seen)
        _counters().bump("daemon_hook_recall_dropped_floor", block.dropped_floor)
        if track and block.ids:
            state.seen.remember(req.session_id, block.ids)
        return JSONResponse({"ok": True, "text": block.text, "count": len(block.ids), "ids": block.ids,
                             "skipped_seen": block.skipped_seen, "truncated": block.truncated,
                             "dropped_floor": block.dropped_floor,
                             "elapsed_ms": int((time.perf_counter() - started) * 1000)})

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> Any:
        async with manager.run():
            state.start_bootstrap()
            idle_task = asyncio.create_task(state.idle_watch(), name="tam-daemon-idle")
            try:
                yield
            finally:
                idle_task.cancel()
        await asyncio.to_thread(state.flush)

    starlette_app = Starlette(debug=False, lifespan=lifespan, routes=[
        Route(MCP_ROUTE, StoreGate(serve_mcp, state), methods=["GET", "POST", "DELETE"]),
        Route(HEALTH_ROUTE, health, methods=["GET"]),
        Route("/internal/hook/recall", hook_recall_route, methods=["POST"]),
        Route("/internal/shutdown", shutdown, methods=["POST"]),
    ])
    return tam._RebindingGuard(BearerAuth(ActivityTracker(starlette_app, state), token), hosts, origins)


def _import_server() -> ModuleType:
    src = str(Path(__file__).resolve().parents[1])
    if src not in sys.path:
        sys.path.insert(0, src)
    import server
    return server


def serve(settings: DaemonSettings) -> int:
    """Run the daemon in the foreground until shutdown, idle exit or a signal. Returns the exit code."""
    import uvicorn

    from paths import memory_dir as resolve_memory_dir
    from version import VERSION

    configure_logging()
    memory_dir = resolve_memory_dir().resolve()
    paths = DaemonPaths.for_memory_dir(memory_dir)
    paths.ensure_root()
    lock = FileLock(paths.lock)
    if not lock.acquire(LOCK_ACQUIRE_GRACE_SEC):
        LOGGER.info("daemon already running for this memory dir", extra={"memory_dir": str(memory_dir)})
        return EXIT_ALREADY_RUNNING
    try:
        token = load_or_create_token(paths)
        try:
            sock = socket.create_server((settings.host, settings.port))
        except OSError as exc:
            LOGGER.error("cannot bind daemon socket", extra={"host": settings.host, "port": settings.port,
                                                             "error": str(exc)})
            return EXIT_FAILED
        with sock:
            tam = _import_server()
            if Path(tam.MEMORY_DIR).resolve() != memory_dir:
                LOGGER.error("server resolved a different memory dir", extra={
                    "expected": str(memory_dir), "actual": str(tam.MEMORY_DIR)})
                return EXIT_FAILED
            port = sock.getsockname()[1]
            state = DaemonState(tam=tam, settings=settings, memory_dir=memory_dir, version=VERSION, port=port)
            server = uvicorn.Server(uvicorn.Config(build_app(tam, state, token), log_level="warning",
                                                   access_log=False, lifespan="on",
                                                   timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC))
            state.server = server
            write_record(paths, DaemonRecord(pid=os.getpid(), host=settings.host, port=port, version=VERSION,
                                             started_at=state.started_wall, memory_dir=str(memory_dir),
                                             python=sys.executable))
            LOGGER.info("daemon listening", extra={"url": f"http://{settings.host}:{port}{MCP_ROUTE}",
                                                   "pid": os.getpid(), "version": VERSION,
                                                   "memory_dir": str(memory_dir),
                                                   "idle_exit_sec": settings.idle_exit_sec})
            server.run(sockets=[sock])
            if not server.started and not state.exit_reason:
                return EXIT_FAILED
            LOGGER.info("daemon exited", extra={"reason": state.exit_reason or "signal", "code": state.exit_code})
            return state.exit_code
    finally:
        remove_record(paths, pid=os.getpid())
        lock.release()
