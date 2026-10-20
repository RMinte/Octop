"""Private resident timeline queries, day grouping and signed keyset cursors."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

from octop.i18n import error_message
from octop.infra.agents.resident_conversation import ResidentModel, ResidentTurn, TurnStatus
from octop.infra.agents.resident_tools import ResidentToolInput
from octop.infra.db.repos.resident_conversations import ResidentConversationRow
from octop.infra.db.repos.resident_history import ResidentHistoryRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.cursors import cursor_codec


def require_resident_binding(
    services: SharedServices, owner_id: int, agent_id: str, thread_id: str
) -> ResidentConversationRow:
    binding = services.resident_conversation_repo.get_by_agent(agent_id)
    if binding is None or binding.owner_id != owner_id or binding.thread_id != thread_id:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    agent, thread = services.agent_repo.get(agent_id), services.thread_repo.get(thread_id)
    if (
        agent is None
        or agent.user_id != owner_id
        or agent.is_shared
        or agent.kind != "expert"
        or thread is None
        or thread.user_id != owner_id
        or thread.agent_id != agent_id
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if binding.status != "ready" or not agent.enabled:
        raise OctopError.localized(ErrorCode.AGENT_NOT_RUNNING)
    return binding


class ResidentHistoryDay(ResidentModel):
    day_key: str
    item_count: int
    timezones: list[str]


class ResidentHistoryDays(ResidentModel):
    agent_id: str
    thread_id: str
    history_revision: int
    days: list[ResidentHistoryDay]
    next_cursor: str | None


class ResidentTaskReport(ResidentModel):
    report_id: str
    message_id: str
    task_id: str
    task_turn_id: str
    status: Literal["completed", "failed", "cancelled"]
    text: str
    error_code: str | None
    error_message: str | None
    artifacts: list[dict[str, Any]]
    delivery_status: Literal["pending", "delivered"]


class ResidentHistoryNotification(ResidentModel):
    notification_id: str
    message_id: str
    followup_id: str
    kind: Literal["note", "task_watch", "task"]
    text: str
    payload: dict[str, Any]
    status: Literal["delivered"] = "delivered"


class ResidentHistoryItem(ResidentModel):
    item_id: str
    kind: Literal["turn", "task_report", "notification"]
    day_key: str
    timezone: str
    created_at: datetime
    updated_at: datetime
    turn: ResidentTurn | None = None
    report: ResidentTaskReport | None = None
    notification: ResidentHistoryNotification | None = None


class ResidentHistoryPage(ResidentModel):
    agent_id: str
    thread_id: str
    history_revision: int
    items: list[ResidentHistoryItem]
    next_cursor: str | None


class ResidentHistoryLocation(ResidentModel):
    agent_id: str
    thread_id: str
    history_revision: int
    anchor_item_id: str
    items: list[ResidentHistoryItem]
    has_before: bool
    has_after: bool


def _item(row: ResidentHistoryRow, locale: str) -> ResidentHistoryItem:
    code: str | None = None
    if row.error_code:
        try:
            code = ErrorCode(row.error_code).value
        except ValueError:
            code = ErrorCode.INTERNAL_ERROR.value
    created, updated = (
        datetime.fromtimestamp(row.created_at / 1000, UTC),
        datetime.fromtimestamp(row.updated_at / 1000, UTC),
    )
    result = ResidentHistoryItem(
        item_id=row.item_id,
        kind=cast(Literal["turn", "task_report", "notification"], row.kind),
        day_key=row.day_key,
        timezone=row.timezone,
        created_at=created,
        updated_at=updated,
    )
    if row.kind == "turn":
        result.turn = ResidentTurn(
            turn_id=row.source_id,
            request_id=row.request_id,
            user_message_id=f"resident:{row.source_id}:user",
            assistant_message_id=f"resident:{row.source_id}:assistant",
            prompt=row.prompt,
            reply=row.reply,
            status=cast(TurnStatus, row.status),
            created_at=created,
            updated_at=updated,
            error_code=code,
            error_message=error_message(code, locale) if code else None,
            authorization=ResidentToolInput.model_validate_json(row.input_json),
        )
    elif row.kind == "task_report":
        artifacts = json.loads(row.artifacts_json)
        result.report = ResidentTaskReport(
            report_id=row.source_id,
            message_id=row.message_id,
            task_id=row.task_id,
            task_turn_id=row.task_turn_id,
            status=cast(Literal["completed", "failed", "cancelled"], row.status),
            text=row.reply,
            error_code=code,
            error_message=error_message(code, locale) if code else None,
            artifacts=artifacts,
            delivery_status=cast(Literal["pending", "delivered"], row.delivery_status),
        )
    else:
        result.notification = ResidentHistoryNotification(
            notification_id=row.source_id,
            message_id=row.message_id,
            followup_id=row.followup_id,
            kind=cast(Literal["note", "task_watch", "task"], row.notification_kind),
            text=row.reply,
            payload=json.loads(row.payload_json),
        )
    return result


def history_days(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    limit: int,
    cursor: str | None,
    at: str,
) -> ResidentHistoryDays:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    codec = cursor_codec(services, owner_id, f"resident-days-v1:{thread_id}:{at}")
    before = codec.decode(cursor, "conversation").after if cursor else ""
    revision, rows = services.resident_history_repo.days(owner_id, thread_id, limit + 1, before, at)
    more, rows = len(rows) > limit, rows[:limit]
    return ResidentHistoryDays(
        agent_id=agent_id,
        thread_id=thread_id,
        history_revision=revision,
        days=[
            ResidentHistoryDay(day_key=day, item_count=count, timezones=zones)
            for day, count, zones in rows
        ],
        next_cursor=codec.encode("conversation", after=rows[-1][0]) if more else None,
    )


def history_page(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    limit: int,
    cursor: str | None,
    locale: str,
    *,
    day_key: str = "",
    query: str = "",
) -> ResidentHistoryPage:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    query_digest = hashlib.sha256(query.encode()).hexdigest()
    codec = cursor_codec(
        services, owner_id, f"resident-items-v1:{thread_id}:{day_key}:{query_digest}"
    )
    boundary = codec.decode(cursor, "conversation") if cursor else None
    revision, rows = services.resident_history_repo.page(
        owner_id,
        thread_id,
        limit + 1,
        day_key=day_key,
        query=query,
        before=(boundary.position, boundary.after) if boundary else None,
    )
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = (
        codec.encode("conversation", position=rows[-1].created_at, after=rows[-1].item_id)
        if more
        else None
    )
    return ResidentHistoryPage(
        agent_id=agent_id,
        thread_id=thread_id,
        history_revision=revision,
        items=[_item(row, locale) for row in reversed(rows)],
        next_cursor=next_cursor,
    )


def history_location(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    item_id: str,
    before: int,
    after: int,
    locale: str,
) -> ResidentHistoryLocation:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    repo = services.resident_history_repo
    revision, rows, has_before, has_after = repo.location(
        owner_id, thread_id, item_id, before, after
    )
    if not rows:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return ResidentHistoryLocation(
        agent_id=agent_id,
        thread_id=thread_id,
        history_revision=revision,
        anchor_item_id=item_id,
        items=[_item(row, locale) for row in rows],
        has_before=has_before,
        has_after=has_after,
    )
