"""Browser sign-in for the local dashboard when it requires an access token (bound beyond loopback).

The long-lived access token is exchanged once for an opaque, random session ID kept in an HttpOnly cookie. The
server stores only the SHA-256 of that ID (and of the token it was opened with), so the cookie never holds the
token, a stolen cookie does not reveal it, and sessions end on sign-out, expiry, a restart or a token change.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qs

SESSION_COOKIE = "tam_dashboard_session"
LEGACY_COOKIE = "tam_dashboard_token"
SESSION_BYTES = 32
SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 256
LOGIN_BODY_MAX_BYTES = 4096
TOKEN_FIELD = "token"

LOGIN_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title data-i18n="Sign in">Sign in</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1><span data-i18n="Dashboard access token">Dashboard access token</span></h1>
<p><span data-i18n="Paste the token from">Paste the token from</span> <code>state/http-token</code> <span data-i18n="in the data directory. It is sent once, in a POST body, and exchanged for a session cookie; the token itself is never stored in the browser.">in the data directory. It is sent once, in a POST body, and
exchanged for a session cookie; the token itself is never stored in the browser.</span></p>
<form id="login" method="post" action="/login">
<input type="password" name="token" autocomplete="off" autofocus required style="width:100%;padding:.5rem">
<p><button type="submit"><span data-i18n="Sign in">Sign in</span></button></p>
</form>
<script>
(function () {
  var match = /^#token=(.+)$/.exec(window.location.hash);
  if (!match) { return; }
  var form = document.getElementById("login");
  form.elements.token.value = decodeURIComponent(match[1]);
  window.history.replaceState(null, "", window.location.pathname);
  form.submit();
})();
</script>
</body></html>
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def token_from_body(body: bytes) -> str:
    """The ``token`` field of a form-encoded sign-in body ('' when absent or not decodable)."""
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return ""
    return (fields.get(TOKEN_FIELD) or [""])[0]


def cookie_value(header: str | None, name: str) -> str:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


class DashboardSessions:
    """In-memory sessions: digest of the session ID -> (expiry, digest of the token it was opened with)."""

    def __init__(self, clock: Callable[[], float] = time.time, ttl: int = SESSION_TTL_SECONDS,
                 capacity: int = MAX_SESSIONS):
        self.clock, self.ttl, self.capacity = clock, ttl, capacity
        self._rows: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def open(self, token: str) -> str:
        session_id = secrets.token_urlsafe(SESSION_BYTES)
        now = self.clock()
        with self._lock:
            self._rows = {key: row for key, row in self._rows.items() if row[0] > now}
            while len(self._rows) >= self.capacity:
                del self._rows[min(self._rows, key=lambda key: self._rows[key][0])]
            self._rows[_digest(session_id)] = (now + self.ttl, _digest(token))
        return session_id

    def valid(self, session_id: str, token: str) -> bool:
        """True while the session is unexpired and was opened with the token the server now requires."""
        if not session_id:
            return False
        with self._lock:
            row = self._rows.get(_digest(session_id))
        return row is not None and row[0] > self.clock() and hmac.compare_digest(row[1], _digest(token))

    def close(self, session_id: str) -> None:
        with self._lock:
            self._rows.pop(_digest(session_id), None)
