"""Relative dates in a query ("three weeks ago", "last month") resolve against an anchor."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import server
from temporal_filter import parse_query_dates, parse_relative_dates, temporal_rerank
from tests.pg_store_support import store_backend  # noqa: F401

ANCHOR = datetime.fromisoformat("2023-05-30T23:40")


def test_n_units_ago_gives_a_window_around_that_day():
    (window,) = parse_relative_dates("What milestone did I mention four weeks ago?", ANCHOR)
    then = ANCHOR - timedelta(weeks=4)
    assert window.contains(then)
    assert window.contains(then - timedelta(days=2)) and window.contains(then + timedelta(days=2))
    assert not window.contains(then - timedelta(days=5))


def test_since_n_units_ago_runs_up_to_the_anchor():
    (window,) = parse_relative_dates("How many pieces have I written since I started writing again three weeks ago?",
                                     ANCHOR)
    assert window.contains(ANCHOR - timedelta(days=1))
    assert window.contains(ANCHOR - timedelta(weeks=3))
    assert not window.contains(ANCHOR - timedelta(weeks=5))


@pytest.mark.parametrize(("query", "days"), [
    ("What certification did I complete last month?", 30),
    ("How many hours of yoga did I do last week?", 7),
    ("How many plants did I acquire in the last month?", 30),
    ("What did I buy in the past two weeks?", 14),
])
def test_last_and_past_windows_end_at_the_anchor(query, days):
    (window,) = parse_relative_dates(query, ANCHOR)
    assert window.end == ANCHOR
    assert window.contains(ANCHOR - timedelta(days=days - 1))
    assert not window.contains(ANCHOR - timedelta(days=days + 2))


def test_yesterday_and_recently():
    (yesterday,) = parse_relative_dates("What did I cook yesterday?", ANCHOR)
    assert yesterday.start == datetime.fromisoformat("2023-05-29T00:00")
    (recent,) = parse_relative_dates("Which food delivery services have I used recently?", ANCHOR)
    assert recent.contains(ANCHOR - timedelta(days=30)) and not recent.contains(ANCHOR - timedelta(days=90))


def test_absolute_dates_still_parse_and_combine_with_relative_ones():
    ranges = parse_query_dates("What happened in March 2023 compared to two weeks ago?", anchor=ANCHOR)
    assert len(ranges) == 2


def test_no_relative_phrase_means_no_window():
    assert parse_relative_dates("When did Alice move to Berlin?", ANCHOR) == []


def test_rerank_prefers_the_record_from_four_weeks_before_the_anchor():
    entries = [
        {"content": "[2023/05/28 (Sun) 10:00] user: wrapping up the Johnson account", "score": 1.0},
        {"content": "[2023/05/02 (Tue) 09:00] user: I signed a contract with my first client", "score": 0.6},
    ]
    plain = temporal_rerank("What milestone did I mention four weeks ago?", entries)
    assert plain[0]["content"].startswith("[2023/05/28")  # anchored to today, nothing is 4 weeks back
    anchored = temporal_rerank("What milestone did I mention four weeks ago?", entries, anchor=ANCHOR)
    assert anchored[0]["content"].startswith("[2023/05/02")


def test_server_parses_as_of_values():
    assert server._parse_as_of(None) is None
    assert server._parse_as_of("2023-05-30") == datetime.fromisoformat("2023-05-30T00:00")
    assert server._parse_as_of("2023-05-30T23:40:00Z") == ANCHOR
    assert server._parse_as_of("2023/05/30 (Tue) 23:40") == ANCHOR
    with pytest.raises(ValueError):
        server._parse_as_of("not a date")


@pytest.fixture
def store(tmp_path, monkeypatch, store_backend):
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(database=store_backend, background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(instance, "bump_recall", lambda ids: None)
    rows = (
        (1, "[2023/05/28 (Sun) 10:00] user: a big milestone, wrapping up the Johnson account", "2023-05-28T10:00:00Z"),
        (2, "[2023/05/02 (Tue) 09:00] user: a big milestone, I signed a contract with my first client", "2023-05-02T09:00:00Z"),
        (3, "[2023/05/02 (Tue) 09:00] user: a big milestone, I opened my second office", "2026-01-01T00:00:00Z"),
    )
    for identity, content, created_at in rows:
        instance.db.execute(
            "INSERT INTO knowledge(id,content,session_id,project,type,status,created_at,last_confirmed) "
            "VALUES (?,?,'s','p','fact','active',?,?)",
            (identity, content, created_at, created_at),
        )
    instance.db.commit()
    yield instance
    instance.db.close()


def test_recall_as_of_reorders_by_the_anchored_window(store):
    recall = server.Recall(store)
    query = "What was the big milestone I mentioned four weeks ago?"
    now = recall.search(query, project="p", limit=5)
    anchored = recall.search(query, project="p", limit=5, as_of="2023-05-30T23:40")
    ids_now = [hit["id"] for group in now["results"].values() for hit in group]
    ids_anchored = [hit["id"] for group in anchored["results"].values() for hit in group]
    assert set(ids_now) == {1, 2, 3}
    assert set(ids_anchored) == {1, 2}
    assert ids_anchored[0] == 2
