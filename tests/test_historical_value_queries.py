"""Questions about an earlier value ("what was it before?") get the older value first.

Stage 4.9 of recall orders records that state different values of one
statement newest first. `query_shapes.is_historical_value_query` detects the
opposite question in English and Russian; recall then fills the group's
places oldest first.
"""

from __future__ import annotations

import pytest

import server
from memory_core import cross_rerank
from memory_core.query_shapes import is_historical_value_query

HISTORICAL = [
    "What database did we use before?",
    "What was the port before the migration?",
    "what did we use before switching to Postgres?",
    "What did we deploy with before we moved to k8s",
    "what was it before",
    "Where did Anna live previously?",
    "What was the value initially?",
    "What did I use to drive?",
    "Where did Anna used to work?",
    "Where Anna used to live?",
    "We used to run Jenkins, which version?",
    "What was my old address?",
    "What was our previous CI provider?",
    "Who was her former employer?",
    "Which company produced Windows Vista originally?",
    "At first, which framework did we pick?",
    "Which phone do I no longer use?",
    "In our previous conversation you said where Anna lived previously. Where was it?",
    "Which phone did I use in the past?",
    "Что мы использовали раньше?",
    "Где работал Олег ранее?",
    "Где Игорь жил прежде?",
    "Какой был старый адрес?",
    "Кто был бывший руководитель?",
    "Какая была прежняя версия?",
    "Какой цвет Маша любила изначально?",
    "Какую БД мы когда-то использовали?",
    "Что я больше не использую?",
    "Какой фреймворк был до этого?",
    "Что мы выбрали сначала?",
    "Какой был порт до миграции?",
]

NOT_HISTORICAL = [
    # procedures: "before" + an action is an instruction, not a question about the past
    "Before deploying, run tests",
    "before deploying, should I run tests?",
    "Should I back up before migrating?",
    "Do the backup before migrating",
    "Run tests before merge.",
    "Сначала запусти тесты, потом деплой",
    "Нужно ли сделать бэкап до миграции?",
    "Прежде чем деплоить, прогони тесты",
    "Как лучше назвать ветку до релиза?",
    # ordering and comparison questions
    "Did Caroline adopt the puppy before or after she moved to Boston?",
    "Which came first, the job or the move?",
    "Was the meeting earlier than the launch?",
    "Что было раньше: переезд или покупка?",
    "Кто приехал раньше, чем Маша?",
    "до или после релиза был баг?",
    # purpose "used to", "get used to", age, "still", "as early as possible"
    "What is the tool used to deploy?",
    "Which library is used to parse dates?",
    "How do I get used to vim?",
    "How old is Anna?",
    "Он по-прежнему работает в Ozon?",
    "Сделай это как можно раньше",
    # instrumental "use ... to", time anchors, and questions about the later state
    "What ingredient did Nate use to make the ice cream lactose-free?",
    "Which tournament did Nate win in the beginning of November 2022?",
    "What type of car did Evan get after his old Prius broke down?",
    "What do we use now instead of the old system?",
    "Что мы используем сейчас вместо старой системы?",
    # a reference to the dialogue itself or a time window (LongMemEval-S dev questions)
    "I was going through our previous conversation about atmospheric correction methods. Which did you mention?",
    "In our previous chat, you suggested a few terms. Can you remind me what they were?",
    "What is the order of the three trips I took in the past three months, from earliest to latest?",
    "How many pieces of furniture did I buy in the past few months?",
    "How much faster did I finish the 5K run compared to my previous year's time?",
    "Напомни, что ты советовал в прошлом разговоре про коктейли?",
    "О чём мы говорили в предыдущем чате?",
    # plain questions about the current value
    "What is my current address?",
    "Where does Igor live?",
    "what database do we use",
    "Где живёт Игорь?",
    "Какой цвет любит Маша?",
    "",
    "   ",
]


@pytest.mark.parametrize("query", HISTORICAL)
def test_detects_historical_questions(query):
    assert is_historical_value_query(query)


@pytest.mark.parametrize("query", NOT_HISTORICAL)
def test_ignores_procedures_comparisons_and_current_questions(query):
    assert not is_historical_value_query(query)


def _dated(*pairs):
    return [{"r": {"id": index, "content": text, "created_at": created}} for index, (text, created) in enumerate(pairs)]


def _order(items):
    return [item["r"]["id"] for item in items]


def _text(item):
    return item["r"]["content"]


def _recency(item):
    return item["r"]["created_at"], item["r"]["id"]


def test_older_first_fills_group_places_oldest_first():
    ranked = _dated(("Jon: nice weather today", "2024-01-05"),
                    ("Messi's citizenship is Argentina", "2024-01-01"),
                    ("Gina: thanks!", "2024-01-04"),
                    ("Messi's citizenship is Armenia", "2024-01-03"),
                    ("Messi's citizenship is Spain", "2024-01-02"))
    newest = cross_rerank.keep_updates_below(ranked, _text, _recency)
    oldest = cross_rerank.keep_updates_below(ranked, _text, _recency, older_first=True)
    assert _order(newest) == [0, 3, 2, 4, 1]
    assert _order(oldest) == [0, 1, 2, 4, 3]


def test_order_value_updates_passes_older_first_and_keeps_tail():
    ranked = _dated(("Anna lives in Rome", "2024-02-01"), ("Anna lives in Paris", "2024-01-01"),
                    ("Unrelated tail record", "2024-03-01"))
    result = cross_rerank.order_value_updates(ranked, _text, _recency, window=2, older_first=True)
    assert _order(result) == [1, 0, 2]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("MEMORY_CROSS_RERANK", "off")
    instance = server.Store()
    instance._embed_mode = "none"
    instance.session_start("s1", project="infra")
    rows = (("The database of the billing service is MySQL", "2024-01-01T00:00:00Z"),
            ("The database of the billing service is PostgreSQL", "2024-06-01T00:00:00Z"))
    for content, created in rows:
        rid, *_ = instance.save_knowledge(sid="s1", content=content, ktype="fact", project="infra",
                                          skip_dedup=True)
        instance.db.execute("UPDATE knowledge SET created_at=? WHERE id=?", (created, rid))
    instance.db.commit()
    yield instance
    instance.db.close()


def _contents(result):
    return [hit["content"] for group in result["results"].values() for hit in group]


@pytest.mark.parametrize("query, first", [
    ("What is the database of the billing service?", "PostgreSQL"),
    ("What was the database of the billing service before?", "MySQL"),
    ("Which database did the billing service use previously?", "MySQL"),
    ("Какая раньше была database у billing service?", "MySQL"),
])
def test_search_orders_values_by_question(store, query, first):
    recall = server.Recall(store)
    try:
        result = recall.search(query, project="infra", limit=5, record_usage=False, _explain=True)
    finally:
        recall.close()
    contents = [text for text in _contents(result) if "billing service is" in text]
    assert len(contents) == 2
    assert contents[0].endswith(first)
    assert result["_explain"]["historical_query"] is (first == "MySQL")


@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize("historical", [False, True])
def test_explicit_rerank_preserves_value_policy_before_limit(store, monkeypatch, limit, historical):
    monkeypatch.setattr(server, "HAS_RERANKER", True)
    monkeypatch.setenv("MEMORY_BOUNDED_RERANK", "false")
    preferred = "PostgreSQL" if historical else "MySQL"

    def biased_rerank(query, ranked, top_k):
        return sorted(ranked, key=lambda item: preferred not in item["r"]["content"])[:top_k]

    monkeypatch.setattr(server, "rerank_results", biased_rerank, raising=False)
    query = "What was the database of the billing service before?" if historical else (
        "What is the database of the billing service?")
    recall = server.Recall(store)
    try:
        result = recall.search(query, project="infra", limit=limit, rerank=True, record_usage=False)
    finally:
        recall.close()
    assert _contents(result)[0].endswith("MySQL" if historical else "PostgreSQL")


@pytest.mark.parametrize("cross_encoder", [False, True])
def test_personal_value_order_does_not_cross_projects(store, monkeypatch, cross_encoder):
    store.db.execute("UPDATE knowledge SET project='another-project' WHERE content LIKE '%PostgreSQL'")
    store.db.commit()
    if cross_encoder:
        from tests.test_cross_rerank import WorldKnowledgeEncoder, ready
        monkeypatch.setattr(cross_rerank, "shared_reranker",
                            lambda: ready(WorldKnowledgeEncoder(("MySQL",)), weight=4.0))
    recall = server.Recall(store)
    try:
        result = recall.search("What is the MySQL database of the billing service?", limit=2,
                               record_usage=False)
    finally:
        recall.close()
    assert len(_contents(result)) == 2
    assert _contents(result)[0].endswith("MySQL")


def test_diversity_cannot_reverse_historical_values(store, monkeypatch):
    monkeypatch.setattr(server, "HAS_RERANKER", True)
    monkeypatch.setattr(store, "embed", lambda texts: [[1.0, 0.0] for _ in texts])
    calls = []

    def reverse_values(ranked, embeddings, top_k):
        calls.append(len(embeddings))
        return list(reversed(ranked))[:top_k]

    monkeypatch.setattr(server, "mmr_diversify", reverse_values, raising=False)
    recall = server.Recall(store)
    try:
        result = recall.search("What was the database of the billing service before?", project="infra",
                               limit=2, diverse=True, record_usage=False)
    finally:
        recall.close()
    assert calls == [2]
    assert _contents(result)[0].endswith("MySQL")
