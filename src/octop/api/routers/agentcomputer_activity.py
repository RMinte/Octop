"""Read-only summary of managed Project document tool use in one Run."""

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
from octop.infra.agentcomputer.tool_activity import RunToolActivity, get_run_tool_activity
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get(
    "/runs/{runID}/tool-activity",
    response_model=RunToolActivity,
    summary="Read an owned Run's Project document tool activity",
    description=(
        "Requires a user Bearer token. Missing and other users' Runs return the same 404. "
        "The original Run, Project, Task, dispatch, tool journal, and completion bindings are "
        "validated together. A Run with no tool call returns an empty list. Each item contains "
        "only the managed document's relative path, read status, and effect outcome; no document "
        "content, tool arguments, private receipt, or host path is returned. Pending or uncertain "
        "effects are labeled without claiming that the document was read. Invalid bindings "
        "return 503. This endpoint never dispatches or resumes execution."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Run owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Run identifier"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Run or tool journal binding unavailable",
        },
    },
)
async def run_tool_activity(
    response: Response,
    run_id: UUID = Path(alias="runID", description="Public Run UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> RunToolActivity | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(get_run_tool_activity, server.services, owner_id=user.id, run_id=run_id)
        activity = await server.agentcomputer_http_operations.run_thread(operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Run tool activity lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer Run tool activity binding is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="runInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Run tool activity storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if activity is None:
        return protocol_error_response("resourceNotFound", 404)
    return activity
