"""Standalone reflection runner — for cron / LaunchAgent.

Runs reflection.agent with scope auto-picked from pending queue depth:
  - no pending            → run_quick    (dedup + decay, <1s)
  - pending 1..20         → run_full     (all 6 phases, 1-3 min)
  - pending >20           → run_full     (same, just more work to do)
  - any failed in queue   → flag for operator, still run_full

Usage:
    ~/claude-memory-server/.venv/bin/python src/tools/run_reflection.py [--scope=auto|quick|full|weekly]

Exits with code 0 on success, 1 on unhandled error. Logs go to stderr.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))

from paths import memory_dir as _resolve_memory_dir


def _log(msg: str) -> None:
    sys.stderr.write(f"[reflection-runner] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    sys.stderr.flush()


def pending_depth(db: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for tbl in ("triple_extraction_queue", "deep_enrichment_queue", "representations_queue"):
        try:
            out[tbl] = db.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE status='pending'"
            ).fetchone()[0]
        except sqlite3.Error:
            out[tbl] = 0
    return out


def pick_scope(depth: dict[str, int], override: str = "auto") -> str:
    if override in ("quick", "full", "weekly", "drain"):
        return override
    total_pending = sum(depth.values())
    if total_pending == 0:
        return "quick"
    # Any pending work → fast drain path only (skip expensive digest+synthesize).
    # digest runs on the hourly StartInterval cron anyway — no need to block
    # on-save reflection with it. Explicit --scope=full still works.
    return "drain"


async def _run(agent, scope: str) -> dict:
    if scope == "quick":
        return await agent.run_quick()
    if scope == "weekly":
        return await agent.run_weekly()
    if scope == "drain":
        return await agent.run_drain()
    return await agent.run_full()


def _acquire_lock(lock_path: Path) -> bool:
    """Return True if we grabbed the lock, False if another runner holds it.

    Uses PID-based locking (no fcntl needed, works on stale locks too).
    If the lock PID is alive, back off. Otherwise take over.
    """
    try:
        if lock_path.exists():
            try:
                old_pid = int(lock_path.read_text().strip())
                os.kill(old_pid, 0)  # throws if pid not alive
                return False
            except (ValueError, ProcessLookupError, PermissionError):
                # stale lock — overwrite
                pass
        lock_path.write_text(str(os.getpid()))
        return True
    except Exception:
        return True  # fail open — better to run than deadlock


def _release_lock(lock_path: Path) -> None:
    try:
        if lock_path.exists() and lock_path.read_text().strip() == str(os.getpid()):
            lock_path.unlink()
    except Exception:
        pass


def _debounce(trigger_path: Path, debounce_seconds: float) -> None:
    """Sleep, re-checking the trigger mtime so bursts of saves coalesce."""
    if not trigger_path.exists() or debounce_seconds <= 0:
        return
    deadline = time.time() + debounce_seconds
    last_mtime = trigger_path.stat().st_mtime
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            m = trigger_path.stat().st_mtime
            if m > last_mtime:
                # Another save ticked — reset the countdown
                last_mtime = m
                deadline = time.time() + debounce_seconds
        except FileNotFoundError:
            break


def main() -> int:
    # CLI arg parsing (minimal, no argparse — keep imports cheap)
    scope_override = "auto"
    debounce_seconds = float(os.environ.get("REFLECT_DEBOUNCE_SEC", "10"))
    for arg in sys.argv[1:]:
        if arg.startswith("--scope="):
            scope_override = arg.split("=", 1)[1]
        elif arg.startswith("--debounce="):
            debounce_seconds = float(arg.split("=", 1)[1])

    memory_dir = _resolve_memory_dir()
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        _log(f"db not found at {db_path}")
        return 1

    # Lock against concurrent runners (watchpath can re-fire while we work).
    lock_path = memory_dir / ".reflect.lock"
    if not _acquire_lock(lock_path):
        _log(f"another runner holds {lock_path} — exiting")
        return 0

    # Debounce: wait for save bursts to settle before draining.
    trigger_path = memory_dir / ".reflect-pending"
    if trigger_path.exists() and debounce_seconds > 0:
        _log(f"debouncing {debounce_seconds}s for save burst to settle")
        _debounce(trigger_path, debounce_seconds)
        # Consume the trigger so WatchPaths doesn't immediately re-fire
        try:
            trigger_path.unlink()
        except FileNotFoundError:
            pass

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    # Auto-pick scope from queue depth
    depth = pending_depth(db)
    scope = pick_scope(depth, scope_override)
    _log(f"pending={depth} → scope={scope}")

    # Build embedder bound to Store (shares FastEmbed / Ollama)
    embedder = None
    try:
        import server as _srv  # noqa: E402
        _srv.MEMORY_DIR = memory_dir
        store = _srv.Store()
        def embed(text: str) -> list[float]:
            embs = store.embed([text])
            return embs[0] if embs else []
        embedder = embed
    except Exception as e:  # noqa: BLE001
        _log(f"embedder init failed (repr generation will skip): {e}")

    from reflection.agent import ReflectionAgent  # noqa: E402

    agent = ReflectionAgent(db, embedder=embedder)

    t0 = time.time()
    try:
        result = asyncio.run(_run(agent, scope))
    except Exception as e:  # noqa: BLE001
        _log(f"reflection crashed: {e}")
        db.close()
        return 1

    elapsed = round(time.time() - t0, 1)
    _log(f"done in {elapsed}s — {json.dumps(result, default=str)[:500]}")
    db.close()
    _release_lock(lock_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
