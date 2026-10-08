"""Directives and multi-query tiers, and the session note written by session_end."""

from __future__ import annotations

import asyncio
import json

import pytest

import server
from memory_core.query_shapes import is_advice_query, sub_queries


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(instance, "bump_recall", lambda ids: None)
    rows = (
        (1, "convention", "Branch names follow feature/<ticket>-<slug>; hotfixes use hotfix/<slug>"),
        (2, "fact", "Caroline adopted a golden retriever puppy in March"),
        (3, "fact", "Caroline moved from Chicago to Boston in June"),
        (4, "fact", "Bob likes jazz and goes to concerts on Fridays"),
        (5, "convention", "Commit subjects are imperative and at most 72 characters"),
        (6, "decision", "We picked PostgreSQL over MySQL for the orders service"),
    )
    for identity, kind, content in rows:
        instance.db.execute(
            "INSERT INTO knowledge(id,content,session_id,project,type,status,created_at,last_confirmed) "
            "VALUES (?,?,'s','p',?,'active','2026-01-01','2026-01-01')",
            (identity, content, kind),
        )
    instance.db.commit()
    yield instance
    instance.db.close()


def ids(result):
    return [hit["id"] for group in result["results"].values() for hit in group]


# ── query shapes ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "How should I name a new branch?",
    "Can you recommend a database for the orders service?",
    "What is the convention for commit messages?",
    "Как лучше назвать ветку?",
    "Посоветуй, что использовать для очереди",
])
def test_advice_queries_are_recognised(query):
    assert is_advice_query(query)


@pytest.mark.parametrize("query", ["When did Caroline move?", "pgbouncer pool size", "Bob jazz"])
def test_plain_queries_are_not_advice(query):
    assert not is_advice_query(query)


def test_ordering_question_is_split_into_its_sides():
    parts = sub_queries("Did Caroline adopt the dog before or after she moved to Boston?")
    assert parts == ["Caroline adopt the dog", "she moved to Boston"]


def test_comparison_and_russian_ordering_questions_split():
    assert sub_queries("React vs Vue for the admin panel") == ["React", "Vue for the admin panel"]
    assert sub_queries("Что было раньше: переезд в Берлин или свадьба?") == ["переезд в Берлин", "свадьба"]


def test_questions_without_an_ordering_cue_are_not_split():
    assert sub_queries("When did Caroline move to Boston?") == []
    assert sub_queries("What did Bob say about the trip and the hotel") == []


# ── directives tier ───────────────────────────────────────────────────────

def test_advice_query_pulls_conventions_in_via_directives(store):
    result = server.Recall(store).search("How should I name a new git branch for a ticket?",
                                         project="p", limit=5, _explain=True)
    assert 1 in ids(result)
    assert "directives" in result["_explain"]["tiers_used"]
    assert any(item["id"] == 1 for item in result["_explain"]["directives"])


def test_directives_only_fire_for_advice_queries(store):
    result = server.Recall(store).search("branch feature ticket", project="p", limit=5, _explain=True)
    assert "directives" not in result["_explain"]["tiers_used"]


def test_directives_can_be_switched_off(store):
    result = server.Recall(store).search("How should I name a new git branch for a ticket?",
                                         project="p", limit=5, _explain=True, tier_weights={"directives": 0})
    assert "directives" not in result["_explain"]["tiers_used"]


# ── multi-query tier ──────────────────────────────────────────────────────

def test_ordering_question_retrieves_both_sides(store):
    result = server.Recall(store).search("Did Caroline adopt the puppy before or after she moved to Boston?",
                                         project="p", limit=5, _explain=True, tier_weights={"multi_query": 0.8})
    found = ids(result)
    assert 2 in found and 3 in found
    assert "multi_query" in result["_explain"]["tiers_used"]
    assert result["_explain"]["sub_queries"] == ["Caroline adopt the puppy", "she moved to Boston"]


def test_multi_query_is_off_by_default(store):
    result = server.Recall(store).search("Did Caroline adopt the puppy before or after she moved to Boston?",
                                         project="p", limit=5, _explain=True)
    assert "multi_query" not in result["_explain"]["tiers_used"]
    assert result["_explain"]["sub_queries"] == []
    assert result["_explain"]["tier_weights"]["multi_query"] == 0


# ── session note ──────────────────────────────────────────────────────────

@pytest.fixture
def dispatcher(store, monkeypatch):
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-note")
    monkeypatch.setattr(server, "BRANCH", "")
    def call(name, args):
        raw = asyncio.run(server._do(name, args))
        return json.loads(raw if isinstance(raw, str) else raw[0].text)

    return call


def test_session_end_writes_a_retrievable_note(dispatcher, store):
    ended = dispatcher("session_end", {
        "session_id": "sess-note", "project": "p",
        "summary": "Migrated the orders service to PostgreSQL 18 and fixed the pgbouncer pool",
        "next_steps": ["re-run the load test"], "pitfalls": ["pool size above 200 exhausts connections"],
    })
    assert ended["note_id"]
    row = store.db.execute("SELECT type, content, tags FROM knowledge WHERE id=?", (ended["note_id"],)).fetchone()
    assert row[0] == "note"
    assert "Session note (p, " in row[1]
    assert "Next steps: re-run the load test" in row[1]
    assert "Pitfalls: pool size above 200 exhausts connections" in row[1]
    assert "session-note" in json.loads(row[2])
    found = server.Recall(store).search("pgbouncer pool migration postgresql", project="p", limit=5)
    assert ended["note_id"] in ids(found)


def test_session_end_without_summary_or_when_disabled_writes_no_note(dispatcher, store, monkeypatch):
    before = store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    ended = dispatcher("session_end", {"session_id": "sess-note", "project": "p", "summary": "",
                                       "auto_compress": True})
    assert "note_id" not in ended
    monkeypatch.setenv("MEMORY_SESSION_NOTES", "off")
    ended = dispatcher("session_end", {"session_id": "sess-note", "project": "p", "summary": "done"})
    assert "note_id" not in ended
    assert store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == before


# ── user-turn boost ───────────────────────────────────────────────────────

def test_user_turn_detection():
    from memory_core.query_shapes import is_user_turn
    assert is_user_turn("[2023-05-20 (Sat) 10:00] user: I shoot with a Sony A7R IV")
    assert is_user_turn("User: hello")
    assert not is_user_turn("[2023-05-20] assistant: Here are some options")
    assert not is_user_turn("We picked PostgreSQL")


@pytest.fixture
def conversation_store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(instance, "bump_recall", lambda ids: None)
    rows = (
        (1, "[2023-05-20] assistant: For a camera flash, popular options are the Godox V1 and the Sony HVL-F60RM"),
        (2, "[2023-05-20] user: I'm looking to upgrade my camera flash for my Sony A7R IV, any options?"),
    )
    for identity, content in rows:
        instance.db.execute(
            "INSERT INTO knowledge(id,content,session_id,project,type,status,created_at,last_confirmed) "
            "VALUES (?,?,'s','p','fact','active','2026-01-01','2026-01-01')",
            (identity, content),
        )
    instance.db.commit()
    yield instance
    instance.db.close()


def test_advice_query_prefers_the_users_own_turn(conversation_store, monkeypatch):
    query = "Can you recommend a camera flash upgrade for my Sony?"
    monkeypatch.setenv("MEMORY_RECALL_USER_TURN_BOOST", "1.0")
    plain = server.Recall(conversation_store).search(query, project="p", limit=5, _explain=True)
    assert plain["_explain"]["advice_query"] is True
    monkeypatch.setenv("MEMORY_RECALL_USER_TURN_BOOST", "1.3")
    boosted = server.Recall(conversation_store).search(query, project="p", limit=5, _explain=True)
    assert ids(boosted)[0] == 2
    assert boosted["_explain"]["user_turn_boosted"] == [2]
    assert plain["_explain"]["user_turn_boosted"] == []


def test_user_turn_boost_is_ignored_for_plain_queries(conversation_store):
    result = server.Recall(conversation_store).search("Sony camera flash", project="p", limit=5, _explain=True)
    assert result["_explain"]["advice_query"] is False
    assert result["_explain"]["user_turn_boosted"] == []
