import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def health(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "docker"))
    module = importlib.import_module("worker_health")
    monkeypatch.setenv("TAM_HEALTH_DIR", str(tmp_path))
    return module


def test_missing_dead_and_expired_workers_are_unhealthy(health, monkeypatch):
    assert not health.check_worker("scheduler")
    clock = [100.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    heartbeat = health.Heartbeat("scheduler", ttl=10)
    heartbeat.pulse()
    assert health.check_worker("scheduler")
    clock[0] = 111.0
    assert not health.check_worker("scheduler")
    heartbeat.pulse()
    monkeypatch.setattr(health.os, "kill", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    assert not health.check_worker("scheduler")


def test_job_deadline_and_exception_restore_idle_heartbeat(health, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    heartbeat = health.Heartbeat("reflection", ttl=10)
    with pytest.raises(RuntimeError):
        with heartbeat.job(timeout=600):
            clock[0] = 699.0
            assert health.check_worker("reflection")
            clock[0] = 711.0
            assert not health.check_worker("reflection")
            raise RuntimeError("job failed")
    assert health.check_worker("reflection")
    clock[0] = 722.0
    assert not health.check_worker("reflection")


@pytest.mark.parametrize("payload", ["broken", "[]", '{"pid":false}', '{"pid":1,"deadline":NaN}', '{"pid":0,"deadline":999999999999}'])
def test_malformed_status_is_unhealthy(health, tmp_path, payload):
    (tmp_path / "scheduler.json").write_text(payload, encoding="utf-8")
    assert not health.check_worker("scheduler")


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_invalid_ttl_rejected(health, ttl):
    with pytest.raises(ValueError):
        health.Heartbeat("reflection", ttl=ttl)


def test_sleep_pulses_from_worker_thread(health, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    def sleep(seconds):
        assert seconds <= 3
        assert health.check_worker("scheduler")
        clock[0] += seconds
    monkeypatch.setattr(health.time, "sleep", sleep)
    heartbeat = health.Heartbeat("scheduler", ttl=9)
    heartbeat.sleep(60)
    assert clock[0] == 160
    assert health.check_worker("scheduler")


def test_status_private_and_worker_name_validated(health, tmp_path):
    health.Heartbeat("reflection").pulse()
    status = tmp_path / "reflection.json"
    assert status.stat().st_mode & 0o777 == 0o600
    assert json.loads(status.read_text(encoding="utf-8"))["pid"] == os.getpid()
    with pytest.raises(ValueError):
        health.Heartbeat("../escape")


def test_container_checks_actual_selected_services(health, monkeypatch):
    monkeypatch.setenv("TAM_SUPERVISOR_SERVICES", "mcp,dashboard,reflection,scheduler")
    health.write_services(["scheduler"])
    health.Heartbeat("scheduler").pulse()
    assert health.check_container()
    health.write_services(["reflection", "scheduler"])
    assert not health.check_container()
    health.Heartbeat("reflection").pulse()
    assert health.check_container()


def test_http_service_failure_fails_container(health, monkeypatch):
    health.write_services(["mcp"])
    monkeypatch.setattr(health, "check_http", lambda service: False)
    assert not health.check_container()
    monkeypatch.setattr(health, "check_http", lambda service: True)
    assert health.check_container()


def test_missing_supervisor_manifest_is_unhealthy(health):
    assert not health.check_container()


@pytest.mark.parametrize("worker", ["reflection", "scheduler"])
def test_actual_worker_is_healthy_and_stopped_loop_expires(health, tmp_path, monkeypatch, worker):
    source = tmp_path / "src"
    (source / "tools").mkdir(parents=True)
    (source / "tools" / "run_reflection.py").write_text("import time\ntime.sleep(0.1)\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_TOTAL_MEMORY_SRC", str(source))
    monkeypatch.setenv("CLAUDE_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TAM_WORKER_HEALTH_TTL_SECONDS", "0.3")
    script = Path(__file__).resolve().parents[1] / "docker" / f"{worker}_daemon.py"
    with subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            deadline = time.monotonic() + 5
            while not health.check_worker(worker) and time.monotonic() < deadline:
                assert proc.poll() is None
                time.sleep(0.02)
            assert health.check_worker(worker)
            time.sleep(0.5)
            assert health.check_worker(worker)
            os.kill(proc.pid, signal.SIGSTOP)
            time.sleep(0.5)
            assert not health.check_worker(worker)
        finally:
            os.kill(proc.pid, signal.SIGCONT)
            proc.terminate()
            proc.communicate(timeout=5)
