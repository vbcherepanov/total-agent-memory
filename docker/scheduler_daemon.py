#!/usr/bin/env python3
"""Cron-style scheduler for total-agent-memory Docker deployments.

Replaces the LaunchAgent / systemd timer set that ships with the host
install (com.claude.memory.orphan-backfill, com.claude.memory.check-updates)
so a single container has the same maintenance behaviour out of the box.

Jobs:
  orphan_backfill — every 6h  (00:00, 06:00, 12:00, 18:00 by default)
  check_updates   — weekly on Sunday 03:00

No external scheduling deps — just `time.sleep` against ``datetime.now()``.
Each job runs as a short-lived subprocess so a hang in one tool doesn't
wedge the others.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Callable

from worker_health import Heartbeat


PY = sys.executable
SRC = os.environ.get("CLAUDE_TOTAL_MEMORY_SRC", "/app/src")


def _log(msg: str) -> None:
    sys.stdout.write(f"[scheduler] {msg}\n")
    sys.stdout.flush()


def _run(cmd: list[str], heartbeat: Heartbeat, timeout: int = 1800) -> None:
    _log(f"running: {' '.join(cmd)}")
    try:
        with heartbeat.job(timeout):
            r = subprocess.run(
                cmd, cwd="/app", timeout=timeout,
                env={**os.environ, "PYTHONPATH": "/app"},
            )
        _log(f"  exit={r.returncode}")
    except subprocess.TimeoutExpired:
        _log(f"  TIMEOUT after {timeout}s")
    except Exception as e:
        _log(f"  ERROR: {e}")


def _job_orphan_backfill(heartbeat: Heartbeat) -> None:
    runner = os.path.join(SRC, "tools", "backfill_orphan_edges.py")
    if not os.path.exists(runner):
        _log(f"skip orphan_backfill — {runner} missing")
        return
    _run([PY, runner, "--min-mentions=1", "--limit=500", "--trigger-now"], heartbeat)


def _job_check_updates(heartbeat: Heartbeat) -> None:
    runner = os.path.join(SRC, "tools", "check_updates.py")
    if not os.path.exists(runner):
        _log(f"skip check_updates — {runner} missing")
        return
    _run([PY, runner], heartbeat, timeout=300)


@dataclass
class Job:
    name: str
    fn: Callable[[Heartbeat], None]
    # Returns the next datetime this job should fire after the given `now`.
    next_fire: Callable[[datetime], datetime]


def _next_orphan(now: datetime) -> datetime:
    # Fire at 00, 06, 12, 18 every day (4× per day).
    for hour in (0, 6, 12, 18):
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    # Past 18:00 — next is tomorrow midnight.
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _next_weekly_sunday(now: datetime) -> datetime:
    # Fire Sunday 03:00 each week. weekday(): Mon=0 … Sun=6.
    candidate = now.replace(hour=3, minute=0, second=0, microsecond=0)
    days_ahead = (6 - candidate.weekday()) % 7
    if days_ahead == 0 and candidate <= now:
        days_ahead = 7
    return candidate + timedelta(days=days_ahead)


JOBS = [
    Job("orphan_backfill", _job_orphan_backfill, _next_orphan),
    Job("check_updates", _job_check_updates, _next_weekly_sunday),
]


def main() -> int:
    heartbeat = Heartbeat("scheduler")
    _log(f"started — {len(JOBS)} jobs")
    now = datetime.now()
    schedule = {j.name: j.next_fire(now) for j in JOBS}
    for j in JOBS:
        _log(f"  {j.name} → next fire {schedule[j.name].isoformat(timespec='seconds')}")

    while True:
        heartbeat.pulse()
        now = datetime.now()
        # Find next due job; if any is overdue, run immediately.
        for j in JOBS:
            if schedule[j.name] <= now:
                _log(f"firing: {j.name}")
                try:
                    j.fn(heartbeat)
                except Exception as e:
                    _log(f"  job {j.name} crashed: {e}")
                schedule[j.name] = j.next_fire(datetime.now())
                _log(f"  next {j.name}: {schedule[j.name].isoformat(timespec='seconds')}")
        # Sleep until next due time (cap at 60s so SIGTERM is responsive
        # — Python's default `time.sleep` blocks signal delivery only on
        # the main thread on some platforms; 60s is the practical max
        # wait between checks).
        next_due = min(schedule.values())
        sleep_for = max(1.0, min(60.0, (next_due - datetime.now()).total_seconds()))
        heartbeat.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
