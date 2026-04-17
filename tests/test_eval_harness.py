"""Tests for src/eval_harness.py — v7.0 Phase F."""

import json

import pytest

from eval_harness import EvalHarness


# ──────────────────────────────────────────────
# Scenario loading
# ──────────────────────────────────────────────

def test_load_scenarios_from_list():
    h = EvalHarness()
    scenarios = [{"type": "recall", "query": "x", "must_contain": []}]
    loaded = h.load_scenarios(scenarios)
    assert loaded == scenarios


def test_load_scenarios_from_file(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps([{"type": "recall", "query": "a", "must_contain": []}]))
    h = EvalHarness()
    loaded = h.load_scenarios(f)
    assert len(loaded) == 1


def test_load_scenarios_from_dir(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"type": "recall", "query": "a",
                                                  "must_contain": []}))
    (tmp_path / "b.json").write_text(json.dumps([{"type": "recall", "query": "b",
                                                   "must_contain": []}]))
    loaded = EvalHarness().load_scenarios(tmp_path)
    assert len(loaded) == 2


# ──────────────────────────────────────────────
# Recall scenarios
# ──────────────────────────────────────────────

def test_recall_pass_when_all_terms_present():
    def fake_recall(q, params):
        return [{"content": "Binary quantization speeds up vector search"}]
    h = EvalHarness(recall_fn=fake_recall)
    r = h.run_scenario({
        "name": "s1", "type": "recall", "query": "vector search",
        "must_contain": ["binary quantization"], "k": 5,
    })
    assert r.passed is True
    assert r.details["first_match_rank"] == 1
    assert r.latency_ms >= 0


def test_recall_fail_when_term_missing():
    def fake_recall(q, params):
        return [{"content": "something else"}]
    h = EvalHarness(recall_fn=fake_recall)
    r = h.run_scenario({
        "name": "s1", "type": "recall", "query": "x",
        "must_contain": ["needle"],
    })
    assert r.passed is False
    assert "needle" in r.details["missing"]


def test_recall_fails_without_recall_fn():
    h = EvalHarness()
    r = h.run_scenario({"type": "recall", "query": "x", "must_contain": ["y"]})
    assert r.passed is False
    assert "error" in r.details


# ──────────────────────────────────────────────
# Prevention scenarios
# ──────────────────────────────────────────────

def test_prevention_pass_when_warning_matches():
    def fake_warn(path, params):
        return {
            "warnings": [{"content": "sqlite locked during DDL"}],
            "related_rules": [],
            "risk_score": 0.8,
        }
    h = EvalHarness(file_warnings_fn=fake_warn)
    r = h.run_scenario({
        "type": "prevention", "file": "src/x.py",
        "must_warn_about": ["sqlite"],
    })
    assert r.passed is True
    assert r.details["warnings_count"] == 1


def test_prevention_fail_when_no_warnings():
    def fake_warn(path, params):
        return {"warnings": [], "related_rules": []}
    h = EvalHarness(file_warnings_fn=fake_warn)
    r = h.run_scenario({
        "type": "prevention", "file": "src/x.py",
        "must_warn_about": ["sqlite"],
    })
    assert r.passed is False


def test_prevention_matches_rule_content():
    def fake_warn(path, params):
        return {
            "warnings": [],
            "related_rules": [{"content": "never use raw sql here"}],
        }
    h = EvalHarness(file_warnings_fn=fake_warn)
    r = h.run_scenario({
        "type": "prevention", "file": "x", "must_warn_about": ["raw sql"],
    })
    assert r.passed is True


# ──────────────────────────────────────────────
# Suite aggregation
# ──────────────────────────────────────────────

def test_run_suite_computes_r_at_k():
    # 2 recall scenarios: one matches at rank 1, other at rank 3
    def fake_recall(q, params):
        if q == "q1":
            return [{"content": "needle"}]
        return [{"content": "foo"}, {"content": "bar"}, {"content": "needle"}]
    h = EvalHarness(recall_fn=fake_recall)
    report = h.run_suite([
        {"name": "r1", "type": "recall", "query": "q1",
         "must_contain": ["needle"], "k": 5},
        {"name": "r2", "type": "recall", "query": "q2",
         "must_contain": ["needle"], "k": 5},
    ])
    assert report["total"] == 2
    assert report["passed"] == 2
    # R@1 = 0.5 (only r1 hits at rank 1); R@5 = 1.0
    assert abs(report["recall"]["r_at_1"] - 0.5) < 1e-6
    assert abs(report["recall"]["r_at_5"] - 1.0) < 1e-6


def test_run_suite_computes_latency_percentiles():
    def fake_recall(q, params):
        return [{"content": "needle"}]
    h = EvalHarness(recall_fn=fake_recall)
    report = h.run_suite([
        {"name": f"r{i}", "type": "recall", "query": "q",
         "must_contain": ["needle"]}
        for i in range(10)
    ])
    assert report["latency"]["mean_ms"] >= 0
    assert report["latency"]["p50_ms"] <= report["latency"]["p95_ms"]
    assert report["latency"]["p95_ms"] <= report["latency"]["max_ms"]


def test_run_suite_prevention_rate():
    def fake_warn(path, params):
        # Warn for 2 of 3 files
        if "fail" in path:
            return {"warnings": [], "related_rules": []}
        return {"warnings": [{"content": "sqlite issue"}], "related_rules": []}
    h = EvalHarness(file_warnings_fn=fake_warn)
    report = h.run_suite([
        {"type": "prevention", "file": "src/a.py", "must_warn_about": ["sqlite"]},
        {"type": "prevention", "file": "src/b.py", "must_warn_about": ["sqlite"]},
        {"type": "prevention", "file": "src/fail.py", "must_warn_about": ["sqlite"]},
    ])
    assert report["prevention"]["total"] == 3
    assert report["prevention"]["passed"] == 2
    assert abs(report["prevention"]["rate"] - 2/3) < 1e-3


def test_mixed_suite_aggregates_correctly():
    def fake_recall(q, params):
        return [{"content": "needle"}]
    def fake_warn(path, params):
        return {"warnings": [{"content": "sqlite"}], "related_rules": []}
    h = EvalHarness(recall_fn=fake_recall, file_warnings_fn=fake_warn)
    report = h.run_suite([
        {"name": "r1", "type": "recall", "query": "q",
         "must_contain": ["needle"]},
        {"name": "p1", "type": "prevention", "file": "x",
         "must_warn_about": ["sqlite"]},
    ])
    assert report["total"] == 2
    assert report["passed"] == 2
    assert report["recall"]["total"] == 1
    assert report["prevention"]["total"] == 1


def test_unknown_scenario_type_fails_cleanly():
    h = EvalHarness()
    r = h.run_scenario({"type": "nonsense"})
    assert r.passed is False
    assert "error" in r.details
