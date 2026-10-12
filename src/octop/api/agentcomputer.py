"""Wire errors for the versioned AgentComputer HTTP surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from octop.infra.agentcomputer.commands import CommandAdmissionError
from octop.infra.errors import ErrorCode, OctopError

API_PREFIX = "/api/agentcomputer/v1"

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
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


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
    }
    setup = error.code == ErrorCode.SETUP_REQUIRED
    return protocol_error_response(
        codes.get(error.code, "internalError"),
        503 if setup else error.status,
        reason="setupRequired" if setup else None,
    )


def admission_error_response(error: CommandAdmissionError) -> JSONResponse:
    """Map domain refusal codes, without exposing untrusted exception details."""
    statuses = {
        "resourceNotFound": 404,
        "accessDenied": 403,
        "resourceConflict": 409,
        "idempotencyConflict": 409,
        "restoreEpochConflict": 409,
        "revisionConflict": 409,
        "activeRunConflict": 409,
        "reconciliationRequired": 409,
        "capabilityUnsupported": 422,
        "runtimeUnavailable": 503,
    }
    safe_reasons = {
        "preparationIncomplete",
        "userMessageAlreadyRegistered",
        "executionUnavailable",
        "permissionModeUnsupported",
    }
    return protocol_error_response(
        error.code,
        statuses[error.code],
        reason=error.reason if error.reason in safe_reasons else None,
    )
