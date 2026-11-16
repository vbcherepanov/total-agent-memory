"""AML adapter — pure units: contract, fragments, config, auth, registry, pool, metrics."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from aml_adapter.app import authorised, presented_keys
from aml_adapter.config import AdapterConfig, ConfigError
from aml_adapter.contracts import AddRequest, Message, content_text
from aml_adapter.errors import Busy, ContractError
from aml_adapter.fragments import build_fragments, iso_from_ms, split_text
from aml_adapter.metrics import Metrics
from aml_adapter.pool import WorkerPool, _Worker
from aml_adapter.registry import Registry, user_namespace


def _add(**overrides) -> AddRequest:
    body = {"request_id": "r1", "user_id": "u1", "session_id": "s1",
            "messages": [{"role": "user", "timestamp": 1704067200000, "content": "hello"}]}
    body.update(overrides)
    return AddRequest.model_validate(body)


# ── contract ────────────────────────────────────────────────────────


def test_fingerprint_ignores_ids_and_detects_payload_change():
    base = _add()
    assert base.fingerprint() == _add(request_id="other", user_id="other").fingerprint()
    assert base.fingerprint() != _add(session_id="s2").fingerprint()
    changed = [{"role": "user", "timestamp": 1704067200000, "content": "hello!"}]
    assert base.fingerprint() != _add(messages=changed).fingerprint()


def test_content_parts_are_joined_and_images_refused():
    parts = Message.model_validate({"role": "user", "content": [{"type": "text", "text": "a"},
                                                                {"type": "text", "text": "b"}]})
    assert content_text(parts.content) == "a\nb"
    image = Message.model_validate({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]})
    with pytest.raises(ContractError, match="image_url"):
        content_text(image.content)
    with pytest.raises(ContractError, match="non-empty"):
        content_text("   ")


@pytest.mark.parametrize("field,value", [("request_id", ""), ("user_id", 7), ("messages", [])])
def test_add_request_rejects_contract_violations(field, value):
    with pytest.raises(ValueError):
        _add(**{field: value})


# ── fragments ───────────────────────────────────────────────────────


def test_iso_from_ms():
    assert iso_from_ms(1704067200000) == "2024-01-01T00:00:00Z"
    assert iso_from_ms(1704067200123) == "2024-01-01T00:00:00.123Z"


def test_split_text_respects_limit_and_keeps_all_text():
    text = "line one\n" * 50 + "x" * 450
    pieces = split_text(text, 200)
    assert all(len(p) <= 200 for p in pieces)
    assert "".join(pieces) == text


def test_annotated_fragments_carry_time_and_role():
    messages = [Message.model_validate({"role": "assistant", "timestamp": 1704067200000, "content": "hi"}),
                Message.model_validate({"role": "user", "content": "no time"})]
    annotated = build_fragments(messages, max_chars=1000, content_format="annotated")
    assert [f.text for f in annotated] == ["[2024-01-01T00:00:00Z] assistant: hi", "user: no time"]
    raw = build_fragments(messages, max_chars=1000, content_format="raw")
    assert [f.text for f in raw] == ["hi", "no time"]
    assert raw[0].timestamp_ms == 1704067200000 and raw[1].timestamp_ms is None


def test_long_message_becomes_several_fragments():
    message = Message.model_validate({"role": "user", "content": "word " * 1000})
    fragments = build_fragments([message], max_chars=500, content_format="annotated")
    assert len(fragments) > 1
    assert all(len(f.text) <= 500 and f.text.startswith("user: ") for f in fragments)
    assert [f.part_index for f in fragments] == list(range(len(fragments)))


# ── config ──────────────────────────────────────────────────────────


def _env(tmp_path: Path, **extra) -> dict[str, str]:
    return {"AML_DATA_DIR": str(tmp_path / "aml"), "AML_API_KEYS": "k1, k2", **extra}


def test_config_defaults(tmp_path):
    config = AdapterConfig.from_env(_env(tmp_path))
    assert config.api_keys == ("k1", "k2")
    assert config.retention_days <= 30 and 1 <= config.retry_after_seconds <= 60
    assert "api_keys" not in config.public_settings()
    assert len(config.fingerprint()) == 64


@pytest.mark.parametrize("extra,message", [
    ({"AML_DATA_DIR": ""}, "AML_DATA_DIR is required"),
    ({"AML_API_KEYS": ""}, "AML_API_KEYS is required"),
    ({"AML_RETENTION_DAYS": "31"}, "AML_RETENTION_DAYS"),
    ({"AML_RETRY_AFTER_SECONDS": "61"}, "AML_RETRY_AFTER_SECONDS"),
    ({"AML_CONTENT_FORMAT": "fancy"}, "AML_CONTENT_FORMAT"),
    ({"AML_WORKERS": "0"}, "AML_WORKERS"),
    ({"AML_AUTH_DISABLED": "maybe"}, "AML_AUTH_DISABLED"),
])
def test_config_rejects_invalid_values(tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        AdapterConfig.from_env(_env(tmp_path, **extra))


def test_config_refuses_the_personal_memory_dir(tmp_path):
    with pytest.raises(ConfigError, match="must not be inside"):
        AdapterConfig.from_env(_env(tmp_path, AML_DATA_DIR=str(Path("~/.tam/aml").expanduser())))


def test_auth_can_be_disabled_for_public_smoke(tmp_path):
    config = AdapterConfig.from_env(_env(tmp_path, AML_API_KEYS="", AML_AUTH_DISABLED="true"))
    assert config.auth_disabled and config.api_keys == ()


# ── auth ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [
    {b"authorization": b"Bearer k2"},
    {b"authorization": b"bearer k2"},
    {b"authorization": b"Token k2"},
    {b"x-api-key": b"k2"},
    {b"token": b"k2"},
])
def test_all_header_styles_are_accepted(headers):
    assert presented_keys(headers) == ["k2"]
    assert authorised(headers, ("k1", "k2"))


@pytest.mark.parametrize("headers", [{}, {b"authorization": b"Bearer nope"},
                                     {b"authorization": b"Basic k1"}, {b"x-api-key": b""}])
def test_bad_credentials_are_refused(headers):
    assert not authorised(headers, ("k1",))


# ── registry / purge journal ────────────────────────────────────────


def test_registry_purge_journal(tmp_path):
    registry = Registry(tmp_path)
    ns = registry.record_write("eval:run:conv-0")
    assert ns == user_namespace("eval:run:conv-0") and registry.lookup("eval:run:conv-0") == ns
    user_dir = registry.users_dir / ns
    user_dir.mkdir()
    (user_dir / "blob").write_bytes(b"x" * 10)
    assert registry.expired(14, now=time.time()) == []
    [user] = registry.expired(14, now=time.time() + 15 * 86400)
    entry = registry.delete(user, "retention")
    assert not user_dir.exists() and registry.lookup("eval:run:conv-0") is None
    assert entry["bytes"] == 10 and entry["reason"] == "retention"
    journal = [json.loads(line) for line in registry.journal_path.read_text(encoding="utf-8").splitlines()]
    assert journal == [entry]
    assert set(entry) >= {"user_ns", "user_id", "deleted_at", "last_write_at", "fragments", "requests"}
    registry.close()


def test_registry_never_purges_on_a_stale_decision(tmp_path):
    registry = Registry(tmp_path)
    registry.record_write("u")
    [user] = registry.expired(1, now=time.time() + 2 * 86400)
    time.sleep(0.01)
    registry.record_write("u")  # a new Add arrived after the decision
    assert registry.delete(user, "retention") is None
    assert registry.lookup("u") is not None and not registry.journal_path.exists()
    registry.close()


# ── pool back-pressure ──────────────────────────────────────────────


def _pool(tmp_path, maximum=1, wait=0.2) -> WorkerPool:
    return WorkerPool(tmp_path, maximum=maximum, timeout=5, wait_seconds=wait,
                      settings_for=lambda ns: None, tam_env={})


def test_pool_is_busy_when_every_worker_serves_another_user(tmp_path):
    pool = _pool(tmp_path)
    pool.workers["other"] = _Worker(busy=True)
    started = time.monotonic()
    with pytest.raises(Busy):
        pool._acquire("mine")
    assert time.monotonic() - started >= 0.2


def test_pool_evicts_an_idle_worker_for_a_new_user(tmp_path):
    pool = _pool(tmp_path)
    pool.workers["idle"] = _Worker(busy=False)
    worker = pool._acquire("mine")
    assert list(pool.workers) == ["mine"] and worker.busy


def test_pool_serialises_requests_of_one_user(tmp_path):
    pool = _pool(tmp_path, maximum=2, wait=2)
    first = pool._acquire("u")
    got = []
    thread = threading.Thread(target=lambda: got.append(pool._acquire("u")))
    thread.start()
    time.sleep(0.1)
    assert got == []
    pool._release("u", first)
    thread.join(2)
    assert got == [first]


def test_exclusive_blocks_new_requests(tmp_path):
    pool = _pool(tmp_path, wait=0.2)
    with pool.exclusive("u"), pytest.raises(Busy):
        pool._acquire("u")
    assert pool._acquire("u").busy


# ── metrics ─────────────────────────────────────────────────────────


def test_metrics_render_counter_and_histogram(tmp_path):
    metrics = Metrics()
    metrics.observe("add", "ok", 0.2)
    metrics.observe("add", "busy", 3.0)
    metrics.count_items("fragments_stored", 4)
    text = metrics.render()
    assert 'aml_requests_total{operation="add",status="ok"} 1' in text
    assert 'aml_request_duration_seconds_bucket{operation="add",le="0.25"} 1' in text
    assert 'aml_request_duration_seconds_bucket{operation="add",le="+Inf"} 2' in text
    assert 'aml_request_duration_seconds_count{operation="add"} 2' in text
    assert 'aml_items_total{kind="fragments_stored"} 4' in text
    metrics.write_textfile(tmp_path / "metrics.prom")
    assert (tmp_path / "metrics.prom").read_text(encoding="utf-8") == text


# ── offline purge CLI ───────────────────────────────────────────────


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers, level = tuple(root.handlers), root.level
    yield
    for handler in tuple(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def test_offline_purge_cli(tmp_path, monkeypatch, capsys, restore_root_logging):
    from aml_adapter.cli import main
    from team_memory.lifecycle import ServerLease

    data_dir = tmp_path / "aml"
    monkeypatch.setenv("AML_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AML_API_KEYS", "k")
    registry = Registry(data_dir)
    ns = registry.record_write("eval:run:conv-9")
    (registry.users_dir / ns).mkdir()
    registry.close()

    assert main(["purge", "--all"]) == 2
    with ServerLease(data_dir):
        assert main(["purge", "--all", "--yes"]) == 1
    assert (data_dir / "users" / ns).exists()
    assert main(["purge", "--all", "--yes"]) == 0
    assert not (data_dir / "users" / ns).exists()
    [entry] = [json.loads(line) for line in (data_dir / "deletion-journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entry["reason"] == "manual" and entry["user_id"] == "eval:run:conv-9"
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["deleted_users"] == 1
