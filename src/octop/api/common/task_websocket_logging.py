"""Allowlisted connection diagnostics; never accept request or conversation payloads."""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)
Event = Literal[
    "connection_received",
    "connection_accepted",
    "authentication_started",
    "authentication_succeeded",
    "authentication_failed",
    "ready",
    "session_renewed",
    "error",
    "close_requested",
    "close_sent",
    "transport_error",
    "unexpected_error",
    "connection_closed",
]
Scope = Literal["connection", "command", "subscription"]
CloseSource = Literal["server", "peer", "transport", "cancelled", "unknown"]


def _uuid(value: str) -> bool:
    if len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _request_id(value: str) -> bool:
    return _uuid(value) or bool(
        re.fullmatch(r"[0-9a-fA-F]{32}|[0-7][0-9A-HJKMNP-TV-Z]{25}", value, re.IGNORECASE)
    )


class TaskWebSocketLog:
    """One physical connection, with independent client and server identifiers."""

    def __init__(self, headers: Headers) -> None:
        client_ids = headers.getlist("x-agentcomputer-connection-id")
        valid = len(client_ids) == 1 and _uuid(client_ids[0])
        request_ids = headers.getlist("x-request-id")
        self.server_connection_id = str(uuid4())
        self.client_connection_id = client_ids[0] if valid else None
        self.client_connection_id_status = (
            "valid" if valid else "invalid" if client_ids else "missing"
        )
        self.request_id = (
            request_ids[0] if len(request_ids) == 1 and _request_id(request_ids[0]) else None
        )
        self.started = time.monotonic()
        self.seq = 0
        self.accepted = False
        self.command_count = 0
        self.subscription_count = 0
        self.auth_check_count = 0
        self.error_count = 0
        self.close_code: int | None = None
        self.close_source: CloseSource = "unknown"
        self.close_stage: str | None = None

    @property
    def response_headers(self) -> list[tuple[bytes, bytes]]:
        headers = [(b"x-octop-connection-id", self.server_connection_id.encode("ascii"))]
        if self.client_connection_id is not None:
            headers.append(
                (b"x-agentcomputer-connection-id", self.client_connection_id.encode("ascii"))
            )
        return headers

    def record(
        self,
        event: Event,
        *,
        stage: str | None = None,
        status: int | None = None,
        error_code: str | None = None,
        scope: Scope | None = None,
        command_type: str | None = None,
        close_code: int | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.seq += 1
        data: dict[str, object] = {
            "schema_version": 1,
            "event": event,
            "seq": self.seq,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
            "server_connection_id": self.server_connection_id,
            "client_connection_id": self.client_connection_id,
            "client_connection_id_status": self.client_connection_id_status,
            "request_id": self.request_id,
            "path": "/api/project-tasks/ws",
        }
        for key, value in (
            ("stage", stage),
            ("status", status),
            ("error_code", error_code),
            ("scope", scope),
            ("command_type", command_type),
            ("close_code", close_code),
        ):
            if value is not None:
                data[key] = value
        if exception is not None:
            data["exception_type"] = type(exception).__name__
            if event == "unexpected_error":
                # No exception text, source lines, local values or absolute paths.
                data["stack"] = [
                    {
                        "file": Path(frame.filename).name,
                        "function": frame.name,
                        "line": frame.lineno,
                    }
                    for frame in traceback.extract_tb(exception.__traceback__)[-8:]
                ]
        if event == "error":
            self.error_count += 1
        if event == "close_sent":
            self.close_code = close_code
            self.close_source = "server"
            self.close_stage = stage
        if event == "connection_closed":
            data.update(
                close_code=self.close_code,
                close_source=self.close_source,
                close_stage=self.close_stage,
                command_count=self.command_count,
                subscription_count=self.subscription_count,
                auth_check_count=self.auth_check_count,
                error_count=self.error_count,
            )
        level = logging.ERROR if event == "unexpected_error" else logging.INFO
        logger.log(level, "project_task_ws %s", json.dumps(data, separators=(",", ":")))

    def disconnected(self, exc: WebSocketDisconnect | OSError, *, stage: str) -> None:
        if self.close_source == "server":
            return
        # Starlette wraps a failed ASGI send's OSError in disconnect code 1006.
        # Preserve that known transport failure without retaining its message.
        cause = exc.__cause__ or exc.__context__
        transport = cause if isinstance(exc, WebSocketDisconnect) and exc.code == 1006 else exc
        if isinstance(transport, OSError):
            self.close_stage = stage
            self.close_code = exc.code if isinstance(exc, WebSocketDisconnect) else None
            self.close_source = "transport"
            self.record("transport_error", stage=stage, exception=transport)
            return
        if isinstance(exc, WebSocketDisconnect):
            # A peer/proxy/transport disconnect is not evidence of a user logout.
            self.close_code = exc.code
            if self.close_source != "transport":
                self.close_source = "peer"
                self.close_stage = stage
