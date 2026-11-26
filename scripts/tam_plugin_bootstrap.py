#!/usr/bin/env python3
"""Launch the total-agent-memory MCP server from the Claude Code plugin.

The plugin ships source, not a Python environment, so this picks the first
runnable server it can find and `exec`s it — stdio is handed straight to
Claude Code, so this process must be replaced, never wrapped.

Resolution order, cheapest and most predictable first:

1. An existing install's venv (``$TAM_HOME/.venv``, ``~/.tam/.venv``,
   ``~/.claude-memory/.venv``) — the common case for anyone who ran
   ``install.sh``, and the only branch that touches no network.
2. ``total-agent-memory`` already on PATH (pipx, brew, a global pip).
3. The plugin's own checkout, when its dependencies happen to be importable.
4. A venv this script creates under the memory dir and installs into. The
   capture hooks run with this venv's interpreter.
5. ``uvx`` / ``npx``, when the venv cannot be built. The server runs, but the
   hooks have no interpreter with the package.

Every branch ends in exec, so the MCP client sees one process either way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = "total-agent-memory"
MIN_PYTHON = "3.11"  # requires-python in pyproject.toml
BUILD_GRACE_SECONDS = 900  # a build marker older than this belongs to a killed build
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def log(message: str) -> None:
    """Diagnostics go to stderr — stdout is the MCP frame channel."""
    sys.stderr.write(f"[tam-plugin] {message}\n")


def _exe(venv: Path, name: str) -> Path:
    bindir = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv / bindir / f"{name}{suffix}"


def _memory_home() -> Path:
    for env in ("TAM_MEMORY_DIR", "CLAUDE_MEMORY_DIR"):
        value = os.environ.get(env)
        if value:
            return Path(value).expanduser()
    tam = Path.home() / ".tam"
    if tam.exists():
        return tam
    legacy = Path.home() / ".claude-memory"
    return legacy if legacy.exists() else tam


def _candidate_venvs() -> list[Path]:
    seen: list[Path] = []
    for path in (
        _memory_home() / ".venv",
        Path.home() / ".tam" / ".venv",
        Path.home() / ".claude-memory" / ".venv",
        PLUGIN_ROOT / ".venv",
    ):
        if path not in seen:
            seen.append(path)
    return seen


def _exec(argv: list[str]) -> None:
    """Replace this process. Returns only if the command cannot start."""
    log(f"starting: {' '.join(argv)}")
    if os.name == "nt":
        # Windows has no exec that preserves the console handles the way MCP
        # needs, so mirror the child's exit code instead.
        raise SystemExit(subprocess.call(argv))
    os.execv(argv[0], argv)


def _build_marker(venv: Path) -> Path:
    """Exists while `_bootstrap_venv` builds `venv`, and stays when that build was killed."""
    return venv.with_name(venv.name + ".building")


def _from_existing_venv() -> list[str] | None:
    for venv in _candidate_venvs():
        if _build_marker(venv).exists():
            continue  # being built, or left half-built by a killed start
        entry = _exe(venv, "total-agent-memory")
        if entry.is_file():
            return [str(entry)]
        python = _exe(venv, "python")
        server = PLUGIN_ROOT / "src" / "server.py"
        if python.is_file() and server.is_file():
            return [str(python), str(server)]
    return None


def _from_path() -> list[str] | None:
    found = shutil.which("total-agent-memory") or shutil.which("tam")
    return [found] if found else None


def _from_plugin_checkout() -> list[str] | None:
    """Use the shipped source when this interpreter can already import mcp."""
    server = PLUGIN_ROOT / "src" / "server.py"
    if not server.is_file():
        return None
    probe = subprocess.run(
        [sys.executable, "-c", "import mcp"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    return [sys.executable, str(server)]


def _from_runner() -> list[str] | None:
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, PACKAGE]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", PACKAGE]
    return None


def _venv_commands(venv: Path) -> list[list[str]]:
    """The commands that create `venv` and install the package into it.

    uv, when present, also supplies a Python new enough for the package, so an old system
    interpreter does not block the install.
    """
    python = str(_exe(venv, "python"))
    uv = shutil.which("uv")
    if uv:
        return [
            [uv, "venv", "--quiet", "--python", f">={MIN_PYTHON}", str(venv)],
            [uv, "pip", "install", "--quiet", "--python", python, PACKAGE],
        ]
    return [
        [sys.executable, "-m", "venv", str(venv)],
        [python, "-m", "pip", "install", "--quiet", "--upgrade", PACKAGE],
    ]


def _bootstrap_venv() -> list[str] | None:
    """Build a venv under the memory dir and install into it.

    The capture hooks look for their interpreter in this venv (hooks/lib/common.sh), so it is
    tried before a runner: a server started by uvx or npx leaves the hooks with a system Python
    that cannot import the package.
    """
    venv = _memory_home() / ".venv"
    marker = _build_marker(venv)
    try:
        if time.time() - marker.stat().st_mtime < BUILD_GRACE_SECONDS:
            log(f"{venv} is being built by another start")
            return None
    except OSError:
        pass  # no marker: nobody is building
    log(f"no runnable install found — creating {venv}")
    entry = _exe(venv, "total-agent-memory")
    ready = False
    try:
        venv.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        shutil.rmtree(venv, ignore_errors=True)  # what a killed build left behind
        for command in _venv_commands(venv):
            # Captured: stdout is the MCP frame channel.
            subprocess.run(command, check=True, capture_output=True)
        ready = entry.is_file()
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"") or b""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        log(f"bootstrap failed: {e}. {detail.strip()[:400]}")
    finally:
        # Reached on KeyboardInterrupt too: Claude Code interrupts a server that takes longer
        # than MCP_TIMEOUT (30 s by default) to start. A half-built venv would be taken for an
        # existing install on the next start, so it does not stay.
        if not ready:
            shutil.rmtree(venv, ignore_errors=True)
        try:
            if ready or not venv.exists():
                marker.unlink(missing_ok=True)
            else:
                # An installer that was still writing kept part of it: the marker stays, aged
                # so that the next start rebuilds at once.
                os.utime(marker, (0, 0))
        except OSError:
            pass
    return [str(entry)] if ready else None


def main() -> int:
    for resolve in (
        _from_existing_venv,
        _from_path,
        _from_plugin_checkout,
        _bootstrap_venv,
        _from_runner,
    ):
        argv = resolve()
        if argv:
            _exec(argv + sys.argv[1:])
            return 0  # only reached if exec failed outright

    log(
        "could not start the memory server. Install it with one of:\n"
        "  pipx install total-agent-memory\n"
        "  uvx total-agent-memory\n"
        "  npx -y total-agent-memory\n"
        "  curl -fsSL https://get.totalmemory.dev | sh"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
