#!/usr/bin/env python3
"""Single-container supervisor for total-agent-memory.

Runs MCP HTTP server (port 3737), dashboard HTTP UI (port 37737) and
the reflection daemon side-by-side in one container so

    docker run -p 127.0.0.1:3737:3737 -p 127.0.0.1:37737:37737 ghcr.io/.../total-agent-memory

(publish on 127.0.0.1 only: both servers bind 0.0.0.0 inside the container and require the
access token in ``<data dir>/state/http-token``).

gives you the full stack with one command.

Why not s6-overlay / supervisord?  Each adds a 30-50 MB image bloat,
a config DSL, and another moving part.  This script is ~120 LOC of
stdlib, prefixes child stdout with [mcp]/[dashboard]/[reflection],
restarts on crash with exponential back-off, and forwards SIGTERM /
SIGINT to children so `docker stop` shuts down cleanly.

Override the set of services via TAM_SUPERVISOR_SERVICES (comma-list).
Default: ``mcp,dashboard,reflection``.
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from worker_health import write_services


# ───────────────────────────────────────────────────────────────────────
# Service registry
# ───────────────────────────────────────────────────────────────────────

@dataclass
class ServiceDef:
    name: str
    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)
    # `False` for one-shot drain jobs — they're expected to exit 0.
    long_running: bool = True


def _service_defs() -> list[ServiceDef]:
    """Service catalogue. Run as ``python -m`` so site-packages resolution
    is consistent whether we're in /app (Dockerfile COPY) or a pip-installed
    venv. ``mcp`` runs Streamable HTTP transport (v12.4+) — clients talk
    to it over the network instead of stdio.
    """
    py = sys.executable
    return [
        ServiceDef(
            name="mcp",
            cmd=[py, "-m", "src.server"],
            env={
                "MCP_TRANSPORT": os.environ.get("MCP_TRANSPORT", "http"),
                "MCP_HTTP_HOST": os.environ.get("MCP_HTTP_HOST", "0.0.0.0"),
                "MCP_HTTP_PORT": os.environ.get("MCP_HTTP_PORT", "3737"),
                "MCP_HTTP_WORKERS": os.environ.get("MCP_HTTP_WORKERS", "1"),
            },
        ),
        ServiceDef(
            name="dashboard",
            cmd=[py, "-m", "src.dashboard"],
            env={
                "DASHBOARD_PORT": os.environ.get("DASHBOARD_PORT", "37737"),
                "DASHBOARD_BIND": os.environ.get("DASHBOARD_BIND", "0.0.0.0"),
            },
        ),
        ServiceDef(
            name="reflection",
            cmd=[py, "/app/docker/reflection_daemon.py"],
            env={
                "REFLECT_DEBOUNCE_SEC": os.environ.get("REFLECT_DEBOUNCE_SEC", "5"),
                "REFLECT_INTERVAL_SEC": os.environ.get("REFLECT_INTERVAL_SEC", "3600"),
            },
        ),
        ServiceDef(
            name="scheduler",
            cmd=[py, "/app/docker/scheduler_daemon.py"],
            env={},
        ),
    ]


# ───────────────────────────────────────────────────────────────────────
# Supervisor
# ───────────────────────────────────────────────────────────────────────

@dataclass
class _Child:
    svc: ServiceDef
    proc: subprocess.Popen | None = None
    restarts: int = 0
    last_restart: float = 0.0


def _log(line: str) -> None:
    sys.stdout.write(f"[tam-sup] {line}\n")
    sys.stdout.flush()


def _spawn(svc: ServiceDef) -> subprocess.Popen:
    env = {**os.environ, **svc.env}
    return subprocess.Popen(
        svc.cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )


def _select_services() -> list[ServiceDef]:
    requested = os.environ.get("TAM_SUPERVISOR_SERVICES", "mcp,dashboard,reflection,scheduler")
    wanted = {s.strip() for s in requested.split(",") if s.strip()}
    available = _service_defs()
    avail_names = {s.name for s in available}
    missing = wanted - avail_names
    if missing:
        _log(f"requested services not available: {sorted(missing)} — known: {sorted(avail_names)}")
    return [s for s in available if s.name in wanted]


def _ensure_db_initialised() -> None:
    """One-shot: create memory.db with migrations on a fresh volume.

    Without this, dashboard /api/stats returns 503 ("Database not found")
    until the user runs the MCP server at least once. In Docker the user
    expects ``docker run`` to just work — so we eagerly bootstrap.

    Run in a subprocess to keep ``src.server`` (which is a heavy import
    with side-effects) out of the supervisor's address space.
    """
    mem_dir = os.environ.get("TAM_MEMORY_DIR") or os.environ.get("CLAUDE_MEMORY_DIR") or "/data"
    db_path = os.path.join(mem_dir, "memory.db")
    if os.path.exists(db_path):
        return
    _log(f"initialising fresh database at {db_path}…")
    # ``Store()`` in src/server.py runs _apply_sql_migrations() in its
    # __init__. We just need to construct one, then exit.
    bootstrap = (
        "import sys; sys.path.insert(0, '/app'); "
        "from src.server import Store; Store(); "
        "print('db initialised')"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", bootstrap],
            capture_output=True, text=True, timeout=120,
            cwd="/app",
        )
        if r.returncode == 0:
            _log("database initialised")
        else:
            _log(f"warning: db init exited {r.returncode}: {r.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        _log("warning: db init timed out (>120s) — dashboard /api/stats may return 503")
    except Exception as e:
        _log(f"warning: could not init DB ({e}) — dashboard /api/stats may return 503 until first save")


def _ensure_http_token(services: list[ServiceDef]) -> None:
    """Create the shared access token before the HTTP services start (they bind beyond loopback)."""
    binds = {"mcp": os.environ.get("MCP_HTTP_HOST", "0.0.0.0"),
             "dashboard": os.environ.get("DASHBOARD_BIND", "0.0.0.0")}
    if not any(s.name in binds for s in services):
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
    import http_auth
    mem_dir = Path(os.environ.get("TAM_MEMORY_DIR") or os.environ.get("CLAUDE_MEMORY_DIR") or "/data")
    needs_token = any(not http_auth.is_loopback_bind(binds[s.name]) for s in services if s.name in binds)
    if needs_token:
        http_auth.load_or_create_token(mem_dir)
        _log(f"HTTP access token required for MCP and dashboard: {http_auth.token_file(mem_dir)} "
             f"(or set {http_auth.TOKEN_ENV})")


def main() -> int:
    services = _select_services()
    if not services:
        _log("no services selected — exiting")
        return 0
    write_services([svc.name for svc in services])
    _ensure_db_initialised()
    _ensure_http_token(services)
    _log(f"starting: {', '.join(s.name for s in services)}")

    children: list[_Child] = []
    for svc in services:
        proc = _spawn(svc)
        children.append(_Child(svc=svc, proc=proc, restarts=0, last_restart=time.time()))
        _log(f"spawned {svc.name} pid={proc.pid}")

    shutdown_requested = False

    def _handle_term(signum: int, _frame) -> None:
        nonlocal shutdown_requested
        _log(f"caught signal {signum} — forwarding to children")
        shutdown_requested = True
        for ch in children:
            if ch.proc and ch.proc.poll() is None:
                try:
                    ch.proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    # Poll loop: forward child stdout with [name] prefix, restart on crash.
    streams: dict[int, _Child] = {ch.proc.stdout.fileno(): ch for ch in children if ch.proc and ch.proc.stdout}
    for fd in streams:
        os.set_blocking(fd, False)

    MAX_RESTARTS = int(os.environ.get("TAM_SUPERVISOR_MAX_RESTARTS", "10"))
    RESTART_WINDOW = float(os.environ.get("TAM_SUPERVISOR_RESTART_WINDOW", "60"))

    while True:
        # Reap exited children, restart long-running ones, exit if all gone.
        alive = 0
        for ch in children:
            if ch.proc is None:
                continue
            rc = ch.proc.poll()
            if rc is None:
                alive += 1
                continue
            # Process exited.
            _log(f"{ch.svc.name} exited with code {rc}")
            try:
                if ch.proc.stdout:
                    fd = ch.proc.stdout.fileno()
                    if fd in streams:
                        del streams[fd]
            except (ValueError, OSError):
                pass
            ch.proc = None
            if shutdown_requested:
                continue
            if not ch.svc.long_running and rc == 0:
                continue  # one-shot success
            # Restart with back-off and rate-limit.
            now = time.time()
            if (now - ch.last_restart) > RESTART_WINDOW:
                ch.restarts = 0  # reset window
            ch.restarts += 1
            if ch.restarts > MAX_RESTARTS:
                _log(f"{ch.svc.name}: {ch.restarts} restarts in <{RESTART_WINDOW}s — giving up; signalling shutdown")
                shutdown_requested = True
                for other in children:
                    if other.proc and other.proc.poll() is None:
                        try: other.proc.send_signal(signal.SIGTERM)
                        except ProcessLookupError: pass
                continue
            backoff = min(2 ** (ch.restarts - 1), 30)
            _log(f"restarting {ch.svc.name} in {backoff}s (attempt {ch.restarts}/{MAX_RESTARTS})")
            time.sleep(backoff)
            ch.proc = _spawn(ch.svc)
            ch.last_restart = time.time()
            if ch.proc.stdout:
                fd = ch.proc.stdout.fileno()
                os.set_blocking(fd, False)
                streams[fd] = ch
            alive += 1

        if alive == 0:
            _log("no live children — exiting")
            return 1 if not shutdown_requested else 0

        # Drain stdout from all children.
        if streams:
            ready, _, _ = select.select(list(streams.keys()), [], [], 0.5)
            for fd in ready:
                ch = streams.get(fd)
                if not ch or not ch.proc or not ch.proc.stdout:
                    continue
                try:
                    chunk = ch.proc.stdout.read()
                except (BlockingIOError, ValueError):
                    chunk = None
                if not chunk:
                    continue
                for line in chunk.rstrip("\n").split("\n"):
                    sys.stdout.write(f"[{ch.svc.name}] {line}\n")
                sys.stdout.flush()
        else:
            time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
