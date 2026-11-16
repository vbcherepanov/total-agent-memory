from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

WORKERS = frozenset({"reflection", "scheduler"})
HTTP_PORTS = {"mcp": ("MCP_HTTP_PORT", 3737), "dashboard": ("DASHBOARD_PORT", 37737)}
DEFAULT_TTL_SECONDS = 90.0
HTTP_TIMEOUT_SECONDS = 1.0
HEALTH_HOST = "127.0.0.1"


def _directory() -> Path:
    return Path(os.environ.get("TAM_HEALTH_DIR", "/tmp/tam-health"))


def _positive(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Health timeout must be finite and positive")
    return value


def _write(path: Path, payload: dict[str, int | float | list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, allow_nan=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _alive(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True


class Heartbeat:
    def __init__(self, worker: str, *, ttl: float | None = None):
        if worker not in WORKERS:
            raise ValueError(f"Unknown worker: {worker}")
        self.path = _directory() / f"{worker}.json"
        self.ttl = _positive(float(ttl if ttl is not None else os.environ.get(
            "TAM_WORKER_HEALTH_TTL_SECONDS", DEFAULT_TTL_SECONDS,
        )))

    def pulse(self, *, timeout: float = 0) -> None:
        _write(self.path, {"pid": os.getpid(), "deadline": time.monotonic() + timeout + self.ttl})

    @contextmanager
    def job(self, timeout: float) -> Iterator[None]:
        self.pulse(timeout=_positive(timeout))
        try:
            yield
        finally:
            self.pulse()

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + _positive(seconds)
        self.pulse()
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(remaining, self.ttl / 3))
            self.pulse()


def check_worker(worker: str) -> bool:
    if worker not in WORKERS:
        return False
    try:
        payload = json.loads((_directory() / f"{worker}.json").read_text())
        if not isinstance(payload, dict):
            return False
        deadline = payload.get("deadline")
        return (type(deadline) in (int, float) and math.isfinite(deadline)
                and deadline > time.monotonic() and _alive(payload.get("pid")))
    except (OSError, ValueError, OverflowError):
        return False


def write_services(services: list[str]) -> None:
    if not services or set(services) - WORKERS - HTTP_PORTS.keys():
        raise ValueError("Invalid supervised services")
    _write(_directory() / "services.json", {"pid": os.getpid(), "services": services})


def check_http(service: str) -> bool:
    env, default = HTTP_PORTS[service]
    try:
        port = int(os.environ.get(env, default))
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://{HEALTH_HOST}:{port}/healthz", timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status == 200
    except (OSError, ValueError, URLError):
        return False


def check_container() -> bool:
    try:
        payload = json.loads((_directory() / "services.json").read_text())
        if not isinstance(payload, dict) or not _alive(payload.get("pid")):
            return False
        services = payload.get("services")
        if not isinstance(services, list) or not services or not all(isinstance(s, str) for s in services):
            return False
        if set(services) - WORKERS - HTTP_PORTS.keys():
            return False
        return all(check_worker(s) if s in WORKERS else check_http(s) for s in services)
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    healthy = check_worker(sys.argv[1]) if len(sys.argv) == 2 else check_container()
    if not healthy:
        sys.stderr.write('{"event":"container_health_failed"}\n')
    sys.exit(0 if healthy else 1)
