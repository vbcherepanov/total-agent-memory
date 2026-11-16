"""Cross-encoder re-ranking of the fused window (memory_core/cross_rerank.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import server
from memory_core import cross_rerank
from memory_core.cross_rerank import CrossReranker, latin_share
from memory_core.telemetry import counters
from tests.pg_store_support import store_backend  # noqa: F401 — fixture


class KeywordEncoder:
    """Scores a record by how many of the query's words it contains."""

    def __init__(self):
        self.calls = 0

    def rerank(self, query, documents):
        self.calls += 1
        words = {w.strip("?.,").lower() for w in query.split()}
        return [float(sum(1 for w in doc.lower().split() if w.strip("?.,\"") in words)) for doc in documents]


def ready(encoder=None, **kwargs) -> CrossReranker:
    reranker = CrossReranker("fake", multilingual=kwargs.pop("multilingual", False),
                             window=kwargs.pop("window", 50), weight=kwargs.pop("weight", 2.0),
                             context_chars=kwargs.pop("context_chars", 0),
                             loader=lambda _model: encoder or KeywordEncoder())
    assert reranker.wait_ready(5)
    return reranker


def items(*texts):
    return [{"r": {"id": n, "content": text}} for n, text in enumerate(texts)]


def order(result):
    return [item["r"]["id"] for item in result]


def test_latin_share_counts_letters_only():
    assert latin_share("What book is Jon reading?") == 1.0
    assert latin_share("Какую книгу читает Джон?") == 0.0
    assert latin_share("2023-05-07 !!") == 1.0


def test_encoder_winner_moves_up_but_fused_rank_still_counts():
    """Weight 2: the encoder's clear winner climbs from 4th to 2nd, not straight to 1st."""
    candidates = items("Jon: Thanks!", "Jon: Cool", "Jon: nice", "Jon: I'm currently reading The Lean Startup book")
    result = ready().rerank("What book is Jon currently reading?", candidates, lambda i: i["r"]["content"], wait=False)
    assert order(result).index(3) == 1


def test_items_beyond_the_window_keep_their_order():
    candidates = items("a", "b", "reading book", "tail one", "tail two")
    result = ready(window=3).rerank("reading book", candidates, lambda i: i["r"]["content"], wait=False)
    assert order(result)[3:] == [3, 4]
    assert sorted(order(result)[:3]) == [0, 1, 2]


def test_english_model_skips_non_latin_queries():
    encoder = KeywordEncoder()
    candidates = items("первый", "второй")
    result = ready(encoder).rerank("какая книга", candidates, lambda i: i["r"]["content"], wait=False)
    assert order(result) == [0, 1]
    assert encoder.calls == 0


def test_multilingual_model_reranks_non_latin_queries():
    encoder = KeywordEncoder()
    ready(encoder, multilingual=True).rerank("какая книга", items("x", "книга"), lambda i: i["r"]["content"], wait=False)
    assert encoder.calls == 1


def test_english_encoder_preserves_non_latin_candidate_positions():
    class EnglishOnly(KeywordEncoder):
        def rerank(self, query, documents):
            assert all(latin_share(text) >= cross_rerank.LATIN_SHARE_REQUIRED for text in documents)
            return super().rerank(query, documents)

    encoder = EnglishOnly()
    candidates = items("Очередь уведомлений обрабатывает сто событий", "irrelevant text", "notification throughput")
    result = ready(encoder).rerank("notification throughput", candidates, lambda i: i['r']['content'], wait=False,
                                  context_of=lambda window: [i['r']['content'] + ' context' for i in window])
    assert order(result) == [0, 2, 1]
    assert encoder.calls == 2


def test_english_encoder_skips_window_with_one_supported_candidate():
    encoder = KeywordEncoder()
    candidates = items("Очередь уведомлений", "notification throughput", "Число событий")
    result = ready(encoder).rerank("notification throughput", candidates, lambda i: i['r']['content'], wait=False)
    assert order(result) == [0, 1, 2]
    assert encoder.calls == 0


def test_multilingual_encoder_can_reorder_mixed_script_candidates():
    candidates = items("Очередь уведомлений", "irrelevant text", "notification throughput")
    result = ready(multilingual=True).rerank("notification throughput", candidates,
                                           lambda i: i['r']['content'], wait=False)
    assert order(result)[0] == 2


def test_auto_mode_keeps_order_until_the_model_is_loaded():
    gate = __import__("threading").Event()

    def slow_loader(_model):
        gate.wait(5)
        return KeywordEncoder()

    reranker = CrossReranker("fake", multilingual=False, window=50, weight=2.0, loader=slow_loader)
    before = counters.get("cross_rerank_not_ready")
    result = reranker.rerank("reading book", items("a", "reading book"), lambda i: i["r"]["content"], wait=False)
    assert order(result) == [0, 1]
    assert counters.get("cross_rerank_not_ready") == before + 1
    gate.set()
    assert reranker.wait_ready(5)


def test_load_failure_keeps_fused_order():
    def broken(_model):
        raise OSError("model download failed")

    reranker = CrossReranker("fake", multilingual=False, window=50, weight=2.0, loader=broken)
    before = counters.get("cross_rerank_load_failed")
    assert reranker.wait_ready(5) is False
    result = reranker.rerank("reading book", items("a", "reading book"), lambda i: i["r"]["content"], wait=True)
    assert order(result) == [0, 1]
    assert counters.get("cross_rerank_load_failed") == before + 1


def test_score_count_mismatch_is_an_error():
    class Short:
        def rerank(self, query, documents):
            return [1.0]

    with pytest.raises(RuntimeError):
        ready(Short()).rerank("q", items("a", "b"), lambda i: i["r"]["content"], wait=False)


def test_context_view_lifts_a_turn_whose_neighbour_matches_the_question():
    """"Self-acceptance and trans stories" shares no word with the question; the turn before it does."""
    candidates = items("Caroline: I love painting", "Melanie: nice weather", "Caroline: self-acceptance and trans stories",
                       "Jon: ok")
    around = {2: "Melanie: What was the poetry reading about?\nCaroline: self-acceptance and trans stories"}
    encoder = KeywordEncoder()
    plain = ready(encoder).rerank("What was the poetry reading about?", candidates,
                                  lambda i: i["r"]["content"], wait=False)
    with_context = ready(encoder).rerank(
        "What was the poetry reading about?", candidates, lambda i: i["r"]["content"], wait=False,
        context_of=lambda window: [around.get(i["r"]["id"], i["r"]["content"]) for i in window])
    assert order(with_context).index(2) < order(plain).index(2)
    assert encoder.calls == 3


class WorldKnowledgeEncoder:
    """Prefers records naming the real-world value, as a web-trained encoder does."""

    def __init__(self, known):
        self.known = known

    def rerank(self, query, documents):
        return [1.0 if any(value in doc for value in self.known) else 0.0 for doc in documents]


def dated(*rows):
    return [{"r": {"id": n, "content": text, "created_at": at}} for n, (text, at) in enumerate(rows)]


def test_a_record_stays_below_its_own_later_update():
    """MemoryAgentBench FC: the encoder lifts the replaced value; its later update must stay above it."""
    candidates = dated(("The company that produced Windows Vista is Raytheon", "2024-01-02T00:00:00Z"),
                       ("Pius VI is affiliated with the religion of Jainism", "2024-01-02T00:00:00Z"),
                       ("The company that produced Windows Vista is Microsoft", "2024-01-01T00:00:00Z"),
                       ("Pius VI is affiliated with the religion of Catholic Church", "2024-01-01T00:00:00Z"))
    reranker = ready(WorldKnowledgeEncoder(("Microsoft", "Catholic")), weight=4.0)
    text_of = lambda i: i["r"]["content"]
    biased = reranker.rerank("Which company produced Windows Vista?", candidates, text_of, wait=False)
    assert order(biased)[:2] == [2, 3]
    fixed = reranker.rerank("Which company produced Windows Vista?", candidates, text_of, wait=False,
                            recency_of=lambda i: (i["r"]["created_at"], i["r"]["id"]))
    assert order(fixed) == [0, 1, 2, 3]


def test_update_order_keeps_unrelated_records_in_place():
    ranked = dated(("Jon: nice weather today", "2024-01-05"),
                   ("Messi's citizenship is Argentina", "2024-01-01"),
                   ("Gina: thanks!", "2024-01-04"),
                   ("Messi's citizenship is Armenia", "2024-01-03"),
                   ("Messi's citizenship is Spain", "2024-01-02"))
    result = cross_rerank.keep_updates_below(ranked, lambda i: i["r"]["content"],
                                             lambda i: (i["r"]["created_at"], i["r"]["id"]))
    assert order(result) == [0, 3, 2, 4, 1]


def test_same_time_updates_fall_back_to_insertion_order():
    ranked = dated(("Anna lives in Paris", "2024-01-01"), ("Anna lives in Rome", "2024-01-01"))
    result = cross_rerank.keep_updates_below(ranked, lambda i: i["r"]["content"],
                                             lambda i: (i["r"]["created_at"], i["r"]["id"]))
    assert order(result) == [1, 0]


def test_search_ranks_the_later_update_first(store, monkeypatch):
    """End to end: a biased encoder cannot lift a value above the record that replaced it."""
    store.save_knowledge(sid="s1", content="The company that produced Windows Vista is Microsoft", ktype="fact",
                         project="books", skip_dedup=True)
    store.save_knowledge(sid="s1", content="The company that produced Windows Vista is Raytheon", ktype="fact",
                         project="books", skip_dedup=True)
    monkeypatch.setattr(cross_rerank, "shared_reranker",
                        lambda: ready(WorldKnowledgeEncoder(("Microsoft",)), weight=4.0))
    result = server.Recall(store).search(query="Which company produced Windows Vista?", project="books",
                                         limit=5, detail="full", record_usage=False)
    texts = [hit["content"] for group in result["results"].values() for hit in group]
    vista = [text for text in texts if "Windows Vista" in text]
    assert vista[0].endswith("Raytheon")


def test_session_window_texts_stays_inside_the_session(store):
    store.session_start("s2", project="books")
    store.save_knowledge(sid="s2", content="other session turn", ktype="fact", project="books", skip_dedup=True)
    rows = [dict(r) for r in store.db.execute(
        "SELECT * FROM knowledge WHERE project='books' AND session_id='s1' ORDER BY created_at, id").fetchall()]
    texts = cross_rerank.session_window_texts(store.db, [rows[0], rows[5], rows[-1]], side_chars=12)
    assert texts[rows[5]["id"]] == "\n".join((rows[4]["content"][-12:], rows[5]["content"], rows[6]["content"][:12]))
    assert texts[rows[0]["id"]] == "\n".join((rows[0]["content"], rows[1]["content"][:12]))
    assert "other session" not in texts[rows[-1]["id"]]
    assert cross_rerank.session_window_texts(store.db, [], side_chars=12) == {}


def test_shared_reranker_follows_config(monkeypatch):
    monkeypatch.setattr(cross_rerank, "_shared", None)
    monkeypatch.setenv("MEMORY_CROSS_RERANK", "off")
    assert cross_rerank.shared_reranker() is None
    monkeypatch.setenv("MEMORY_CROSS_RERANK", "auto")
    monkeypatch.setenv("MEMORY_CROSS_RERANK_WINDOW", "30")
    first = cross_rerank.shared_reranker()
    assert first.window == 30
    monkeypatch.setenv("MEMORY_CROSS_RERANK_WINDOW", "40")
    assert cross_rerank.shared_reranker().window == 40
    assert cross_rerank.shared_reranker().context_chars == 400
    monkeypatch.setenv("MEMORY_CROSS_RERANK_CONTEXT", "0")
    assert cross_rerank.shared_reranker().context_chars == 0
    monkeypatch.setenv("MEMORY_CROSS_RERANK_CONTEXT", "-1")
    with pytest.raises(ValueError):
        cross_rerank.shared_reranker()
    monkeypatch.delenv("MEMORY_CROSS_RERANK_CONTEXT")
    monkeypatch.setenv("MEMORY_CROSS_RERANK", "sometimes")
    with pytest.raises(ValueError):
        cross_rerank.shared_reranker()


@pytest.fixture
def store(store_backend, tmp_path, monkeypatch):  # noqa: F811 — pytest fixture injection
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    s = server.Store(database=store_backend)
    s.session_start("s1", project="books")
    for n in range(30):
        s.save_knowledge(sid="s1", content=f"Jon: small talk number {n}, thanks Gina", ktype="fact",
                         project="books", skip_dedup=True)
    s.save_knowledge(sid="s1", content="Jon: I'm currently reading The Lean Startup, a book about building a business",
                     ktype="fact", project="books")
    try:
        yield s
    finally:
        s.db.close()


def test_search_widens_the_pool_and_returns_the_limit(store, monkeypatch):
    """Thirty short turns crowd the answer out of the top ten; the re-ranker brings it back."""

    def top_ten(reranker):
        monkeypatch.setattr(cross_rerank, "shared_reranker", lambda: reranker)
        result = server.Recall(store).search(query="What book is Jon currently reading?", project="books",
                                             limit=10, detail="full", record_usage=False)
        return [hit["content"] for group in result["results"].values() for hit in group]

    assert not any("Lean Startup" in text for text in top_ten(None))
    encoder = KeywordEncoder()
    reranked = top_ten(ready(encoder, window=40))
    assert len(reranked) == 10
    assert any("Lean Startup" in text for text in reranked)
    assert encoder.calls == 1
    encoder = KeywordEncoder()
    assert any("Lean Startup" in text for text in top_ten(ready(encoder, window=40, context_chars=200)))
    assert encoder.calls == 2


def test_cached_rerank_is_identical_and_never_infers_missing_view():
    from memory_core.rerank_cache import ScoreCacheMiss
    encoder = KeywordEncoder()
    cross = ready(encoder)
    candidates = items('billing database', 'billing fallback')
    with pytest.raises(ScoreCacheMiss):
        cross.rerank('billing', candidates, lambda item: item['r']['content'], wait=False, cache_only=True)
    assert encoder.calls == 0
    expected = cross.rerank('billing', candidates, lambda item: item['r']['content'], wait=False)
    assert cross.rerank('billing', candidates, lambda item: item['r']['content'], wait=False,
                        cache_only=True) == expected
    with pytest.raises(ScoreCacheMiss):
        cross.rerank('billing', candidates, lambda item: item['r']['content'], wait=False,
                     context_of=lambda window: ['neighbour one', 'neighbour two'], cache_only=True)
    assert encoder.calls == 1
