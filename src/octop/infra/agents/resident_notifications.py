"""Durable, model-free proactive messages and owner-scoped delivery receipts."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Literal, cast

from langchain_core.messages import AIMessage, message_to_dict, messages_from_dict
from pydantic import Field, JsonValue

from octop.infra.agentcomputer.bootstrap import settle_task
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.resident_identity import resident_execution_scope
from octop.infra.db.repos.resident_notifications import ResidentNotificationRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.history.projection import message_inputs
from octop.infra.history.service import HistoryArchive
from octop.infra.projects.cursors import cursor_codec
from octop.infra.projects.models import ProjectModel


class ResidentNotification(ProjectModel):
    notification_id: str
    message_id: str = Field(description="历史与上下文消息的稳定标识，补投时保持不变。")
    followup_id: str
    authorization_version: int
    kind: Literal["note", "task_watch", "task"]
    text: str
    payload: dict[str, JsonValue]
    day_key: str
    timezone: str
    created_at: datetime
    updated_at: datetime
    status: Literal["pending", "delivered", "discarded"]
    delivery_error: str | None


class ResidentNotificationPage(ProjectModel):
    items: list[ResidentNotification]
    next_page_cursor: str | None


def _view(row: ResidentNotificationRow) -> ResidentNotification:
    return ResidentNotification(
        notification_id=row.notification_id,
        message_id=row.message_id,
        followup_id=row.followup_id,
        authorization_version=row.authorization_version,
        kind=cast(Literal["note", "task_watch", "task"], row.kind),
        text=row.text,
        payload=json.loads(row.payload_json),
        day_key=row.day_key,
        timezone=row.timezone,
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
        updated_at=datetime.fromtimestamp(row.updated_at / 1000, UTC),
        status=cast(Literal["pending", "delivered", "discarded"], row.status),
        delivery_error=row.delivery_error or None,
    )


def list_notifications(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    limit: int = 50,
    page_cursor: str | None = None,
) -> ResidentNotificationPage:
    from octop.infra.agents.resident_history import require_resident_binding

    require_resident_binding(services, owner_id, agent_id, thread_id)
    codec = cursor_codec(services, owner_id, "resident_notifications:" + thread_id)
    boundary: tuple[int, str] | None = None
    if page_cursor:
        cursor = codec.decode(page_cursor, "history")
        boundary = cursor.updated_at, cursor.after
    with services.db.read_snapshot() as conn:
        rows = services.resident_notification_repo.page(
            conn, owner_id, thread_id, limit + 1, boundary
        )
    more = len(rows) > limit
    rows = rows[:limit]
    return ResidentNotificationPage(
        items=[_view(row) for row in rows],
        next_page_cursor=(
            codec.encode("history", updated_at=rows[-1].created_at, after=rows[-1].notification_id)
            if more
            else None
        ),
    )


def get_notification(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    notification_id: str,
) -> ResidentNotification:
    from octop.infra.agents.resident_history import require_resident_binding

    require_resident_binding(services, owner_id, agent_id, thread_id)
    row = services.resident_notification_repo.get(owner_id, notification_id)
    if row is None or row.thread_id != thread_id or row.agent_id != agent_id:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return _view(row)


class ResidentNotificationDelivery:
    """Caller holds the runtime's thread lock and follow-up authorization scope."""

    def __init__(
        self,
        services: SharedServices,
        registry: AgentManager,
        history_archive: HistoryArchive | None,
    ) -> None:
        self.services, self.registry, self.archive = services, registry, history_archive
        self.task_backed = False

    async def deliver(self, notification: ResidentNotificationRow) -> None:
        # Executor writes continue after cancellation. Keep both caller-held locks
        # until the complete admitted delivery has drained.
        operation = asyncio.create_task(self._deliver(notification))
        _, cancelled = await settle_task(operation)
        if cancelled:
            raise asyncio.CancelledError

    async def _deliver(self, notification: ResidentNotificationRow) -> None:
        repo = self.services.resident_notification_repo
        row = await asyncio.to_thread(repo.get, notification.owner_id, notification.notification_id)
        if row is None or row.status != "pending":
            return
        if not await asyncio.to_thread(repo.is_authorized, row):
            await asyncio.to_thread(repo.discard, row.notification_id, time.time_ns() // 1_000_000)
            return
        binding = await asyncio.to_thread(
            self.services.resident_conversation_repo.get_by_thread,
            row.thread_id,
        )
        if (
            binding is None
            or binding.owner_id != row.owner_id
            or binding.agent_id != row.agent_id
            or binding.status != "ready"
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        message = AIMessage(
            id=row.message_id,
            content=row.text,
            additional_kwargs={
                "checkpoint_ts": row.created_at,
                "source": "resident_followup",
                "resident_notification": {
                    "notification_id": row.notification_id,
                    "followup_id": row.followup_id,
                    "authorization_version": row.authorization_version,
                    "kind": row.kind,
                    "payload": json.loads(row.payload_json),
                },
            },
        )
        frozen = await asyncio.to_thread(
            repo.prepare_message,
            row.notification_id,
            json.dumps(message_to_dict(message), ensure_ascii=False),
        )
        message = cast(AIMessage, messages_from_dict([json.loads(frozen)])[0])
        if self.task_backed:
            from octop.infra.projects.resident_tasks import append_message

            await append_message(self.services, row.owner_id, row.thread_id, message)
            now = time.time_ns() // 1_000_000
            await asyncio.to_thread(repo.mark_stage, row.notification_id, "archive_written_at", now)
            await asyncio.to_thread(repo.mark_stage, row.notification_id, "context_written_at", now)
            await asyncio.to_thread(repo.mark_delivered, row.notification_id, now)
            return
        if row.archive_written_at is None:
            if self.archive is not None:
                await asyncio.to_thread(
                    self.archive.append_external_message,
                    row.agent_id,
                    row.thread_id,
                    row.message_id,
                    message_to_dict(message),
                )
            else:
                await asyncio.to_thread(
                    self.services.thread_message_repo.append_legacy_interval,
                    row.thread_id,
                    message_inputs([message]),
                )
            await asyncio.to_thread(
                repo.mark_stage,
                row.notification_id,
                "archive_written_at",
                time.time_ns() // 1_000_000,
            )
        if row.context_written_at is None:
            try:
                harness = self.registry.get_agent(row.agent_id)
            except OctopError:
                await self.registry.start(row.agent_id)
                harness = self.registry.get_agent(row.agent_id)
            with resident_execution_scope(row.owner_id, row.thread_id):
                await harness.aappend_messages(row.thread_id, [message])
            await asyncio.to_thread(
                repo.mark_stage,
                row.notification_id,
                "context_written_at",
                time.time_ns() // 1_000_000,
            )
        await asyncio.to_thread(
            repo.mark_delivered, row.notification_id, time.time_ns() // 1_000_000
        )
