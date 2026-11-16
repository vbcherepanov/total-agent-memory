"""Ambient memory (src/ambient + the hooks that drive it).

Covers: the agent action log (migration 040, retention, redaction, erasure,
export, forget), the settings resolution, the prompt context (rules, warm
daemon path, cold FTS path, dedupe, budget, deadline), PreToolUse rule
reminders, the Stop-hook compliance check (prohibitions, Wilson lower bound,
fire_count untouched by loading), the turn summary auto-save, `tam
forget-auto`, Codex notify capture, and the hook scripts end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import rule_surfaces as rs
from ambient import actions as act
from ambient import codex, forget, prompt_context, reminders, turn_end
from ambient.settings import (
    ACTION_LOG_RETENTION_ENV,
    AMBIENT_LATENCY_ENV,
    AMBIENT_RECALL_ENV,
    AMBIENT_TOKEN_BUDGET_ENV,
    MAX_TOKEN_BUDGET,
    SETTING_KEYS,
    TURN_AUTOSAVE_ENV,
    AmbientSettings,
)
from ambient.state import SessionState, prune_states, state_path
from memory_core.timestamps import format_utc

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
NOW = "2026-10-13T00:00:00Z"
SUDO_RULE = "Never use sudo for pip installs"
COVERAGE_RULE = "Always run pytest with coverage before a release"
SUDO_COMMAND = "sudo pip install requests"
SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def mem(tmp_path, monkeypatch) -> Iterator[Path]:
    """A memory dir holding a fully migrated store (no embedder, no Chroma)."""
    import server

    root = tmp_path / "mem"
    root.mkdir()
    monkeypatch.setattr(server, "MEMORY_DIR", root)
    monkeypatch.setattr(server, "HAS_CHROMA", False)
    monkeypatch.setattr(server.Store, "_init_embed_mode", lambda self: "none")
    monkeypatch.setattr(server.Store, "_check_embed_dim_compat", lambda self: None)
    monkeypatch.setattr(server.Store, "embed", lambda self, texts: [])
    monkeypatch.setenv("MEMORY_ASYNC_ENRICHMENT", "false")
    for key in SETTING_KEYS:
        monkeypatch.delenv(key, raising=False)
    store = server.Store()
    store.db.close()
    worker = getattr(store, "_enrich_worker", None)
    if worker is not None:
        worker.stop()
    yield root


def connect(mem: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(mem / "memory.db"))


def add_rule(mem: Path, content: str, priority: int = 5, scope: str = "global") -> int:
    with connect(mem) as db:
        cursor = db.execute(
            "INSERT INTO rules (session_id, content, category, scope, priority, project, tags, status, created_at, "
            "updated_at) VALUES ('s', ?, 'x', ?, ?, 'general', '[]', 'active', ?, ?)",
            (content, scope, priority, NOW, NOW))
        return int(cursor.lastrowid)


def add_record(mem: Path, content: str, project: str = "proj", session: str = "other",
               source: str = "explicit", tags: str = "[]") -> int:
    with connect(mem) as db:
        cursor = db.execute(
            "INSERT INTO knowledge (session_id, type, content, context, project, tags, status, confidence, source, "
            "created_at, last_confirmed, recall_count) VALUES (?, 'fact', ?, '', ?, ?, 'active', 1.0, ?, ?, ?, 0)",
            (session, content, project, tags, source, NOW, NOW))
        return int(cursor.lastrowid)


def rule_row(mem: Path, rule_id: int) -> dict[str, Any]:
    with connect(mem) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone())


def log_action(mem: Path, tool: str, tool_input: dict, session: str = "S1", project: str = "proj",
               status: act.Status = "ok", at: datetime | None = None) -> int:
    action = act.build_action(tool=tool, tool_input=tool_input, session_id=session, project=project,
                              status=status, exit_code=0 if status == "ok" else 1, now=at)
    assert action is not None
    with connect(mem) as db:
        return act.ActionLog(db).record(action, retention_days=30, now=at)


def settings(**overrides: Any) -> AmbientSettings:
    return AmbientSettings(**overrides)


# ── migration 040 ──────────────────────────────────────────────────────────


def test_migration_040_creates_the_action_log_and_the_lower_bound(mem):
    with connect(mem) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(agent_actions)")]
        indexes = {row[1] for row in db.execute("PRAGMA index_list(agent_actions)")}
        rules = [row[1] for row in db.execute("PRAGMA table_info(rules)")]
        applied = {row[0] for row in db.execute("SELECT version FROM migrations")}
    assert columns == ["id", "ts", "client", "session_id", "project", "tool", "summary", "status", "exit_code"]
    assert {"idx_agent_actions_session_ts", "idx_agent_actions_ts", "idx_agent_actions_project_ts"} <= indexes
    assert rules[-3:] == ["success_lower_bound", "auto_success_count", "auto_fail_count"]
    assert "040" in applied


def test_action_status_is_constrained(mem):
    with connect(mem) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO agent_actions (ts, tool, status) VALUES (?, 'Bash', 'weird')", (NOW,))


def test_store_start_backfills_lower_bounds_of_rated_rules(mem, monkeypatch):
    import server

    rule_id = add_rule(mem, "Prefer explicit migrations")
    with connect(mem) as db:
        db.execute("UPDATE rules SET success_count=8, fail_count=2, success_lower_bound=0 WHERE id=?", (rule_id,))
    store = server.Store()
    store.db.close()
    expected = rs.wilson_interval(8, 10)[0]
    assert rule_row(mem, rule_id)["success_lower_bound"] == pytest.approx(expected)


def test_postgres_workspace_twin_is_numbered_without_gaps():
    from tam_db import pg_schema

    migrations = pg_schema.workspace_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    twin = (ROOT / "migrations" / "postgres" / "workspace" / "0006_agent_actions.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE agent_actions" in twin and "success_lower_bound" in twin and "'040'" in twin


# ── action log ─────────────────────────────────────────────────────────────


def test_summary_keeps_the_command_and_redacts_secrets():
    summary = act.summarize_tool_input("Bash", {"command": f"curl -H 'Authorization: Bearer {SECRET}' x"})
    assert summary.startswith("curl") and SECRET not in summary


def test_summary_of_an_edit_is_the_path_plus_new_text_and_bounded():
    summary = act.summarize_tool_input("Edit", {"file_path": "/w/a.py", "new_string": "print('x')\n" * 200})
    assert summary.startswith("/w/a.py :: print('x')")
    assert len(summary) <= act.SUMMARY_MAX_CHARS
    multi = act.summarize_tool_input("MultiEdit", {"file_path": "/w/b.py",
                                                   "edits": [{"new_string": "alpha"}, {"new_string": "beta"}]})
    assert multi == "/w/b.py :: alpha beta"


def test_summary_of_an_mcp_tool_keeps_argument_names_only():
    summary = act.summarize_tool_input("mcp__memory__memory_save", {"content": "private text", "project": "p"})
    assert summary == "keys: content, project"
    assert act.summarize_tool_input("mcp__memory__memory_recall", {"query": "private"}) == "keys: query"
    assert act.summarize_tool_input("WebFetch", {"url": "https://example.org/a"}) == "https://example.org/a"


def test_read_only_tools_are_not_logged():
    assert act.build_action(tool="Read", tool_input={"file_path": "/a"}, session_id="s", project="p",
                            status="ok", exit_code=None) is None
    assert act.build_action(tool=" ", tool_input={}, session_id="s", project="p", status="ok",
                            exit_code=None) is None


def test_record_prunes_by_age_and_by_row_cap(mem):
    old = datetime.now(UTC) - timedelta(days=40)
    log_action(mem, "Bash", {"command": "ls old"}, at=old)
    with connect(mem) as db:
        log = act.ActionLog(db)
        assert log.count() == 1
        fresh = act.build_action(tool="Bash", tool_input={"command": "ls new"}, session_id="S1", project="p",
                                 status="ok", exit_code=0)
        log.record(fresh, retention_days=30)
        assert [row[0] for row in db.execute("SELECT summary FROM agent_actions")] == ["ls new"]
        for index in range(5):
            log.record(act.build_action(tool="Bash", tool_input={"command": f"ls {index}"}, session_id="S1",
                                        project="p", status="ok", exit_code=0), retention_days=30, max_rows=3)
        assert [row[0] for row in db.execute("SELECT summary FROM agent_actions ORDER BY id")] == \
            ["ls 2", "ls 3", "ls 4"]


def test_prune_rejects_a_non_positive_retention(mem):
    with connect(mem) as db, pytest.raises(ValueError):
        act.ActionLog(db).prune(0)


def test_session_actions_returns_the_turn_after_the_boundary(mem):
    base = datetime.now(UTC)
    log_action(mem, "Bash", {"command": "first"}, at=base)
    log_action(mem, "Bash", {"command": "second"}, at=base + timedelta(seconds=1))
    log_action(mem, "Bash", {"command": "elsewhere"}, session="S2", at=base + timedelta(seconds=2))
    with connect(mem) as db:
        log = act.ActionLog(db)
        assert [a.summary for a in log.session_actions("S1")] == ["first", "second"]
        assert [a.summary for a in log.session_actions("S1", format_utc(base))] == ["second"]


def test_erase_mentions_removes_rows_quoting_the_text(mem):
    log_action(mem, "Bash", {"command": "echo 'customer Acme renews in March 2027' > notes.txt"})
    log_action(mem, "Bash", {"command": "ls -la"})
    with connect(mem) as db:
        removed = act.ActionLog(db).erase_mentions(["customer Acme renews in March 2027", "x"])
        db.commit()
        assert removed == 1
        assert [row[0] for row in db.execute("SELECT summary FROM agent_actions")] == ["ls -la"]


# ── settings ───────────────────────────────────────────────────────────────


def test_settings_defaults_env_and_dashboard_precedence(tmp_path):
    assert AmbientSettings.load(tmp_path, {}) == AmbientSettings()
    env = {AMBIENT_RECALL_ENV: "false", AMBIENT_TOKEN_BUDGET_ENV: "999999", AMBIENT_LATENCY_ENV: "abc",
           ACTION_LOG_RETENTION_ENV: "0"}
    loaded = AmbientSettings.load(tmp_path, env)
    assert loaded.ambient_recall is False
    assert loaded.token_budget == MAX_TOKEN_BUDGET
    assert loaded.latency_ms == AmbientSettings().latency_ms
    assert loaded.action_log_retention_days == 1
    (tmp_path / "settings.json").write_text(json.dumps(
        {"version": 1, "values": {AMBIENT_RECALL_ENV: "true", TURN_AUTOSAVE_ENV: "false"}, "secrets": {}}), encoding="utf-8")
    loaded = AmbientSettings.load(tmp_path, env)
    assert loaded.ambient_recall is True and loaded.turn_autosave is False


def test_unreadable_settings_file_falls_back_to_environment(tmp_path):
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert AmbientSettings.load(tmp_path, {TURN_AUTOSAVE_ENV: "false"}).turn_autosave is False


def test_every_ambient_setting_is_in_the_dashboard_catalog():
    from settings_catalog import LOCAL_CATALOG, validate_value

    specs = {spec.key: spec for spec in LOCAL_CATALOG}
    assert set(SETTING_KEYS) <= set(specs)
    defaults = AmbientSettings()
    samples = {AMBIENT_TOKEN_BUDGET_ENV: str(defaults.token_budget), AMBIENT_LATENCY_ENV: str(defaults.latency_ms),
               ACTION_LOG_RETENTION_ENV: str(defaults.action_log_retention_days)}
    for key in SETTING_KEYS:
        assert validate_value(specs[key], samples.get(key, "true"))


# ── session state ──────────────────────────────────────────────────────────


def test_session_state_round_trip_caps_and_pruning(tmp_path):
    state = SessionState.load(tmp_path, "s/1")
    state.remember_records(range(600))
    state.remember_rules([3, 1, 3])
    state.remember_reminders([7], 100.0)
    state.last_stop = NOW
    state.save()
    again = SessionState.load(tmp_path, "s/1")
    assert len(again.records) == 500 and again.records[-1] == 599
    assert again.rules == [1, 3] and again.reminded_since(50.0) == {7} and again.last_stop == NOW
    state_path(tmp_path, "s/1").write_text("garbage", encoding="utf-8")
    assert SessionState.load(tmp_path, "s/1").records == []
    os.utime(state_path(tmp_path, "s/1"), (0, 0))
    assert prune_states(tmp_path) == 1


# ── prompt context ─────────────────────────────────────────────────────────


def build(mem: Path, prompt: str, session: str = "S1", project: str = "proj", warm=None,
          **overrides: Any) -> prompt_context.PromptContext:
    no_daemon = warm if warm is not None else (lambda body, timeout: None)
    return prompt_context.build_prompt_context(
        db_path=mem / "memory.db", memory_dir=mem, prompt=prompt, project=project, session_id=session,
        settings=settings(**overrides), warm=no_daemon)


def test_prompt_gets_matching_rules_once_per_session_and_loading_never_fires(mem):
    sudo = add_rule(mem, SUDO_RULE, priority=8)
    add_rule(mem, COVERAGE_RULE)
    first = build(mem, "install the pip packages with sudo on the build box")
    assert first.rule_ids == (sudo,)
    assert f"#{sudo} (P8) {SUDO_RULE}" in first.text
    row = rule_row(mem, sudo)
    assert row["shown_count"] == 1 and row["fire_count"] == 0
    with connect(mem) as db:
        assert db.execute("SELECT source FROM rule_surfaces WHERE rule_id=?", (sudo,)).fetchall() == \
            [("ambient_prompt",)]
    again = build(mem, "install the pip packages with sudo on the build box")
    assert again.rule_ids == () and SUDO_RULE not in again.text
    other_session = build(mem, "install the pip packages with sudo on the build box", session="S2")
    assert other_session.rule_ids == (sudo,)
    assert rule_row(mem, sudo)["fire_count"] == 0


def test_a_single_shared_word_does_not_pull_a_rule(mem):
    add_rule(mem, COVERAGE_RULE)
    assert build(mem, "write the release notes for the customer").rule_ids == ()


def test_cold_path_recalls_project_records_within_budget_and_dedupes(mem):
    wanted = add_record(mem, "Deploy pipeline uses blue green release with canary checks")
    add_record(mem, "Deploy pipeline of another product", project="elsewhere")
    add_record(mem, "Deploy pipeline note from this very session", session="S1")
    result = build(mem, "how does the deploy pipeline release work?", token_budget=100)
    assert result.background == "cold" and result.record_ids == (wanted,)
    assert "blue green" in result.text and "another product" not in result.text and "very session" not in result.text
    assert len(result.text) <= 100 * prompt_context.CHARS_PER_TOKEN
    assert build(mem, "how does the deploy pipeline release work?").record_ids == ()


def test_warm_path_is_used_and_records_already_seen_are_dropped(mem):
    calls: list[tuple[dict, float]] = []

    def warm(body, timeout):
        calls.append((dict(body), timeout))
        return {"ok": True, "text": "[TAM memory: x]\n- (fact #5) five\n- (fact #6) six", "ids": [5, 6]}

    first = build(mem, "tell me about the deploy pipeline", warm=warm)
    assert first.background == "warm" and first.record_ids == (5, 6)
    body, timeout = calls[0]
    assert body["session_id"] == "S1" and body["project"] == "proj" and body["dedupe"] is True
    assert 0 < timeout <= AmbientSettings().latency_ms / 1000
    second = build(mem, "tell me about the deploy pipeline again", warm=warm)
    assert second.record_ids == () and second.text == ""


def test_warm_failure_falls_back_to_fts(mem):
    wanted = add_record(mem, "Kafka consumer lag alerts page the on-call engineer")
    result = build(mem, "why did kafka consumer lag alerts fire", warm=lambda body, timeout: None)
    assert result.background == "cold" and result.record_ids == (wanted,)


def test_expired_deadline_skips_background_and_never_raises(mem):
    add_record(mem, "Kafka consumer lag alerts page the on-call engineer")
    ticks = iter([0.0, 0.0] + [10.0] * 50)
    result = prompt_context.build_prompt_context(
        db_path=mem / "memory.db", memory_dir=mem, prompt="kafka consumer lag alerts", project="proj",
        session_id="S1", settings=settings(), warm=lambda body, timeout: pytest.fail("no time left"),
        clock=lambda: next(ticks))
    assert result.record_ids == () and result.background == "none"


@pytest.mark.parametrize("prompt", ["/compact", "ok", "   "])
def test_commands_and_trivial_prompts_get_nothing(mem, prompt):
    add_rule(mem, SUDO_RULE)
    assert build(mem, prompt).text == ""


def test_disabled_recall_does_nothing(mem):
    add_rule(mem, SUDO_RULE)
    assert build(mem, "install pip packages with sudo", ambient_recall=False).text == ""


def test_drop_seen_lines_keeps_header_and_unseen_records():
    text, ids = prompt_context.drop_seen_lines("H\n- a\n- b", [1, 2], frozenset({1}))
    assert (text, ids) == ("H\n- b", [2])
    assert prompt_context.drop_seen_lines("H\n- a b", [1, 2], frozenset({1})) == ("", [])


class _RecallHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict]] = []

    def do_POST(self):  # noqa: N802 — http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _RecallHandler.seen.append((self.headers.get("Authorization", ""), body))
        payload = json.dumps({"ok": True, "text": "[TAM memory]\n- (fact #9) nine", "ids": [9]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


def test_daemon_recall_talks_to_the_registered_daemon_with_its_token(tmp_path, monkeypatch):
    from tam_daemon.registry import DaemonPaths, DaemonRecord, write_record

    assert prompt_context.daemon_recall(tmp_path) is None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RecallHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        paths = DaemonPaths.for_memory_dir(tmp_path)
        paths.ensure_root()
        write_record(paths, DaemonRecord(pid=os.getpid(), host="127.0.0.1", port=httpd.server_address[1],
                                         version="t", started_at=0.0, memory_dir=str(tmp_path), python="py"))
        paths.token.write_text("tok\n", encoding="utf-8")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        caller = prompt_context.daemon_recall(tmp_path)
        assert caller is not None
        answer = caller({"query": "q", "dedupe": True}, 2.0)
        assert answer["ids"] == [9]
        assert _RecallHandler.seen[-1] == ("Bearer tok", {"query": "q", "dedupe": True})
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── rule reminders ─────────────────────────────────────────────────────────


def remind(mem: Path, tool: str, tool_input: dict, state: SessionState, now: float) -> reminders.Reminder:
    with connect(mem) as db:
        return reminders.build_reminder(db, tool=tool, tool_input=tool_input, project="proj", session_id="S1",
                                        state=state, now=now)


def test_reminder_flags_a_command_a_prohibition_forbids_and_respects_cooldown(mem):
    sudo = add_rule(mem, SUDO_RULE, priority=8)
    state = SessionState.load(mem, "S1")
    first = remind(mem, "Bash", {"command": SUDO_COMMAND}, state, now=1000.0)
    assert first.rule_ids == (sudo,) and first.forbidden_hits == {sudo: ("sudo",)}
    assert "contains what the rule forbids: sudo" in first.text
    assert remind(mem, "Bash", {"command": SUDO_COMMAND}, state, now=1100.0).rule_ids == ()
    later = 1000.0 + reminders.REMINDER_COOLDOWN_SEC + 1
    assert remind(mem, "Bash", {"command": SUDO_COMMAND}, state, now=later).rule_ids == (sudo,)
    assert rule_row(mem, sudo)["fire_count"] == 0


def test_reminder_matches_the_new_text_of_an_edit(mem):
    rule = add_rule(mem, "Never call print in service handlers, use the structured logger")
    state = SessionState.load(mem, "S1")
    hit = remind(mem, "Edit", {"file_path": "/w/svc/handlers.py", "new_string": "print('debug')"}, state, 1.0)
    assert hit.rule_ids == (rule,)


def test_reminder_ignores_unrelated_actions_and_other_tools(mem):
    add_rule(mem, SUDO_RULE)
    state = SessionState.load(mem, "S1")
    assert remind(mem, "Bash", {"command": "ls -la"}, state, 1.0).text == ""
    assert remind(mem, "WebFetch", {"url": "sudo pip"}, state, 1.0).text == ""


# ── Stop: compliance + turn summary ────────────────────────────────────────


def stop(mem: Path, session: str = "S1", message: str = "", **overrides: Any) -> turn_end.TurnEndResult:
    return turn_end.run_turn_end(db_path=mem / "memory.db", memory_dir=mem, session_id=session, project="proj",
                                 cwd="/w/proj", assistant_message=message, settings=settings(**overrides))


MANUAL_COLUMNS = ("success_count", "fail_count", "success_rate", "success_lower_bound", "status")


def manual_stats(mem: Path, rule_id: int) -> tuple:
    row = rule_row(mem, rule_id)
    return tuple(row[column] for column in MANUAL_COLUMNS)


def violate(mem: Path, session: str) -> turn_end.TurnEndResult:
    build(mem, "install the pip packages with sudo on the build box", session=session)
    log_action(mem, "Bash", {"command": SUDO_COMMAND}, session=session)
    return stop(mem, session=session)


def test_violated_prohibition_is_reported_and_counted_as_auto_fail_only(mem):
    sudo = add_rule(mem, SUDO_RULE, priority=8)
    before = manual_stats(mem, sudo)
    result = violate(mem, "S1")
    assert [v.rule_id for v in result.compliance.violations] == [sudo]
    assert SUDO_RULE in result.notice and "sudo" in result.notice
    row = rule_row(mem, sudo)
    assert (row["auto_fail_count"], row["auto_success_count"], row["fire_count"]) == (1, 0, 0)
    assert manual_stats(mem, sudo) == before
    again = stop(mem)
    assert again.actions == 0 and again.notice == "" and rule_row(mem, sudo)["auto_fail_count"] == 1


def test_followed_rule_counts_as_auto_success_only(mem):
    rule = add_rule(mem, "Run pytest with coverage and mypy before tagging a release")
    before = manual_stats(mem, rule)
    build(mem, "tag the release after running pytest with coverage and mypy")
    log_action(mem, "Bash", {"command": "pytest --cov src && mypy src && echo tagging release"})
    result = stop(mem)
    assert [v.rule_id for v in result.compliance.followed] == [rule]
    row = rule_row(mem, rule)
    assert (row["auto_success_count"], row["auto_fail_count"], row["fire_count"]) == (1, 0, 1)
    assert manual_stats(mem, rule) == before


def test_heuristic_verdicts_never_change_manual_ratings_or_suspension(mem):
    import server

    sudo = add_rule(mem, SUDO_RULE)
    twin = add_rule(mem, "Never use sudo for npm installs either")
    store = server.Store()
    try:
        # Four explicit failures: one more would suspend (RULE_SUSPEND_MIN_RATED = 5).
        for rule in (sudo, twin):
            for _ in range(rs.RULE_SUSPEND_MIN_RATED - 1):
                store.manage_rule("s", "rate", id=rule, success=False)
        for index in range(rs.RULE_SUSPEND_MIN_RATED + 2):
            violate(mem, f"V{index}")
        assert rule_row(mem, sudo)["auto_fail_count"] == rs.RULE_SUSPEND_MIN_RATED + 2
        assert manual_stats(mem, sudo) == manual_stats(mem, twin)[:4] + ("active",)
        # The next explicit rating decides exactly as it would have without any heuristic verdict.
        with_heuristics = store.manage_rule("s", "rate", id=sudo, success=True)
        without = store.manage_rule("s", "rate", id=twin, success=True)
    finally:
        store.db.close()
    assert {k: v for k, v in with_heuristics.items() if k != "id"} == {k: v for k, v in without.items() if k != "id"}
    assert manual_stats(mem, sudo) == manual_stats(mem, twin)
    assert (rule_row(mem, sudo)["success_count"], rule_row(mem, sudo)["fail_count"]) == (1, 4)


def test_rule_listings_expose_the_auto_counters(mem):
    import server

    sudo = add_rule(mem, SUDO_RULE)
    violate(mem, "S1")
    store = server.Store()
    try:
        listed = store.manage_rule("s", "list")["rules"]
        context = store.get_rules_for_context(project="proj")["rules"]
        ranked = store.get_rules_for_context(project="proj", query="pip installs with sudo", session_id="X")["rules"]
        report = store.analyze_patterns(view="rule_effectiveness")
    finally:
        store.db.close()
    for rows in (listed, context, ranked):
        row = next(r for r in rows if r["id"] == sudo)
        assert (row["auto_success_count"], row["auto_fail_count"]) == (0, 1)
    stats = next(r for r in report["rule_effectiveness"]["rules"] if r["id"] == sudo)
    assert stats["auto_fail_count"] == 1


def test_turn_summary_saved_for_a_changing_turn_redacted_and_queued(mem):
    with connect(mem) as db:
        db.execute("INSERT INTO intents (session_id, project, prompt, created_at, turn_index, prompt_hash) "
                   "VALUES ('S1', 'proj', 'fix the flaky login test', ?, 0, 'h')", (NOW,))
    log_action(mem, "Edit", {"file_path": "/w/proj/login_test.py", "new_string": "retry = 3"})
    log_action(mem, "Bash", {"command": f"API_KEY={SECRET} pytest tests/login_test.py"}, status="failed")
    result = stop(mem, message=f"Fixed; the token was {SECRET}")
    assert result.saved_id is not None
    with connect(mem) as db:
        content, tags, source, confidence = db.execute(
            "SELECT content, tags, source, confidence FROM knowledge WHERE id=?", (result.saved_id,)).fetchone()
        pending = db.execute("SELECT COUNT(*) FROM embedding_pending WHERE knowledge_id=?",
                             (result.saved_id,)).fetchone()[0]
    assert "Request: fix the flaky login test" in content and "Files: /w/proj/login_test.py" in content
    assert "(1 failed)" in content and SECRET not in content
    assert json.loads(tags)[:2] == ["auto-saved", "turn-autosave"] and source == "auto" and confidence < 0.8
    assert pending == 1


def test_read_only_or_disabled_turns_save_nothing(mem):
    log_action(mem, "WebFetch", {"url": "https://example.org"})
    assert stop(mem).saved_id is None
    log_action(mem, "Bash", {"command": "ls"})
    assert stop(mem, turn_autosave=False).saved_id is None
    log_action(mem, "Bash", {"command": "ls"})
    assert stop(mem, turn_autosave=False, rule_compliance=False).actions == 0


def test_identical_summary_is_not_saved_twice(mem):
    with connect(mem) as db:
        kwargs = dict(content="Turn summary X", project="proj", session_id="S1", cwd="/w")
        assert turn_end.save_turn_summary(db, **kwargs) is not None
        assert turn_end.save_turn_summary(db, **kwargs) is None


def test_manual_rating_keeps_its_contract_and_stores_the_lower_bound(mem):
    import server

    rule = add_rule(mem, "Keep handlers thin")
    store = server.Store()
    try:
        result = store.manage_rule("s", "rate", id=rule, success=True)
        missing = store.manage_rule("s", "rate", id=987654, success=True)
    finally:
        store.db.close()
    assert result["rated"] is True and result["success_rate"] == 1.0 and result["ci_upper"] == 1.0
    assert {k: missing[k] for k in ("rated", "id", "success_rate")} == {
        "rated": True, "id": 987654, "success_rate": None}
    assert missing["error"] == "Rule 987654 not found"
    assert rule_row(mem, rule)["success_lower_bound"] == pytest.approx(rs.wilson_interval(1, 1)[0])


# ── Codex ──────────────────────────────────────────────────────────────────


def test_codex_turn_is_logged_and_summarised(mem):
    raw = json.dumps({"type": "agent-turn-complete", "thread-id": "T1", "cwd": "/w/codexproj",
                      "input-messages": ["rename foo to bar"], "last-assistant-message": "Renamed."})
    turn = codex.parse_notification(raw)
    result = codex.capture_turn(mem / "memory.db", turn, settings())
    assert result.recorded and result.saved_id is not None
    with connect(mem) as db:
        assert db.execute("SELECT client, session_id, project, tool, summary FROM agent_actions").fetchall() == \
            [("codex", "T1", "codexproj", "codex_turn", "rename foo to bar")]
    assert codex.parse_notification(json.dumps({"type": "approval-requested"})) is None
    with pytest.raises(codex.CodexNotificationError):
        codex.parse_notification("[1]")


# ── erasure / forget / export ──────────────────────────────────────────────


def test_hard_erasure_removes_action_rows_quoting_the_record(mem):
    import erasure
    import server

    text = "Customer Acme renews the enterprise contract in March 2027"
    record = add_record(mem, text)
    log_action(mem, "Bash", {"command": f"echo '{text}' >> notes.md"})
    log_action(mem, "Bash", {"command": "ls"})
    store = server.Store()
    try:
        result = erasure.erase(store, record, mem / "raw", None)
    finally:
        store.db.close()
    assert result["actions_erased"] == 1 and result["erased"] == [record]
    with connect(mem) as db:
        assert [row[0] for row in db.execute("SELECT summary FROM agent_actions")] == ["ls"]


def test_retention_export_and_forget_cover_the_action_log(mem, monkeypatch):
    import server

    log_action(mem, "Bash", {"command": "mine"})
    log_action(mem, "Bash", {"command": "theirs"}, project="elsewhere")
    with connect(mem) as db:
        db.execute("INSERT INTO agent_actions (ts, session_id, project, tool, summary) VALUES (?, 'S0', 'proj', "
                   "'Bash', 'old')", (format_utc(datetime.now(UTC) - timedelta(days=45)),))
    store = server.Store()
    monkeypatch.setattr(server, "store", store)
    try:
        dry = call_tool(server, "memory_forget", dry_run=True)
        assert dry["would_prune_actions"] == 1
        exported = store.export_all(project="proj")
        assert [row["summary"] for row in exported["agent_actions"]] == ["old", "mine"]
        applied = call_tool(server, "memory_forget", dry_run=False)
        assert applied["applied"] is True and applied["actions_pruned"] == 1
        assert [row["summary"] for row in store.export_all()["agent_actions"]] == ["mine", "theirs"]
    finally:
        store.db.close()


def call_tool(server, tool: str, **args: Any) -> dict[str, Any]:
    content, is_error = asyncio.run(server._call_tool_impl(tool, args))
    assert not is_error, content[0].text
    return json.loads(content[0].text)


def test_forget_auto_erases_only_auto_saved_records(mem, capsys):
    keep = add_record(mem, "A decision the user saved on purpose")
    turn = add_record(mem, "Turn summary one", source="auto", tags='["auto-saved", "turn-autosave", "proj"]')
    session = add_record(mem, "Session auto-save two", source="auto",
                         tags='["session-autosave", "context-recovery", "proj"]')
    other = add_record(mem, "Turn summary elsewhere", project="elsewhere", source="auto",
                       tags='["auto-saved", "turn-autosave", "elsewhere"]')
    log_action(mem, "Bash", {"command": "ls"})

    assert forget.main(["--memory-dir", str(mem), "--project", "proj", "--dry-run", "--json",
                        "--include-actions"]) == forget.EXIT_OK
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True and preview["records"] == 2 and preview["actions"] == 1

    assert forget.main(["--memory-dir", str(mem), "--project", "proj", "--include-actions"]) == forget.EXIT_OK
    assert "deleted 2 auto-saved record(s) in project proj and 1 action log row(s)" in capsys.readouterr().out
    with connect(mem) as db:
        remaining = {row[0] for row in db.execute("SELECT id FROM knowledge")}
        assert db.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0] == 0
    assert keep in remaining and other in remaining and turn not in remaining and session not in remaining


def test_forget_auto_reports_a_missing_store_and_bad_usage(tmp_path, capsys):
    assert forget.main(["--memory-dir", str(tmp_path), "--dry-run"]) == forget.EXIT_FAILURE
    assert "no memory store" in capsys.readouterr().err
    assert forget.main(["--bogus"]) == forget.EXIT_USAGE


def test_tam_entry_point_dispatches_forget_auto(tmp_path, monkeypatch, capsys):
    import total_agent_memory.server as entry

    monkeypatch.setattr(entry, "_adopt_existing_install", lambda: None)
    monkeypatch.setattr(sys, "argv", ["tam", "forget-auto", "--memory-dir", str(tmp_path), "--dry-run"])
    with pytest.raises(SystemExit) as exit_info:
        entry.main_sync()
    assert exit_info.value.code == forget.EXIT_FAILURE
    assert "no memory store" in capsys.readouterr().err


# ── hooks end to end ───────────────────────────────────────────────────────

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
# The hook helper starts `docker` with subprocess; Windows CreateProcess finds docker.exe, never a shebang fake.
needs_fake_docker = pytest.mark.skipif(sys.platform == "win32", reason="shebang fake docker is not executable on Windows")


def hook_env(mem: Path, home: Path, install: Path = ROOT, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TAM_", "CLAUDE_MEMORY", "MEMORY_NUDGE", "MEMORY_PROJECT", "HOOK_"))}
    env.update({"HOME": str(home), "CLAUDE_MEMORY_DIR": str(mem), "CLAUDE_MEMORY_INSTALL_DIR": str(install),
                "HOOK_PYTHON": sys.executable, "TAM_HOOK_NOTIFY": "0"})
    env.update(extra)
    return env


def run_hook(name: str, payload: dict | None, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    data = json.dumps(payload).encode() if payload is not None else b""
    return subprocess.run([BASH, str(HOOKS / name), *args], input=data, env=env, capture_output=True,
                          timeout=60, check=False)


def only_json(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr.decode()
    lines = [line for line in proc.stdout.decode().splitlines() if line.strip()]
    assert len(lines) == 1, proc.stdout.decode()
    return json.loads(lines[0])


@needs_bash
def test_hooks_drive_the_whole_turn(mem, tmp_path):
    sudo = add_rule(mem, SUDO_RULE, priority=8)
    add_record(mem, "Build boxes install Python packages into a virtualenv, never system-wide")
    env = hook_env(mem, tmp_path)
    base = {"session_id": "H1", "cwd": "/w/proj"}

    prompt = only_json(run_hook("user-prompt-submit.sh", {
        **base, "hook_event_name": "UserPromptSubmit",
        "prompt": "install the python packages on the build boxes with sudo pip"}, env))["hookSpecificOutput"]
    assert prompt["hookEventName"] == "UserPromptSubmit"
    assert SUDO_RULE in prompt["additionalContext"] and "virtualenv" in prompt["additionalContext"]

    reminder = only_json(run_hook("rule-reminder.sh", {
        **base, "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": SUDO_COMMAND}}, env))["hookSpecificOutput"]
    assert set(reminder) == {"hookEventName", "additionalContext"}, "never a permission decision"
    assert "forbids: sudo" in reminder["additionalContext"]

    post = run_hook("post-tool-use.sh", {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                         "tool_input": {"command": SUDO_COMMAND}, "tool_response": {"exit_code": 0}},
                    env)
    assert post.returncode == 0
    failed = run_hook("post-tool-use.sh", {**base, "hook_event_name": "PostToolUseFailure", "tool_name": "Edit",
                                           "tool_input": {"file_path": "/w/proj/a.py", "new_string": "x = 1"},
                                           "error": "File has been modified since read"}, env)
    assert failed.returncode == 0 and failed.stdout.decode().strip() == ""
    with connect(mem) as db:
        assert db.execute("SELECT tool, status FROM agent_actions ORDER BY id").fetchall() == \
            [("Bash", "ok"), ("Edit", "failed")]
    nudge_state = json.loads((mem / "state" / "nudge-H1.json").read_text(encoding="utf-8"))
    assert nudge_state["edits"] == 0, "a failed edit is not counted as an edit"

    stopped = only_json(run_hook("on-stop.sh", {**base, "hook_event_name": "Stop",
                                               "last_assistant_message": "Installed."}, env))
    assert "TAM rule check" in stopped["systemMessage"] and SUDO_RULE in stopped["systemMessage"]
    assert rule_row(mem, sudo)["auto_fail_count"] == 1 and rule_row(mem, sudo)["fail_count"] == 0
    with connect(mem) as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge WHERE source='auto'").fetchone()[0] == 1


@needs_bash
def test_ambient_hooks_stay_silent_without_src_or_store(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    install = tmp_path / "install-without-src"
    install.mkdir()
    env = hook_env(mem, tmp_path, install=install)
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "anything at all here", "session_id": "x"}
    for name in ("user-prompt-submit.sh", "rule-reminder.sh", "post-tool-use.sh"):
        proc = run_hook(name, payload, env)
        assert proc.returncode == 0 and proc.stdout.decode().strip() == "", name
    (mem / "memory.db").write_bytes(b"")
    proc = run_hook("user-prompt-submit.sh", payload, env)
    assert proc.returncode == 0 and proc.stdout.decode().strip() == ""
    assert "ambient memory unavailable" in (mem / "hooks.log").read_text(encoding="utf-8")


@needs_bash
def test_prompt_hook_survives_a_broken_store(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "memory.db").write_bytes(b"this is not a database")
    proc = run_hook("user-prompt-submit.sh", {"hook_event_name": "UserPromptSubmit", "session_id": "x",
                                              "prompt": "explain the deployment pipeline"}, hook_env(mem, tmp_path))
    assert proc.returncode == 0 and proc.stdout.decode().strip() == ""
    assert "ambient_rules_skipped" in (mem / "hooks.log").read_text(encoding="utf-8")


@needs_bash
def test_codex_notify_hook_reads_the_argument_and_stdin(mem, tmp_path):
    env = hook_env(mem, tmp_path)
    turn = {"type": "agent-turn-complete", "thread-id": "C1", "cwd": "/w/cx", "input-messages": ["add tests"],
            "last-assistant-message": "Added."}
    assert run_hook("codex-notify.sh", None, env, json.dumps(turn)).returncode == 0
    assert run_hook("codex-notify.sh", {**turn, "thread-id": "C2"}, env).returncode == 0
    assert run_hook("codex-notify.sh", None, env, json.dumps({"type": "other"})).returncode == 0
    with connect(mem) as db:
        assert db.execute("SELECT session_id FROM agent_actions ORDER BY id").fetchall() == [("C1",), ("C2",)]


# ── registration and PowerShell parity (static) ────────────────────────────


def test_plugin_and_installer_register_the_ambient_hooks(tmp_path):
    from setup_wizard import clients, extras

    plugin = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    pre = [h["command"] for block in plugin["PreToolUse"] for h in block["hooks"]]
    failure = [h["command"] for block in plugin["PostToolUseFailure"] for h in block["hooks"]]
    assert any(c.endswith('rule-reminder.sh"') for c in pre)
    assert any(c.endswith('post-tool-use.sh"') for c in failure)

    plan = extras.plan_hooks(ROOT, clients.Host(tmp_path, "Darwin", {}, lambda _b: None), memory_dir=tmp_path / "m")
    settings_json = json.loads(plan.text)["hooks"]

    def scripts(event: str) -> list[str]:
        return [word for b in settings_json[event] for h in b["hooks"] for word in shlex.split(h["command"])]

    assert any(s.endswith("rule-reminder.sh") for s in scripts("PreToolUse"))
    assert any(s.endswith("post-tool-use.sh") for s in scripts("PostToolUseFailure"))


@pytest.mark.parametrize("name,command", [
    ("user-prompt-submit", "prompt-context"), ("rule-reminder", "rule-reminder"),
    ("post-tool-use", "record-action"), ("codex-notify", "codex-notify"), ("on-stop", "--src"),
])
def test_ps1_twins_call_the_same_ambient_helper(name, command):
    assert command in (HOOKS / f"{name}.sh").read_text(encoding="utf-8")
    ps1 = (HOOKS / f"{name}.ps1").read_text(encoding="utf-8")
    assert command in ps1 and '. (Join-Path $PSScriptRoot "lib\\common.ps1")' in ps1
    assert "TODO" not in ps1


def test_codex_twins_do_not_block_on_stdin_when_given_an_argument():
    assert "HOOK_SKIP_STDIN=1" in (HOOKS / "codex-notify.sh").read_text(encoding="utf-8")
    assert "$HookSkipStdin = $args.Count -gt 0" in (HOOKS / "codex-notify.ps1").read_text(encoding="utf-8")
    assert "HookSkipStdin" in (HOOKS / "lib" / "common.ps1").read_text(encoding="utf-8")


@pytest.mark.postgres
def test_postgres_backfill_matches_the_python_wilson_bound(pg_database):
    import psycopg

    twin = (ROOT / "migrations" / "postgres" / "workspace" / "0006_agent_actions.sql").read_text(encoding="utf-8")
    update = twin[twin.index("UPDATE rules SET success_lower_bound"):]
    update = update[:update.index(";") + 1]
    with psycopg.connect(pg_database.url) as conn:
        conn.execute("CREATE TEMP TABLE rules (id bigint, success_count bigint, fail_count bigint, "
                     "success_lower_bound double precision DEFAULT 0.0)")
        conn.execute("INSERT INTO rules VALUES (1, 8, 2, 0), (2, 0, 3, 0), (3, 5, 0, 0), (4, NULL, NULL, 0)")
        conn.execute(update)
        bounds = dict(conn.execute("SELECT id, success_lower_bound FROM rules").fetchall())
    assert bounds[1] == pytest.approx(rs.wilson_lower_bound(8, 2))
    assert bounds[3] == pytest.approx(rs.wilson_lower_bound(5, 0))
    assert bounds[2] == 0.0 and bounds[4] == 0.0


# ── Docker installs ────────────────────────────────────────────────────────

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Stands in for `docker`: logs the call and runs `docker exec ... python -m ambient` on this machine.
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
[ "$1" = "exec" ] || exit 0
shift
envs=()
while [ $# -gt 0 ]; do
    case "$1" in
        -i) shift ;;
        -w) [ "$2" = "/app/src" ] || exit 97; shift 2 ;;
        -e) envs+=("$2"); shift 2 ;;
        *) break ;;
    esac
done
shift
shift
cd "$FAKE_SRC" && exec env ${envs[@]+"${envs[@]}"} TAM_MEMORY_DIR="$FAKE_MEM" "$FAKE_PYTHON" "$@"
"""


def fake_bin(tmp_path: Path, scripts: dict[str, str]) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


@needs_bash
@needs_fake_docker
def test_docker_mode_runs_the_ambient_commands_inside_the_container(mem, tmp_path):
    sudo = add_rule(mem, SUDO_RULE, priority=8)
    bin_dir = fake_bin(tmp_path, {"docker": FAKE_DOCKER})
    host_mem = tmp_path / "host-mem"
    host_mem.mkdir()
    no_src = tmp_path / "install-without-src"
    no_src.mkdir()
    docker_log = tmp_path / "docker.log"
    env = hook_env(host_mem, tmp_path, install=no_src, TAM_DOCKER="1", TAM_CONTAINER="tam-mcp",
                   TAM_AMBIENT_TOKEN_BUDGET="300", PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   FAKE_DOCKER_LOG=str(docker_log), FAKE_SRC=str(ROOT / "src"), FAKE_MEM=str(mem),
                   FAKE_PYTHON=sys.executable)
    base = {"session_id": "D1", "cwd": "/w/proj"}

    prompt = only_json(run_hook("user-prompt-submit.sh", {
        **base, "hook_event_name": "UserPromptSubmit",
        "prompt": "install the pip packages with sudo on the build box"}, env))
    assert SUDO_RULE in prompt["hookSpecificOutput"]["additionalContext"]
    reminder = only_json(run_hook("rule-reminder.sh", {**base, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                                        "tool_input": {"command": SUDO_COMMAND}}, env))
    assert "forbids: sudo" in reminder["hookSpecificOutput"]["additionalContext"]
    assert run_hook("post-tool-use.sh", {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                         "tool_input": {"command": SUDO_COMMAND},
                                         "tool_response": {"exit_code": 0}}, env).returncode == 0
    stopped = only_json(run_hook("on-stop.sh", {**base, "hook_event_name": "Stop"}, env))
    assert SUDO_RULE in stopped["systemMessage"]
    turn = {"type": "agent-turn-complete", "thread-id": "DC", "cwd": "/w/cx", "input-messages": ["add tests"],
            "last-assistant-message": "Added."}
    assert run_hook("codex-notify.sh", None, env, json.dumps(turn)).returncode == 0

    with connect(mem) as db:
        assert db.execute("SELECT session_id, tool FROM agent_actions ORDER BY id").fetchall() == \
            [("D1", "Bash"), ("DC", "codex_turn")]
    assert rule_row(mem, sudo)["auto_fail_count"] == 1
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert any("tam-mcp python -m ambient prompt-context" in c and "TAM_AMBIENT_TOKEN_BUDGET=300" in c
               for c in calls)
    assert not (host_mem / "memory.db").exists()


@needs_bash
@needs_fake_docker
def test_docker_mode_without_a_running_container_stays_silent(tmp_path):
    bin_dir = fake_bin(tmp_path, {"docker": "#!/usr/bin/env bash\necho 'No such container: tam-mcp' >&2\nexit 1\n"})
    mem = tmp_path / "mem"
    mem.mkdir()
    env = hook_env(mem, tmp_path, TAM_DOCKER="1", PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    proc = run_hook("user-prompt-submit.sh", {"hook_event_name": "UserPromptSubmit", "session_id": "x",
                                              "prompt": "explain the deployment pipeline"}, env)
    assert proc.returncode == 0 and proc.stdout.decode().strip() == ""
    assert "No such container" in (mem / "hooks.log").read_text(encoding="utf-8")


def test_hook_runtime_forwards_every_ambient_setting():
    from tests.test_hook_runtime import tam_hook

    assert set(tam_hook.AMBIENT_ENV_KEYS) == set(SETTING_KEYS)


def test_ambient_module_cli_reports_bad_input(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "ambient", "record-action", "--memory-dir", str(tmp_path)],
                          input=b"not json", cwd=ROOT / "src", capture_output=True, timeout=60, check=False)
    assert proc.returncode == 1 and b"ambient_command_failed" in proc.stderr


@needs_bash
@pytest.mark.skipif(sys.platform == "win32", reason="install-docker.sh is a POSIX-only installer")
def test_docker_installer_registers_the_ambient_hooks(tmp_path):
    root = tmp_path / "checkout"
    shutil.copytree(HOOKS, root / "hooks", ignore=shutil.ignore_patterns("_docker-launchers", "__pycache__"))
    shutil.copy2(ROOT / "install-docker.sh", root / "install-docker.sh")
    shutil.copytree(ROOT / "src" / "setup_wizard", root / "src" / "setup_wizard",
                    ignore=shutil.ignore_patterns("__pycache__"))
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = fake_bin(tmp_path, {"docker": "#!/usr/bin/env bash\nexit 0\n", "curl": "#!/usr/bin/env bash\nexit 0\n"})
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.run([BASH or "bash", str(root / "install-docker.sh")], env=env, capture_output=True,
                          timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr.decode()
    hooks = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
    launchers = root / "hooks" / "_docker-launchers"

    def commands(event: str, matcher: str) -> list[str]:
        return [h["command"] for b in hooks[event] if b["matcher"] == matcher for h in b["hooks"]]

    assert commands("UserPromptSubmit", "") == [str(launchers / "user-prompt-submit.sh")]
    assert commands("PreToolUse", "Bash|Edit|Write|MultiEdit|NotebookEdit") == [str(launchers / "rule-reminder.sh")]
    matcher = "Edit|Write|MultiEdit|NotebookEdit|Bash"
    assert str(launchers / "post-tool-use.sh") in commands("PostToolUse", matcher)
    assert str(launchers / "post-tool-use.sh") in commands("PostToolUseFailure", matcher)
    assert not commands("PostToolUse", "")
    for name in ("user-prompt-submit.sh", "rule-reminder.sh", "post-tool-use.sh", "codex-notify.sh"):
        text = (launchers / name).read_text(encoding="utf-8")
        assert "export TAM_DOCKER=1" in text and f'exec "{root / "hooks" / name}"' in text
    assert "codex-notify.sh" in proc.stdout.decode()


# ── correction capture (UserPromptSubmit) ──────────────────────────────────

CORRECTION = "I already told you: never use sudo for pip installs on the build box"


def drafts(mem: Path) -> list[tuple]:
    with connect(mem) as db:
        return db.execute("SELECT id, content, category, tags, importance, session_id, project FROM insights "
                          "WHERE category = 'user_correction' ORDER BY id").fetchall()


def test_a_correction_in_the_prompt_creates_a_draft_once_per_session(mem):
    result = build(mem, CORRECTION)
    [draft] = drafts(mem)
    assert result.correction_id == draft[0]
    assert draft[1] == CORRECTION and draft[2] == "user_correction" and draft[5:] == ("S1", "proj")
    assert {"draft", "user-correction", "strength:strong"} <= set(json.loads(draft[3]))
    assert build(mem, CORRECTION).correction_id is None, "the same prompt counts once per session"
    assert drafts(mem)[0][4] == draft[4]
    repeated = build(mem, "How many times: never use sudo for pip installs on the build box!")
    assert repeated.correction_id == draft[0] and drafts(mem)[0][4] > draft[4]


def test_ordinary_prompts_commands_and_disabled_flag_create_nothing(mem):
    assert build(mem, "add a parser test for the config loader").correction_id is None
    assert build(mem, "/compact I already told you").correction_id is None
    assert build(mem, CORRECTION, correction_capture=False).correction_id is None
    assert build(mem, CORRECTION, correction_capture=False, ambient_recall=False).correction_id is None
    assert drafts(mem) == []


def test_correction_is_captured_with_recall_off_and_short_complaints(mem):
    assert build(mem, "WTF", ambient_recall=False).correction_id is not None
    assert len(drafts(mem)) == 1


def test_correction_capture_runs_on_the_warm_path_and_survives_a_missing_table(mem):
    warm = lambda body, timeout: {"ok": True, "text": "[TAM memory]\n- (fact #5) five", "ids": [5]}  # noqa: E731
    result = build(mem, CORRECTION, warm=warm)
    assert result.background == "warm" and result.correction_id is not None
    with connect(mem) as db:
        db.execute("DROP TABLE insights")
    assert build(mem, "сколько раз повторять: запускай линтер", session="S2").correction_id is None


def test_correction_capture_setting_is_forwarded_and_catalogued():
    from settings_catalog import LOCAL_CATALOG

    from ambient.settings import CORRECTION_CAPTURE_ENV

    assert CORRECTION_CAPTURE_ENV in SETTING_KEYS
    assert CORRECTION_CAPTURE_ENV in {spec.key for spec in LOCAL_CATALOG}
    assert AmbientSettings.load(Path("/nonexistent"), {CORRECTION_CAPTURE_ENV: "false"}).correction_capture is False


CORRECTION_PAYLOAD = {"hook_event_name": "UserPromptSubmit", "session_id": "C1", "cwd": "/w/proj",
                      "prompt": CORRECTION}


@needs_bash
def test_prompt_hook_drafts_a_correction_natively(mem, tmp_path):
    proc = run_hook("user-prompt-submit.sh", CORRECTION_PAYLOAD, hook_env(mem, tmp_path))
    assert proc.returncode == 0
    assert [row[6] for row in drafts(mem)] == ["proj"]


@needs_bash
@needs_fake_docker
def test_prompt_hook_drafts_a_correction_in_docker(mem, tmp_path):
    bin_dir = fake_bin(tmp_path, {"docker": FAKE_DOCKER})
    host_mem = tmp_path / "host-mem"
    host_mem.mkdir()
    docker_env = hook_env(host_mem, tmp_path, TAM_DOCKER="1", PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                          FAKE_DOCKER_LOG=str(tmp_path / "docker.log"), FAKE_SRC=str(ROOT / "src"),
                          FAKE_MEM=str(mem), FAKE_PYTHON=sys.executable)
    other = {**CORRECTION_PAYLOAD, "session_id": "C2", "cwd": "/w/other", "prompt": "сколько раз: не коммить в main"}
    assert run_hook("user-prompt-submit.sh", other, docker_env).returncode == 0
    off = {**other, "session_id": "C3", "cwd": "/w/third", "prompt": "how many times: rebase before merge"}
    assert run_hook("user-prompt-submit.sh", off, {**docker_env, "TAM_CORRECTION_CAPTURE": "false"}).returncode == 0
    assert [row[6] for row in drafts(mem)] == ["other"]


@pytest.mark.parametrize('wrapper', ['task-notification', 'local-command-stdout', 'command-name'])
def test_automation_prompt_does_not_recall(mem, wrapper):
    prompt = f'<{wrapper}><task-id>abc</task-id><summary>Deploy pipeline finished</summary></{wrapper}>'
    add_record(mem, 'Deploy pipeline finished with canary checks')
    result = build(mem, prompt, warm=lambda *args: pytest.fail('automation must not call daemon'))
    assert result.text == '' and result.record_ids == ()


def test_mixed_automation_passes_only_user_text(mem):
    calls = []
    def warm(body, timeout):
        calls.append(body['query'])
        return {'ok': True, 'text': '', 'ids': []}
    build(mem, '<task-notification>Deploy pipeline finished</task-notification> Explain database rotation', warm=warm)
    assert calls == ['Explain database rotation']


def test_cold_floor_drops_unrelated_general_records(mem, monkeypatch):
    monkeypatch.setenv('TAM_HOOK_RECALL_MIN_SCORE', '0.2')
    monkeypatch.setenv('TAM_HOOK_RECALL_CROSS_PROJECT_MIN_SCORE', '0.6')
    wanted = add_record(mem, 'ledger invoice payroll reconciliation', project='general')
    for content in ('ledger book excerpts', 'invoice book excerpts', 'payroll book excerpts'):
        add_record(mem, content, project='general')
    result = build(mem, 'ledger invoice payroll')
    assert result.record_ids == (wanted,)
    assert result.dropped_floor == 3
    assert 'general' in result.text and '(project proj)' not in result.text


def test_ambient_log_reports_floor_drops(mem, monkeypatch):
    from ambient import hooks
    events = []
    monkeypatch.setattr(hooks, 'log_event', lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(prompt_context, 'daemon_recall', lambda memory_dir: lambda *args: {
        'ok': True, 'text': '', 'ids': [], 'dropped_floor': 7})
    hooks.prompt_context(hooks.HookCall({'prompt': 'explain ledger invoice payroll'}, 'proj', 'S1'), mem)
    event, fields = events[-1]
    assert event == 'ambient_prompt_context' and fields['dropped_floor'] == 7
