from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import Field, JsonValue, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Router

from team_memory.contracts import DTO, DomainError
from team_memory.service import MemoryService

TOKEN: ContextVar[str] = ContextVar("team_memory_token", default="")
READONLY: ContextVar[bool] = ContextVar("team_memory_readonly", default=False)
CALL_PATH = "/api/call"


class ToolInvocation(DTO):
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class RouterReply:
    status: int
    payload: JsonValue


class TeamCallRouter:
    def __init__(self, service: MemoryService):
        self.service = service
        self.route = Route(CALL_PATH, self.invoke, methods=["POST"])
        self.router = Router(routes=[self.route])

    async def invoke(self, request: Request) -> JSONResponse:
        try:
            payload = ToolInvocation.model_validate(await request.json())
            options = {"readonly": True} if READONLY.get() else {}
            result = await self.service.call(TOKEN.get(), payload.name, payload.arguments, **options)
            return JSONResponse(result)
        except (DomainError, ValidationError, ValueError, KeyError, TypeError) as error:
            return JSONResponse({"error": str(error), "code": getattr(error, "code", "invalid_request")}, status_code=400)

    async def replay(self, name: str, arguments: dict[str, JsonValue]) -> RouterReply:
        body = ToolInvocation(name=name, arguments=arguments).model_dump_json().encode()
        request_sent = False
        status = 500
        response_body = bytearray()

        async def receive():
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))

        await self.router({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                           "scheme": "http", "path": CALL_PATH, "raw_path": CALL_PATH.encode(), "root_path": "",
                           "query_string": b"", "headers": [(b"content-type", b"application/json")],
                           "client": None, "server": None}, receive, send)
        return RouterReply(status, json.loads(response_body))
