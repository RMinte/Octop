"""Authenticated AgentComputer discovery and immutable receipt queries."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse

from octop.api.agentcomputer import ProtocolErrorEnvelope, protocol_error_response
from octop.api.deps import current_bearer_user, get_server, require_database
from octop.infra.agentcomputer.capabilities import (
    AgentComputerCapabilitiesResponse,
    get_capabilities,
)
from octop.infra.agentcomputer.commands import (
    StorageDataError,
    StorageUnavailableError,
    get_command_receipt,
)
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.agentcomputer.models import CommandReceipt
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get(
    "/capabilities",
    response_model=AgentComputerCapabilitiesResponse,
    summary="Inspect AgentComputer protocol and capabilities",
    description=(
        "Requires an Octop user Bearer token in the Authorization header. "
        "User-session authentication does not bind a client device. "
        "Command receipt support describes queries for existing receipts only; "
        "HTTP command submission and task execution remain unavailable. "
        "Unavailable capabilities must not enable task execution in the client."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Setup or identity storage unavailable",
        },
    },
)
async def capabilities(
    response: Response,
    server: OctopServer = Depends(get_server),
    _user: User = Depends(current_bearer_user),
) -> AgentComputerCapabilitiesResponse | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        # Identity initialization/reads touch disk; keep them off the ASGI loop.
        return await asyncio.get_running_loop().run_in_executor(
            None, get_capabilities, server.services
        )
    except IdentityError:
        logger.exception("AgentComputer identity is invalid; it was not regenerated")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except OSError:
        logger.exception("AgentComputer identity storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)


@router.get(
    "/commands/{commandID}",
    response_model=CommandReceipt,
    summary="Read an existing AgentComputer command receipt",
    description=(
        "Requires an Octop user Bearer token in the Authorization header. "
        "Returns only the authenticated user's immutable admission receipt. "
        "An accepted receipt proves original durable registration, not current Run state "
        "or task execution. Missing and other users' commands return the same 404. "
        "This read-only endpoint does not submit, replay, or dispatch commands."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No command owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid command identifier"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Receipt or identity storage unavailable",
        },
    },
)
async def command_receipt(
    response: Response,
    command_id: UUID = Path(alias="commandID", description="Public command UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> CommandReceipt | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        receipt = await asyncio.get_running_loop().run_in_executor(
            None, get_command_receipt, server.services, user.id, command_id
        )
    except IdentityError:
        logger.error("AgentComputer identity is invalid during receipt lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored command receipt is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="receiptInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer command receipt storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if receipt is None:
        return protocol_error_response("resourceNotFound", 404)
    return receipt
