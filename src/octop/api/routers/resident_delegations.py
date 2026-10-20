"""Explicit delegation of resident-conversation work into normal project tasks."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Response

from octop.api.deps import current_user, get_server, require_database
from octop.infra.agents import resident_delegations
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
_PATH = "/agents/{agent_id}/threads/{thread_id}/conversation/delegations"


def _services(server: OctopServer) -> SharedServices:
    require_database(server)
    assert server.services is not None
    return server.services


@router.post(
    _PATH,
    response_model=resident_delegations.ResidentDelegation,
    status_code=202,
    summary="Explicitly delegate selected resident-conversation work to a new task",
    description="Requires the user's selected project_id (or explicit null), exact source turn, and complete task input. Task, first turn, and delegation receipt commit atomically. Reuse request_id after disconnecting. No model tool is enabled by this endpoint.",
)
async def create_delegation(
    agent_id: str,
    thread_id: str,
    body: resident_delegations.ResidentDelegationCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_delegations.ResidentDelegation:
    response.headers["Cache-Control"] = "no-store"
    worker = server.project_task_worker
    if worker is None or worker.closing:
        raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY)
    result = await asyncio.to_thread(
        resident_delegations.create_delegation,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        body,
    )
    worker.wake()
    return result


@router.get(
    _PATH,
    response_model=resident_delegations.ResidentDelegationPage,
    summary="List this private conversation's delegation receipts and captured results",
    description="Owner-scoped keyset pagination. Captured terminal results survive deletion of the original task; file references may then be unavailable.",
)
async def list_delegations(
    agent_id: str,
    thread_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: str | None = Query(default=None, max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_delegations.ResidentDelegationPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_delegations.list_delegations,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        limit,
        page_cursor,
    )


@router.get(
    _PATH + "/{delegation_id}",
    response_model=resident_delegations.ResidentDelegation,
    summary="Read one delegation and its immutable per-turn task results",
    description="Only the initial delegated turn and its explicit retries return results. Ordinary subsequent task messages do not become resident-conversation reports.",
)
async def get_delegation(
    agent_id: str,
    thread_id: str,
    delegation_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_delegations.ResidentDelegation:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_delegations.get_delegation,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        delegation_id,
    )
