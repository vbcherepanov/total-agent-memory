"""Regression tests for the 14.10.0 review of src/server.py and the top-level modules."""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config  # noqa: E402
from secret_redaction import REDACTED, redact_secrets, redact_value  # noqa: E402

SRC = Path(__file__).parent.parent / "src"


@pytest.fixture
def live(monkeypatch, tmp_path):
    for sub in ("raw", "blobs", "chroma", "backups"):
        (tmp_path / sub).mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-review")
    monkeypatch.setattr(server, "BRANCH", "")
    monkeypatch.setattr(server, "_v5_modules", {})
    store.db.execute("INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
                     (server.SID, "2026-09-28T00:00:00Z", "p", "open"))
    store.db.commit()
    yield server, store, tmp_path
    store.db.close()


def call(server, tool, **args):
    content, is_error = asyncio.run(server._call_tool_impl(tool, args))
    text = content[0].text
    return (json.loads(text) if text.startswith("{") else text), is_error


def raw_text(root: Path) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in (root / "raw").glob("*.jsonl"))


def stored(store, record_id: int) -> str:
    return store.db.execute("SELECT content FROM knowledge WHERE id=?", (record_id,)).fetchone()[0]


# ── H1 autofilter keeps the prose ────────────────────────────────────────────

TRACE = ('Traceback (most recent call last):\n  File "worker.py", line 10, in run\n    marker_zq81_call(q)\n'
         "sqlite3.ProgrammingError: Cannot operate on a closed database.")
LESSON = ("Root cause: the worker reused a closed sqlite connection after fork; "
          "fix = open the connection inside the child.")


def test_traceback_with_explanation_keeps_the_explanation(live):
    server, store, _ = live
    saved, _ = call(server, "memory_save", type="lesson", project="p", content=f"{LESSON}\n{TRACE}")
    text = stored(store, saved["id"])
    assert LESSON in text
    assert "sqlite3.ProgrammingError: Cannot operate on a closed database." in text
    assert "marker_zq81_call" not in text
    assert saved["filter_applied"] == "stack_trace"
    assert saved["filter_stats"]["output_chars"] < saved["filter_stats"]["input_chars"]


def test_pytest_output_with_explanation_keeps_the_explanation(live):
    server, store, _ = live
    prose = "unique-marker-qq77 lesson: always seed the random generator before parametrized tests"
    pytest_out = ("============== test session starts ==============\ncollected 10 items\n"
                  "test_a.py::test_one PASSED\ntest_b.py::test_x FAILED\n"
                  "test_b.py:42: AssertionError: expected 1 got 2\n====== 1 failed, 9 passed in 0.05s ======")
    saved, _ = call(server, "memory_save", type="lesson", project="p", content=f"{prose}\n{pytest_out}")
    assert prose in stored(store, saved["id"])


def test_pure_traceback_is_still_trimmed(live):
    server, store, _ = live
    noisy = TRACE + "\n" + "\n".join(f'  File "m{i}.py", line {i}, in f{i}\n    step_{i}()' for i in range(5))
    saved, _ = call(server, "memory_save", type="fact", project="p", content=noisy)
    assert "step_3()" not in stored(store, saved["id"])
    assert saved["filter_applied"] == "stack_trace"


def test_stack_trace_filter_keeps_dotted_exception_lines():
    from content_filter import load_filter_config, run_pipeline
    cfg = load_filter_config(SRC.parent / "filters" / "stack_trace.toml")
    out = run_pipeline("sqlite3.ProgrammingError: closed\nunrelated line", cfg["stages"])
    assert "sqlite3.ProgrammingError: closed" in out


# ── H2 secret redaction: prose survives, real credentials do not ─────────────

@pytest.mark.parametrize("text", [
    "Authorization middleware checks JWT scopes per route",
    "bearer token refresh flow",
    "authorization middleware design",
    "the bearer refresh_token_validation_flow handler",
    "token: validation",
])
def test_credential_words_in_prose_survive(text):
    assert redact_secrets(text) == (text, False)


@pytest.mark.parametrize("text, secret", [
    ("Authorization: Bearer abc123def456ghi789jkl", "abc123def456ghi789jkl"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("curl -H 'bearer Abc123Def456Ghi789Jkl0'", "Abc123Def456Ghi789Jkl0"),
    ("TOKEN: abcdefgh1234", "abcdefgh1234"),
    ("api_key = 'ab12CD34ef56'", "ab12CD34ef56"),
    ("API_KEY=abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),
])
def test_credential_shaped_values_are_redacted(text, secret):
    cleaned, changed = redact_secrets(text)
    assert changed
    assert secret not in cleaned


def test_email_stays_in_identifier_fields_only():
    value, _ = redact_value({"subject": "alice@acme.io", "query": "bob@acme.io", "content": "mail alice@acme.io"})
    assert value["subject"] == "alice@acme.io"
    assert value["query"] == "bob@acme.io"
    assert value["content"] == f"mail {REDACTED}"
    secret_value, _ = redact_value({"subject": "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"})
    assert secret_value["subject"] == REDACTED


def test_kg_subjects_with_emails_stay_distinct(live):
    server, store, _ = live
    call(server, "kg_add_fact", subject="alice@acme.io", predicate="owns", object="billing-service", project="p")
    call(server, "kg_add_fact", subject="bob@acme.io", predicate="owns", object="auth-service", project="p")
    rows = store.db.execute("SELECT subject, valid_to FROM fact_assertions ORDER BY subject").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("alice@acme.io", None), ("bob@acme.io", None)]


def test_recall_query_and_saved_prose_are_not_rewritten(live):
    server, store, root = live
    saved, _ = call(server, "memory_save", type="fact", project="p",
                    content="Authorization middleware checks JWT scopes per route in the gateway")
    assert stored(store, saved["id"]).startswith("Authorization middleware")
    call(server, "memory_recall", query="bearer token refresh flow", project="p")
    entries = [json.loads(line) for line in raw_text(root).splitlines()]
    queries = [e["args"].get("query") for e in entries if e.get("tool") == "memory_recall"]
    assert queries == ["bearer token refresh flow"]


# ── H3 self_rules_context is parametrised ────────────────────────────────────

def test_rules_context_survives_quotes_and_does_not_leak_rows(live):
    server, store, _ = live
    store.manage_rule(server.SID, "add_manual", content="Never skip tests", category="process",
                      scope="project:o'reilly", project="o'reilly")
    ok, is_error = call(server, "self_rules_context", project="o'reilly")
    assert not is_error and ok["rules_count"] == 1
    store.save_knowledge(server.SID, "Secret knowledge row that must stay private", "fact", "p")
    columns = len(store.db.execute("SELECT * FROM rules LIMIT 0").description)
    payload = "x') UNION SELECT " + ",".join(["content"] * columns) + " FROM knowledge --"
    result, is_error = call(server, "self_rules_context", project=payload)
    assert not is_error
    assert result["rules_count"] == 0


def test_rules_context_categories_with_quotes(live):
    server, store, _ = live
    result, is_error = call(server, "self_rules_context", project="p", categories=["a'b"])
    assert not is_error and result["rules_count"] == 0


# ── H4 / H5 raw log ──────────────────────────────────────────────────────────

def test_private_sections_never_reach_the_raw_log(live):
    server, _, root = live
    call(server, "memory_save", type="fact", project="p",
         content="Deploy note for the staging cluster <private>root pin quokka-4821</private> rolled on friday")
    assert "quokka-4821" not in raw_text(root)


def test_hard_delete_erases_private_submission_from_raw_log(live):
    server, store, root = live
    saved, _ = call(server, "memory_save", type="fact", project="p",
                    content="Vault unseal procedure for cluster zeta <private>shard key K-99812</private> "
                            "takes three operators")
    result, is_error = call(server, "memory_delete", id=saved["id"], hard=True)
    assert not is_error
    assert "K-99812" not in raw_text(root)
    assert result["raw_logs_rewritten"] >= 1


def test_hard_delete_erases_the_submitted_form_of_an_autofiltered_record(live):
    server, store, root = live
    saved, _ = call(server, "memory_save", type="lesson", project="p", content=f"{LESSON}\n{TRACE}")
    assert "marker_zq81_call" not in stored(store, saved["id"])
    assert "marker_zq81_call" in raw_text(root)
    result, _ = call(server, "memory_delete", id=saved["id"], hard=True)
    assert "marker_zq81_call" not in raw_text(root)
    assert LESSON not in raw_text(root)
    assert result["raw_logs_rewritten"] >= 1


def test_scrub_ignores_unrelated_calls(tmp_path):
    import erasure
    log = tmp_path / "s.jsonl"
    keep = json.dumps({"type": "tool_call", "tool": "memory_save", "call_id": "b", "args": {"content": "keep me"}})
    drop = json.dumps({"type": "tool_call", "tool": "memory_save", "call_id": "a", "args": {"content": "drop me"}})
    result = json.dumps({"type": "tool_result", "call_id": "a", "knowledge_ids": [7]})
    log.write_text(f"{drop}\n{result}\n{keep}\n", encoding="utf-8")
    assert erasure.scrub_raw_logs(tmp_path, [], [7]) == 1
    text = log.read_text(encoding="utf-8")
    assert "drop me" not in text and "keep me" in text


# ── M1 excluded tags are whole tags ──────────────────────────────────────────

def test_has_excluded_tag_compares_whole_tags():
    excluded = ("recovery", "auto-extract")
    assert not config.has_excluded_tag('["disaster-recovery"]', excluded)
    assert not config.has_excluded_tag(["password-recovery", "auto-extract-v2"], excluded)
    assert config.has_excluded_tag('["x", "Recovery"]', excluded)
    assert config.has_excluded_tag(["auto-extract"], excluded)
    assert not config.has_excluded_tag("", excluded)


def test_recall_finds_record_whose_tag_contains_an_excluded_word(live):
    server, _, _ = live
    call(server, "memory_save", type="solution", project="p", tags=["disaster-recovery"],
         content="Disaster plan: restore postgres from WAL archive on node beta-7")
    found, _ = call(server, "memory_recall", query="restore postgres WAL archive beta-7", project="p")
    assert found["total"] >= 1
    call(server, "memory_save", type="solution", project="p", tags=["recovery"],
         content="Snapshot of mongo shard gamma-3 taken before the migration window")
    hidden, _ = call(server, "memory_recall", query="mongo shard gamma-3 snapshot", project="p")
    assert "mongo shard" not in json.dumps(hidden["results"])


# ── M2 / M3 eval tools ───────────────────────────────────────────────────────

def test_eval_long_context_leaves_no_records_and_keeps_counters(live):
    server, store, _ = live
    from memory_core.telemetry import counters
    counters.bump("review_probe_counter", 5)
    before = store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    result, is_error = call(server, "memory_eval_long_context", n_records=10, mode="fast")
    assert not is_error and result["n_records"] == 10
    assert store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == before
    assert counters.snapshot()["review_probe_counter"] == 5


def test_eval_long_context_rejects_unbounded_n_records(live):
    server, _, _ = live
    result, _ = call(server, "memory_eval_long_context", n_records=server.EVAL_MAX_RECORDS + 1)
    assert result["status"] == "error"


def test_eval_recall_seeds_with_the_live_session_id_and_cleans_up(live, monkeypatch):
    server, store, _ = live
    sids = []
    original = store.save_knowledge

    def spy(*args, **kwargs):
        sids.append(kwargs.get("sid"))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "save_knowledge", spy)
    call(server, "memory_eval_recall", mode="fast")
    assert sids == ["sess-review", "sess-review"]
    assert store.db.execute("SELECT COUNT(*) FROM knowledge WHERE project='memory_eval_recall'").fetchone()[0] == 0


def test_eval_temporal_removes_its_facts(live):
    server, store, _ = live
    call(server, "memory_eval_temporal", mode="fast")
    assert store.db.execute(
        "SELECT COUNT(*) FROM fact_assertions WHERE project='memory_eval_temporal'").fetchone()[0] == 0


def test_server_module_registers_itself_when_run_as_script():
    source = (SRC / "server.py").read_text(encoding="utf-8")
    assert 'sys.modules.setdefault("server", sys.modules[__name__])' in source
    assert '__import__("server")' not in source


# ── M4 consolidation without the all-pairs scan ──────────────────────────────

def naive_groups(rows, threshold):
    import server
    groups, used = [], set()
    for i, a in enumerate(rows):
        if a["id"] in used:
            continue
        group = [a]
        for b in rows[i + 1:]:
            if b["id"] in used or b["type"] != a["type"] or b["project"] != a["project"]:
                continue
            if server.Store._jaccard(a["content"], b["content"]) > threshold:
                group.append(b)
                used.add(b["id"])
        if len(group) > 1:
            used.add(a["id"])
            groups.append(group)
    return groups


@pytest.mark.parametrize("threshold", [0.0, 0.3, 0.6, 0.75, 0.95])
def test_similar_groups_match_the_all_pairs_result(live, threshold):
    server, store, _ = live
    rng = random.Random(7)
    vocabulary = [f"w{i}" for i in range(40)]
    for n in range(120):
        words = rng.sample(vocabulary, rng.randint(3, 12))
        store.db.execute(
            "INSERT INTO knowledge (session_id,type,content,project,created_at,last_confirmed) VALUES (?,?,?,?,?,?)",
            (server.SID, rng.choice(["fact", "lesson"]), " ".join(words), rng.choice(["a", "b"]),
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    store.db.commit()
    rows = store.q("SELECT id, content, type, project FROM knowledge WHERE status='active' ORDER BY id")
    expected = sorted([[r["id"] for r in g] for g in naive_groups(rows, threshold)])
    actual = sorted([[r["id"] for r in g] for g in store.find_similar_groups(None, threshold)])
    assert actual == expected


# ── M5 export file name ──────────────────────────────────────────────────────

@pytest.mark.parametrize("project", ["org/repo", "../../escape", "plain"])
def test_export_slugifies_the_project_name(live, project):
    server, _, root = live
    result, is_error = call(server, "memory_export", project=project)
    assert not is_error, result
    exported = Path(result["file"]).resolve()
    assert exported.parent == (root / "backups").resolve()
    assert exported.is_file()


# ── M6 timeline honours project ──────────────────────────────────────────────

def test_timeline_filters_by_project_in_every_mode(live):
    server, store, _ = live
    for sid, started, project in (("s-a", "2026-01-01T00:00:00Z", "alpha"), ("s-b", "2026-01-02T00:00:00Z", "beta"),
                                  ("s-c", "2026-01-03T00:00:00Z", "alpha")):
        store.db.execute("INSERT INTO sessions (id, started_at, project, status) VALUES (?,?,?,?)",
                         (sid, started, project, "closed"))
        store.save_knowledge(sid, f"deploy checklist note for {project} session {sid}", "fact", project)
    store.db.commit()

    by_query = server.recall.timeline(query="deploy", project="beta")
    assert {s["project"] for s in by_query["sessions"]} == {"beta"}
    by_ago = server.recall.timeline(sessions_ago=0, project="beta", limit=1)
    assert [s["id"] for s in by_ago["sessions"]] == ["s-b"]
    default = server.recall.timeline(project="alpha")
    assert {s["project"] for s in default["sessions"]} == {"alpha"}


# ── M7 memory_update is one transaction ──────────────────────────────────────

def test_update_supersedes_in_the_same_transaction(live):
    server, store, _ = live
    saved, _ = call(server, "memory_save", type="fact", project="p",
                    content="Retention window for audit logs is thirty days")
    updated, _ = call(server, "memory_update", id=saved["id"],
                      new_content="Retention window for audit logs is ninety days", reason="policy")
    old = store.db.execute("SELECT status, superseded_by FROM knowledge WHERE id=?", (saved["id"],)).fetchone()
    assert (old[0], old[1]) == ("superseded", updated["new_id"])


def test_superseding_a_record_changed_by_another_writer_inserts_nothing(live):
    server, store, _ = live
    rid, *_ = store.save_knowledge(server.SID, "Cache TTL is sixty seconds for the catalog", "fact", "p")
    store.db.execute("UPDATE knowledge SET status='superseded' WHERE id=?", (rid,))
    store.db.commit()
    count = store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    with pytest.raises(ValueError, match="no longer active"):
        store.save_knowledge(server.SID, "Cache TTL is ninety seconds for the catalog", "fact", "p",
                             skip_dedup=True, skip_quality=True, supersedes=rid)
    assert store.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == count


# ── L1 quality gate log row ──────────────────────────────────────────────────

def test_quality_gate_backfill_targets_its_own_log_row(live, monkeypatch):
    server, store, _ = live
    import quality_gate
    monkeypatch.setenv("MEMORY_QUALITY_GATE_ENABLED", "true")
    monkeypatch.setenv("MEMORY_QUALITY_LOG_ALL", "1")
    score = quality_gate.QualityScore("pass", 0.9, 0.9, 0.9, 0.9, "ok", 0.5)
    monkeypatch.setattr(quality_gate, "score_quality", lambda *a, **k: score)
    real_log = quality_gate.log_decision
    inserted = []

    def racing_log(db, sc, **kwargs):
        row_id = real_log(db, sc, **kwargs)
        inserted.append(row_id)
        real_log(db, sc, **kwargs)  # a concurrent save logs a newer NULL pass row
        return row_id

    monkeypatch.setattr(quality_gate, "log_decision", racing_log)
    rid, *_ = store.save_knowledge(server.SID, "Pool size for the billing database is forty connections", "fact", "p")
    owner = store.db.execute("SELECT knowledge_id FROM quality_gate_log WHERE id=?", (inserted[0],)).fetchone()[0]
    assert owner == rid


# ── L2 filter name ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["../../../etc/x", "a/b"])
def test_path_like_filter_name_is_a_caller_error(live, name):
    server, _, _ = live
    _, is_error = call(server, "memory_save", type="fact", project="p", filter=name,
                       content="A note that is saved with a filter name that is not a plain name")
    assert is_error


def test_unknown_plain_filter_name_is_reported_and_stores_raw(live):
    server, store, _ = live
    text = "A note that is saved with a filter name that does not exist"
    saved, is_error = call(server, "memory_save", type="fact", project="p", filter="no_such_filter", content=text)
    assert not is_error
    assert "no_such_filter" in saved["filter_warning"]
    assert stored(store, saved["id"]) == text


# ── L3 retention ─────────────────────────────────────────────────────────────

def test_retention_cleans_only_rows_moved_in_this_run(live, monkeypatch):
    server, store, _ = live
    old = "2020-01-01T00:00:00Z"
    ids = {}
    for label, status in (("history", "archived"), ("fresh", "active")):
        cur = store.db.execute(
            "INSERT INTO knowledge (session_id,type,content,project,status,created_at,last_confirmed,recall_count,"
            "confidence) VALUES (?,?,?,?,?,?,?,0,0.5)",
            (server.SID, "fact", f"{label} retention row", "p", status, old, old))
        ids[label] = cur.lastrowid
    store.db.commit()
    monkeypatch.setattr(server, "PURGE_AFTER_DAYS", 100_000)
    cleaned = []
    monkeypatch.setattr(store, "_delete_embedding", cleaned.append)
    result = store.apply_retention()
    assert result["archived"] == 1
    assert cleaned == [ids["fresh"]]


# ── L4 recall limit ──────────────────────────────────────────────────────────

def test_recall_limit_is_clamped(live, monkeypatch):
    server, _, _ = live
    seen = []
    real_search = server.recall.search

    def spy(query, project=None, ktype="all", limit=10, *args, **kwargs):
        seen.append(limit)
        return real_search(query, project, ktype, limit, *args, **kwargs)

    monkeypatch.setattr(server.recall, "search", spy)
    call(server, "memory_recall", query="anything at all", limit=100_000)
    assert seen and max(seen) <= server.RECALL_MAX_LIMIT


# ── L5 filesystem paths ──────────────────────────────────────────────────────

def test_ingest_codebase_refuses_paths_outside_allowed_roots(live):
    server, _, _ = live
    for path in ("~", "/etc", "/"):
        _, is_error = call(server, "ingest_codebase", path=path)
        assert is_error, path


def test_ingest_codebase_accepts_a_listed_root(live, monkeypatch, tmp_path_factory):
    server, _, _ = live
    extra = tmp_path_factory.mktemp("extra")
    (extra / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv(server.ALLOWED_ROOTS_ENV, str(extra))
    result, is_error = call(server, "ingest_codebase", path=str(extra))
    assert not is_error and "total_chunks" in result


def test_ingest_php_vue_reports_symbols_without_storing_knowledge(live, monkeypatch, tmp_path_factory):
    server, store, _ = live
    root = tmp_path_factory.mktemp("php-vue")
    (root / "Invoice.php").write_text("<?php function total() { return 42; }", encoding="utf-8")
    (root / "Invoice.vue").write_text('<script setup lang="ts">const submit = () => 42;</script>', encoding="utf-8")
    monkeypatch.setenv(server.ALLOWED_ROOTS_ENV, str(root))
    before = store.db.execute("SELECT count(*) FROM knowledge").fetchone()[0]
    result, is_error = call(server, "ingest_codebase", path=str(root), sample_limit=10)
    assert not is_error
    assert result["persisted"] and result["total_files"] == 2
    assert {(item["name"], item["language"]) for item in result["sample"]
            if item["kind"] not in {"metadata", "text"}} == {
        ("total", "php"), ("Invoice", "vue"), ("submit", "typescript")}
    assert store.db.execute("SELECT count(*) FROM knowledge").fetchone()[0] == before
    assert store.db.execute("SELECT count(*) FROM resource_files").fetchone()[0] == 2
    found, error = call(server, "ingest_codebase", path=str(root), action="search", query="submit")
    assert not error and found["count"] > 0


def test_benchmark_scenarios_path_is_restricted(live):
    server, _, _ = live
    _, is_error = call(server, "benchmark", scenarios_path="/etc/passwd")
    assert is_error


# ── L6 extract-session arguments ─────────────────────────────────────────────

def test_extract_session_validates_chunk_and_session_id(live):
    server, _, root = live
    queue = root / "extract-queue"
    queue.mkdir(exist_ok=True)
    (queue / "pending-abc.json").write_text(json.dumps({"session_id": "abc", "x": "y" * 250_000}), encoding="utf-8")
    ok, _ = call(server, "memory_extract_session", action="get", session_id="abc", chunk=0)
    assert ok["_total_chunks"] == 3
    for chunk in (-1, 3):
        bad, _ = call(server, "memory_extract_session", action="get", session_id="abc", chunk=chunk)
        assert "error" in bad
    traversal, _ = call(server, "memory_extract_session", action="get", session_id="../x")
    assert "error" in traversal


# ── L7 entity resolve threshold ──────────────────────────────────────────────

def test_entity_resolve_keeps_a_zero_threshold(monkeypatch):
    import memory_core.entity_resolver as resolver
    import v11_handlers
    seen = {}

    class Result:
        canonical_id, canonical_name, matched_via, confidence, is_new = 1, "x", "exact", 1.0, False

    def fake_resolve(conn, mention, project, type_, **kwargs):
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    v11_handlers.handle_entity_resolve({"mention": "x", "threshold": 0}, conn=None, embed_fn=lambda t: None)
    assert seen["threshold"] == 0.0
    v11_handlers.handle_entity_resolve({"mention": "x"}, conn=None, embed_fn=lambda t: None)
    assert seen["threshold"] == v11_handlers.DEFAULT_ENTITY_RESOLVE_THRESHOLD
    out = v11_handlers.handle_entity_resolve({"mention": "x", "threshold": 3}, conn=None, embed_fn=lambda t: None)
    assert "error" in out


# ── L8 mutations of missing ids ──────────────────────────────────────────────

@pytest.mark.parametrize("tool, args", [
    ("self_rules", {"action": "retire", "id": 999999}),
    ("self_rules", {"action": "suspend", "id": 999999}),
    ("self_rules", {"action": "activate", "id": 999999}),
    ("self_rules", {"action": "fire", "id": 999999}),
    ("self_insight", {"action": "upvote", "id": 999999}),
    ("self_insight", {"action": "downvote", "id": 999999}),
    ("self_insight", {"action": "edit", "id": 999999, "content": "new text"}),
])
def test_mutating_a_missing_id_reports_not_found(live, tool, args):
    server, _, _ = live
    result, _ = call(server, tool, **args)
    assert "error" in result and "not found" in result["error"]


# ── L9 half-life settings ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["0", "-30"])
def test_non_positive_half_life_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("MEMORY_DECAY_PARENT_DAYS", raw)
    assert config.get_parent_half_life_days() == 90


# ── L10 dead module ──────────────────────────────────────────────────────────

def test_dead_models_module_is_gone():
    assert not (SRC / "models.py").exists()


# ── workers/integration follow-ups ───────────────────────────────────────────

def test_sanitize_content_drops_an_unclosed_private_block():
    import server
    cleaned, flagged = server.Store._sanitize_content("public part <private>hidden tail without a closing tag")
    assert flagged and "hidden" not in cleaned and cleaned.startswith("public part")


def test_ingest_codebase_sample_is_redacted(live, monkeypatch, tmp_path_factory):
    server, _, _ = live
    extra = tmp_path_factory.mktemp("ingest")
    secret = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    (extra / "mod.py").write_text(f'def f():\n    key = "{secret}"\n    return key\n', encoding="utf-8")
    monkeypatch.setenv(server.ALLOWED_ROOTS_ENV, str(extra))
    result, is_error = call(server, "ingest_codebase", path=str(extra))
    assert not is_error
    assert secret not in json.dumps(result)


# ── cognitive additional_solutions honour the recall noise filter ────────────

def test_cognitive_additional_solutions_skip_excluded_tags(live, monkeypatch):
    server, store, _ = live
    solutions = [
        {"id": 9001, "content": "noise", "tags": '["recovery"]'},
        {"id": 9002, "content": "kept", "tags": '["disaster-recovery"]'},
    ]
    for solution in solutions:
        store.db.execute(
            "INSERT INTO knowledge(id,session_id,type,content,project,tags,status,created_at) VALUES (?,?,'solution',?,'p',?,'active',?)",
            (solution["id"], server.SID, solution["content"], solution["tags"], server.utc_now()))
    store.db.commit()
    solutions.append({"id": 99999, "content": "missing record", "tags": '[]'})

    class FakeEngine:
        def on_query(self, query, project, *, scope=None):
            return {"available_solutions": solutions}

    monkeypatch.setattr(server, "_get_v5", lambda name, db: FakeEngine())
    call(server, "memory_save", type="solution", project="p", content="Restart the nginx workers on host alpha-4")
    out, _ = call(server, "memory_recall", query="nginx workers alpha-4", project="p")
    ids = [s["id"] for s in out.get("cognitive", {}).get("additional_solutions", [])]
    assert ids == [9002]


def test_resource_index_mcp_actions_and_scope(live, monkeypatch, tmp_path_factory):
    server, store, _ = live
    root = tmp_path_factory.mktemp('universal-index')
    (root / 'notes.new_format').write_text('UniversalResourceNeedle', encoding='utf-8')
    monkeypatch.setenv(server.ALLOWED_ROOTS_ENV, str(root))
    preview, error = call(server, 'ingest_codebase', path=str(root), action='preview', project='alpha')
    assert not error and not preview['persisted']
    assert store.db.execute('SELECT COUNT(*) FROM resource_roots').fetchone()[0] == 0
    result, error = call(server, 'ingest_codebase', path=str(root), project='alpha')
    assert not error and result['persisted']
    result, error = call(server, 'ingest_codebase', path=str(root), action='search',
                         query='UniversalResourceNeedle', project='alpha')
    assert not error and result['count'] > 0
    result, error = call(server, 'ingest_codebase', path=str(root), action='search',
                         query='UniversalResourceNeedle', project='beta')
    assert not error and result['count'] == 0
    result, error = call(server, 'ingest_codebase', path=str(root), action='status', project='alpha')
    assert not error and result['total_files'] == 1
    _, error = call(server, 'ingest_codebase', path=str(root), action='search', limit=-1)
    assert error
