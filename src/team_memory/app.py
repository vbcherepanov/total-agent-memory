import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route

from team_memory.accounts import AccountPolicy, Accounts
from team_memory.call_router import READONLY, TOKEN, TeamCallRouter
from team_memory.contracts import DomainError, Unauthorized
from team_memory.dashboard import (
    COOKIE,
    HSTS_VALUE,
    SECURE_COOKIE,
    DashboardConfig,
    forwarded_client,
    peer_address,
    register_database_section,
    request_is_https,
)
from team_memory.dashboard import routes as dashboard_routes
from team_memory.dashboard_service import DashboardService
from team_memory.database_contracts import (
    DatabaseChecker,
    DatabaseConfigService,
    MaintenanceGate,
    MaintenanceReason,
    MigrationRunner,
    maintenance_allows,
)
from team_memory.learning import api as learning_api
from team_memory.metrics import Metrics, RequestMetrics
from team_memory.reports import api as reports_api
from team_memory.service import READ_ONLY_TOOLS, TOOLS, MemoryService
from team_memory.settings import SettingsStore, load_cipher
from team_memory.setup import SetupPolicy, SetupService
from team_memory.setup_database import SetupDatabaseService
from team_memory.setup_web import routes as setup_routes
from version import RELEASE_DATE, VERSION

MAX_REQUEST_BYTES = 1_000_000
# Requests being served for an authenticated caller (default of TAM_TEAM_MAX_PENDING_REQUESTS).
MAX_PENDING_REQUESTS = 32
# Requests that have not proven who they are yet (bad bearer tokens being looked up, logins; default
# of TAM_TEAM_MAX_PENDING_UNAUTHENTICATED). Kept smaller and separate so a flood of anonymous requests fills only
# this pool and signed-in users and agents keep their own.
MAX_PENDING_UNAUTHENTICATED = 8
# Sign-in, invite redemption and the setup wizard (routes that need no credential) are served from
# their own pool, with a per-client cap, so a slow anonymous body can block neither credential lookups nor each
# other beyond one client's share.
MAX_PENDING_PUBLIC = 16
MAX_PUBLIC_PER_CLIENT = 4
# A credential that passed a lookup is admitted without one for this long; the service re-checks it on use, so
# this delays no revocation. It is on the scale of an agent's idle gap, not of a single burst.
RECENT_CREDENTIAL_SECONDS = 300
RECENT_CREDENTIAL_ENV = "TAM_TEAM_RECENT_CREDENTIAL_SECONDS"
RECENT_CREDENTIAL_CAPACITY = 1024
BEARER = "bearer"
SESSION = "session"
PENDING_ENV = "TAM_TEAM_MAX_PENDING_REQUESTS"
PENDING_UNAUTHENTICATED_ENV = "TAM_TEAM_MAX_PENDING_UNAUTHENTICATED"
PENDING_PUBLIC_ENV = "TAM_TEAM_MAX_PENDING_PUBLIC"
PUBLIC_PER_CLIENT_ENV = "TAM_TEAM_MAX_PUBLIC_PER_CLIENT"
REQUEST_READ_TIMEOUT_SECONDS = 15
# Anonymous bodies (a password, an invite code) are tiny; a client that cannot send one in this long is not a user.
PUBLIC_READ_TIMEOUT_SECONDS = 4
PUBLIC_PATHS = ("/", "/healthz")
# The dashboard shell and its assets: bodyless reads of files that need no credential. A browser fetches a dozen
# of them at once, so they bypass the request pools (which guard slow bodies and credential lookups).
ASSET_PATHS = ("/dashboard", "/dashboard/")
ASSET_PREFIX = "/dashboard/static/"
ASSET_METHODS = ("GET", "HEAD")
# Tokens are secrets.token_urlsafe(32): 43 URL-safe characters. Anything shaped otherwise cannot be one, so it
# is refused before it costs a database lookup.
TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{32,128}")
WS_POLICY_VIOLATION = 1008
DEFAULT_BASE_URL = "http://127.0.0.1:3737"
SESSION_PREFIXES = ("/dashboard", "/learning", "/reports")
MAINTENANCE_MESSAGES = {
    MaintenanceReason.MIGRATION: "The server is moving its data to PostgreSQL; try again shortly",
    MaintenanceReason.ROLLBACK: "The server is restoring its SQLite databases; try again shortly",
    MaintenanceReason.LEASE_LOST: ("Another TAM server is using this database; this server accepts no changes. "
                                   "Stop the other server or fix the configuration, then restart this one"),
}


@dataclass(frozen=True)
class PendingLimits:
    authenticated: int = MAX_PENDING_REQUESTS
    unauthenticated: int = MAX_PENDING_UNAUTHENTICATED
    public: int = MAX_PENDING_PUBLIC
    public_per_client: int = MAX_PUBLIC_PER_CLIENT

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "PendingLimits":
        env = os.environ if environ is None else environ
        limits = cls(authenticated=int(env.get(PENDING_ENV) or MAX_PENDING_REQUESTS),
                     unauthenticated=int(env.get(PENDING_UNAUTHENTICATED_ENV) or MAX_PENDING_UNAUTHENTICATED),
                     public=int(env.get(PENDING_PUBLIC_ENV) or MAX_PENDING_PUBLIC),
                     public_per_client=int(env.get(PUBLIC_PER_CLIENT_ENV) or MAX_PUBLIC_PER_CLIENT))
        if min(limits.authenticated, limits.unauthenticated, limits.public, limits.public_per_client) < 1:
            raise ValueError(f"{PENDING_ENV}, {PENDING_UNAUTHENTICATED_ENV}, {PENDING_PUBLIC_ENV} and "
                             f"{PUBLIC_PER_CLIENT_ENV} must be positive integers")
        return limits


@dataclass(frozen=True)
class Identity:
    """Outcome of the credential check: a response to send, or whether the caller is authenticated."""
    response: JSONResponse | None = None
    authenticated: bool = False
    token: str = ""


class RecentCredentials:
    """Credentials that passed a lookup recently. Admission only: a known credential is still checked on every
    request, inside the authenticated pool, so revocation is never delayed. It lets a signed-in caller skip the
    anonymous pool while a flood of unknown credentials keeps that pool full."""

    def __init__(self, clock=time.monotonic, ttl: float = RECENT_CREDENTIAL_SECONDS,
                 capacity: int = RECENT_CREDENTIAL_CAPACITY):
        self.clock, self.ttl, self.capacity = clock, ttl, capacity
        self._seen: dict[str, float] = {}

    @staticmethod
    def _key(kind: str, value: str) -> str:
        return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()

    def known(self, kind: str, value: str) -> bool:
        return self._seen.get(self._key(kind, value), 0.0) > self.clock()

    def remember(self, kind: str, value: str) -> None:
        now = self.clock()
        if len(self._seen) >= self.capacity:
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
            while len(self._seen) >= self.capacity:
                del self._seen[min(self._seen, key=self._seen.__getitem__)]
        self._seen[self._key(kind, value)] = now + self.ttl

    def forget(self, kind: str, value: str) -> None:
        self._seen.pop(self._key(kind, value), None)


class Authentication:
    def __init__(self, app, registry, config: DashboardConfig | None = None, accounts: Accounts | None = None,
                 limits: PendingLimits | None = None):
        self.app, self.registry, self.accounts = app, registry, accounts
        self.config = config or DashboardConfig()
        self.limits = limits or PendingLimits.from_env()
        self.recent = RecentCredentials(ttl=float(os.environ.get(RECENT_CREDENTIAL_ENV) or RECENT_CREDENTIAL_SECONDS))
        self.pending = 0
        self.unauthenticated = 0
        self.public = 0
        self.public_clients: dict[str, int] = {}

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind != "http":
            # No route speaks websocket; refusing here keeps the bearer, origin and size checks from being skipped.
            if kind == "websocket":
                await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})
            return
        send = self._with_hsts(scope, send)
        if scope["path"] in PUBLIC_PATHS or self._is_asset(scope):
            await self.app(scope, receive, send)
            return
        credential = self._credential(scope)
        if credential is not None and self.recent.known(*credential):
            await self._serve_known(scope, receive, send, credential)
            return
        if credential is None and scope["path"].startswith(SESSION_PREFIXES):
            # Nothing to look up: sign-in, invites and the setup wizard go straight to the public pool.
            await self._serve_public(scope, receive, send)
            return
        # Only the credential check runs in the small anonymous pool, so a flood of bad credentials can neither
        # occupy the thread pool with lookups nor use up the slots of callers that are already authenticated.
        if self.unauthenticated >= self.limits.unauthenticated:
            await self._busy(scope, receive, send)
            return
        self.unauthenticated += 1
        try:
            identity = await self._identify(scope, credential)
        finally:
            self.unauthenticated -= 1
        if identity.response is not None:
            await identity.response(scope, receive, send)
        elif identity.authenticated:
            await self._serve_authenticated(scope, receive, send, identity.token)
        else:
            await self._serve_public(scope, receive, send)

    @staticmethod
    def _is_asset(scope) -> bool:
        path = scope["path"]
        return scope.get("method") in ASSET_METHODS and (path in ASSET_PATHS or path.startswith(ASSET_PREFIX))

    async def _serve_public(self, scope, receive, send):
        client = forwarded_client(self.config, peer_address(scope.get("client")),
                                  dict(scope["headers"]).get(b"x-forwarded-for", b"").decode("latin-1"))
        if self.public >= self.limits.public or self.public_clients.get(client, 0) >= self.limits.public_per_client:
            await self._busy(scope, receive, send)
            return
        self.public += 1
        self.public_clients[client] = self.public_clients.get(client, 0) + 1
        try:
            await self._serve(scope, receive, send, "", PUBLIC_READ_TIMEOUT_SECONDS)
        finally:
            self.public -= 1
            if self.public_clients[client] <= 1:
                del self.public_clients[client]
            else:
                self.public_clients[client] -= 1

    async def _serve_known(self, scope, receive, send, credential: tuple[str, str]):
        """A recently verified credential is checked again in an authenticated slot, never in the anonymous pool."""
        if self.pending >= self.limits.authenticated:
            await self._busy(scope, receive, send)
            return
        self.pending += 1
        try:
            identity = await self._identify(scope, credential)
            if identity.response is not None:
                await identity.response(scope, receive, send)
            elif identity.authenticated:
                await self._serve(scope, receive, send, identity.token)
            else:
                await self._serve(scope, receive, send, "", PUBLIC_READ_TIMEOUT_SECONDS)
        finally:
            self.pending -= 1

    async def _serve_authenticated(self, scope, receive, send, token: str):
        if self.pending >= self.limits.authenticated:
            await self._busy(scope, receive, send)
            return
        self.pending += 1
        try:
            await self._serve(scope, receive, send, token)
        finally:
            self.pending -= 1

    @staticmethod
    def _credential(scope) -> tuple[str, str] | None:
        """(kind, value) of the credential the route authenticates with, None when it carries none."""
        if scope["path"].startswith(SESSION_PREFIXES):
            cookies = HTTPConnection(scope).cookies
            session_id = cookies.get(SECURE_COOKIE) or cookies.get(COOKIE)
            return (SESSION, session_id) if session_id else None
        authorization = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        return (BEARER, authorization[len("Bearer "):]) if authorization.startswith("Bearer ") else None

    @staticmethod
    async def _busy(scope, receive, send):
        await JSONResponse({"error": "Server busy"}, status_code=429)(scope, receive, send)

    async def _identify(self, scope, credential: tuple[str, str] | None) -> Identity:
        """Bearer routes need a valid token; session routes count as authenticated when the cookie names a live
        session. Session routes without one (sign-in, invites, the setup wizard) stay in the anonymous pool."""
        if credential is None:
            if scope["path"].startswith(SESSION_PREFIXES):
                return Identity()
            return Identity(response=JSONResponse({"error": "Bearer token required"}, status_code=401,
                                                  headers={"WWW-Authenticate": "Bearer"}))
        kind, value = credential
        if kind == SESSION:
            if self.accounts is None:
                return Identity()
            try:
                await asyncio.to_thread(self.accounts.session, value, False)
            except DomainError:
                # Not a live session: the route itself answers 401 (or serves the sign-in page).
                self.recent.forget(kind, value)
                return Identity()
        else:
            try:
                if TOKEN_FORMAT.fullmatch(value) is None:
                    raise Unauthorized("Invalid or revoked token")
                await asyncio.to_thread(self.registry.authenticate, value)
            except Unauthorized:
                self.recent.forget(kind, value)
                return Identity(response=JSONResponse({"error": "Invalid or revoked token"}, status_code=401))
        self.recent.remember(kind, value)
        return Identity(authenticated=True, token=value if kind == BEARER else "")

    def _with_hsts(self, scope, send):
        """Every response over HTTPS (directly or through a trusted proxy) carries Strict-Transport-Security."""
        forwarded = dict(scope["headers"]).get(b"x-forwarded-proto", b"").decode("latin-1")
        if not request_is_https(self.config, scope.get("scheme", "http"), peer_address(scope.get("client")), forwarded):
            return send

        async def hardened_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                                                  (b"strict-transport-security", HSTS_VALUE.encode())]}
            await send(message)
        return hardened_send

    async def _serve(self, scope, receive, send, token: str, read_timeout: float = REQUEST_READ_TIMEOUT_SECONDS):
        headers = dict(scope["headers"])
        # Browser cross-origin requests are denied for tokens and sessions alike.
        if b"origin" in headers:
            origin = urlsplit(headers[b"origin"].decode("latin-1"))
            if origin.netloc.encode() != headers.get(b"host"):
                await JSONResponse({"error": "Cross-origin request denied"}, status_code=403)(scope, receive, send)
                return
        context = TOKEN.set(token)
        try:
            messages, size = [], 0
            deadline = time.monotonic() + read_timeout
            while True:
                try:
                    message = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
                except TimeoutError:
                    await JSONResponse({"error": "Request body timeout"}, status_code=408)(scope, receive, send)
                    return
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > MAX_REQUEST_BYTES:
                    await JSONResponse({"error": "Request too large"}, status_code=413)(scope, receive, send)
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break

            async def buffered_receive():
                return messages.pop(0) if messages else await receive()

            async def private_send(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [*message.get("headers", []), (b"cache-control", b"no-store")]}
                await send(message)

            await self.app(scope, buffered_receive, private_send)
        finally:
            TOKEN.reset(context)


class Maintenance:
    """Answers 503 + Retry-After while a database migration or rollback holds the server (decision D3)."""

    def __init__(self, app, gate: MaintenanceGate | None, metrics: Metrics):
        self.app, self.gate, self.metrics = app, gate, metrics

    async def __call__(self, scope, receive, send):
        state = self.gate.state() if self.gate is not None and scope["type"] == "http" else None
        if state is None or maintenance_allows(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        self.metrics.count("maintenance_rejected", reason=state.reason.value)
        await JSONResponse({"error": MAINTENANCE_MESSAGES[state.reason], "code": "unavailable",
                            "maintenance": state.model_dump(mode="json")}, status_code=503,
                           headers={"Retry-After": str(state.retry_after_seconds)})(scope, receive, send)


def build_dashboard(service: MemoryService, metrics: Metrics, database: DatabaseConfigService | None = None,
                    migration: MigrationRunner | None = None,
                    maintenance: MaintenanceGate | None = None) -> DashboardService:
    settings = SettingsStore(service.registry, load_cipher(service.registry.root))
    accounts = Accounts(service.registry, AccountPolicy.from_env())
    return DashboardService(service.registry, accounts, service, settings, metrics, database=database,
                            migration=migration, maintenance=maintenance)


def create_app(service: MemoryService, dashboard: DashboardService | None = None,
               config: DashboardConfig | None = None, setup: SetupService | None = None,
               base_url: str = DEFAULT_BASE_URL, database: DatabaseConfigService | None = None,
               migration: MigrationRunner | None = None, maintenance: MaintenanceGate | None = None,
               setup_checker: DatabaseChecker | None = None):
    config = config or DashboardConfig.from_env()
    metrics = dashboard.metrics if dashboard is not None else Metrics()
    dashboard = dashboard or build_dashboard(service, metrics, database, migration, maintenance)
    # The gate given here is the one the middleware enforces; the Database page reports the same one.
    dashboard.maintenance = dashboard.maintenance or maintenance
    setup = setup or SetupService(service.registry, dashboard.accounts, metrics, SetupPolicy.from_env())
    setup_database = SetupDatabaseService(setup, service.registry, dashboard.database, dashboard.migration,
                                          setup_checker)
    server = Server("total-agent-memory", version=VERSION)
    calls = TeamCallRouter(service)

    async def list_tools():
        return [Tool(name=name, description=description, inputSchema=model.model_json_schema())
                for name, (model, description) in TOOLS.items() if not READONLY.get() or name in READ_ONLY_TOOLS]

    async def call_tool(name, arguments):
        try:
            reply = await calls.replay(name, arguments or {})
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(reply.payload, ensure_ascii=False))],
                                  isError=reply.status >= 400)
        except (DomainError, ValidationError) as exc:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(
                {"error": str(exc), "code": getattr(exc, "code", "invalid_request")}))], isError=True)

    if hasattr(server, "list_tools"):
        server.list_tools()(list_tools)
        server.call_tool(validate_input=False)(call_tool)
    else:
        from mcp.types import (
            CallToolRequestParams,
            ListToolsResult,
            PaginatedRequestParams,
        )

        async def handle_list(_context, _params):
            return ListToolsResult(tools=await list_tools())

        async def handle_call(_context, params):
            return await call_tool(params.name, params.arguments)

        server.add_request_handler("tools/list", PaginatedRequestParams, handle_list)
        server.add_request_handler("tools/call", CallToolRequestParams, handle_call)
    manager = StreamableHTTPSessionManager(app=server, event_store=None, json_response=True, stateless=True)

    async def mcp(scope, receive, send):
        values = Request(scope).query_params.getlist("readonly")
        if values and values not in (["0"], ["1"]):
            await JSONResponse({"error": "readonly must be 0 or 1", "code": "invalid_request"}, status_code=400)(scope, receive, send)
            return
        policy = READONLY.set(values == ["1"])
        try:
            await manager.handle_request(scope, receive, send)
        finally:
            READONLY.reset(policy)

    async def health(_request):
        from server_capabilities import TEAM_CAPABILITIES
        if not config.health_version:
            return JSONResponse({"status": "ok", "name": "total-agent-memory", "capabilities": list(TEAM_CAPABILITIES)})
        return JSONResponse({"status": "ok", "name": "total-agent-memory", "version": VERSION,
                             "release_date": RELEASE_DATE, "capabilities": list(TEAM_CAPABILITIES)})

    async def index(_request):
        from team_memory.ui import PAGE
        return HTMLResponse(PAGE.replace("__VERSION__", VERSION).replace("__DATE__", RELEASE_DATE),
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "X-Frame-Options": "DENY",
                                     "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @asynccontextmanager
    async def lifespan(_app):
        from team_memory.lifecycle import ServerLease
        with ServerLease(service.registry.root):
            public_url = service.registry.organization().get("public_url")
            await asyncio.to_thread(setup.announce, public_url or base_url)
            async with manager.run():
                try:
                    yield
                finally:
                    await asyncio.to_thread(service.pool.close)

    register_database_section()
    learning_api.register_section()
    reports_api.register_section()
    app = Starlette(routes=[Route("/", index), Route("/healthz", health), *dashboard_routes(), *setup_routes(),
                            *learning_api.routes(service), *reports_api.routes(service),
                            calls.route, Mount("/mcp", app=mcp)], lifespan=lifespan,
                    middleware=[Middleware(Maintenance, gate=maintenance, metrics=metrics)])
    app.state.dashboard = dashboard
    app.state.setup = setup
    app.state.setup_database = setup_database
    app.state.migration = dashboard.migration
    app.state.maintenance = maintenance
    app.state.dashboard_config = config
    return RequestMetrics(Authentication(app, service.registry, config, dashboard.accounts), metrics)
