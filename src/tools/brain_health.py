#!/usr/bin/env python3
"""Brain Health Check — verify all autonomous components are operational."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paths import memory_dir

# Paths
MEMORY_DIR = memory_dir()
DB_PATH = MEMORY_DIR / "memory.db"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
CONFIG_DIR = MEMORY_DIR

# LaunchAgent identifiers
LAUNCH_AGENTS = {
    "com.claude.memory-reflection": "Reflection Scheduler",
    "com.claude.memory-telegram": "Telegram Bot",
    "com.claude.memory-dashboard": "Dashboard (port 37737)",
    "com.claude.memory-file-watcher": "File Watcher",
    "com.claude.memory-auto-extract": "Auto Extract",
}

# Config files expected by brain features
CONFIG_FILES = {
    "monitored_projects.json": "Monitored Projects (git-observer)",
    "github_repos.json": "GitHub Repos (tech-radar)",
    "rss_feeds.json": "RSS Feeds (tech-radar)",
}


@dataclass
class HealthResult:
    """Health check result for a single component."""

    name: str
    status: str  # "ok", "warning", "error"
    last_run: str
    details: str


def check_launchctl_status() -> dict[str, dict[str, Any]]:
    """Get status of all claude LaunchAgents via launchctl."""
    result: dict[str, dict[str, Any]] = {}
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in proc.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) == 3 and "com.claude" in parts[2]:
                label = parts[2].strip()
                pid = parts[0].strip()
                exit_code = parts[1].strip()
                result[label] = {
                    "pid": pid if pid != "-" else None,
                    "exit_code": int(exit_code) if exit_code != "-" else None,
                    "running": pid != "-",
                }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return result


def check_launch_agents(launchctl_status: dict[str, dict[str, Any]]) -> list[HealthResult]:
    """Check LaunchAgent plist files and their running status."""
    results: list[HealthResult] = []

    for label, display_name in LAUNCH_AGENTS.items():
        plist_path = LAUNCH_AGENTS_DIR / f"{label}.plist"

        if not plist_path.exists():
            results.append(HealthResult(
                name=f"LaunchAgent: {display_name}",
                status="error",
                last_run="N/A",
                details=f"Plist not found: {plist_path}",
            ))
            continue

        status_info = launchctl_status.get(label)
        if status_info is None:
            results.append(HealthResult(
                name=f"LaunchAgent: {display_name}",
                status="error",
                last_run="N/A",
                details="Not loaded in launchctl",
            ))
        elif status_info["running"]:
            results.append(HealthResult(
                name=f"LaunchAgent: {display_name}",
                status="ok",
                last_run="running",
                details=f"PID {status_info['pid']}",
            ))
        else:
            exit_code = status_info.get("exit_code", "?")
            status = "ok" if exit_code == 0 else "warning"
            results.append(HealthResult(
                name=f"LaunchAgent: {display_name}",
                status=status,
                last_run="idle",
                details=f"Exit code: {exit_code}",
            ))

    return results


def check_log_freshness() -> list[HealthResult]:
    """Check log files for recency of activity."""
    results: list[HealthResult] = []
    log_files = {
        "reflection-scheduler.log": ("Reflection Log", 86400),
        "telegram-bot.log": ("Telegram Log", 3600),
        "dashboard.log": ("Dashboard Log", 3600),
        "file-watcher.log": ("File Watcher Log", 3600),
        "auto-extract.log": ("Auto Extract Log", 86400),
    }

    for filename, (display_name, max_age_sec) in log_files.items():
        log_path = MEMORY_DIR / filename
        if not log_path.exists():
            results.append(HealthResult(
                name=f"Log: {display_name}",
                status="warning",
                last_run="N/A",
                details="Log file not found",
            ))
            continue

        mtime = log_path.stat().st_mtime
        age_sec = time.time() - mtime
        age_str = _format_age(age_sec)

        if age_sec > max_age_sec * 3:
            status = "error"
        elif age_sec > max_age_sec:
            status = "warning"
        else:
            status = "ok"

        size = log_path.stat().st_size
        results.append(HealthResult(
            name=f"Log: {display_name}",
            status=status,
            last_run=age_str,
            details=f"Size: {_format_size(size)}",
        ))

    return results


def check_database(db_path: Path | None = None) -> HealthResult:
    """Check memory database health."""
    path = db_path or DB_PATH
    if not path.exists():
        return HealthResult(
            name="Memory Database",
            status="error",
            last_run="N/A",
            details="Database not found",
        )

    try:
        conn = sqlite3.connect(str(path))
        cursor = conn.cursor()

        # Record count
        cursor.execute("SELECT COUNT(*) FROM knowledge")
        total = cursor.fetchone()[0]

        # Last entry timestamp
        cursor.execute("SELECT MAX(updated_at) FROM knowledge")
        last_update = cursor.fetchone()[0] or "never"

        # DB file size
        size = path.stat().st_size

        # Check for self-improvement tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        extra_parts: list[str] = []
        for t in ["errors", "insights", "rules"]:
            if t in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {t}")  # noqa: S608
                count = cursor.fetchone()[0]
                extra_parts.append(f"{t}={count}")

        conn.close()

        details = f"Records: {total}, Size: {_format_size(size)}"
        if extra_parts:
            details += f", Self-improve: {', '.join(extra_parts)}"

        return HealthResult(
            name="Memory Database",
            status="ok",
            last_run=str(last_update),
            details=details,
        )
    except Exception as e:
        return HealthResult(
            name="Memory Database",
            status="error",
            last_run="N/A",
            details=f"DB error: {e}",
        )


def check_chroma() -> HealthResult:
    """Check ChromaDB vector store."""
    chroma_dir = MEMORY_DIR / "chroma"
    if not chroma_dir.exists():
        return HealthResult(
            name="ChromaDB (Vector Store)",
            status="error",
            last_run="N/A",
            details="Chroma directory not found",
        )

    size = sum(f.stat().st_size for f in chroma_dir.rglob("*") if f.is_file())
    return HealthResult(
        name="ChromaDB (Vector Store)",
        status="ok",
        last_run="available",
        details=f"Size: {_format_size(size)}",
    )


def check_ollama() -> HealthResult:
    """Check Ollama availability and key models."""
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return HealthResult(
                name="Ollama",
                status="error",
                last_run="N/A",
                details=f"ollama list failed: {proc.stderr.strip()[:60]}",
            )

        models: list[str] = []
        for line in proc.stdout.strip().split("\n")[1:]:
            if line.strip():
                model_name = line.split()[0]
                models.append(model_name)

        has_brain = any("vitalii-brain" in m for m in models)
        has_embed = any("nomic-embed" in m for m in models)

        status = "ok" if has_brain and has_embed else "warning"
        missing: list[str] = []
        if not has_brain:
            missing.append("vitalii-brain")
        if not has_embed:
            missing.append("nomic-embed-text")

        details = f"Models: {len(models)}"
        if missing:
            details += f", Missing: {', '.join(missing)}"
        else:
            details += " (brain + embed ready)"

        return HealthResult(
            name="Ollama",
            status=status,
            last_run="available",
            details=details,
        )
    except FileNotFoundError:
        return HealthResult(
            name="Ollama",
            status="error",
            last_run="N/A",
            details="Ollama not installed",
        )
    except subprocess.TimeoutExpired:
        return HealthResult(
            name="Ollama",
            status="error",
            last_run="N/A",
            details="Ollama timed out (not running?)",
        )


def check_config_files() -> list[HealthResult]:
    """Check that brain config files exist and contain valid JSON."""
    results: list[HealthResult] = []

    for filename, display_name in CONFIG_FILES.items():
        filepath = CONFIG_DIR / filename
        if not filepath.exists():
            results.append(HealthResult(
                name=f"Config: {display_name}",
                status="error",
                last_run="N/A",
                details=f"Not found: {filepath}",
            ))
            continue

        try:
            with open(filepath) as f:
                data = json.load(f)

            count = len(data) if isinstance(data, list) else len(data.keys())
            results.append(HealthResult(
                name=f"Config: {display_name}",
                status="ok",
                last_run="present",
                details=f"Entries: {count}",
            ))
        except json.JSONDecodeError as e:
            results.append(HealthResult(
                name=f"Config: {display_name}",
                status="error",
                last_run="present",
                details=f"Invalid JSON: {e}",
            ))

    return results


def check_dashboard_port() -> HealthResult:
    """Check if dashboard is listening on port 37737."""
    try:
        proc = subprocess.run(
            ["lsof", "-i", ":37737", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.stdout.strip():
            return HealthResult(
                name="Dashboard Port 37737",
                status="ok",
                last_run="listening",
                details="Port is active",
            )
        return HealthResult(
            name="Dashboard Port 37737",
            status="warning",
            last_run="N/A",
            details="Port not listening",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return HealthResult(
            name="Dashboard Port 37737",
            status="warning",
            last_run="N/A",
            details="Could not check port",
        )


def _format_age(seconds: float) -> str:
    """Format seconds into a human-readable age string."""
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def _format_size(size_bytes: int | float) -> str:
    """Format byte count into a human-readable size string."""
    val = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{int(val)}{unit}" if unit == "B" else f"{val:.1f}{unit}"
        val /= 1024
    return f"{val:.1f}TB"


def check_brain_health(db_path: str | None = None) -> dict[str, Any]:
    """Check health of all brain components. Returns structured status dict."""
    resolved_db = Path(db_path) if db_path else None
    launchctl_status = check_launchctl_status()

    all_results: list[HealthResult] = []
    all_results.extend(check_launch_agents(launchctl_status))
    all_results.append(check_database(resolved_db))
    all_results.append(check_chroma())
    all_results.append(check_ollama())
    all_results.extend(check_log_freshness())
    all_results.extend(check_config_files())
    all_results.append(check_dashboard_port())

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": [
            {
                "name": r.name,
                "status": r.status,
                "last_run": r.last_run,
                "details": r.details,
            }
            for r in all_results
        ],
        "summary": {
            "total": len(all_results),
            "ok": sum(1 for r in all_results if r.status == "ok"),
            "warning": sum(1 for r in all_results if r.status == "warning"),
            "error": sum(1 for r in all_results if r.status == "error"),
        },
    }


def print_health_table(health: dict[str, Any]) -> None:
    """Print a formatted health check table to stdout."""
    print()
    print("=" * 80)
    print("  CLAUDE BRAIN -- HEALTH CHECK")
    print(f"  {health['timestamp']}")
    print("=" * 80)
    print()

    name_w = 38
    status_w = 8
    last_w = 14

    header = f"  {'Component':<{name_w}} {'Status':<{status_w}} {'Last Run':<{last_w}} {'Details'}"
    print(header)
    print("  " + "-" * 78)

    for comp in health["components"]:
        raw_status = comp["status"].upper()
        if raw_status == "OK":
            status_fmt = f"\033[32m{'OK':<{status_w}}\033[0m"
        elif raw_status == "WARNING":
            status_fmt = f"\033[33m{'WARN':<{status_w}}\033[0m"
        else:
            status_fmt = f"\033[31m{'ERR':<{status_w}}\033[0m"

        name = comp["name"][:name_w]
        last_run = comp["last_run"][:last_w]
        details = comp["details"]

        print(f"  {name:<{name_w}} {status_fmt} {last_run:<{last_w}} {details}")

    print()
    s = health["summary"]
    ok_str = f"\033[32m{s['ok']} OK\033[0m"
    parts = [f"Total: {s['total']}", ok_str]
    if s["warning"]:
        parts.append(f"\033[33m{s['warning']} WARN\033[0m")
    if s["error"]:
        parts.append(f"\033[31m{s['error']} ERR\033[0m")

    if s["error"] > 0:
        overall = "\033[31mUNHEALTHY\033[0m"
    elif s["warning"] > 0:
        overall = "\033[33mDEGRADED\033[0m"
    else:
        overall = "\033[32mHEALTHY\033[0m"

    print(f"  Summary: {' | '.join(parts)}")
    print(f"  Overall: {overall}")
    print()


def main() -> None:
    """Entry point for CLI usage."""
    health = check_brain_health()
    print_health_table(health)


if __name__ == "__main__":
    main()
