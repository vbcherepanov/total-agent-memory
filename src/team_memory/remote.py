import json
import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from server_capabilities import CapabilityFailure, require_capabilities, required_capabilities

DEFAULT_TIMEOUT_SECONDS = 180
MAX_FRAME_BYTES = 1_000_000


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
