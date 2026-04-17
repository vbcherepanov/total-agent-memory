"""Tests for src/procedural.py — v7.0 Phase B."""

import sqlite3
from pathlib import Path

import pytest

from procedural import ProceduralMemory


@pytest.fixture
def pm_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migration = Path(__file__).parent.parent / "migrations" / "009_procedural.sql"
    conn.executescript(migration.read_text())
    yield conn
    conn.close()


@pytest.fixture
def pm(pm_db):
    return ProceduralMemory(pm_db)


# ──────────────────────────────────────────────
# learn_workflow
# ──────────────────────────────────────────────

def test_learn_workflow_creates_new(pm):
    wf_id = pm.learn_workflow(
        "deploy_docker",
        ["build", "test", "push", "apply"],
        description="Deploy via Docker",
        trigger_pattern="deploy",
        context={"stack": "docker"},
    )
    wf = pm.get_workflow(wf_id)
    assert wf["name"] == "deploy_docker"
    assert wf["steps"] == ["build", "test", "push", "apply"]
    assert wf["trigger_pattern"] == "deploy"
    assert wf["context"] == {"stack": "docker"}
    assert wf["times_run"] == 0
    assert wf["success_rate"] == 0.0


def test_learn_workflow_upserts_on_same_name_project(pm):
    id1 = pm.learn_workflow("wf", ["a"])
    id2 = pm.learn_workflow("wf", ["a", "b"])
    assert id1 == id2
    wf = pm.get_workflow(id1)
    assert wf["steps"] == ["a", "b"]


def test_learn_workflow_validates_input(pm):
    with pytest.raises(ValueError):
        pm.learn_workflow("", ["step"])
    with pytest.raises(ValueError):
        pm.learn_workflow("wf", [])
    with pytest.raises(ValueError):
        pm.learn_workflow("wf", "not a list")


def test_learn_workflow_separate_projects(pm):
    id1 = pm.learn_workflow("wf", ["a"], project="p1")
    id2 = pm.learn_workflow("wf", ["a"], project="p2")
    assert id1 != id2


# ──────────────────────────────────────────────
# track_outcome
# ──────────────────────────────────────────────

def test_track_outcome_records_and_updates_aggregates(pm):
    wf_id = pm.learn_workflow("deploy", ["build", "push"])
    pm.track_outcome(wf_id, "success", duration_ms=5000)
    pm.track_outcome(wf_id, "success", duration_ms=7000)
    pm.track_outcome(wf_id, "failure", duration_ms=3000, error_details="push timeout")

    wf = pm.get_workflow(wf_id)
    assert wf["times_run"] == 3
    assert wf["success_count"] == 2
    assert wf["failure_count"] == 1
    assert abs(wf["success_rate"] - 2/3) < 1e-6
    assert wf["avg_duration_ms"] == 5000  # (5000+7000+3000)/3


def test_track_outcome_rejects_invalid_outcome(pm):
    wf_id = pm.learn_workflow("wf", ["a"])
    with pytest.raises(ValueError):
        pm.track_outcome(wf_id, "unknown_status")


def test_track_outcome_rejects_missing_workflow(pm):
    with pytest.raises(ValueError):
        pm.track_outcome("ghost_id", "success")


def test_track_outcome_accepts_partial_and_aborted(pm):
    wf_id = pm.learn_workflow("wf", ["a"])
    pm.track_outcome(wf_id, "partial")
    pm.track_outcome(wf_id, "aborted")
    wf = pm.get_workflow(wf_id)
    assert wf["times_run"] == 2
    assert wf["success_count"] == 0
    assert wf["failure_count"] == 0


# ──────────────────────────────────────────────
# predict_outcome
# ──────────────────────────────────────────────

def test_predict_returns_not_found(pm):
    pred = pm.predict_outcome(workflow_id="ghost")
    assert pred["found"] is False
    assert pred["success_probability"] is None


def test_predict_laplace_smoothed_on_single_run(pm):
    wf_id = pm.learn_workflow("wf", ["a"])
    pm.track_outcome(wf_id, "success")
    pred = pm.predict_outcome(workflow_id=wf_id)
    # (1+1)/(1+2) = 0.667, NOT 1.0
    assert pred["found"] is True
    assert abs(pred["success_probability"] - 2/3) < 1e-3
    assert pred["confidence"] < 0.5  # low confidence on 1 run


def test_predict_confidence_saturates(pm):
    wf_id = pm.learn_workflow("wf", ["a"])
    for _ in range(30):
        pm.track_outcome(wf_id, "success")
    pred = pm.predict_outcome(workflow_id=wf_id)
    assert pred["confidence"] >= 0.85


def test_predict_by_trigger_pattern(pm):
    wf_id = pm.learn_workflow(
        "rollback_deploy", ["revert", "verify"],
        trigger_pattern="rollback",
    )
    pm.track_outcome(wf_id, "success")
    pred = pm.predict_outcome(trigger="rollback")
    assert pred["found"] is True
    assert pred["workflow_id"] == wf_id


def test_predict_picks_highest_success_rate(pm):
    a = pm.learn_workflow("deploy_v1", ["a"], trigger_pattern="deploy")
    b = pm.learn_workflow("deploy_v2", ["a"], trigger_pattern="deploy")
    pm.track_outcome(a, "failure")
    pm.track_outcome(a, "failure")
    pm.track_outcome(b, "success")
    pm.track_outcome(b, "success")
    pred = pm.predict_outcome(trigger="deploy")
    assert pred["workflow_id"] == b


# ──────────────────────────────────────────────
# listing & stats
# ──────────────────────────────────────────────

def test_list_workflows_orders_by_success_rate_desc(pm):
    low = pm.learn_workflow("low", ["a"])
    high = pm.learn_workflow("high", ["a"])
    pm.track_outcome(low, "failure")
    pm.track_outcome(high, "success")

    listing = pm.list_workflows()
    names = [w["name"] for w in listing]
    assert names.index("high") < names.index("low")


def test_list_workflows_hides_deprecated_by_default(pm):
    a = pm.learn_workflow("active", ["a"])
    d = pm.learn_workflow("dep", ["a"])
    pm.deprecate_workflow(d)
    listing = pm.list_workflows()
    names = [w["name"] for w in listing]
    assert "active" in names
    assert "dep" not in names


def test_recent_runs_returns_in_reverse_chronological_order(pm):
    wf_id = pm.learn_workflow("wf", ["a"])
    pm.track_outcome(wf_id, "success", notes="first")
    pm.track_outcome(wf_id, "failure", notes="second")
    runs = pm.recent_runs(wf_id)
    assert len(runs) == 2
    assert runs[0]["notes"] == "second"
    assert runs[1]["notes"] == "first"


def test_stats(pm):
    a = pm.learn_workflow("a", ["x"])
    b = pm.learn_workflow("b", ["x"])
    pm.deprecate_workflow(b)
    pm.track_outcome(a, "success")
    pm.track_outcome(a, "failure")
    s = pm.stats()
    assert s["total_workflows"] == 2
    assert s["active_workflows"] == 1
    assert s["total_runs"] == 2


def test_deprecate_workflow(pm):
    wf_id = pm.learn_workflow("wf", ["a"])
    assert pm.deprecate_workflow(wf_id) is True
    wf = pm.get_workflow(wf_id)
    assert wf["status"] == "deprecated"
