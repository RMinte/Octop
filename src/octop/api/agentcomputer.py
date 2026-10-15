"""Wire errors for the versioned AgentComputer HTTP surface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from octop.infra.errors import ErrorCode, OctopError

API_PREFIX = "/api/agentcomputer/v1"
_BRIDGE_PREFIX = re.compile(r"^/api/bridge/connections/[^/]+/agentcomputer/v1(?:/|$)")

ProtocolErrorCode = Literal[
    "authenticationRequired",
    "credentialExpired",
    "accessDenied",
    "runtimeUnavailable",
    "storageUnavailable",
    "resourceNotFound",
    "resourceConflict",
    "idempotencyConflict",
    "restoreEpochConflict",
    "revisionConflict",
    "activeRunConflict",
    "reconciliationRequired",
    "capabilityUnsupported",
    "payloadLimitExceeded",
    "invalidRequest",
    "internalError",
]


class ProtocolError(BaseModel):
    code: ProtocolErrorCode = Field(description="Stable code for client-side localization.")
    retryable: bool = Field(description="Whether the same request can be retried automatically.")
    reasonCode: str | None = Field(default=None, description="Optional machine-readable cause.")


class ProtocolErrorEnvelope(BaseModel):
    error: ProtocolError


def is_agentcomputer_path(path: str) -> bool:
    return (
        path == API_PREFIX
        or path.startswith(f"{API_PREFIX}/")
        or _BRIDGE_PREFIX.match(path) is not None
    )


def protocol_error_response(
    code: ProtocolErrorCode,
    status: int,
    *,
    retryable: bool = False,
    reason: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ProtocolErrorEnvelope(
        error=ProtocolError(code=code, retryable=retryable, reasonCode=reason)
    )
    response_headers = dict(headers or {})
    response_headers["Cache-Control"] = "no-store"
    if status == 401:
        response_headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status, content=body.model_dump(exclude_none=True), headers=response_headers
    )


def octop_error_response(error: OctopError) -> JSONResponse:
    """Translate at the transport boundary, without exposing internal details."""
    codes: dict[ErrorCode, ProtocolErrorCode] = {
        ErrorCode.AUTH_FAILED: "authenticationRequired",
        ErrorCode.TOKEN_EXPIRED: "credentialExpired",
        ErrorCode.FORBIDDEN: "accessDenied",
        ErrorCode.USER_DISABLED: "accessDenied",
        ErrorCode.NOT_FOUND: "resourceNotFound",
        ErrorCode.SETUP_REQUIRED: "runtimeUnavailable",
        **PROJECT_TASK_ERROR_CODES,
    }
    setup = error.code == ErrorCode.SETUP_REQUIRED
    return protocol_error_response(
        codes.get(error.code, "internalError"),
        503 if setup else error.status,
        reason="setupRequired"
        if setup
        else error.code.value
        if error.code in PROJECT_TASK_ERROR_CODES
        else None,
    )


PROJECT_TASK_ERROR_CODES: dict[ErrorCode, ProtocolErrorCode] = {
    ErrorCode.PROJECT_SYNC_RESET: "reconciliationRequired",
    ErrorCode.PROJECT_NAME_TAKEN: "resourceConflict",
    ErrorCode.PROJECT_REQUEST_CONFLICT: "idempotencyConflict",
    ErrorCode.PROJECT_REVISION_CONFLICT: "revisionConflict",
    ErrorCode.PROJECT_RESOURCE_INVALID: "invalidRequest",
    ErrorCode.PROJECT_DIRECTORY_INVALID: "invalidRequest",
    ErrorCode.TASK_BUSY: "activeRunConflict",
    ErrorCode.TASK_REQUEST_CONFLICT: "idempotencyConflict",
    ErrorCode.TASK_MODEL_UNAVAILABLE: "runtimeUnavailable",
    ErrorCode.TASK_RESOURCE_UNAVAILABLE: "invalidRequest",
    ErrorCode.CONTROL_PLANE_BUSY: "runtimeUnavailable",
    ErrorCode.TOKEN_QUOTA_EXCEEDED: "accessDenied",
    ErrorCode.BRIDGE_NOT_FOUND: "resourceNotFound",
    ErrorCode.BRIDGE_NOT_CONNECTED: "runtimeUnavailable",
    ErrorCode.BRIDGE_REMOTE_UNSUPPORTED: "capabilityUnsupported",
    ErrorCode.BRIDGE_TUNNEL_FAILED: "runtimeUnavailable",
}
