"""Environment knobs of the 14.9 recall pipeline: defaults, valid values, malformed values."""

from __future__ import annotations

import logging

import pytest

import config
import server


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("MEMORY_RECALL_TOUCH_FLUSH_MS", "MEMORY_RECALL_SINGLEFLIGHT", "MEMORY_RECALL_SINGLEFLIGHT_WAIT_SECONDS",
                 "MEMORY_RECALL_FTS_CROSSLINGUAL_DAMP", "MEMORY_RECALL_COMPOUND_SLOTS"):
        monkeypatch.delenv(name, raising=False)


def test_touch_flush_ms_default_and_override(monkeypatch):
    assert config.get_recall_touch_flush_ms() == config.DEFAULT_RECALL_TOUCH_FLUSH_MS
    monkeypatch.setenv("MEMORY_RECALL_TOUCH_FLUSH_MS", "40")
    assert config.get_recall_touch_flush_ms() == 40


@pytest.mark.parametrize("raw", ["abc", "12.5", "5", "-1", "nan"])
def test_touch_flush_ms_malformed_falls_back(monkeypatch, caplog, raw):
    monkeypatch.setenv("MEMORY_RECALL_TOUCH_FLUSH_MS", raw)
    with caplog.at_level(logging.WARNING, logger="config"):
        assert config.get_recall_touch_flush_ms() == config.DEFAULT_RECALL_TOUCH_FLUSH_MS
    assert any("malformed" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("raw, expected", [("", True), ("on", True), ("1", True), ("off", False),
                                           ("false", False), ("0", False), ("maybe", True)])
def test_singleflight_flag(monkeypatch, raw, expected):
    monkeypatch.setenv("MEMORY_RECALL_SINGLEFLIGHT", raw)
    assert config.get_recall_singleflight_enabled() is expected


@pytest.mark.parametrize("raw, expected", [("", 30.0), ("2.5", 2.5), ("0", 0.0), ("-3", 30.0), ("inf", 30.0),
                                           ("x", 30.0)])
def test_singleflight_wait_seconds(monkeypatch, raw, expected):
    monkeypatch.setenv("MEMORY_RECALL_SINGLEFLIGHT_WAIT_SECONDS", raw)
    assert config.get_recall_singleflight_wait_seconds() == expected


@pytest.mark.parametrize("raw, expected", [("", 0.3), ("0.5", 0.5), ("0", 0.0), ("1", 1.0), ("7", 1.0),
                                           ("-0.2", 0.3), ("nan", 0.3), ("half", 0.3)])
def test_fts_crosslingual_damp_is_clamped_to_unit_interval(monkeypatch, raw, expected):
    monkeypatch.setenv("MEMORY_RECALL_FTS_CROSSLINGUAL_DAMP", raw)
    assert config.get_recall_fts_crosslingual_damp() == expected


@pytest.mark.parametrize("raw, expected", [("", True), ("off", False), ("no", False), ("yes", True),
                                           ("garbage", True)])
def test_compound_slots_flag(monkeypatch, raw, expected):
    monkeypatch.setenv("MEMORY_RECALL_COMPOUND_SLOTS", raw)
    assert config.get_recall_compound_slots() is expected


def test_pipeline_signature_follows_the_switches(monkeypatch):
    base = server.Recall._pipeline_signature()
    monkeypatch.setenv("MEMORY_RECALL_FTS_CROSSLINGUAL_DAMP", "1")
    damp_off = server.Recall._pipeline_signature()
    monkeypatch.setenv("MEMORY_RECALL_COMPOUND_SLOTS", "off")
    slots_off = server.Recall._pipeline_signature()
    monkeypatch.setenv("MEMORY_GRAPH_HUB_DEGREE", "7")
    hub = server.Recall._pipeline_signature()
    assert len({base, damp_off, slots_off, hub}) == 4


def test_pipeline_signature_tracks_resolved_embedding_models(monkeypatch):
    monkeypatch.delenv("MEMORY_CODE_EMBED_MODEL", raising=False)
    default_code = server.Recall._pipeline_signature()
    monkeypatch.setenv("MEMORY_CODE_EMBED_MODEL", "")
    text_code = server.Recall._pipeline_signature()
    monkeypatch.setenv("MEMORY_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("MEMORY_EMBED_MODEL", "text-embedding-3-small")
    small = server.Recall._pipeline_signature()
    monkeypatch.setenv("MEMORY_EMBED_MODEL", "text-embedding-3-large")
    large = server.Recall._pipeline_signature()
    assert len({default_code, text_code, small, large}) == 4


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    store.session_start("s1", project="p")
    yield store
    store.db.close()


def _counts(store) -> list[int]:
    return [row[0] for row in store.db.execute("SELECT recall_count FROM knowledge ORDER BY id").fetchall()]


def test_bump_recall_counts_each_id_once_per_occurrence(store):
    first_id = store.save_knowledge("s1", content="Orders use UUID v7 keys", ktype="fact", project="p")[0]
    second_id = store.save_knowledge("s1", content="Invoices go to RabbitMQ", ktype="fact", project="p")[0]
    store.bump_recall([first_id, second_id, first_id])
    assert _counts(store) == [2, 1]
    assert store.db.execute("SELECT COUNT(*) FROM knowledge WHERE last_recalled IS NOT NULL").fetchone()[0] == 2


def test_bump_recall_with_no_ids_writes_nothing(store):
    store.save_knowledge("s1", content="Orders use UUID v7 keys", ktype="fact", project="p")
    changes = store.db.total_changes
    store.bump_recall([])
    assert store.db.total_changes == changes
    assert _counts(store) == [0]
