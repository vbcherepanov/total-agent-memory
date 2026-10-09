"""examples/correction-vs-old-memory: a newer correction retires the older stored memory."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

EXAMPLE = Path(__file__).parent.parent / "examples" / "correction-vs-old-memory" / "run.py"


def load_example():
    spec = importlib.util.spec_from_file_location("correction_vs_old_memory", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


example = load_example()


@pytest.fixture
def srv(monkeypatch, tmp_path):
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(server, "store", server.Store())
    monkeypatch.setattr(server, "recall", server.Recall(server.store))
    monkeypatch.setattr(server, "SID", "test")
    monkeypatch.setattr(server, "BRANCH", "")
    monkeypatch.setattr(server, "_v5_modules", {})
    yield server
    server.store.db.close()


def failures(checks):
    return [(c.name, c.expected, c.actual) for c in checks if not c.passed]


def test_correction_retires_the_older_memory_and_wins_recall(srv):
    checks = example.run_memory_scenario(srv)
    assert len(checks) == 6
    assert failures(checks) == []


def test_dated_kg_facts_answer_point_in_time_queries(srv):
    checks = example.run_kg_scenario(srv)
    assert len(checks) == 4
    assert failures(checks) == []


def test_old_value_is_still_in_history_not_deleted(srv):
    example.run_memory_scenario(srv)
    old_id = srv.store.db.execute("SELECT id FROM knowledge WHERE content=?", (example.OLD_FACT,)).fetchone()[0]
    history = example.call(srv, "memory_history", id=old_id)
    assert history["total_versions"] == 2
    assert [(v["content"], v["status"]) for v in history["versions"]] == [
        (example.NEW_FACT, "active"),
        (example.OLD_FACT, "superseded"),
    ]


def test_report_exit_code_follows_the_checks(capsys):
    passed = example.Check(name="a", expected="x", actual="x", passed=True)
    failed = example.Check(name="b", expected="x", actual="y", passed=False)
    assert example.report([passed]) == 0
    assert example.report([passed, failed]) == 1
    out = capsys.readouterr().out
    assert "PASS  a" in out
    assert "FAIL  b" in out
    assert "1 passed, 1 failed" in out
