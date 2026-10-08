"""The reflection job's consolidation sweep: idle projects judged by knowledge rows, episodes materialized."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import server
from workers.consolidation_daemon import (
    EPISODE_MIN_FACTS,
    consolidate_idle_projects,
    list_idle_projects_by_knowledge,
)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(background_queues=False)
    instance._embed_mode = "none"
    yield instance
    instance.db.close()


def _seed(store, project: str, session_id: str, age: timedelta, count: int) -> None:
    base = datetime.now(UTC) - age
    for index in range(count):
        when = _iso(base + timedelta(minutes=index))
        store.db.execute(
            "INSERT INTO knowledge(content,session_id,project,type,status,created_at,last_confirmed) "
            "VALUES (?,?,?,'fact','active',?,?)",
            (f"{project} fact number {index} about deployment step {index}", session_id, project, when, when),
        )
    store.db.commit()


def test_idle_projects_come_from_knowledge_rows_oldest_first(store):
    _seed(store, "stale", "s-old", timedelta(days=3), EPISODE_MIN_FACTS)
    _seed(store, "older", "s-older", timedelta(days=9), EPISODE_MIN_FACTS)
    _seed(store, "busy", "s-now", timedelta(minutes=1), EPISODE_MIN_FACTS)
    assert list_idle_projects_by_knowledge(store.db, idle_seconds=1800) == ["older", "stale"]


def test_sweep_materializes_episodes_for_old_sessions_and_respects_cooldown(store):
    _seed(store, "stale", "s-old", timedelta(days=3), EPISODE_MIN_FACTS + 2)
    before = store.db.execute("SELECT COUNT(*) FROM episodes_v11").fetchone()[0]
    runs = consolidate_idle_projects(store.db, budget_seconds=60, max_projects=3)
    assert [run.project for run in runs] == ["stale"]
    assert runs[0].error is None and not runs[0].paused
    after = store.db.execute("SELECT COUNT(*) FROM episodes_v11 WHERE project='stale'").fetchone()[0]
    assert after > before
    assert store.db.execute("SELECT last_consolidated_at FROM consolidation_state WHERE project='stale'").fetchone()[0]
    assert consolidate_idle_projects(store.db, budget_seconds=60, max_projects=3) == []


def test_sweep_honours_max_projects_and_budget(store):
    for name, days in (("a", 9), ("b", 8), ("c", 7)):
        _seed(store, name, f"s-{name}", timedelta(days=days), EPISODE_MIN_FACTS)
    runs = consolidate_idle_projects(store.db, budget_seconds=60, max_projects=2)
    assert [run.project for run in runs] == ["a", "b"]
    assert consolidate_idle_projects(store.db, budget_seconds=0, max_projects=5) == []
