"""MEMORY_RECALL_TIER_WEIGHTS: tier weights come from config, 0 switches a tier off."""

from __future__ import annotations

import logging

import pytest

import config
import server


@pytest.fixture(autouse=True)
def _fresh_weight_cache():
    config._tier_weights_cache.clear()
    yield
    config._tier_weights_cache.clear()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store()
    instance._embed_mode = "none"
    monkeypatch.setattr(instance, "bump_recall", lambda ids: None)
    rows = (
        "postgres owner of the billing database",
        "billing database owner is the platform team",
        "frontend uses vue and nuxt",
    )
    for identity, content in enumerate(rows, 1):
        instance.db.execute(
            "INSERT INTO knowledge(id,content,session_id,project,type,status,created_at,last_confirmed) "
            "VALUES (?,?,'s','p','fact','active','2026-01-01','2026-01-01')",
            (identity, content),
        )
    instance.db.commit()
    yield instance
    instance.db.close()


def test_defaults_match_the_historical_constants():
    weights = config.get_recall_tier_weights()
    assert weights["fts"] == 1.0
    assert weights["semantic"] == 1.2
    assert weights["fuzzy"] == 0.5
    assert weights["graph"] == 0.3
    assert weights["episode"] == 0.9
    assert set(weights) == set(config.RECALL_TIERS)
    assert server.Recall.RRF_WEIGHTS == config.DEFAULT_RECALL_TIER_WEIGHTS


def test_env_overrides_named_tiers_only(monkeypatch):
    monkeypatch.setenv("MEMORY_RECALL_TIER_WEIGHTS", " fuzzy=0 , GRAPH=0.3 ")
    weights = config.get_recall_tier_weights()
    assert weights["fuzzy"] == 0.0
    assert weights["graph"] == 0.3
    assert weights["fts"] == 1.0


@pytest.mark.parametrize("raw", ["nope=1", "fts=high", "fts=-0.1", "fts", "fts=nan"])
def test_malformed_values_are_rejected_by_the_parser(raw):
    with pytest.raises(ValueError, match="MEMORY_RECALL_TIER_WEIGHTS"):
        config.parse_tier_weights(raw)


def test_malformed_env_is_logged_and_defaults_apply(monkeypatch, caplog):
    monkeypatch.setenv("MEMORY_RECALL_TIER_WEIGHTS", "fts=high")
    with caplog.at_level(logging.WARNING, logger="config"):
        weights = config.get_recall_tier_weights()
    assert weights == config.DEFAULT_RECALL_TIER_WEIGHTS
    assert any("MEMORY_RECALL_TIER_WEIGHTS" in record.getMessage() for record in caplog.records)


def test_signature_is_none_for_defaults_and_stable_otherwise():
    assert config.tier_weight_signature(config.get_recall_tier_weights()) is None
    custom = config.parse_tier_weights("fuzzy=0")
    assert config.tier_weight_signature(custom) == config.tier_weight_signature(dict(custom))
    assert "fuzzy=0" in config.tier_weight_signature(custom)


def test_zero_weight_skips_the_tier(store):
    recall = server.Recall(store)
    with_fuzzy = recall.search("databse ownr", project="p", limit=5, _explain=True)
    assert "fuzzy" in with_fuzzy["_explain"]["tiers_used"]

    without = recall.search("databse ownr", project="p", limit=5, _explain=True,
                            tier_weights={"fuzzy": 0})
    assert "fuzzy" not in without["_explain"]["tiers_used"]
    assert without["_explain"]["tier_weights"]["fuzzy"] == 0


def test_zero_fts_weight_skips_fts_without_counting_an_error(store):
    from memory_core.telemetry import counters

    before = counters.get("retrieval_fts_errors")
    result = server.Recall(store).search("billing database", project="p", limit=5, _explain=True,
                                         tier_weights={"fts": 0})
    assert "fts" not in result["_explain"]["tiers_used"]
    assert counters.get("retrieval_fts_errors") == before


def test_env_weights_reach_the_search(store, monkeypatch):
    monkeypatch.setenv("MEMORY_RECALL_TIER_WEIGHTS", "fts=0")
    result = server.Recall(store).search("billing database", project="p", limit=5, _explain=True)
    assert "fts" not in result["_explain"]["tiers_used"]
    assert result["_explain"]["tier_weights"]["fts"] == 0


def test_call_argument_wins_over_env(store, monkeypatch):
    monkeypatch.setenv("MEMORY_RECALL_TIER_WEIGHTS", "fts=0")
    result = server.Recall(store).search("billing database", project="p", limit=5, _explain=True,
                                         tier_weights={"fts": 2.0})
    assert "fts" in result["_explain"]["tiers_used"]
    assert result["_explain"]["tier_weights"]["fts"] == 2.0


def test_different_weights_do_not_share_a_cached_answer(store):
    recall = server.Recall(store)
    full = recall.search("billing database", project="p", limit=5)
    full_ids = [hit["id"] for group in full["results"].values() for hit in group]
    assert full_ids

    none = recall.search("billing database", project="p", limit=5,
                         tier_weights={"fts": 0, "fuzzy": 0, "semantic": 0, "graph": 0,
                                       "episode": 0, "atomic_facts": 0, "multi_repr": 0, "hyde": 0})
    none_ids = [hit["id"] for group in none["results"].values() for hit in group]
    assert none_ids == []

    again = recall.search("billing database", project="p", limit=5)
    assert [hit["id"] for group in again["results"].values() for hit in group] == full_ids
