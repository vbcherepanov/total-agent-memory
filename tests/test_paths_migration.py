"""Tests for src/paths.py — memory dir resolution + ~/.claude-memory -> ~/.tam migration."""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Re-point Path.home() and clear migration state so each test starts clean."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TAM_MEMORY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_MEMORY_DIR", raising=False)
    import paths
    paths.NEW_DIR = tmp_path / ".tam"
    paths.OLD_DIR = tmp_path / ".claude-memory"
    paths._reset_for_tests()
    yield tmp_path


def test_fresh_install_creates_tam(isolated_home):
    import paths
    result = paths.memory_dir()
    assert result == isolated_home / ".tam"
    assert result.is_dir()
    assert not (isolated_home / ".claude-memory").exists()


def test_existing_tam_used_as_is(isolated_home):
    (isolated_home / ".tam").mkdir()
    (isolated_home / ".tam" / "memory.db").write_text("preexisting")
    import paths
    result = paths.memory_dir()
    assert result == isolated_home / ".tam"
    assert (result / "memory.db").read_text() == "preexisting"


def test_legacy_dir_migrates_to_tam_with_symlink(isolated_home):
    old = isolated_home / ".claude-memory"
    old.mkdir()
    (old / "memory.db").write_text("data")
    (old / "wikis").mkdir()
    (old / "wikis" / "proj.md").write_text("wiki")

    import paths
    result = paths.memory_dir()

    assert result == isolated_home / ".tam"
    assert (result / "memory.db").read_text() == "data"
    assert (result / "wikis" / "proj.md").read_text() == "wiki"
    assert old.is_symlink()
    assert old.resolve() == result.resolve()


def test_legacy_symlink_treated_as_already_migrated(isolated_home):
    (isolated_home / ".tam").mkdir()
    (isolated_home / ".tam" / "memory.db").write_text("new")
    (isolated_home / ".claude-memory").symlink_to(isolated_home / ".tam")

    import paths
    result = paths.memory_dir()
    assert result == isolated_home / ".tam"
    assert (result / "memory.db").read_text() == "new"


def test_new_env_takes_priority(isolated_home, monkeypatch, tmp_path):
    custom = tmp_path / "custom"
    monkeypatch.setenv("TAM_MEMORY_DIR", str(custom))
    import paths
    result = paths.memory_dir()
    assert result == custom
    assert custom.is_dir()


def test_legacy_env_warns_and_used(isolated_home, monkeypatch, tmp_path):
    custom = tmp_path / "legacy-custom"
    monkeypatch.setenv("CLAUDE_MEMORY_DIR", str(custom))
    import paths
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = paths.memory_dir()
    assert result == custom
    assert any(
        issubclass(w.category, DeprecationWarning) and "CLAUDE_MEMORY_DIR" in str(w.message)
        for w in caught
    )


def test_new_env_wins_over_legacy_env(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("TAM_MEMORY_DIR", str(tmp_path / "new"))
    monkeypatch.setenv("CLAUDE_MEMORY_DIR", str(tmp_path / "old"))
    import paths
    result = paths.memory_dir()
    assert result == tmp_path / "new"


def test_legacy_env_warning_emitted_once(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_MEMORY_DIR", str(tmp_path / "legacy"))
    import paths
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        paths.memory_dir()
        paths.memory_dir()
        paths.memory_dir()
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecation) == 1


def test_migrate_now_fresh_install(isolated_home):
    import paths
    status = paths.migrate_now()
    assert status["status"] == "fresh_install"
    assert (isolated_home / ".tam").is_dir()


def test_migrate_now_migrates_legacy(isolated_home):
    old = isolated_home / ".claude-memory"
    old.mkdir()
    (old / "f.txt").write_text("x")
    import paths
    status = paths.migrate_now()
    assert status["status"] == "migrated"
    assert (isolated_home / ".tam" / "f.txt").read_text() == "x"
    assert old.is_symlink()


def test_migrate_now_idempotent_after_symlink(isolated_home):
    (isolated_home / ".tam").mkdir()
    (isolated_home / ".claude-memory").symlink_to(isolated_home / ".tam")
    import paths
    status = paths.migrate_now()
    assert status["status"] == "already_migrated"


def test_migrate_now_detects_conflict(isolated_home):
    (isolated_home / ".tam").mkdir()
    (isolated_home / ".tam" / "a").write_text("new")
    (isolated_home / ".claude-memory").mkdir()
    (isolated_home / ".claude-memory" / "b").write_text("old")
    import paths
    status = paths.migrate_now()
    assert status["status"] == "conflict"


def test_memory_db_returns_db_path(isolated_home):
    import paths
    db = paths.memory_db()
    assert db == isolated_home / ".tam" / "memory.db"
    assert (isolated_home / ".tam").is_dir()
