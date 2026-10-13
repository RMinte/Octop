"""Authenticated AgentComputer discovery and immutable receipt queries."""

from __future__ import annotations

import asyncio
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
from octop.infra.agentcomputer.cancellations import (
    CancelRunCommand,
    CancelRunReceipt,
    cancel_run,
)
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
    NewMessageCommand,
    ProjectCreateCommand,
    ProjectCreateReceipt,
    ProjectView,
    RunPauseDecisionReceipt,
    RunView,
    TaskCreateReceipt,
    TaskView,
)
from octop.infra.agentcomputer.projects import create_project, get_project
from octop.infra.agentcomputer.runs import get_run
from octop.infra.agentcomputer.submission import submit_new_message
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
        "Task creation remains unavailable; guarded message submission rejects unavailable execution. "
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
    response_model=(
        CommandReceipt
        | ProjectCreateReceipt
        | TaskCreateReceipt
        | CancelRunReceipt
        | RunPauseDecisionReceipt
    ),
    summary="Read an existing AgentComputer command receipt",
    description=(
        "Requires an Octop user Bearer token in the Authorization header. "
        "Returns only the authenticated user's immutable admission, cancellation, or pause decision receipt. "
        "A receipt preserves the original durable fact; a pause decision receipt does not authorize a tool "
        "or prove engine delivery. It does not report current Run state, project preparation, or task execution. "
        "Missing and other users' commands return the same 404. "
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
) -> (
    CommandReceipt
    | ProjectCreateReceipt
    | TaskCreateReceipt
    | CancelRunReceipt
    | RunPauseDecisionReceipt
    | JSONResponse
):
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
        "do not enable HTTP task creation or production model execution. "
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


@router.post(
    "/tasks/{taskID}/turns",
    status_code=201,
    response_model=CommandReceipt,
    summary="Register a new-message Turn when execution admission is available",
    description=(
        "Requires a user Bearer token and an existing owned Task. "
        "Production execution admission is unavailable: new commands return 503 "
        "runtimeUnavailable/executionUnavailable without registering a Turn or Run. "
        "Only newMessage is accepted by this schema; nonempty artifacts or selected skills "
        "remain unsupported. A successful admission returns the original durable queued receipt "
        "with status 201, including same-key replays. It does not create a Task, dispatch a model, "
        "or prove execution. Replays preserve original facts after capability changes; "
        "an old restore epoch is still rejected before replay."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No task owned by this user"},
        409: {
            "model": ProtocolErrorEnvelope,
            "description": "Command, message, revision, restore epoch, or active Run conflict",
        },
        413: {
            "model": ProtocolErrorEnvelope,
            "description": "Actual command body exceeds 65536 bytes",
        },
        422: {
            "model": ProtocolErrorEnvelope,
            "description": "Invalid or unsupported typed command",
        },
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Execution admission or storage unavailable",
        },
    },
)
async def new_message_turn(
    body: NewMessageCommand,
    response: Response,
    task_id: UUID = Path(alias="taskID", description="Existing public Task UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> CommandReceipt | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(
            submit_new_message, server.services, owner_id=user.id, task_id=task_id, command=body
        )
        return await asyncio.get_running_loop().run_in_executor(None, operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Turn admission")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Turn registration is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="submissionInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Turn admission storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)


@router.get(
    "/runs/{runID}",
    response_model=RunView,
    summary="Read an existing Run's current persisted state and dispatch facts",
    description=(
        "Requires a user Bearer token. Returns only this user's Run; missing and other users' "
        "Runs return the same 404, including for admins. This is a read-only current view, "
        "separate from the original queued command receipt. queued/notDispatched means "
        "durable registration, not model execution. claimed reserves a worker handoff; "
        "dispatching means a handoff may have started. engineAccepted proves an application-owned "
        "intake receipt before model execution, not running or completed execution. "
        "The daemon's conservative pre-runtime scan tightens unresolved claimed/dispatching/engineAccepted "
        "facts to interrupted with reconciliationRequired=true and keeps both lanes. "
        "Embedded startup skips this scan by default. Tightening does not request user cancellation, "
        "prove an older process or model stopped, or authorize re-execution. "
        "cancelRequested reports a validated durable request, not a proven engine stop; "
        "read state for the current outcome. "
        "Dispatch phases do not prove native harness exactly-once execution or a reserved history Turn. "
        "Historical restore epochs remain readable. "
        "This endpoint does not dispatch, cancel, repair storage, or expose model input, "
        "internal thread/agent IDs, host paths, or an unimplemented event cursor."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Run owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Run identifier"},
        503: {"model": ProtocolErrorEnvelope, "description": "Run binding or storage unavailable"},
    },
)
async def run_status(
    response: Response,
    run_id: UUID = Path(alias="runID", description="Public Run UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> RunView | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(get_run, server.services, owner_id=user.id, run_id=run_id)
        run = await asyncio.get_running_loop().run_in_executor(None, operation)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Run lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Run binding is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="runInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Run storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if run is None:
        return protocol_error_response("resourceNotFound", 404)
    return run


@router.post(
    "/runs/{runID}/cancel",
    status_code=201,
    response_model=CancelRunReceipt,
    summary="Record cancellation for one owned Run",
    description=(
        "Requires a user Bearer token and an explicit owned Run ID. The command fixes the "
        "current restore epoch and expected Run revision; same-key retries return the original "
        "durable receipt with replayed=true and status 201. A queued Run proven never dispatched "
        "can be stopped atomically. For a claimed or started Run, status=cancelRequested only "
        "proves that the request was recorded and new execution handoffs are fenced; it does "
        "not prove that an engine or child process stopped. Read the Run for current state and "
        "the original command receipt for response-loss recovery. This endpoint never invokes "
        "a thread-wide stop or cancels another Run."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Run owned by this user"},
        409: {
            "model": ProtocolErrorEnvelope,
            "description": "Command identity, restore epoch, Run revision, or reconciliation conflict",
        },
        413: {
            "model": ProtocolErrorEnvelope,
            "description": "Actual command body exceeds 65536 bytes",
        },
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid typed command or Run ID"},
        503: {"model": ProtocolErrorEnvelope, "description": "Run graph or storage unavailable"},
    },
)
async def request_run_cancellation(
    body: CancelRunCommand,
    response: Response,
    run_id: UUID = Path(alias="runID", description="Exact public Run UUID to cancel."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> CancelRunReceipt | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(
            cancel_run, server.services, owner_id=user.id, run_id=run_id, command=body
        )
        return await asyncio.get_running_loop().run_in_executor(None, operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Run cancellation")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Run cancellation graph is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="cancellationInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Run cancellation storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
