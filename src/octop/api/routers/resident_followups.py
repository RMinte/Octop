"""User-authorized reminders and task watches for a private resident conversation."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Response

from octop.api.deps import current_user, get_server, require_database
from octop.infra.agentcomputer.bootstrap import settle_task
from octop.infra.agents import resident_followups
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
_PATH = "/agents/{agent_id}/threads/{thread_id}/conversation/followups"


def _services(server: OctopServer) -> SharedServices:
    require_database(server)
    assert server.services is not None
    return server.services


@router.post(
    _PATH,
    response_model=resident_followups.ResidentFollowup,
    status_code=201,
    summary="Explicitly authorize a reminder, task watch, or scheduled task",
    description="Nothing follows up by default. note/task_watch authorize conversation notifications only. task additionally requires an explicit task_create capability, complete TaskMessage, and selected project or explicit null; each due admission reuses normal task permissions and approvals. This endpoint stores authorization without executing work. Reuse request_id after disconnecting.",
)
async def create_followup(
    agent_id: str,
    thread_id: str,
    body: resident_followups.FollowupCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_followups.ResidentFollowup:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_followups.create_followup, _services(server), user.id, agent_id, thread_id, body
    )


@router.get(
    _PATH,
    response_model=resident_followups.ResidentFollowupPage,
    summary="List this conversation's explicit follow-up authorizations",
    description="Owner-scoped pagination includes enabled, paused, cancelled, and completed items. No authorization is created by reading this list.",
)
async def list_followups(
    agent_id: str,
    thread_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: str | None = Query(default=None, max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_followups.ResidentFollowupPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_followups.list_followups,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        limit,
        page_cursor,
    )


@router.get(
    _PATH + "/{followup_id}",
    response_model=resident_followups.ResidentFollowup,
    summary="Read one follow-up's current authorization and scheduling state",
)
async def get_followup(
    agent_id: str,
    thread_id: str,
    followup_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_followups.ResidentFollowup:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_followups.get_followup,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        followup_id,
    )


@router.put(
    _PATH + "/{followup_id}",
    response_model=resident_followups.ResidentFollowup,
    summary="Replace or pause a follow-up with an explicit current revision",
    description="Provide the complete replacement authorization and expected_revision. Updates invalidate older unsent notifications. Cancelled or successfully completed items cannot be reactivated. A task schedule stopped by admission_failed may be explicitly reauthorized with this full replacement request.",
)
async def update_followup(
    agent_id: str,
    thread_id: str,
    followup_id: str,
    body: resident_followups.FollowupUpdate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_followups.ResidentFollowup:
    response.headers["Cache-Control"] = "no-store"
    runtime = server.app_runtime.resident_conversations if server.app_runtime else None
    if runtime is None:
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED)
    async with runtime.followup_scope(followup_id):
        operation = asyncio.create_task(
            asyncio.to_thread(
                resident_followups.update_followup,
                _services(server),
                user.id,
                agent_id,
                thread_id,
                followup_id,
                body,
            )
        )
        result, cancelled = await settle_task(operation)
        if cancelled:
            raise asyncio.CancelledError
        return result


@router.post(
    _PATH + "/{followup_id}/cancel",
    response_model=resident_followups.ResidentFollowup,
    summary="Cancel a follow-up and invalidate its unsent older notifications",
    description="Requires expected_revision. Repeating the same successful cancellation is safe. A stale cancellation cannot overwrite a later authorization. Cancelling a task schedule stops future admissions; already accepted tasks continue under their normal task controls.",
)
async def cancel_followup(
    agent_id: str,
    thread_id: str,
    followup_id: str,
    body: resident_followups.FollowupCancel,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_followups.ResidentFollowup:
    response.headers["Cache-Control"] = "no-store"
    runtime = server.app_runtime.resident_conversations if server.app_runtime else None
    if runtime is None:
        raise OctopError.localized(ErrorCode.SETUP_REQUIRED)
    async with runtime.followup_scope(followup_id):
        operation = asyncio.create_task(
            asyncio.to_thread(
                resident_followups.cancel_followup,
                _services(server),
                user.id,
                agent_id,
                thread_id,
                followup_id,
                body,
            )
        )
        result, cancelled = await settle_task(operation)
        if cancelled:
            raise asyncio.CancelledError
        return result
