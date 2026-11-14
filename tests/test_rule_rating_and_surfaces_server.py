"""Server wiring of SOUL rules: rating fix, migration 038, query surfacing, followed/report."""

from __future__ import annotations

import asyncio
import json

import pytest

import rule_surfaces
from tests.pg_store_support import store_backend  # noqa: F401 — fixture

SESSION = "rules-test"


@pytest.fixture
def server_store(store_backend, tmp_path, monkeypatch):  # noqa: F811 — pytest fixture injection
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=store_backend)
    store.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) "
        "VALUES (?, '2026-10-13T00:00:00Z', 'proj', 'open')", (SESSION,))
    store.db.commit()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "SID", SESSION)
    yield server, store
    store.db.close()


def add_rule(store, content, priority=5, scope="global"):
    return store.manage_rule(SESSION, "add_manual", content=content, category="manual",
                             priority=priority, scope=scope)["id"]


def rule(store, rule_id):
    return dict(store.db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone())


def call(server, tool, args):
    return json.loads(asyncio.run(server.call_tool(tool, args))[0].text)


# ── the rating bug ───────────────────────────────────────────────────────


def test_ten_loads_and_one_helpful_rating_do_not_suspend(server_store):
    _, store = server_store
    rid = add_rule(store, "Run tests before commit")
    for _ in range(10):
        store.get_rules_for_context("proj")
    result = store.manage_rule(SESSION, "rate", id=rid, success=True)
    row = rule(store, rid)
    assert "auto_suspended" not in result
    assert row["status"] == "active"
    assert (row["shown_count"], row["fire_count"]) == (10, 0)
    assert row["success_rate"] == pytest.approx(1.0)
    assert result["success_rate"] == pytest.approx(1.0)


def test_six_failures_without_success_suspend(server_store):
    _, store = server_store
    rid = add_rule(store, "Bad rule")
    results = [store.manage_rule(SESSION, "rate", id=rid, success=False) for _ in range(6)]
    assert all("auto_suspended" not in r for r in results[:5])
    assert results[-1]["auto_suspended"] is True
    assert results[-1]["ci_upper"] < rule_surfaces.RULE_SUSPEND_MAX_UPPER
    assert results[-2]["ci_upper"] >= rule_surfaces.RULE_SUSPEND_MAX_UPPER
    assert rule(store, rid)["status"] == "suspended"


def test_mixed_ratings_stay_active_and_rate_uses_rated_outcomes(server_store):
    _, store = server_store
    rid = add_rule(store, "Mixed rule")
    for success in (True, False, False, True, False):
        store.manage_rule(SESSION, "rate", id=rid, success=success)
    row = rule(store, rid)
    assert row["status"] == "active"
    assert row["success_rate"] == pytest.approx(2 / 5)


def test_rate_unknown_rule_keeps_old_shape(server_store):
    _, store = server_store
    result = store.manage_rule(SESSION, "rate", id=12345, success=True)
    assert result["rated"] is True and result["id"] == 12345 and result["success_rate"] is None
    assert result["error"] == "Rule 12345 not found"


def test_explicit_fire_still_counts(server_store):
    _, store = server_store
    rid = add_rule(store, "Rule")
    store.manage_rule(SESSION, "fire", id=rid)
    assert rule(store, rid)["fire_count"] == 1


def test_rule_effectiveness_view_lists_shown_rules(server_store):
    _, store = server_store
    rid = add_rule(store, "Shown only")
    store.get_rules_for_context("proj")
    stats = store.analyze_patterns("rule_effectiveness")["rule_effectiveness"]["rules"]
    assert [r["id"] for r in stats] == [rid] and stats[0]["shown_count"] == 1


# ── migration 038 data move ──────────────────────────────────────────────


def test_migration_038_moves_inflated_fire_counts(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    db = store.db
    rows = [  # fire_count, success, fail
        (12, 1, 0),
        (7, 0, 6),
        (0, 0, 0),
        (2, 3, 1),
    ]
    ids = []
    for fire, success, fail in rows:
        cur = db.execute(
            "INSERT INTO rules (session_id, content, category, fire_count, success_count, fail_count, "
            "success_rate, created_at, updated_at) VALUES ('s', 'r', 'c', ?, ?, ?, 0.08, 'x', 'x')",
            (fire, success, fail))
        ids.append(cur.lastrowid)
    db.execute("DROP TABLE rule_surfaces")
    db.execute("ALTER TABLE rules DROP COLUMN shown_count")
    db.execute("DELETE FROM migrations WHERE version='038'")
    db.commit()
    db.close()

    reopened = server.Store()
    try:
        got = [tuple(reopened.db.execute(
            "SELECT shown_count, fire_count, success_rate FROM rules WHERE id=?", (rid,)).fetchone())
            for rid in ids]
        assert got == [(12, 1, 1.0), (7, 6, 0.0), (0, 0, 0.0), (2, 2, 0.75)]
        assert reopened.db.execute("SELECT COUNT(*) FROM rule_surfaces").fetchone()[0] == 0
    finally:
        reopened.db.close()


# ── tool wiring ──────────────────────────────────────────────────────────


def test_rules_context_without_query_keeps_shape(server_store):
    server, store = server_store
    rid = add_rule(store, "Run tests before commit")
    payload = call(server, "self_rules_context", {"project": "proj"})
    assert payload["rules_count"] == 1
    assert "surface_id" not in payload["rules"][0]
    assert rule(store, rid)["shown_count"] == 1 and rule(store, rid)["fire_count"] == 0


def test_rules_context_returns_auto_consolidated_rule_of_its_project(server_store):
    server, _ = server_store
    learned = {}
    for project in ("projA", "projB"):
        for attempt in range(3):
            learned[project] = call(server, "learn_error", {
                "file": "cmd/x/main.go", "error": f"dial tcp refused #{attempt}",
                "root_cause": "service started before the database was ready",
                "fix": "add a healthcheck", "pattern": "startup-order", "project": project,
            })["rule_id"]
    assert learned["projA"] != learned["projB"]
    for project in ("projA", "projB"):
        payload = call(server, "self_rules_context", {"project": project})
        assert [r["id"] for r in payload["rules"]] == [learned[project]]
        assert payload["rules"][0]["scope"] == f"project:{project}"


def test_rules_context_with_query_surfaces_and_followed_roundtrip(server_store):
    server, store = server_store
    target = add_rule(store, "Every SQLite migration needs a PostgreSQL twin")
    add_rule(store, "Write docstrings for public functions")
    payload = call(server, "self_rules_context", {"project": "proj", "query": "new sqlite migration"})
    assert payload["ranking"] == "lexical"
    assert [r["id"] for r in payload["rules"]] == [target]
    surface_id = payload["rules"][0]["surface_id"]
    row = store.db.execute("SELECT session_id, project, source FROM rule_surfaces WHERE id=?",
                           (surface_id,)).fetchone()
    assert tuple(row) == (SESSION, "proj", "self_rules_context")
    assert rule(store, target)["shown_count"] == 1 and rule(store, target)["fire_count"] == 0

    marked = call(server, "self_rules", {"action": "followed", "surface_id": surface_id,
                                         "followed": True, "note": "added 0002 twin"})
    assert marked["recorded"] is True and marked["followed"] == 1
    assert rule(store, target)["fire_count"] == 1
    na = call(server, "self_rules", {"action": "followed", "surface_id": surface_id, "applicable": False})
    assert na["followed"] == -1


def test_rules_context_query_with_explicit_session_and_phase(server_store):
    server, store = server_store
    add_rule(store, "Deploy only after review")
    payload = call(server, "self_rules_context", {"project": "proj", "query": "deploy",
                                                  "session_id": "other", "phase": "build"})
    assert payload["phase_filter"] == "build"
    surface_id = payload["rules"][0]["surface_id"]
    assert store.db.execute("SELECT session_id FROM rule_surfaces WHERE id=?",
                            (surface_id,)).fetchone()[0] == "other"


def test_rules_context_query_with_no_rules(server_store):
    server, _ = server_store
    payload = call(server, "self_rules_context", {"project": "proj", "query": "anything"})
    assert payload == {"rules_count": 0, "rules": [], "ranking": "lexical"}


def test_followed_unknown_surface_is_an_error(server_store):
    server, _ = server_store
    payload = call(server, "self_rules", {"action": "followed", "surface_id": "rs_nope", "followed": True})
    assert "error" in payload and payload["surface_id"] == "rs_nope"


def test_report_action(server_store):
    server, store = server_store
    add_rule(store, "Deploy only after review")
    surface_id = call(server, "self_rules_context", {"project": "proj", "query": "deploy"})["rules"][0]["surface_id"]
    call(server, "self_rules", {"action": "followed", "surface_id": surface_id, "followed": True})
    report = call(server, "self_rules", {"action": "report", "project": "proj"})
    assert report["rules"][0]["verdict"] == "insufficient"
    assert report["rules"][0]["followed"] == 1
    assert report["caveats"]
    assert "error" in call(server, "self_rules", {"action": "report", "min_n": 0})


# ── module SQL on the store backend (SQLite and PostgreSQL) ──────────────


def test_followcheck_and_report_on_store_backend(server_store):
    from datetime import UTC, datetime, timedelta

    _, store = server_store
    rid = add_rule(store, "Never force-push to the main branch")
    surfacer = rule_surfaces.RuleSurfacer(store.db)
    [surface] = surfacer.surface_rules("branch protection", "proj", SESSION, "test")
    action = rule_surfaces.ActionEvidence("git push --force origin main",
                                          datetime.now(UTC) + timedelta(seconds=1))
    [verdict] = surfacer.followcheck(SESSION, [action])
    assert (verdict.surface_id, verdict.verdict, verdict.persisted) == (surface.surface_id, "ignored", True)
    [entry] = surfacer.effectiveness_report("proj").rules
    assert (entry.rule_id, entry.ignored, entry.verdict) == (rid, 1, "insufficient")


def test_correction_capture_on_store_backend(server_store):
    import correction_capture

    _, store = server_store
    first = correction_capture.capture(store.db, "I already told you: run the linter first", "proj", SESSION)
    again = correction_capture.capture(store.db, "How many times: run the linter first!", "proj", SESSION)
    assert first is not None and again == first
    row = store.db.execute("SELECT importance, tags FROM insights WHERE id=?", (first,)).fetchone()
    assert row[0] == correction_capture.DRAFT_IMPORTANCE + correction_capture.REPEAT_IMPORTANCE_STEP
    assert correction_capture.DRAFT_TAG in json.loads(row[1])
