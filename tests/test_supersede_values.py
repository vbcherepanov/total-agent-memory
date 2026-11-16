"""memory_save(supersede=true): a new value retires the stored one."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memory_core.dedup import updates_value
from tests.pg_store_support import store_backend  # noqa: F401 — fixture


def test_documented_correction_pair_satisfies_detector():
    from memory_core.cross_rerank import order_value_updates

    document = (Path(__file__).resolve().parents[1] / "docs" / "whats-new.md").read_text(encoding="utf-8")
    row = next(line for line in document.splitlines() if "Corrections rank above" in line)
    examples = re.findall(r'"([^"]+)"', row)
    assert len(examples) == 2, "Document both the original statement and its correction"
    old, new = examples
    records = [(old, 1), (new, 2)]
    assert order_value_updates(records, lambda item: item[0], lambda item: (item[1],)) == records[::-1]

VALUE_UPDATES = [
    ("Lionel Messi's country of citizenship is Armenia", "Lionel Messi's country of citizenship is Argentina"),
    ("Сервис billing работает на PostgreSQL 18", "Сервис billing работает на PostgreSQL 16"),
    ("Маша любит зелёный цвет", "Маша любит красный цвет"),
    ("Israel was founded by Philippe, Duke of Orléans", "Israel was founded by David Ben-Gurion"),
    # the value is followed by a shared modifier that opens with a preposition
    ("Dana's standup meeting is at 10:15 on Mondays.", "Dana's standup meeting is at 9:30 on Mondays."),
    ("Планёрка у Даны в 10:15 по понедельникам", "Планёрка у Даны в 9:30 по понедельникам"),
    ("Alice works in Berlin for Acme since March", "Alice works in Paris for Acme since March"),
]

NOT_UPDATES = [
    # the subject differs, the value is shared
    ("The company that produced Chevrolet Caprice is General Motors",
     "The company that produced Buick Riviera is General Motors"),
    # the subject differs inside the prefix
    ("The capital of Tang Dynasty is Y", "The capital of Tang Empire is X"),
    ("Bob likes red", "Alice likes red"),
    # a shared tail that opens with a verb, not a preposition
    ("The meeting with Bob is on Monday", "The meeting with Alice is on Monday"),
    # a different relation
    ("Monk was written in the language of Hebrew", "Monk was written by Matthew Lewis"),
    # the same statement
    ("Deploy runs nightly", "deploy runs nightly."),
]


@pytest.mark.parametrize(("new", "old"), VALUE_UPDATES)
def test_value_update_is_detected(new, old):
    assert updates_value(new, old)


@pytest.mark.parametrize(("new", "old"), NOT_UPDATES)
def test_other_changes_are_not_value_updates(new, old):
    assert not updates_value(new, old)


@pytest.fixture
def srv(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(server, "store", server.Store(database=store_backend))
    monkeypatch.setattr(server, "recall", server.Recall(server.store))
    monkeypatch.setattr(server, "SID", "test")
    yield server
    server.store.db.close()


def save(srv, content, project="p", **extra):
    out = asyncio.run(srv._do("memory_save", {"content": content, "type": "fact", "project": project, **extra}))
    return json.loads(out)


def active(srv):
    return [row[0] for row in srv.store.db.execute("SELECT content FROM knowledge WHERE status='active' ORDER BY id")]


def test_without_flag_both_values_stay(srv):
    old, new = VALUE_UPDATES[0][1], VALUE_UPDATES[0][0]
    save(srv, old)
    assert "superseded" not in save(srv, new)
    assert active(srv) == [old, new]


def test_flag_retires_the_old_value(srv):
    old, new = VALUE_UPDATES[0][1], VALUE_UPDATES[0][0]
    old_id = save(srv, old)["id"]
    result = save(srv, new, supersede=True)
    assert result["superseded"] == [old_id]
    assert active(srv) == [new]
    status, by = srv.store.db.execute("SELECT status, superseded_by FROM knowledge WHERE id=?", (old_id,)).fetchone()
    assert (status, by) == ("superseded", result["id"])
    assert srv.store.db.execute("SELECT COUNT(*) FROM embeddings WHERE knowledge_id=?", (old_id,)).fetchone()[0] == 0


def test_flag_leaves_other_subjects_and_projects(srv):
    save(srv, NOT_UPDATES[0][1])
    save(srv, VALUE_UPDATES[0][1], project="other")
    assert "superseded" not in save(srv, NOT_UPDATES[0][0], supersede=True)
    assert "superseded" not in save(srv, VALUE_UPDATES[0][0], supersede=True)
    assert len(active(srv)) == 4


def test_fast_path_supports_the_flag(srv):
    old, new = VALUE_UPDATES[1][1], VALUE_UPDATES[1][0]
    save(srv, old)
    out = json.loads(asyncio.run(srv._do("memory_save_fast", {"content": new, "type": "fact", "project": "p",
                                                               "supersede": True})))
    assert len(out["superseded"]) == 1
    assert active(srv) == [new]


def recall_contents(srv, query):
    out = json.loads(asyncio.run(srv._do("memory_recall", {"query": query, "project": "p", "limit": 5})))
    return [hit["content"] for group in out["results"].values() for hit in group]


@pytest.mark.parametrize(("new", "old", "query"), [
    (VALUE_UPDATES[4][0], VALUE_UPDATES[4][1], "When is Dana's standup meeting on Mondays?"),
    (VALUE_UPDATES[5][0], VALUE_UPDATES[5][1], "Во сколько планёрка у Даны по понедельникам?"),
])
def test_without_flag_the_correction_ranks_above_the_value_it_corrects(srv, new, old, query):
    """The cross-encoder is off in the suite (and never applies to Russian): the order holds without it."""
    save(srv, old)
    save(srv, new)
    assert recall_contents(srv, query) == [new, old]


@pytest.mark.parametrize(('old', 'new', 'query'), [
    ("The stand-up is at 10:15 on Thursdays.", "The stand-up moved to 9:30 on Mondays.",
     "When is the stand-up?"),
    ("Стендап проходит в 10:15 по четвергам.", "Стендап перенесли на 9:30 по понедельникам.",
     "Когда проходит стендап?"),
    ("Dana's standup meeting is at 10:15 on Thursdays.",
     "Dana's stand-up meeting is now at 9:30 on Mondays.", "When is Dana's standup meeting?"),
])
def test_reported_schedule_paraphrase_wins_real_recall_without_retiring_history(srv, old, new, query):
    save(srv, old)
    save(srv, new)
    assert recall_contents(srv, query) == [new, old]
    assert active(srv) == [old, new]


def test_memory_get_reports_what_superseded_a_record(srv):
    old, new = VALUE_UPDATES[4][1], VALUE_UPDATES[4][0]
    old_id = save(srv, old)["id"]
    new_id = save(srv, new, supersede=True)["id"]
    out = json.loads(asyncio.run(srv._do("memory_get", {"ids": [old_id, new_id]})))
    assert [(row["status"], row["superseded_by"]) for row in out["results"]] == [("superseded", new_id), ("active", None)]


@pytest.mark.parametrize(("new", "old"), [
    ("Деплой релиза через пятницу", "Деплой релиза через четверг"),
    ("Время обеда перенесено на среду", "Время обеда перенесено на четверг"),
])
def test_flag_retires_the_old_russian_value_with_e_words(srv, new, old):
    old_id = save(srv, old)["id"]
    result = save(srv, new, supersede=True)
    assert result["superseded"] == [old_id]
    assert active(srv) == [new]
