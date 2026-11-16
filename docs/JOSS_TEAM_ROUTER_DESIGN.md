# Issue 45: team MCP through the REST router

Approved by the owner on 2026-11-15: «да подтверждай». Targets 15.0.0 only. Personal MCP catalogue is outside this change.

## Recommended design

1. Keep the existing outer authentication, body size/deadline, admission limits and maintenance middleware. They execute once for each incoming HTTP request, whether REST or MCP.
2. Define one internal Starlette router with `POST /api/call`. Its typed request contains tool name and arguments; its handler invokes `MemoryService.call` once. Existing authorization, argument redaction, workspace checks and the `memory_call` audit event stay in that service.
3. Mount that router for ordinary REST requests. The MCP adapter serializes the same typed request and dispatches it directly through that same internal ASGI router, using the authenticated request context. It does not make a network request or re-enter the outer middleware. The internal router is an in-process object, not a separate listening port or bypass URL.
4. Store request policy in a ContextVar set/reset around each MCP HTTP request. `?readonly=1` uses an explicit allowlist of genuinely read-only operations, including learning/report operations. Anything not classified read-only is denied. `tools/list` and the common dispatcher use the same policy. A hidden write called by name returns the `readonly` domain error before any mutation. Concurrent read-only and normal requests do not share policy.
5. Workspace erasure and reindex are absent from the team tool registry and therefore unreachable from both MCP and `/api/call`; their CLI behavior stays unchanged.
6. Audit and service metrics remain at one service boundary. Successful/failed MCP and REST calls have the same `memory_call` fields and exactly one event. Rejected read-only calls also pass through that boundary so failures are counted without invoking a worker.

## Acceptance

- Real HTTP MCP and REST calls: equal payloads, permission errors and audit event fields; one service audit event per call.
- Read-only list/call tests cover memory, learning and report tools, plus unknown and destructive names.
- Concurrent MCP requests prove policy isolation. Revoked tokens, malformed input, body limits and maintenance keep their existing behavior.
- SQLite and PostgreSQL suites, team security tests, personal tool contract, package build and lint.

Review requirement from `PROMPT-TAM-AGENT.md`, lines 139 and 145, is satisfied by the owner's approval. Implementation and acceptance are authorized.
