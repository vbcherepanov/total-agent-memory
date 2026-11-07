"""The LoCoMo rerank-mode runner: warm-up, readiness accounting and rerun comparison."""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "benchmarks" / "locomo_rerank_modes.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("locomo_rerank_modes_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


class FakeReranker:
    def __init__(self, ready_after_calls: int):
        self.calls = 0
        self._ready_after = ready_after_calls

    @property
    def ready(self) -> bool:
        return self.calls >= self._ready_after


def fake_server(reranker: FakeReranker, seen: list[dict]):
    class Store:
        pass

    class Recall:
        def __init__(self, store):
            self.store = store

        def search(self, **kwargs):
            seen.append(kwargs)
            reranker.calls += 1
            return {"results": {"fact": []}}

    return type("Server", (), {"Store": Store, "Recall": Recall})


def row(question: str, dia_ids: list[str], evidence: list[str], *, category: int = 1, ready: bool = True) -> dict:
    return {"project": "locomo_0", "question": question, "category": category, "evidence": evidence,
            "record_ids": list(range(len(dia_ids))), "dia_ids": dia_ids, "latency_ms": 1.0,
            "encoder_ready_before": ready}


def write_rows(path: Path, rows: list[dict]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for item in rows:
            handle.write(json.dumps(item) + "\n")
    return path


def test_warmup_asks_untimed_queries_without_recording_usage(runner):
    seen: list[dict] = []
    reranker = FakeReranker(ready_after_calls=2)
    result = runner.run_warmup(fake_server(reranker, seen), 4, reranker)

    assert len(seen) == 4
    assert all(call["record_usage"] is False for call in seen)
    assert all(call["project"] == runner.WARMUP_PROJECT for call in seen)
    assert {call["query"] for call in seen} <= set(runner.WARMUP_QUERIES)
    assert result["queries"] == 4
    assert result["encoder_ready_after"] is True


def test_warmup_reports_an_encoder_that_is_still_loading(runner):
    reranker = FakeReranker(ready_after_calls=10)
    result = runner.run_warmup(fake_server(reranker, []), 2, reranker)
    assert result["encoder_ready_after"] is False


def test_warmup_without_a_reranker_reports_none(runner):
    result = runner.run_warmup(fake_server(FakeReranker(0), []), 1, None)
    assert result["encoder_ready_after"] is None


def test_warmup_queries_are_not_locomo_questions(runner):
    dataset = runner.REPO_ROOT / "benchmarks" / "data" / "locomo" / "data" / "locomo10.json"
    if not dataset.exists():
        pytest.skip("LoCoMo dataset is not downloaded")
    questions = {qa.get("question", "") for sample in json.loads(dataset.read_text()) for qa in sample.get("qa", [])}
    assert not questions & set(runner.WARMUP_QUERIES)


def test_gold_index_keeps_both_labels_of_a_repeated_question(runner):
    samples = [{"qa": [{"question": "same", "category": 4, "evidence": ["D1:1"]},
                       {"question": "other", "category": 1, "evidence": ["D1:2"]},
                       {"question": "same", "category": 5}]}]
    gold = runner.gold_index(samples)
    assert gold[("locomo_0", "same")].popleft() == (4, ["D1:1"])
    assert gold[("locomo_0", "same")].popleft() == (5, [])
    assert gold[("locomo_0", "other")].popleft() == (1, ["D1:2"])


def test_gold_index_grades_all_1536_locomo_questions(runner):
    dataset = runner.REPO_ROOT / "benchmarks" / "data" / "locomo" / "data" / "locomo10.json"
    if not dataset.exists():
        pytest.skip("LoCoMo dataset is not downloaded")
    gold = runner.gold_index(json.loads(dataset.read_text()))
    labels = [label for queue in gold.values() for label in queue]
    graded = [label for label in labels if label[0] != runner.ADVERSARIAL_CATEGORY and label[1]]
    assert len(labels) == 1986
    assert len(graded) == 1536


def test_summary_counts_queries_asked_before_the_encoder_loaded(runner):
    rows = [row("a", ["D1:1"], ["D1:1"], ready=False), row("b", ["D1:2"], ["D1:9"]),
            row("c", ["D1:3"], [], category=5, ready=False)]
    summary = runner.summarize(rows)
    assert summary["queries"] == 3
    assert summary["graded"] == 2
    assert summary["R@1"] == 0.5
    assert summary["queries_before_encoder_ready"] == 2


def test_compare_finds_the_questions_whose_ranking_changed(runner, tmp_path):
    first = [row("a", ["D1:1", "D1:2"], ["D1:1"]), row("b", ["D1:3", "D1:4"], ["D1:4"])]
    second = [row("a", ["D1:1", "D1:2"], ["D1:1"]), row("b", ["D1:5", "D1:6"], ["D1:4"])]
    result = runner.compare(write_rows(tmp_path / "a.rows.jsonl.gz", first),
                            write_rows(tmp_path / "b.rows.jsonl.gz", second))
    assert result["rankings_differ"] == 1
    assert result["top5_differ"] == 1
    assert result["r5_gold_hits_differ"] == 1
    assert result["first_differing_positions"] == [1]


def test_compare_refuses_runs_that_asked_different_questions(runner, tmp_path):
    a = write_rows(tmp_path / "a.rows.jsonl.gz", [row("a", ["D1:1"], ["D1:1"])])
    b = write_rows(tmp_path / "b.rows.jsonl.gz", [row("z", ["D1:1"], ["D1:1"])])
    with pytest.raises(ValueError):
        runner.compare(a, b)
