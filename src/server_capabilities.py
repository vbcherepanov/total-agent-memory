from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

CAPABILITY_VERSION = "15.0.0"
PREFLIGHT_TIMEOUT_SECONDS = 5
MAX_HEALTH_BYTES = 65536
UNSUPPORTED_EXIT = 2
UNREACHABLE_EXIT = 3
HTTP_NOT_FOUND = 404
HTTP_AUTH_REJECTED = frozenset({401, 403})
PERSONAL_CAPABILITIES = ("recall.snapshot", "recall.evidence", "records.source_format", "records.embedding_space",
                         "records.branch", "records.incidents", "export.snapshot")
TEAM_CAPABILITIES = ("records.source_format", "records.branch", "writes.request_id", "writes.expected_revision", "endpoint.readonly")


class CapabilityFailure(RuntimeError):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def required_capabilities(frame: Mapping[str, object]) -> tuple[str, ...]:
    if frame.get("method") != "tools/call":
        return ()
    params = frame.get("params")
    if not isinstance(params, Mapping):
        return ()
    arguments = params.get("arguments")
    if not isinstance(arguments, Mapping):
        return ()
    name = params.get("name")
    required = []
    if name == "memory_export" and arguments.get("format") == "jsonl":
        required.append("export.snapshot")
    if name in ("memory_save", "memory_save_fast", "memory_update", "memory_recall") and (
            arguments.get("type") == "incident" or any(key in arguments for key in (
                "incident", "detection", "affected", "resolution", "incident_status"))):
        required.append("records.incidents")
    if name == "memory_recall":
        if arguments.get("as_of"):
            required.append("recall.snapshot")
        if arguments.get("mode") == "evidence":
            required.append("recall.evidence")
        if arguments.get("embedding_space"):
            required.append("records.embedding_space")
    if name == "memory_temporal_query" and arguments.get("op") == "snapshot":
        required.append("recall.snapshot")
    if name in ("memory_save", "memory_save_fast"):
        if "source_format" in arguments:
            required.append("records.source_format")
        if "branch" in arguments:
            required.append("records.branch")
    if name in ("memory_save", "memory_update", "memory_delete"):
        if "request_id" in arguments:
            required.append("writes.request_id")
        if "expected_revision" in arguments:
            required.append("writes.expected_revision")
    return tuple(required)


def require_capabilities(opener: urllib.request.OpenerDirector, endpoint: str,
                         required: Sequence[str], timeout: float) -> None:
    if not required:
        return
    parts = urlsplit(endpoint)
    prefix = parts.path.rstrip("/").rsplit("/", 1)[0]
    url = urlunsplit((parts.scheme, parts.netloc, prefix + "/healthz", "", ""))
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                         timeout=min(timeout, PREFLIGHT_TIMEOUT_SECONDS)) as response:
            raw = response.read(MAX_HEALTH_BYTES + 1)
        if len(raw) > MAX_HEALTH_BYTES:
            raise ValueError("Health response exceeds the size limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Health response must be an object")
        capabilities = payload.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
            raise ValueError("Invalid capabilities list")
    except urllib.error.HTTPError as error:
        if error.code in HTTP_AUTH_REJECTED:
            raise CapabilityFailure(f"The server refused the capability check with HTTP {error.code}. "
                                    "Check the credentials and access rules for the server endpoint.",
                                    UNREACHABLE_EXIT) from error
        if error.code != HTTP_NOT_FOUND:
            raise CapabilityFailure("Could not ask the server about capabilities. Check the connection and retry.",
                                    UNREACHABLE_EXIT) from error
        # A server too old to have /healthz cannot advertise anything.
        capabilities = []
    except (OSError, ValueError) as error:
        raise CapabilityFailure("Could not ask the server about capabilities. Check the connection and retry.",
                                UNREACHABLE_EXIT) from error
    missing = sorted(set(required) - set(capabilities))
    if missing:
        raise CapabilityFailure(f"The server does not support {', '.join(missing)}. Upgrade the server to version {CAPABILITY_VERSION} or newer.",
                                UNSUPPORTED_EXIT)
