"""Persistent, user-scoped proactive notification receipts."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Response

from octop.api.deps import current_user, get_server, require_database
from octop.infra.agents import resident_notifications
from octop.infra.db.services import SharedServices
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter()
_PATH = "/agents/{agent_id}/threads/{thread_id}/conversation/notifications"


def _services(server: OctopServer) -> SharedServices:
    require_database(server)
    assert server.services is not None
    return server.services


@router.get(
    _PATH,
    response_model=resident_notifications.ResidentNotificationPage,
    summary="Read durable follow-up notifications and scheduled-task receipts",
    description="Owner-scoped signed pagination. Includes delivered messages and authorized pending messages; cancelled or superseded pending authorizations are hidden. No model or external notification channel is invoked.",
)
async def list_notifications(
    agent_id: str,
    thread_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: str | None = Query(default=None, max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_notifications.ResidentNotificationPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_notifications.list_notifications,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        limit,
        page_cursor,
    )


@router.get(
    _PATH + "/{notification_id}",
    response_model=resident_notifications.ResidentNotification,
    summary="Read a single proactive notification receipt, including discarded state",
    description="Only this private conversation's owner can read this receipt. Discarded receipts are audit state and must not be displayed as a new notification.",
)
async def get_notification(
    agent_id: str,
    thread_id: str,
    notification_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> resident_notifications.ResidentNotification:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        resident_notifications.get_notification,
        _services(server),
        user.id,
        agent_id,
        thread_id,
        notification_id,
    )
