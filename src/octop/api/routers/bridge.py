"""HTTP API for Bridge connection management + inbound WS."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from starlette.websockets import WebSocketState

from octop.api.agentcomputer import ProtocolErrorEnvelope, protocol_error_response
from octop.api.deps import (
    current_bearer_user,
    current_user,
    extract_raw_token,
    get_server,
    resolve_user_from_token,
)
from octop.api.routers.agentcomputer_documents import (
    DocumentImport,
    ProjectDocument,
    ProjectDocumentList,
)
from octop.infra.agentcomputer.cancellations import CancelRunReceipt
from octop.infra.agentcomputer.capabilities import AgentComputerCapabilitiesResponse
from octop.infra.agentcomputer.commands import CommandAdmissionError
from octop.infra.agentcomputer.models import (
    CommandReceipt,
    NewMessageCommand,
    ProjectCreateCommand,
    ProjectCreateReceipt,
    ProjectListResponse,
    ProjectView,
    RunPauseDecisionReceipt,
    RunView,
    TaskCreateCommand,
    TaskCreateReceipt,
    TaskListResponse,
    TaskView,
)
from octop.infra.agentcomputer.project_document_tool import (
    MAX_DOCUMENT_BYTES,
    validate_document_filename,
)
from octop.infra.agentcomputer.results import RunResult
from octop.infra.agentcomputer.turn_listing import TurnListResponse
from octop.infra.errors import ErrorCode, OctopError

logger = logging.getLogger(__name__)

router = APIRouter()

_SAFE_AGENTCOMPUTER_REASONS = frozenset(
    {
        "cancellationInvalid",
        "eventInvalid",
        "executionUnavailable",
        "identityInvalid",
        "pauseInvalid",
        "permissionModeUnsupported",
        "preparationIncomplete",
        "projectInvalid",
        "receiptInvalid",
        "runInvalid",
        "runtimeNotReady",
        "setupRequired",
        "snapshotInvalid",
        "submissionInvalid",
        "taskInvalid",
        "turnInvalid",
        "userMessageAlreadyRegistered",
    }
)


class BridgeCreateBody(BaseModel):
    peer_base_url: str = Field(..., description="Remote Octop base URL, e.g. https://cloud.example")
    peer_username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Unique display name for chat group switching",
    )
    notes: str | None = Field(default=None, max_length=500, description="Optional notes")
    icon_name: str | None = Field(
        default=None,
        max_length=64,
        description="Optional Lucide icon key shown on remote expert badges",
    )
    connect: bool = Field(default=True, description="Dial Bridge WS immediately after login")


class BridgeProbeBody(BaseModel):
    peer_base_url: str = Field(..., description="Remote Octop base URL")
    peer_username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class BridgeConnectionProbeBody(BaseModel):
    peer_base_url: str = Field(..., description="Remote Octop base URL")
    peer_username: str = Field(..., min_length=1)
    password: str | None = Field(
        default=None,
        description="Override password; omit or empty to use the stored secret",
    )


def _bridge(server: Any) -> Any:
    rt = server.app_runtime
    if rt is None or getattr(rt, "bridge_manager", None) is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "bridge not ready")
    return rt.bridge_manager


@router.post("/bridge/probe", summary="Probe a remote Octop (login + list experts)")
async def probe_peer(
    body: BridgeProbeBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Validate remote credentials and return that account's expert list.

    Does not create a bridge connection or open the Bridge WebSocket.
    """
    _ = user
    mgr = _bridge(server)
    return cast(
        dict[str, Any],
        await mgr.probe_peer(
            peer_base_url=body.peer_base_url,
            peer_username=body.peer_username,
            password=body.password,
        ),
    )


@router.post(
    "/bridge/connections/{connection_id}/probe",
    summary="Probe a saved connection (stored password if omitted)",
)
async def probe_connection(
    connection_id: str,
    body: BridgeConnectionProbeBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Login with the form URL/user; reuse the stored password when the field is blank."""
    mgr = _bridge(server)
    return cast(
        dict[str, Any],
        await mgr.probe_owned_connection(
            connection_id,
            owner_user_id=user.id,
            peer_base_url=body.peer_base_url,
            peer_username=body.peer_username,
            password=body.password,
        ),
    )


@router.get("/bridge/connections", summary="List bridge connections")
async def list_connections(
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    mgr = _bridge(server)
    rows = mgr.list_connections(user.id)
    return [cast(dict[str, Any], mgr.connection_public(r)) for r in rows]


@router.post("/bridge/connections", status_code=201, summary="Add a bridge connection")
async def create_connection(
    body: BridgeCreateBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    mgr = _bridge(server)
    row = await mgr.create_connection(
        owner_user_id=user.id,
        peer_base_url=body.peer_base_url,
        peer_username=body.peer_username,
        password=body.password,
        display_name=body.display_name,
        notes=body.notes,
        icon_name=body.icon_name,
        connect=body.connect,
    )
    return cast(dict[str, Any], mgr.connection_public(row))


@router.get("/bridge/connections/{connection_id}", summary="Get a bridge connection")
async def get_connection(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    mgr = _bridge(server)
    row = mgr.get_owned(connection_id, user.id)
    return cast(dict[str, Any], mgr.connection_public(row))


@router.post(
    "/bridge/connections/{connection_id}/connect",
    summary="Connect / reconnect Bridge WS",
)
async def connect_connection(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    mgr = _bridge(server)
    row = await mgr.connect(connection_id, owner_user_id=user.id)
    return cast(dict[str, Any], mgr.connection_public(row))


@router.post(
    "/bridge/connections/{connection_id}/disconnect",
    summary="Disconnect Bridge WS",
)
async def disconnect_connection(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    mgr = _bridge(server)
    mgr.get_owned(connection_id, user.id)
    await mgr.disconnect(connection_id)
    row = mgr.get_owned(connection_id, user.id)
    return cast(dict[str, Any], mgr.connection_public(row))


class BridgePatchBody(BaseModel):
    auto_reconnect: bool | None = Field(
        default=None,
        description="When true, dial again after unexpected disconnect (default on)",
    )
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Unique display name for chat group switching",
    )
    notes: str | None = Field(
        default=None,
        max_length=500,
        description="Optional notes; send empty string to clear",
    )
    icon_name: str | None = Field(
        default=None,
        max_length=64,
        description="Lucide icon key; send empty string to clear",
    )
    peer_base_url: str | None = Field(
        default=None,
        min_length=1,
        description="Remote Octop base URL (re-login when changed)",
    )
    peer_username: str | None = Field(
        default=None,
        min_length=1,
        description="Remote username (re-login when changed)",
    )
    password: str | None = Field(
        default=None,
        description="New remote password; omit or empty to keep the stored secret",
    )


@router.patch(
    "/bridge/connections/{connection_id}",
    summary="Update bridge connection settings",
)
async def patch_connection(
    connection_id: str,
    body: BridgePatchBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    mgr = _bridge(server)
    fields_set = body.model_fields_set
    row = None
    meta_keys = {
        "display_name",
        "notes",
        "icon_name",
        "peer_base_url",
        "peer_username",
        "password",
    }
    if fields_set & meta_keys:
        pwd = body.password if "password" in fields_set else None
        row = await mgr.update_connection_meta(
            connection_id,
            owner_user_id=user.id,
            display_name=body.display_name,
            notes=body.notes,
            update_notes="notes" in fields_set,
            icon_name=body.icon_name,
            update_icon="icon_name" in fields_set,
            peer_base_url=body.peer_base_url if "peer_base_url" in fields_set else None,
            peer_username=body.peer_username if "peer_username" in fields_set else None,
            password=pwd,
        )
    if body.auto_reconnect is not None:
        row = await mgr.set_auto_reconnect(
            connection_id, owner_user_id=user.id, enabled=bool(body.auto_reconnect)
        )
    if row is None:
        row = mgr.get_owned(connection_id, user.id)
    return cast(dict[str, Any], mgr.connection_public(row))


@router.delete(
    "/bridge/connections/{connection_id}",
    status_code=204,
    summary="Delete a bridge connection",
)
async def delete_connection(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> None:
    mgr = _bridge(server)
    await mgr.delete_connection(connection_id, owner_user_id=user.id)


@router.get(
    "/bridge/connections/{connection_id}/agents",
    summary="List remote agents via bridge",
)
async def list_remote_agents(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    mgr = _bridge(server)
    agents = await mgr.list_remote_agents(connection_id, owner_user_id=user.id)
    return [cast(dict[str, Any], item) for item in agents]


@router.get(
    "/bridge/connections/{connection_id}/providers/resolved",
    summary="List peer resolved models via bridge tunnel",
)
async def list_remote_resolved_models(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    """Read-only: peer ``GET /api/providers/resolved`` for remote chat model picker."""
    mgr = _bridge(server)
    models = await mgr.list_remote_resolved_models(connection_id, owner_user_id=user.id)
    return [cast(dict[str, Any], item) for item in models]


@router.get(
    "/bridge/connections/{connection_id}/providers/active-model",
    summary="Get peer active model via bridge tunnel",
)
async def get_remote_active_model(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Read-only: peer ``GET /api/providers/active-model``."""
    mgr = _bridge(server)
    return cast(
        dict[str, Any],
        await mgr.get_remote_active_model(connection_id, owner_user_id=user.id),
    )


@router.get(
    "/bridge/connections/{connection_id}/knowledge-bases",
    summary="List peer knowledge bases via bridge tunnel",
)
async def list_remote_knowledge_bases(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    """Read-only: peer ``GET /api/knowledge-bases`` for remote chat KB picker."""
    mgr = _bridge(server)
    bases = await mgr.list_remote_knowledge_bases(connection_id, owner_user_id=user.id)
    return [cast(dict[str, Any], item) for item in bases]


@router.get(
    "/bridge/connections/{connection_id}/knowledge-bases/capability",
    summary="Get peer knowledge capability via bridge tunnel",
)
async def get_remote_knowledge_capability(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Read-only: peer ``GET /api/knowledge-bases/capability``."""
    mgr = _bridge(server)
    return cast(
        dict[str, Any],
        await mgr.get_remote_knowledge_capability(connection_id, owner_user_id=user.id),
    )


@router.get(
    "/bridge/connections/{connection_id}/browser/env-status",
    summary="Get peer browser env status via bridge tunnel",
)
async def get_remote_browser_env_status(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Read-only: peer ``GET /api/browser/env-status`` for remote chat dock."""
    mgr = _bridge(server)
    return cast(
        dict[str, Any],
        await mgr.get_remote_browser_env_status(connection_id, owner_user_id=user.id),
    )


@router.get(
    "/bridge/connections/{connection_id}/browser/harness-sessions",
    summary="List peer browser harness sessions via bridge tunnel",
)
async def get_remote_browser_sessions(
    connection_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Read-only: peer ``GET /api/browser/harness-sessions``."""
    mgr = _bridge(server)
    return cast(
        dict[str, Any],
        await mgr.get_remote_browser_sessions(connection_id, owner_user_id=user.id),
    )


class BridgeBrowserHandoffBody(BaseModel):
    target: str = Field(..., description="'agent' or 'user'")
    reason: str = Field(default="", description="Optional handoff reason")


@router.post(
    "/bridge/connections/{connection_id}/browser/sessions/{session_id}/handoff",
    summary="Handoff peer browser control via bridge tunnel",
)
async def post_remote_browser_handoff(
    connection_id: str,
    session_id: str,
    body: BridgeBrowserHandoffBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Proxy peer ``POST /api/browser/sessions/{id}/handoff``."""
    mgr = _bridge(server)
    return cast(
        dict[str, Any],
        await mgr.post_remote_browser_handoff(
            connection_id,
            owner_user_id=user.id,
            session_id=session_id,
            body=body.model_dump(),
        ),
    )


async def _bridge_bearer_user(
    request: Request,
    authorization: str | None = Header(default=None),
    user: Any = Depends(current_bearer_user),
) -> Any:
    # The ordinary Bridge routes allow the dashboard's legacy URL token. The
    # AgentComputer protocol requires a header-only user session on both hops.
    if "access_token" in request.query_params or not extract_raw_token(authorization=authorization):
        raise OctopError(ErrorCode.AUTH_FAILED, "Bearer token required")
    return user


async def _agentcomputer_tunnel(
    request: Request,
    *,
    connection_id: str,
    remote_path: str,
    response_type: Any,
    user: Any,
    server: Any,
) -> JSONResponse:
    """Relay one explicit protocol route, returning only typed JSON, never peer headers."""
    has_json_body = request.method in {"POST", "PUT"}
    try:
        peer = await _bridge(server).tunnel_http(
            connection_id=connection_id,
            owner_user_id=user.id,
            method=request.method,
            path=remote_path,
            query=request.url.query,
            headers={"content-type": "application/json"} if has_json_body else None,
            body=await request.body() if has_json_body else None,
        )
    except OctopError as exc:
        if exc.code == ErrorCode.BRIDGE_TUNNEL_FAILED:
            raise OctopError(ErrorCode.BRIDGE_TUNNEL_FAILED, "peer request failed") from exc
        raise
    if 200 <= peer.status_code < 300:
        try:
            adapter = TypeAdapter(response_type)
            safe_payload = adapter.dump_python(adapter.validate_python(peer.json()), mode="json")
        except (ValueError, ValidationError) as exc:
            raise OctopError(
                ErrorCode.BRIDGE_TUNNEL_FAILED, "peer returned an invalid AgentComputer response"
            ) from exc
    else:
        try:
            envelope = ProtocolErrorEnvelope.model_validate(peer.json())
            safe_payload = envelope.model_dump(mode="json", exclude_none=True)
            if envelope.error.reasonCode not in _SAFE_AGENTCOMPUTER_REASONS:
                safe_payload["error"].pop("reasonCode", None)
        except (ValueError, ValidationError):
            # Older peers may return a generic 404/503 body. Keep the status,
            # but do not reflect arbitrary peer text or response headers.
            safe_payload = {
                "error": {
                    "code": "resourceNotFound" if peer.status_code == 404 else "internalError",
                    "retryable": peer.status_code >= 500,
                }
            }
    return JSONResponse(
        status_code=peer.status_code,
        content=safe_payload,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/capabilities",
    response_model=AgentComputerCapabilitiesResponse,
    summary="Read peer AgentComputer capabilities via Bridge",
)
async def bridge_agentcomputer_capabilities(
    request: Request,
    connection_id: str,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path="/api/agentcomputer/v1/capabilities",
        response_type=AgentComputerCapabilitiesResponse,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects",
    response_model=ProjectListResponse,
    summary="List peer AgentComputer projects via Bridge",
)
async def bridge_agentcomputer_projects(
    request: Request,
    connection_id: str,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path="/api/agentcomputer/v1/projects",
        response_type=ProjectListResponse,
        user=user,
        server=server,
    )


@router.post(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects",
    status_code=201,
    response_model=ProjectCreateReceipt,
    summary="Create a peer AgentComputer project via Bridge",
)
async def bridge_agentcomputer_create_project(
    request: Request,
    connection_id: str,
    body: ProjectCreateCommand,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    _ = body
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path="/api/agentcomputer/v1/projects",
        response_type=ProjectCreateReceipt,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects/{project_id}",
    response_model=ProjectView,
    summary="Read a peer AgentComputer project via Bridge",
)
async def bridge_agentcomputer_project(
    request: Request,
    connection_id: str,
    project_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/projects/{project_id}",
        response_type=ProjectView,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects/{project_id}/documents",
    response_model=ProjectDocumentList,
    summary="List a peer AgentComputer project's text documents via Bridge",
    description="Reads only eligible .md and .txt filenames from the connection owner's peer Project.",
)
async def bridge_agentcomputer_documents(
    request: Request,
    connection_id: str,
    project_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/projects/{project_id}/documents",
        response_type=ProjectDocumentList,
        user=user,
        server=server,
    )


def _project_document_path(project_id: UUID, filename: str) -> str:
    validate_document_filename(filename)
    return f"/api/agentcomputer/v1/projects/{project_id}/documents/{filename}"


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects/{project_id}/documents/{filename}",
    response_model=ProjectDocument,
    summary="Read a peer AgentComputer project text document via Bridge",
    description="Reads one bounded .md or .txt document owned by the peer connection's user.",
)
async def bridge_agentcomputer_document(
    request: Request,
    connection_id: str,
    project_id: UUID,
    filename: str = Path(description="Single .md or .txt filename under documents/."),
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    try:
        remote_path = _project_document_path(project_id, filename)
    except CommandAdmissionError:
        return protocol_error_response("invalidRequest", 422)
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=remote_path,
        response_type=ProjectDocument,
        user=user,
        server=server,
    )


@router.put(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects/{project_id}/documents/{filename}",
    status_code=201,
    response_model=ProjectDocument,
    summary="Create a peer AgentComputer project text document via Bridge",
    description=(
        "Creates one UTF-8 .md or .txt file in the connection owner's peer Project. "
        "The file must not already exist; content is limited to 16384 UTF-8 bytes."
    ),
)
async def bridge_agentcomputer_create_document(
    request: Request,
    connection_id: str,
    project_id: UUID,
    body: DocumentImport,
    filename: str = Path(description="New single .md or .txt filename under documents/."),
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    try:
        remote_path = _project_document_path(project_id, filename)
    except CommandAdmissionError:
        return protocol_error_response("invalidRequest", 422)
    try:
        content_bytes = body.content.encode("utf-8")
    except UnicodeError:
        return protocol_error_response("invalidRequest", 422)
    if len(content_bytes) > MAX_DOCUMENT_BYTES:
        return protocol_error_response("payloadLimitExceeded", 413)
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=remote_path,
        response_type=ProjectDocument,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects/{project_id}/tasks",
    response_model=TaskListResponse,
    summary="List Tasks in a peer AgentComputer project via Bridge",
)
async def bridge_agentcomputer_tasks(
    request: Request,
    connection_id: str,
    project_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/projects/{project_id}/tasks",
        response_type=TaskListResponse,
        user=user,
        server=server,
    )


@router.post(
    "/bridge/connections/{connection_id}/agentcomputer/v1/projects/{project_id}/tasks",
    status_code=201,
    response_model=TaskCreateReceipt,
    summary="Create a Task in a peer AgentComputer project via Bridge",
)
async def bridge_agentcomputer_create_task(
    request: Request,
    connection_id: str,
    project_id: UUID,
    body: TaskCreateCommand,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    _ = body
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/projects/{project_id}/tasks",
        response_type=TaskCreateReceipt,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/tasks/{task_id}",
    response_model=TaskView,
    summary="Read a peer AgentComputer Task via Bridge",
)
async def bridge_agentcomputer_task(
    request: Request,
    connection_id: str,
    task_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/tasks/{task_id}",
        response_type=TaskView,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/tasks/{task_id}/turns",
    response_model=TurnListResponse,
    summary="List peer AgentComputer Task conversation turns via Bridge",
)
async def bridge_agentcomputer_turns(
    request: Request,
    connection_id: str,
    task_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/tasks/{task_id}/turns",
        response_type=TurnListResponse,
        user=user,
        server=server,
    )


@router.post(
    "/bridge/connections/{connection_id}/agentcomputer/v1/tasks/{task_id}/turns",
    status_code=201,
    response_model=CommandReceipt,
    summary="Submit a peer AgentComputer Task message via Bridge",
)
async def bridge_agentcomputer_new_turn(
    request: Request,
    connection_id: str,
    task_id: UUID,
    body: NewMessageCommand,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    _ = body
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/tasks/{task_id}/turns",
        response_type=CommandReceipt,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/runs/{run_id}",
    response_model=RunView,
    summary="Read a peer AgentComputer Run via Bridge",
)
async def bridge_agentcomputer_run(
    request: Request,
    connection_id: str,
    run_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/runs/{run_id}",
        response_type=RunView,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/runs/{run_id}/result",
    response_model=RunResult,
    summary="Read a peer AgentComputer Run result via Bridge",
)
async def bridge_agentcomputer_run_result(
    request: Request,
    connection_id: str,
    run_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/runs/{run_id}/result",
        response_type=RunResult,
        user=user,
        server=server,
    )


@router.get(
    "/bridge/connections/{connection_id}/agentcomputer/v1/commands/{command_id}",
    response_model=(
        CommandReceipt
        | ProjectCreateReceipt
        | TaskCreateReceipt
        | CancelRunReceipt
        | RunPauseDecisionReceipt
    ),
    summary="Read an immutable peer AgentComputer command receipt via Bridge",
)
async def bridge_agentcomputer_command_receipt(
    request: Request,
    connection_id: str,
    command_id: UUID,
    user: Any = Depends(_bridge_bearer_user),
    server: Any = Depends(get_server),
) -> JSONResponse:
    return await _agentcomputer_tunnel(
        request,
        connection_id=connection_id,
        remote_path=f"/api/agentcomputer/v1/commands/{command_id}",
        response_type=(
            CommandReceipt
            | ProjectCreateReceipt
            | TaskCreateReceipt
            | CancelRunReceipt
            | RunPauseDecisionReceipt
        ),
        user=user,
        server=server,
    )


@router.websocket("/bridge/connections/{connection_id}/browser-stream/ws")
async def bridge_browser_stream_ws(
    websocket: WebSocket,
    connection_id: str,
) -> None:
    """Dashboard screencast WS relayed to the peer browser harness."""
    server = websocket.app.state.octop_server
    raw_token = websocket.query_params.get("token")
    if not raw_token:
        await websocket.close(code=4001, reason="missing token")
        return
    try:
        user = resolve_user_from_token(server, raw_token)
    except OctopError as exc:
        await websocket.close(code=4001, reason=f"auth: {exc.code.value}")
        return

    mgr = getattr(getattr(server, "app_runtime", None), "bridge_manager", None)
    if mgr is None:
        await websocket.close(code=1011, reason="bridge not ready")
        return
    try:
        mgr.get_owned(connection_id, user.id)
        mgr.require_session(connection_id)
    except OctopError as exc:
        await websocket.close(code=1011, reason=str(exc.code.value))
        return

    listen_only = websocket.query_params.get("listen_only", "0") in {"1", "true", "True"}
    try:
        width = int(websocket.query_params.get("width") or 1280)
        height = int(websocket.query_params.get("height") or 800)
    except ValueError:
        width, height = 1280, 800

    await websocket.accept()
    client_messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def send_frame(frame: dict[str, Any]) -> None:
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        await websocket.send_text(json.dumps(frame, ensure_ascii=False))

    async def pump_client() -> None:
        try:
            while websocket.application_state == WebSocketState.CONNECTED:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    await client_messages.put(payload)
                    if payload.get("type") == "stop":
                        break
        except WebSocketDisconnect:
            pass
        finally:
            await client_messages.put(None)

    pump_task = asyncio.create_task(pump_client())
    try:
        await mgr.relay_browser_stream(
            connection_id=connection_id,
            owner_user_id=user.id,
            listen_only=listen_only,
            width=width,
            height=height,
            on_frame=send_frame,
            client_messages=client_messages,
        )
    except OctopError as exc:
        await send_frame({"type": "error", "message": str(exc)})
        await send_frame({"type": "status", "status": "error"})
    except Exception:
        logger.exception("bridge browser stream relay failed")
        await send_frame({"type": "error", "message": "bridge browser failed"})
        await send_frame({"type": "status", "status": "error"})
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
        if websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


@router.websocket("/bridge/ws")
async def bridge_inbound_ws(
    websocket: WebSocket,
) -> None:
    """Peer Octop dials in; JWT is the peer user's token on this instance."""
    server = websocket.app.state.octop_server
    raw = websocket.query_params.get("token")
    if not raw:
        await websocket.close(code=4001, reason="missing token")
        return
    try:
        user = resolve_user_from_token(server, raw)
    except OctopError as exc:
        await websocket.close(code=4001, reason=f"auth: {exc.code.value}")
        return
    mgr = getattr(getattr(server, "app_runtime", None), "bridge_manager", None)
    if mgr is None:
        await websocket.close(code=1011, reason="bridge not ready")
        return
    await websocket.accept()
    try:
        await mgr.accept_inbound(websocket=websocket, user=user)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("bridge inbound ws error")
