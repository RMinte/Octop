"""Bound versioned control-command bytes before parsing or bridge forwarding."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from octop.api.agentcomputer import is_agentcomputer_path, protocol_error_response
from octop.infra.agentcomputer.limits import MAX_COMMAND_BYTES

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AgentComputerBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in _MUTATING_METHODS
            or not is_agentcomputer_path(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        # Headers cannot establish the actual size of a streamed body. Buffer
        # only a bounded command, then replay it once to the typed JSON parser.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_COMMAND_BYTES:
                response = protocol_error_response("payloadLimitExceeded", 413)
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_body() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_body, send)


def install(app: FastAPI) -> None:
    app.add_middleware(AgentComputerBodyLimitMiddleware)
