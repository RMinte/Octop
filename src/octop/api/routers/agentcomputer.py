"""Authenticated AgentComputer protocol discovery."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from octop.api.agentcomputer import ProtocolErrorEnvelope, protocol_error_response
from octop.api.deps import current_bearer_user, get_server, require_database
from octop.infra.agentcomputer.capabilities import (
    AgentComputerCapabilitiesResponse,
    get_capabilities,
)
from octop.infra.agentcomputer.identity import IdentityError
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
