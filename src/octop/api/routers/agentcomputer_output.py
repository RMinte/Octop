"""Read-only AgentComputer text output snapshots."""

from __future__ import annotations

import logging
from functools import partial
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse

from octop.api.agentcomputer import (
    ProtocolErrorEnvelope,
    admission_error_response,
    protocol_error_response,
)
from octop.api.deps import current_bearer_user, get_server, require_database
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
)
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.agentcomputer.output import RunOutputSnapshot, get_run_output
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get(
    "/runs/{runID}/output",
    response_model=RunOutputSnapshot,
    summary="Read an owned Run's text output snapshot",
    description=(
        "Requires an Octop user Bearer token. Missing and other users' Runs return the same 404. "
        "Before output exists, text and digest are null and provisional is false. "
        "For an unfinished Run, text comes only from the exact original user input's bound v2 "
        "history Turn; provisional is true because no completion receipt has validated it. "
        "For a succeeded Run, the existing final-result receipt and history checks are required, "
        "and provisional is false. The digest is SHA-256 of canonical JSON text. "
        "Invalid bindings or archives return 503 rather than exposing untrusted content. "
        "This read does not start, resume, or dispatch execution."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Run owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Run identifier"},
        503: {"model": ProtocolErrorEnvelope, "description": "Run binding or archive unavailable"},
    },
)
async def run_output(
    response: Response,
    run_id: UUID = Path(alias="runID", description="Public Run UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> RunOutputSnapshot | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(get_run_output, server.services, owner_id=user.id, run_id=run_id)
        output = await server.agentcomputer_http_operations.run_thread(operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Run output lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer Run output binding or archive is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="runInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Run output storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if output is None:
        return protocol_error_response("resourceNotFound", 404)
    return output
