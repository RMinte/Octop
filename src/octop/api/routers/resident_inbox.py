"""Authenticated resident conversation read watermarks."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response

from octop.api.deps import current_user, get_server
from octop.api.routers.resident_conversations import _runtime
from octop.infra.agents.resident_inbox import ResidentInbox, ResidentRead, inbox_state
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
_BASE = "/agents/{agent_id}/threads/{thread_id}/conversation"


@router.get(
    _BASE + "/inbox",
    response_model=ResidentInbox,
    summary="Read shared unread state for one private conversation",
)
async def inbox(
    agent_id: str,
    thread_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentInbox:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        inbox_state, _runtime(server).services, user.id, agent_id, thread_id
    )


@router.post(
    _BASE + "/read",
    response_model=ResidentInbox,
    summary="Advance the private conversation read watermark",
    description="Uses an actual message anchor in this conversation. Concurrent devices cannot move the watermark backwards or consume a later arriving message.",
)
async def mark_read(
    agent_id: str,
    thread_id: str,
    body: ResidentRead,
    response: Response,
    user: User = Depends(current_user),
    server: OctopServer = Depends(get_server),
) -> ResidentInbox:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        inbox_state, _runtime(server).services, user.id, agent_id, thread_id, body.message_id
    )
