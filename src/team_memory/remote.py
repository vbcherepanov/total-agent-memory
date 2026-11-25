"""Stdio MCP bridge to a remote TAM server.

Standard library only and self-contained: this one file can be copied to a client machine
and run with Python alone (docs/team-server.md). Do not import other TAM modules here.
"""
from __future__ import annotations

import json
import argparse
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

DEFAULT_TIMEOUT_SECONDS = 180
MAX_FRAME_BYTES = 1_000_000
CAPABILITY_VERSION = "15.0.0"
PREFLIGHT_TIMEOUT_SECONDS = 5
MAX_HEALTH_BYTES = 65536
UNSUPPORTED_EXIT = 2
UNREACHABLE_EXIT = 3
HTTP_NOT_FOUND = 404
HTTP_AUTH_REJECTED = frozenset({401, 403})


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


def write_frame(frame):
    sys.stdout.buffer.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Remote MCP redirects are disabled; configure the final endpoint")


def endpoint(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("TAM_REMOTE_URL must be an HTTP(S) MCP endpoint without credentials or fragment")
    if parts.query and parse_qsl(parts.query, keep_blank_values=True) not in ([('readonly', '1')], [('readonly', '0')]):
        raise ValueError("Only the readonly=0 or readonly=1 query is supported")
    if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Remote connections require HTTPS; HTTP is allowed only on loopback")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/", parts.query, ""))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=os.environ.get("TAM_REMOTE_URL"))
    parser.add_argument("--token-file", type=Path, default=os.environ.get("TAM_REMOTE_TOKEN_FILE"))
    parser.add_argument("--timeout", type=float, default=os.environ.get("TAM_REMOTE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    parser.add_argument("--require-capability", action="append", default=[], dest="capabilities")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tam-remote")
    add_arguments(parser)
    return run(parser.parse_args(argv), parser)


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not args.url or not args.token_file:
        parser.error("Set --url/--token-file or TAM_REMOTE_URL/TAM_REMOTE_TOKEN_FILE")
    url = endpoint(args.url)
    token_file = Path(args.token_file).expanduser()
    timeout = float(args.timeout)
    if timeout <= 0:
        raise ValueError("TAM_REMOTE_TIMEOUT must be positive")
    opener = urllib.request.build_opener(NoRedirect())
    try:
        required = [*args.capabilities]
        if dict(parse_qsl(urlsplit(url).query)).get("readonly") == "1":
            required.append("endpoint.readonly")
        require_capabilities(opener, url, required, timeout)
    except CapabilityFailure as error:
        parser.exit(error.exit_code, str(error) + "\n")
    protocol = "2025-06-18"
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_FRAME_BYTES:
            raise ValueError("MCP frame exceeds size limit")
        frame = json.loads(line)
        request_id = frame.get("id")
        try:
            require_capabilities(opener, url, required_capabilities(frame), timeout)
            token = token_file.read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError("Empty token file")
            request = urllib.request.Request(url, data=line, method="POST", headers={
                "Authorization": "Bearer " + token, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": protocol,
            })
            with opener.open(request, timeout=timeout) as response:
                payload = response.read()
            if payload:
                result = json.loads(payload)
                if frame.get("method") == "initialize":
                    protocol = result.get("result", {}).get("protocolVersion", protocol)
                write_frame(result)
        except CapabilityFailure as error:
            if request_id is not None:
                write_frame({"jsonrpc": "2.0", "id": request_id,
                             "error": {"code": -32000 - error.exit_code, "message": str(error)}})
            parser.exit(error.exit_code, str(error) + "\n")
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            message = "Remote MCP request failed"
            if isinstance(exc, urllib.error.HTTPError):
                message += f" (HTTP {exc.code})"
            sys.stderr.write(json.dumps({"event": "remote_request_failed", "message": message}) + "\n")
            if request_id is not None:
                write_frame({"jsonrpc": "2.0", "id": request_id,
                             "error": {"code": -32000, "message": message}})


if __name__ == "__main__":
    main()
