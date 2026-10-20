"""Authenticated private Agent identity and durable text-conversation receipts."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Request, Response

from octop.api.deps import current_user, get_server
from octop.infra.agents.resident_conversation import (
    ResidentBinding,
    ResidentCancel,
    ResidentCapabilities,
    ResidentConversationRuntime,
    ResidentMessage,
    ResidentSnapshot,
    ResidentTurn,
    binding_view,
    turn_view,
)
from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User
from octop.infra.utils.locale import resolve_request_locale

router = APIRouter()
_THREAD = "/agents/{agent_id}/threads/{thread_id}"


def _runtime(server: OctopServer) -> ResidentConversationRuntime:
    if server.app_runtime is None or server.app_runtime.resident_conversations is None:
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED)
    return server.app_runtime.resident_conversations


@router.get(
    "/agents/resident-capabilities",
    response_model=ResidentCapabilities,
    summary="Read implemented private assistant capabilities for client integration",
    description="Authentication required. Reading capabilities does not create an Agent, a conversation or an authorization.",
)
async def capabilities(
    response: Response,
    user: User = Depends(current_user),
) -> ResidentCapabilities:
    response.headers["Cache-Control"] = "no-store"
    return ResidentCapabilities()


@router.get(
    "/agents/resident-conversation",
    response_model=ResidentBinding,
    summary="Read the current user's private resident Agent binding",
)
async def get_binding(
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentBinding:
    response.headers["Cache-Control"] = "no-store"
    runtime = _runtime(server)
    return await asyncio.to_thread(binding_view, runtime.services, user.id)


@router.put(
    "/agents/resident-conversation",
    response_model=ResidentBinding,
    summary="Ensure one stable private Agent and conversation for the current user",
    description="Idempotent initialization. Never reuses another expert or resets an existing thread.",
)
async def ensure_binding(
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentBinding:
    response.headers["Cache-Control"] = "no-store"
    runtime = _runtime(server)
    await ensure_resident_identity(runtime.services, runtime.registry, user.id)
    return await asyncio.to_thread(binding_view, runtime.services, user.id)


@router.get(
    _THREAD + "/conversation",
    response_model=ResidentSnapshot,
    summary="Read a bounded resident conversation snapshot",
    description="Includes partial replies and durable status. Poll after reconnecting; no execution is started.",
)
async def conversation(
    agent_id: str,
    thread_id: str,
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentSnapshot:
    response.headers["Cache-Control"] = "no-store"
    return await _runtime(server).snapshot(
        user.id, agent_id, thread_id, limit, user.locale or resolve_request_locale(request)
    )


@router.post(
    _THREAD + "/messages",
    response_model=ResidentTurn,
    status_code=202,
    summary="Durably accept a plain-text turn for the resident conversation",
    description="Reuse request_id after a lost response. Disconnecting does not cancel execution; changed input conflicts.",
)
async def send_message(
    agent_id: str,
    thread_id: str,
    body: ResidentMessage,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentTurn:
    response.headers["Cache-Control"] = "no-store"
    row = await _runtime(server).send(user.id, agent_id, thread_id, body)
    return turn_view(row, user.locale or resolve_request_locale(request))


@router.get(
    _THREAD + "/requests/{request_id}",
    response_model=ResidentTurn,
    summary="Look up a resident turn by its original send request ID",
)
async def request_status(
    agent_id: str,
    thread_id: str,
    request_id: str,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentTurn:
    response.headers["Cache-Control"] = "no-store"
    row = await _runtime(server).request(user.id, agent_id, thread_id, request_id)
    return turn_view(row, user.locale or resolve_request_locale(request))


@router.post(
    _THREAD + "/cancel",
    response_model=ResidentTurn,
    summary="Cancel one exact resident turn with a durable cancellation receipt",
    description="A repeated cancellation for an old turn never stops a newer turn.",
)
async def cancel_turn(
    agent_id: str,
    thread_id: str,
    body: ResidentCancel,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentTurn:
    response.headers["Cache-Control"] = "no-store"
    row = await _runtime(server).cancel(user.id, agent_id, thread_id, body)
    return turn_view(row, user.locale or resolve_request_locale(request))
