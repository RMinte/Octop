"""Authenticated AgentComputer discovery and immutable receipt queries."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse

from octop.api.agentcomputer import (
    ProtocolErrorEnvelope,
    admission_error_response,
    protocol_error_response,
)
from octop.api.deps import current_bearer_user, get_server, require_database
from octop.infra.agentcomputer.capabilities import (
    AgentComputerCapabilitiesResponse,
    get_capabilities,
)
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    get_command_receipt,
)
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.agentcomputer.models import (
    CommandReceipt,
    ProjectCreateCommand,
    ProjectCreateReceipt,
    ProjectView,
    TaskCreateReceipt,
    TaskView,
)
from octop.infra.agentcomputer.projects import create_project, get_project
from octop.infra.agentcomputer.tasks import get_task
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
        "Local project intent registration, task metadata queries, and receipt queries are implemented. "
        "Task creation and execution remain unavailable over HTTP. "
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
    response_model=CommandReceipt | ProjectCreateReceipt | TaskCreateReceipt,
    summary="Read an existing AgentComputer command receipt",
    description=(
        "Requires an Octop user Bearer token in the Authorization header. "
        "Returns only the authenticated user's immutable admission receipt. "
        "An accepted receipt proves original durable registration, not current Run state or project preparation "
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
) -> CommandReceipt | ProjectCreateReceipt | TaskCreateReceipt | JSONResponse:
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


@router.post(
    "/projects",
    status_code=201,
    response_model=ProjectCreateReceipt,
    summary="Register a local project intent and prepare stopped resources",
    description=(
        "Requires an Octop user Bearer token; owner is derived from the authenticated user. "
        "Registers a durable intent, then awaits local stopped-resource preparation. "
        "The response preserves the original pending acceptance fact even if preparation fails. "
        "Same-key retries return that original receipt with replayed=true and status 201. "
        "Read the project endpoint for current preparation status. This does not bind a device, "
        "start a harness, call a model, or enable task execution."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        409: {
            "model": ProtocolErrorEnvelope,
            "description": "Identity, key, resource, or restore epoch conflict",
        },
        413: {
            "model": ProtocolErrorEnvelope,
            "description": "Actual command body exceeds 65536 bytes",
        },
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid typed project command"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Runtime or storage unavailable; query the original command",
        },
    },
)
async def register_project(
    body: ProjectCreateCommand,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectCreateReceipt | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    if server.app_runtime is None:
        return protocol_error_response("runtimeUnavailable", 503, reason="runtimeNotReady")
    try:
        return await create_project(
            server.services, server.app_runtime.agent_registry, owner_id=user.id, command=body
        )
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during project registration")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored project data is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="projectInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer project storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)


@router.get(
    "/projects/{projectID}",
    response_model=ProjectView,
    summary="Read current local project preparation status",
    description=(
        "Requires a user Bearer token. Returns only the authenticated user's project; "
        "missing and other users' projects return the same 404, including for admins. "
        "This current view is separate from the immutable creation receipt. "
        "Stopped resources and a configured Provider do not enable task execution. "
        "Host paths, credentials, and internal agent IDs are omitted."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No project owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid project identifier"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Project or identity storage unavailable",
        },
    },
)
async def project_status(
    response: Response,
    project_id: UUID = Path(alias="projectID", description="Public project UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectView | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        project = await asyncio.get_running_loop().run_in_executor(
            None, get_project, server.services, user.id, project_id
        )
    except IdentityError:
        logger.error("AgentComputer identity is invalid during project lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored project data is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="projectInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer project storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if project is None:
        return protocol_error_response("resourceNotFound", 404)
    return project


@router.get(
    "/tasks/{taskID}",
    response_model=TaskView,
    summary="Read an existing task's metadata and mapping state",
    description=(
        "Requires a user Bearer token. Returns only the authenticated user's task; "
        "missing and other users' tasks return the same 404, including for admins. "
        "The preparation state describes a stored session/thread mapping. "
        "Even ready metadata has executionReady=false. Internal registration and test fixtures "
        "do not enable HTTP task creation, message submission, or model execution. "
        "Internal thread/agent IDs and host paths are omitted."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No task owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid task identifier"},
        503: {"model": ProtocolErrorEnvelope, "description": "Task mapping or storage unavailable"},
    },
)
async def task_metadata(
    response: Response,
    task_id: UUID = Path(alias="taskID", description="Public task UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> TaskView | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        task = await asyncio.get_running_loop().run_in_executor(
            None, get_task, server.services, user.id, task_id
        )
    except IdentityError:
        logger.error("AgentComputer identity is invalid during task lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored task mapping is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="taskInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer task storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if task is None:
        return protocol_error_response("resourceNotFound", 404)
    return task
