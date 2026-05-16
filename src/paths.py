"""Memory directory + env var resolution with backward-compat migration.

Single source of truth for "where does this install store its data".
Replaces the historical pattern

    MEMORY_DIR = Path(os.environ.get("CLAUDE_MEMORY_DIR",
                                     os.path.expanduser("~/.claude-memory")))

scattered across 18+ modules. All code that needs the memory dir must
call ``memory_dir()`` so the one-time migration from the legacy
``~/.claude-memory/`` to ``~/.tam/`` happens transparently on the first
access after upgrade.

Env vars:
    TAM_MEMORY_DIR      — preferred (new)
    CLAUDE_MEMORY_DIR   — legacy (still respected; emits DeprecationWarning)

Filesystem:
    ~/.tam/             — preferred
    ~/.claude-memory/   — legacy; auto-migrated on first call, replaced
                          with a symlink to ~/.tam/ so scripts pinned to
                          the old path keep working.
"""
from __future__ import annotations

import os
import shutil
import sys
import warnings
from pathlib import Path

NEW_ENV = "TAM_MEMORY_DIR"
OLD_ENV = "CLAUDE_MEMORY_DIR"
NEW_DIR = Path.home() / ".tam"
OLD_DIR = Path.home() / ".claude-memory"

_warned_env = False
_migration_attempted = False


def memory_dir() -> Path:
    """Resolve the memory directory, migrating from legacy layout if needed.

    Resolution order:
        1. ``$TAM_MEMORY_DIR`` (new env)
        2. ``$CLAUDE_MEMORY_DIR`` (legacy env, with DeprecationWarning)
        3. ``~/.tam/`` if it already exists
        4. ``~/.claude-memory/`` → migrate to ``~/.tam/`` (move + symlink back)
        5. fresh ``~/.tam/`` for new installs

    Idempotent and safe to call from any number of modules; the migration
    runs at most once per process.
    """
    global _warned_env

    new_env_val = os.environ.get(NEW_ENV)
    if new_env_val:
        path = Path(new_env_val).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    old_env_val = os.environ.get(OLD_ENV)
    if old_env_val:
        if not _warned_env:
            warnings.warn(
                f"{OLD_ENV} is deprecated; please switch to {NEW_ENV}. "
                f"The legacy variable will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
            _warned_env = True
        path = Path(old_env_val).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    if NEW_DIR.exists():
        return NEW_DIR

    if OLD_DIR.exists() and not OLD_DIR.is_symlink():
        if _try_migrate():
            return NEW_DIR
        return OLD_DIR

    NEW_DIR.mkdir(parents=True, exist_ok=True)
    return NEW_DIR


def memory_db() -> Path:
    """Convenience: full path to the SQLite memory database."""
    return memory_dir() / "memory.db"


def _try_migrate() -> bool:
    """Move ``~/.claude-memory/`` to ``~/.tam/`` and leave a symlink behind.

    Returns True on success (or if already migrated), False on failure.
    Failure is non-fatal: the caller falls back to the legacy directory
    so the server keeps working until the user resolves the issue.
    """
    global _migration_attempted
    if _migration_attempted:
        return NEW_DIR.exists()
    _migration_attempted = True

    if NEW_DIR.exists():
        return True
    if not OLD_DIR.exists() or OLD_DIR.is_symlink():
        return False

    try:
        shutil.move(str(OLD_DIR), str(NEW_DIR))
    except (OSError, shutil.Error) as exc:
        sys.stderr.write(
            f"[total-agent-memory] WARN: could not migrate {OLD_DIR} -> {NEW_DIR}: {exc}\n"
            f"  Falling back to {OLD_DIR}. Run `total-agent-memory migrate` "
            f"after fixing permissions to complete the rename.\n"
        )
        return False

    try:
        os.symlink(str(NEW_DIR), str(OLD_DIR))
        symlink_note = "symlink kept for backward-compat"
    except OSError as exc:
        symlink_note = f"symlink NOT created ({exc}); scripts pinned to {OLD_DIR} will break"

    sys.stderr.write(
        f"[total-agent-memory] Migrated {OLD_DIR} -> {NEW_DIR} ({symlink_note}).\n"
    )
    return True


def migrate_now() -> dict:
    """Manual migration entry-point for the CLI.

    Returns a small status dict so the CLI can format a user-facing
    message. Always safe to call; idempotent.
    """
    if NEW_DIR.exists() and not OLD_DIR.exists():
        return {"status": "already_migrated", "new": str(NEW_DIR)}
    if NEW_DIR.exists() and OLD_DIR.is_symlink():
        return {"status": "already_migrated", "new": str(NEW_DIR), "symlink": str(OLD_DIR)}
    if not OLD_DIR.exists():
        NEW_DIR.mkdir(parents=True, exist_ok=True)
        return {"status": "fresh_install", "new": str(NEW_DIR)}
    if OLD_DIR.is_symlink():
        return {"status": "already_migrated", "new": str(NEW_DIR), "symlink": str(OLD_DIR)}
    if NEW_DIR.exists():
        return {
            "status": "conflict",
            "message": f"Both {OLD_DIR} and {NEW_DIR} exist as real directories. "
                       f"Merge them manually, then remove {OLD_DIR}.",
        }
    ok = _try_migrate()
    if ok:
        return {"status": "migrated", "from": str(OLD_DIR), "to": str(NEW_DIR)}
    return {"status": "failed", "from": str(OLD_DIR), "to": str(NEW_DIR)}


def _reset_for_tests() -> None:
    """Test helper. Resets module-level state so tests can re-trigger logic."""
    global _warned_env, _migration_attempted
    _warned_env = False
    _migration_attempted = False
