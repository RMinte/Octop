"""Explicit user-authorized task delegation from a private resident conversation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StringConstraints

from octop.infra.agents.resident_history import require_resident_binding
from octop.infra.db.repos.resident_delegations import (
    ResidentDelegationRow,
    ResidentTaskReportRow,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import tasks
from octop.infra.projects.cursors import cursor_codec
from octop.infra.projects.interaction_models import public_status
from octop.infra.projects.models import ProjectModel, Reference
from octop.infra.utils.ulid import new_ulid


class ResidentDelegationCreate(ProjectModel):
    request_id: tasks.RequestID = Field(
        description="本次明确委派的唯一标识；断线重试必须复用，相同标识与内容仅创建一个任务。"
    )
    source_turn_id: Annotated[str, StringConstraints(min_length=1, max_length=200)] = Field(
        description="触发委派的本人私人助手会话轮次。"
    )
    project_id: Reference | None = Field(
        description="用户明确选定的本人项目 ID；明确传 null 创建无项目任务，不推断项目。"
    )
    message: tasks.TaskMessage = Field(
        description="完整的任务首条消息与执行选项；复用普通任务相同的资源、权限、附件和客户端工具校验。"
    )


class ResidentReportArtifact(ProjectModel):
    artifact_id: str
    name: str
    media_type: str
    byte_count: int
    sha256: str
    business_category: str


class ResidentTaskReport(ProjectModel):
    report_id: str
    message_id: str = Field(description="成果回流消息的稳定标识；重试投递时保持不变。")
    task_id: str
    task_turn_id: str
    status: Literal["completed", "failed", "cancelled"]
    text: str = Field(
        description="任务这一具体轮次最终正文的前 8000 字符；完整内容读取原任务，不会被之后的新工作覆盖。"
    )
    error_code: str | None
    artifacts: list[ResidentReportArtifact] = Field(
        description="这一具体轮次产生的附件事实引用；任务删除后引用保留，原文件可能已不可下载。"
    )
    day_key: str
    timezone: str
    captured_at: datetime
    delivery_status: Literal["pending", "delivered"] = Field(
        description="持久成果事实已可读取；pending 表示正式会话归档或上下文投递尚待完成。"
    )


class ResidentDelegation(ProjectModel):
    delegation_id: str
    request_id: str
    source_turn_id: str
    task_id: str
    initial_turn_id: str
    latest_task_turn_id: str
    status: tasks.TurnStatus | Literal["deleted"]
    task_exists: bool
    reports: list[ResidentTaskReport]
    created_at: datetime


class ResidentDelegationPage(ProjectModel):
    items: list[ResidentDelegation]
    next_page_cursor: str | None


def report_view(row: ResidentTaskReportRow) -> ResidentTaskReport:
    return ResidentTaskReport(
        report_id=row.report_id,
        message_id=row.message_id,
        task_id=row.task_id,
        task_turn_id=row.task_turn_id,
        status=cast(Literal["completed", "failed", "cancelled"], row.status),
        text=row.text,
        error_code=row.error_code or None,
        artifacts=[
            ResidentReportArtifact.model_validate(item) for item in json.loads(row.artifacts_json)
        ],
        day_key=row.day_key,
        timezone=row.timezone,
        captured_at=datetime.fromtimestamp(row.captured_at / 1000, UTC),
        delivery_status=cast(Literal["pending", "delivered"], row.delivery_status),
    )


def _view(services: SharedServices, conn: Any, row: ResidentDelegationRow) -> ResidentDelegation:
    task = services.project_task_repo.get_in_transaction(conn, row.task_id, row.owner_id)
    latest = services.resident_delegation_repo.latest_linked_turn(conn, row) if task else None
    reports = services.resident_task_report_repo.for_delegation(
        conn, row.owner_id, row.delegation_id
    )
    status: tasks.TurnStatus | Literal["deleted"] = "deleted"
    if latest is not None:
        is_waiting = (
            latest["status"] == "running"
            and not latest["worker_id"]
            and bool(latest["checkpoint_id"] or latest["wait_kind"] == "interrupted")
        )
        wait_kind = (
            "user"
            if latest["pause_requested"] and latest["wait_kind"] != "interrupted"
            else str(latest["wait_kind"])
        )
        status = cast(
            tasks.TurnStatus, public_status(str(latest["status"]), wait_kind if is_waiting else "")
        )
    return ResidentDelegation(
        delegation_id=row.delegation_id,
        request_id=row.request_id,
        source_turn_id=row.source_turn_id,
        task_id=row.task_id,
        initial_turn_id=row.initial_turn_id,
        latest_task_turn_id=(
            str(latest["turn_id"])
            if latest
            else (reports[-1].task_turn_id if reports else row.initial_turn_id)
        ),
        status=status,
        task_exists=task is not None,
        reports=[report_view(item) for item in reports],
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
    )


def create_delegation(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    body: ResidentDelegationCreate,
    *,
    require_live_source: bool = False,
) -> ResidentDelegation:
    """User selects the task input; admission and origin linkage commit together."""
    require_resident_binding(services, owner_id, agent_id, thread_id)
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with services.db.connect() as conn:
        existing = services.resident_delegation_repo.by_request(conn, owner_id, body.request_id)
    prepared: tasks.PreparedSubmission | None = None
    configuration: tuple[str, str] | None = None
    if existing is None:
        prepared = tasks.prepare_submission(
            services, owner_id, body.message, project_id=body.project_id
        )
        if prepared.previous_turn_id is None:
            configuration = tasks.submission_configuration(services, owner_id, prepared)
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        prior = services.resident_delegation_repo.by_request(conn, owner_id, body.request_id)
        if prior is not None:
            if (
                prior.fingerprint != fingerprint
                or prior.origin_agent_id != agent_id
                or prior.origin_thread_id != thread_id
            ):
                raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT)
            return _view(services, conn, prior)
        profile = services.resident_profile_repo.get_by_agent_in_transaction(
            conn, owner_id, agent_id
        )
        if profile is not None and profile.archived:
            raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_ARCHIVED)
        source = conn.execute(
            "SELECT turn_id,status,cancel_requested,lease_until FROM resident_requests WHERE owner_id=? AND thread_id=? AND turn_id=?"
            + (
                " FOR UPDATE" if require_live_source and services.db.dialect == "postgresql" else ""
            ),
            (owner_id, thread_id, body.source_turn_id),
        ).fetchone()
        if source is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if require_live_source and (
            source["status"] != "running"
            or source["cancel_requested"]
            or source["lease_until"] <= tasks.now_ms()
        ):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if (
            services.project_task_repo.by_request(conn, owner_id, body.message.request_id)
            is not None
        ):
            raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
        timezone = services.config.default_timezone
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise OctopError.localized(ErrorCode.INTERNAL_ERROR) from exc
        if prepared is None:
            raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT)
        task_id, turn_id = tasks.send_in_transaction(
            services,
            conn,
            owner_id,
            body.message,
            project_id=body.project_id,
            prepared=prepared,
            prepared_configuration=configuration,
        )
        now = tasks.now_ms()
        row = ResidentDelegationRow(
            delegation_id="delegation_" + new_ulid(),
            owner_id=owner_id,
            origin_agent_id=agent_id,
            origin_thread_id=thread_id,
            source_turn_id=body.source_turn_id,
            task_id=task_id,
            initial_turn_id=turn_id,
            request_id=body.request_id,
            fingerprint=fingerprint,
            timezone=timezone,
            created_at=now,
            updated_at=now,
        )
        services.resident_delegation_repo.insert(conn, row)
        conn.execute(
            "UPDATE resident_conversations SET revision=revision+1,updated_at=? WHERE thread_id=?",
            (now, thread_id),
        )
        return _view(services, conn, row)


def get_delegation(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    delegation_id: str,
) -> ResidentDelegation:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    with services.db.read_snapshot() as conn:
        row = services.resident_delegation_repo.get_in_transaction(conn, owner_id, delegation_id)
        if row is None or row.origin_thread_id != thread_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return _view(services, conn, row)


def list_delegations(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    limit: int = 50,
    page_cursor: str | None = None,
) -> ResidentDelegationPage:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    codec = cursor_codec(services, owner_id, f"resident:{thread_id}:delegations")
    before = codec.decode(page_cursor, "conversation").position if page_cursor else None
    with services.db.read_snapshot() as conn:
        rows = services.resident_delegation_repo.page(conn, owner_id, thread_id, limit + 1, before)
        more = len(rows) > limit
        rows = rows[:limit]
        return ResidentDelegationPage(
            items=[_view(services, conn, row) for row in rows],
            next_page_cursor=(codec.encode("conversation", position=rows[-1].pk) if more else None),
        )
