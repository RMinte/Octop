"""Authenticated AgentComputer discovery and immutable receipt queries."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
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
from octop.infra.agentcomputer.event_listing import (
    InvalidTaskEventCursor,
    TaskEventListResponse,
    list_task_events,
)
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.agentcomputer.models import (
    CommandReceipt,
    NewMessageCommand,
    ProjectCreateCommand,
    ProjectCreateReceipt,
    ProjectListResponse,
    ProjectView,
    RunPauseDecisionReceipt,
    RunPauseStatus,
    RunView,
    TaskCreateCommand,
    TaskCreateReceipt,
    TaskListResponse,
    TaskView,
)
from octop.infra.agentcomputer.pauses import get_pause_status, get_run_pause_status
from octop.infra.agentcomputer.projects import create_project, get_project, list_projects
from octop.infra.agentcomputer.results import RunResult, get_run_result
from octop.infra.agentcomputer.runs import get_run
from octop.infra.agentcomputer.submission import submit_new_message
from octop.infra.agentcomputer.task_listing import InvalidTaskListCursor, list_tasks
from octop.infra.agentcomputer.task_snapshot import TaskControlSnapshot, get_task_control_snapshot
from octop.infra.agentcomputer.tasks import admit_task, get_task
from octop.infra.agentcomputer.turn_listing import (
    InvalidTurnListCursor,
    TurnListResponse,
    list_turns,
)
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
        "Task creation and message submission reject unavailable execution. "
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
        return await server.agentcomputer_http_operations.run_thread(
            get_capabilities, server.services
        )
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
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
        receipt = await server.agentcomputer_http_operations.run_thread(
            get_command_receipt, server.services, user.id, command_id
        )
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
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
    services = server.services
    agent_registry = server.app_runtime.agent_registry
    try:
        return await server.agentcomputer_http_operations.run_async(
            lambda: create_project(
                services,
                agent_registry,
                owner_id=user.id,
                command=body,
            )
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
    "/projects",
    response_model=ProjectListResponse,
    summary="List the authenticated user's local project statuses",
    description=(
        "Requires a user Bearer token. Returns only this user's projects, including for admins, "
        "in stable public project UUID order. Use nextCursor as the next request's cursor; "
        "null means no further page. Each item is a current preparation view, not execution readiness. "
        "Host paths, credentials, and internal agent IDs are omitted."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid limit or cursor"},
        503: {"model": ProtocolErrorEnvelope, "description": "Project storage unavailable"},
    },
)
async def project_list(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100, description="Page size, from 1 to 100."),
    cursor: UUID | None = Query(
        default=None, description="Last public project UUID from the previous page."
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectListResponse | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        return await server.agentcomputer_http_operations.run_thread(
            list_projects, server.services, user.id, limit, cursor
        )
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during project listing")
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
        project = await server.agentcomputer_http_operations.run_thread(
            get_project, server.services, user.id, project_id
        )
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
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
    "/projects/{projectID}/tasks",
    response_model=TaskListResponse,
    summary="List an owned project's Tasks and each latest verified Run",
    description=(
        "Requires a user Bearer token. Missing and other users' projects return the same 404, "
        "including for admins. Tasks are ordered by creation time and public Task UUID, both "
        "descending. Pass nextCursor unchanged to read the next page; a cursor from another box, "
        "user, project, or restore epoch is invalid. Each item has a current Task mapping view and "
        "its latest fully verified Run, or null if no Run exists. A broken stored binding fails the "
        "whole page instead of omitting an item. This query never dispatches a model."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No project owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid project, limit, or cursor"},
        503: {"model": ProtocolErrorEnvelope, "description": "Task or Run storage unavailable"},
    },
)
async def task_list(
    response: Response,
    project_id: UUID = Path(alias="projectID", description="Owned public project UUID."),
    limit: int = Query(default=50, ge=1, le=100, description="Page size, from 1 to 100."),
    cursor: str | None = Query(
        default=None, description="Opaque cursor returned by the previous page."
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> TaskListResponse | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        listing = await server.agentcomputer_http_operations.run_thread(
            list_tasks, server.services, user.id, project_id, limit, cursor
        )
    except InvalidTaskListCursor:
        return protocol_error_response("invalidRequest", 422)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Task listing")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Task or Run listing data is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="taskInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Task listing storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if listing is None:
        return protocol_error_response("resourceNotFound", 404)
    return listing


@router.post(
    "/projects/{projectID}/tasks",
    status_code=201,
    response_model=TaskCreateReceipt,
    summary="Create an owned Task and its dedicated thread when available",
    description=(
        "Requires a user Bearer token. The body projectID must match the path, and the project "
        "must belong to the authenticated user. New Task registration requires the tasks capability "
        "to be available for the project's fixed text model; otherwise it returns 503 without "
        "creating a Task. "
        "When available, the mapping and original receipt commit atomically. Same-key retries "
        "return the original receipt with replayed=true, even if admission later closes. "
        "This endpoint never starts a model."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No project owned by this user"},
        409: {
            "model": ProtocolErrorEnvelope,
            "description": "Command, project revision, restore epoch, or resource conflict",
        },
        413: {
            "model": ProtocolErrorEnvelope,
            "description": "Actual command body exceeds 65536 bytes",
        },
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid command or project mismatch"},
        503: {"model": ProtocolErrorEnvelope, "description": "Execution or storage unavailable"},
    },
)
async def create_task(
    body: TaskCreateCommand,
    response: Response,
    project_id: UUID = Path(alias="projectID", description="Owned public project UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> TaskCreateReceipt | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    if body.projectID != project_id:
        return protocol_error_response("invalidRequest", 422)
    try:
        operation = partial(admit_task, server.services, owner_id=user.id, command=body)
        return await server.agentcomputer_http_operations.run_thread(operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Task registration")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Task registration is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="taskInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Task registration storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)


@router.get(
    "/tasks/{taskID}",
    response_model=TaskView,
    summary="Read an existing task's metadata and mapping state",
    description=(
        "Requires a user Bearer token. Returns only the authenticated user's task; "
        "missing and other users' tasks return the same 404, including for admins. "
        "The preparation state describes a stored session/thread mapping. "
        "executionReady reflects the current text execution gate and the project's fixed model. "
        "Task creation and model execution remain capability gated. "
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
        task = await server.agentcomputer_http_operations.run_thread(
            get_task, server.services, user.id, task_id
        )
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
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


@router.get(
    "/tasks/{taskID}/turns",
    response_model=TurnListResponse,
    summary="List an owned Task's original messages and verified Run results",
    description=(
        "Requires a user Bearer token. Missing and other users' Tasks return the same 404, "
        "including for admins. Turns are ordered by insertion order, newest first. Pass nextCursor "
        "unchanged to read older Turns; a cursor from another box, user, Task, or restore epoch "
        "is invalid. Each item returns the exact accepted prompt, a fully validated current Run "
        "and its result. The result text is null until success and is then revalidated against "
        "the completion receipt and bound v2 history. Old-epoch accepted Turns remain readable "
        "through the current installation identity. A Run may advance between the Run snapshot "
        "and the later result read; refresh to see current state. A broken fact fails the whole page."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Task owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Task, limit, or cursor"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Turn, Run, or result evidence unavailable",
        },
    },
)
async def turn_list(
    response: Response,
    task_id: UUID = Path(alias="taskID", description="Owned public Task UUID."),
    limit: int = Query(default=20, ge=1, le=100, description="Page size, from 1 to 100."),
    cursor: str | None = Query(
        default=None, description="Opaque cursor returned by the previous page."
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> TurnListResponse | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        listing = await server.agentcomputer_http_operations.run_thread(
            list_turns, server.services, user.id, task_id, limit, cursor
        )
    except InvalidTurnListCursor:
        return protocol_error_response("invalidRequest", 422)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Turn listing")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Turn, Run, or result data is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="turnInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Turn listing storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if listing is None:
        return protocol_error_response("resourceNotFound", 404)
    return listing


@router.get(
    "/tasks/{taskID}/snapshot",
    response_model=TaskControlSnapshot,
    summary="Read an owned Task control snapshot and event continuation",
    description=(
        "Requires a user Bearer token. Missing and other users' Tasks return the same 404, "
        "including for admins. The Task, latest verified Run, committed event watermark, "
        "and continuation cursor are read from one control-database snapshot. Pass nextCursor "
        "to the Task events endpoint to read facts committed after coveredSequence. The cursor "
        "is scoped to this box, user, Task, and restore epoch; after a cursor failure, fetch "
        "a new snapshot. This control baseline omits messages, model text, tool payloads, "
        "history pages, and artifacts; it is not the complete events capability."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Task owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Task identifier"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Task control snapshot graph or storage unavailable",
        },
    },
)
async def task_control_snapshot(
    response: Response,
    task_id: UUID = Path(alias="taskID", description="Owned public Task UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> TaskControlSnapshot | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        snapshot = await server.agentcomputer_http_operations.run_thread(
            get_task_control_snapshot, server.services, user.id, task_id
        )
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Task control snapshot")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Task control snapshot is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="snapshotInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Task control snapshot storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if snapshot is None:
        return protocol_error_response("resourceNotFound", 404)
    return snapshot


@router.get(
    "/tasks/{taskID}/events",
    response_model=TaskEventListResponse,
    summary="Read an owned Task's durable control events in sequence",
    description=(
        "Requires a user Bearer token. Missing and other users' Tasks return the same 404, "
        "including for admins. Events are ordered by ascending Task sequence. The opaque "
        "nextCursor continues a fixed committed window while hasMore is true; once caught up, "
        "pass that same cursor on a later request to poll new events. Cursors are bound to the "
        "box, owner, Task, and current restore epoch; older epochs' stored events can be reread "
        "from the beginning, but old cursors fail. Each listed event is a safe summary of a "
        "persisted control fact, after its Run graph and sequence are validated. Internal event "
        "payloads, model text, tool arguments, results, paths, and credentials are omitted. "
        "Use the separate Run result endpoint for a verified final answer. Engine acceptance "
        "does not prove model generation; tool events are journal facts, not proof that a "
        "production tool executed. This read-only page is not the complete snapshot/event "
        "protocol advertised by the events capability."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Task owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Task, limit, or cursor"},
        503: {
            "model": ProtocolErrorEnvelope,
            "description": "Task event graph or storage unavailable",
        },
    },
)
async def task_events(
    response: Response,
    task_id: UUID = Path(alias="taskID", description="Owned public Task UUID."),
    limit: int = Query(default=50, ge=1, le=100, description="Page size, from 1 to 100."),
    cursor: str | None = Query(
        default=None, description="Opaque cursor returned by the previous page or poll."
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> TaskEventListResponse | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        listing = await server.agentcomputer_http_operations.run_thread(
            list_task_events, server.services, user.id, task_id, limit, cursor
        )
    except InvalidTaskEventCursor:
        return protocol_error_response("invalidRequest", 422)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Task event listing")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Task event graph is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="eventInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Task event storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if listing is None:
        return protocol_error_response("resourceNotFound", 404)
    return listing


@router.post(
    "/tasks/{taskID}/turns",
    status_code=201,
    response_model=CommandReceipt,
    summary="Register a new-message Turn when execution admission is available",
    description=(
        "Requires a user Bearer token and an existing owned Task. "
        "When text execution admission is unavailable, new commands return 503 "
        "runtimeUnavailable/executionUnavailable without registering a Turn or Run. "
        "Only newMessage is accepted by this schema; nonempty artifacts or selected skills "
        "remain unsupported. A successful admission returns the original durable queued receipt "
        "with status 201, including same-key replays. The server schedules an available background "
        "worker after registration; the receipt alone does not prove execution. "
        "Replays preserve original facts after capability changes; "
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
        receipt = await server.agentcomputer_http_operations.run_thread(operation)
        worker = server.agentcomputer_text_worker
        if worker is not None:
            worker.schedule(owner_id=user.id, run_id=receipt.runID)
        return receipt
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
        "An embedded server without the text opt-in skips this scan by default. "
        "Tightening does not request user cancellation, "
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
        run = await server.agentcomputer_http_operations.run_thread(operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
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


@router.get(
    "/runs/{runID}/result",
    response_model=RunResult,
    summary="Read an owned Run's verified final text",
    description=(
        "Requires a user Bearer token. Missing and other users' Runs return the same 404. "
        "Before verified success, this returns the current Run state with null answer fields. "
        "For a succeeded Run, the original control-plane completion receipt and fixed v2 history "
        "must agree on the final assistant ID and text digest. A missing or inconsistent archive "
        "returns 503 rather than an unverified answer. Internal thread IDs and host paths are omitted."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Run owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Run identifier"},
        503: {"model": ProtocolErrorEnvelope, "description": "Run or result evidence unavailable"},
    },
)
async def run_result(
    response: Response,
    run_id: UUID = Path(alias="runID", description="Public Run UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> RunResult | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(get_run_result, server.services, owner_id=user.id, run_id=run_id)
        result = await server.agentcomputer_http_operations.run_thread(operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Run result lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Run result evidence is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="runInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Run result storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if result is None:
        return protocol_error_response("resourceNotFound", 404)
    return result


@router.get(
    "/runs/{runID}/pause",
    response_model=RunPauseStatus | None,
    summary="Read the persisted pause status for one owned Run",
    description=(
        "Requires a user Bearer token. Returns null when the owned Run has no pause; "
        "missing and other users' Runs return the same 404, including for admins. "
        "Pending, adjudicated, and expired facts are validated with the original Run, "
        "dispatch, tool-effect, event, and cancellation journals in one transaction. "
        "A decision command ID identifies an immutable receipt; adjudicated does not "
        "authorize a tool, resume an engine, or prove execution. The response omits "
        "opaque action or question content and internal engine identities. "
        "This read-only endpoint does not dispatch, decide, repair, or resume anything."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No Run owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid Run identifier"},
        503: {"model": ProtocolErrorEnvelope, "description": "Pause graph or storage unavailable"},
    },
)
async def run_pause_status(
    response: Response,
    run_id: UUID = Path(alias="runID", description="Public Run UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> RunPauseStatus | JSONResponse | None:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(get_run_pause_status, server.services, owner_id=user.id, run_id=run_id)
        found, pause = await server.agentcomputer_http_operations.run_thread(operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during Run pause lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored Run pause graph is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="pauseInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer Run pause storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if not found:
        return protocol_error_response("resourceNotFound", 404)
    return pause


async def _pause_status_by_id(
    response: Response,
    server: OctopServer,
    user: User,
    pause_id: UUID,
    kind: Literal["approval", "question"],
) -> RunPauseStatus | JSONResponse:
    require_database(server)
    assert server.services is not None
    response.headers["Cache-Control"] = "no-store"
    try:
        operation = partial(
            get_pause_status, server.services, owner_id=user.id, pause_id=pause_id, kind=kind
        )
        pause = await server.agentcomputer_http_operations.run_thread(operation)
    except CommandAdmissionError as exc:
        return admission_error_response(exc)
    except IdentityError:
        logger.error("AgentComputer identity is invalid during pause lookup")
        return protocol_error_response("storageUnavailable", 503, reason="identityInvalid")
    except StorageDataError:
        logger.error("AgentComputer stored pause graph is invalid")
        return protocol_error_response("storageUnavailable", 503, reason="pauseInvalid")
    except (OSError, StorageUnavailableError):
        logger.error("AgentComputer pause storage is unavailable")
        return protocol_error_response("storageUnavailable", 503, retryable=True)
    if pause is None:
        return protocol_error_response("resourceNotFound", 404)
    return pause


@router.get(
    "/approvals/{approvalID}",
    response_model=RunPauseStatus,
    summary="Read one owned approval's persisted status",
    description=(
        "Requires a user Bearer token. Returns only the authenticated owner's persisted approval "
        "status; unknown IDs, questions, and other users' approvals all return the same 404, "
        "including for admins. The original Run and its dispatch, tool-effect, pause, event, and "
        "cancellation journals are validated in one transaction. The response omits the action "
        "payload and internal engine identities. A decided status records adjudication only; it "
        "does not authorize a tool, resume an engine, or prove execution. This endpoint does not "
        "submit a decision or change persisted facts."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No approval owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid approval identifier"},
        503: {"model": ProtocolErrorEnvelope, "description": "Pause graph or storage unavailable"},
    },
)
async def approval_status(
    response: Response,
    approval_id: UUID = Path(alias="approvalID", description="Public approval UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> RunPauseStatus | JSONResponse:
    return await _pause_status_by_id(response, server, user, approval_id, "approval")


@router.get(
    "/questions/{questionID}",
    response_model=RunPauseStatus,
    summary="Read one owned question's persisted status",
    description=(
        "Requires a user Bearer token. Returns only the authenticated owner's persisted question "
        "status; unknown IDs, approvals, and other users' questions all return the same 404, "
        "including for admins. The original Run and its dispatch, tool-effect, pause, event, and "
        "cancellation journals are validated in one transaction. The response omits the question "
        "payload and internal engine identities. A decided status records adjudication only; it "
        "does not answer the engine, resume a Run, or prove execution. This endpoint does not "
        "submit an answer or change persisted facts."
    ),
    responses={
        401: {"model": ProtocolErrorEnvelope, "description": "Authentication required or expired"},
        403: {"model": ProtocolErrorEnvelope, "description": "User access denied"},
        404: {"model": ProtocolErrorEnvelope, "description": "No question owned by this user"},
        422: {"model": ProtocolErrorEnvelope, "description": "Invalid question identifier"},
        503: {"model": ProtocolErrorEnvelope, "description": "Pause graph or storage unavailable"},
    },
)
async def question_status(
    response: Response,
    question_id: UUID = Path(alias="questionID", description="Public question UUID."),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> RunPauseStatus | JSONResponse:
    return await _pause_status_by_id(response, server, user, question_id, "question")


@router.post(
    "/runs/{runID}/cancel",
    status_code=201,
    response_model=CancelRunReceipt,
    summary="Record cancellation for one owned Run",
    description=(
        "Requires a user Bearer token and an explicit owned Run ID. The command fixes the "
        "current restore epoch and expected Run revision; same-key retries return the original "
        "durable receipt with replayed=true and status 201. A queued Run proven never to have "
        "begun dispatch, including an unstarted claim, can be stopped atomically. For a Run "
        "whose dispatch has started, status=cancelRequested only "
        "proves that the request was recorded and new execution handoffs are fenced; it does "
        "not prove that an engine or child process stopped. Read the Run for current state and "
        "the original command receipt for response-loss recovery. This endpoint never invokes "
        "a thread-wide stop or cancels another Run. After the durable request commits, it "
        "best-effort signals a matching local Run invocation; the receipt still does not "
        "prove the stream, remote model, or a child process stopped."
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
        services = server.services
        active_runs = server.agentcomputer_active_runs
        owner_id = user.id

        async def operation() -> CancelRunReceipt:
            receipt = await asyncio.to_thread(
                cancel_run, services, owner_id=owner_id, run_id=run_id, command=body
            )
            active_runs.signal_cancel(owner_id=owner_id, receipt=receipt)
            return receipt

        return await server.agentcomputer_http_operations.run_async(operation)
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
