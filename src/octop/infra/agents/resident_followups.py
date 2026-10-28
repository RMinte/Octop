"""Explicit, revisioned reminders and quiet task watches for private conversations."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo

from apscheduler.triggers.interval import IntervalTrigger
from pydantic import Field, StringConstraints, model_validator

from octop.i18n import error_message, tr
from octop.infra.agents.resident_history import require_resident_binding
from octop.infra.cron.trigger import AgentlyMailTrigger, build_trigger
from octop.infra.db.repos.resident_delegations import ResidentDelegationRow
from octop.infra.db.repos.resident_followups import ResidentFollowupRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import tasks
from octop.infra.projects.cursors import cursor_codec
from octop.infra.projects.interaction_models import public_status
from octop.infra.projects.models import ProjectModel, Reference
from octop.infra.utils.ulid import new_ulid

FollowupKind = Literal["note", "task_watch", "task"]
FollowupStatus = Literal["enabled", "paused", "cancelled", "completed"]
RequestId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
logger = logging.getLogger(__name__)


class FollowupPayload(ProjectModel):
    text: str = Field(
        default="", max_length=8000, description="用户明确授权的提醒正文；不作为模型指令执行。"
    )


class FollowupTaskPayload(ProjectModel):
    source_turn_id: Reference = Field(description="本会话中用户明确委派工作的原始轮次。")
    project_id: Reference | None = Field(
        description="明确指定本人项目，或显式 null 使用无项目任务。"
    )
    message: tasks.TaskMessage = Field(
        description="用户授权的完整任务消息和执行选项；派发时替换 request_id 为该授权版本与到期时刻的稳定标识。周期任务不接受一次性附件。"
    )


class FollowupFields(ProjectModel):
    purpose: str = Field(min_length=1, max_length=512, description="用户授权持续关注的明确目的。")
    kind: FollowupKind = Field(
        description="note 发送提醒；task_watch 观察任务；task 按用户明确授权定时创建普通 Task。"
    )
    schedule: str | None = Field(
        default=None,
        max_length=256,
        description="note/task 使用 date:ISO、cron:五段表达式或 interval:秒数（至少 60 秒）。task_watch 留空。",
    )
    subject_task_id: Reference | None = Field(
        default=None, description="task_watch 明确选定的本人任务；note/task 留空。"
    )
    payload: FollowupPayload | FollowupTaskPayload = Field(default_factory=FollowupPayload)
    capabilities: list[Literal["conversation_notify", "task_create"]] = Field(
        default=["conversation_notify"],
        min_length=1,
        max_length=2,
        description="note/watch 仅 conversation_notify。task 必须显式同时授予 task_create 与 conversation_notify；不外发消息。",
    )
    notification_target: Literal["conversation"] = "conversation"

    @model_validator(mode="after")
    def valid_kind(self) -> FollowupFields:
        if not self.purpose.strip():
            raise ValueError("purpose must not be blank")
        if self.kind == "note":
            if (
                self.schedule is None
                or self.subject_task_id is not None
                or not isinstance(self.payload, FollowupPayload)
                or not self.payload.text.strip()
            ):
                raise ValueError("note requires schedule and text, without subject_task_id")
        elif self.kind == "task_watch":
            if (
                self.subject_task_id is None
                or self.schedule is not None
                or not isinstance(self.payload, FollowupPayload)
                or self.payload.text
            ):
                raise ValueError(
                    "task_watch requires subject_task_id, without schedule or reminder text"
                )
        else:
            if (
                self.schedule is None
                or self.subject_task_id is not None
                or not isinstance(self.payload, FollowupTaskPayload)
            ):
                raise ValueError(
                    "task requires schedule and complete task payload, without subject_task_id"
                )
            if self.schedule.partition(":")[0] != "date" and self.payload.message.attachment_ids:
                raise ValueError("periodic tasks cannot reuse one-time task attachment IDs")
        expected = (
            ["task_create", "conversation_notify"]
            if self.kind == "task"
            else ["conversation_notify"]
        )
        if len(self.capabilities) != len(expected) or set(self.capabilities) != set(expected):
            raise ValueError("capabilities must explicitly match the selected follow-up kind")
        self.capabilities = cast(list[Literal["conversation_notify", "task_create"]], expected)
        return self


class FollowupCreate(FollowupFields):
    request_id: RequestId = Field(
        description="显式授权的唯一标识；断线重试复用，相同标识与内容仅产生一个关注事项。"
    )


class FollowupUpdate(FollowupFields):
    expected_revision: int = Field(
        ge=1, description="读取到的当前修订；不匹配返回冲突，避免覆盖他处的修改或取消。"
    )
    status: Literal["enabled", "paused"] = Field(description="完整更新并重新授权，或暂停此事项。")


class FollowupCancel(ProjectModel):
    expected_revision: int = Field(ge=1)


class ResidentFollowup(FollowupFields):
    followup_id: str
    request_id: str
    agent_id: str
    thread_id: str
    timezone: str = Field(
        description="授权时的服务端时区快照；服务端设置变化不会悄悄改动已授权的时间。"
    )
    status: FollowupStatus
    revision: int
    authorization_version: int = Field(
        description="每次用户修改、暂停或取消均递增；旧版本尚未投递的通知失效。"
    )
    next_fire_at: datetime | None
    created_at: datetime
    updated_at: datetime
    last_outcome: Literal["admitted", "admission_failed"] | None = None
    last_error_code: str | None = None
    last_task_id: str | None = None


class ResidentFollowupPage(ProjectModel):
    items: list[ResidentFollowup]
    next_page_cursor: str | None


def _now() -> int:
    return time.time_ns() // 1_000_000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _view(row: ResidentFollowupRow) -> ResidentFollowup:
    last = json.loads(row.last_observed_json) if row.last_observed_json else {}
    return ResidentFollowup(
        followup_id=row.followup_id,
        request_id=row.request_id,
        agent_id=row.agent_id,
        thread_id=row.thread_id,
        purpose=row.purpose,
        kind=cast(FollowupKind, row.kind),
        schedule=row.schedule if row.kind in {"note", "task"} else None,
        timezone=row.timezone,
        subject_task_id=row.subject_task_id or None,
        payload=(
            FollowupTaskPayload.model_validate_json(row.payload_json)
            if row.kind == "task"
            else FollowupPayload.model_validate_json(row.payload_json)
        ),
        capabilities=json.loads(row.capabilities_json),
        notification_target="conversation",
        status=cast(FollowupStatus, row.status),
        revision=row.revision,
        authorization_version=row.authorization_version,
        next_fire_at=datetime.fromtimestamp(row.next_fire_at / 1000, UTC)
        if row.next_fire_at is not None
        else None,
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
        updated_at=datetime.fromtimestamp(row.updated_at / 1000, UTC),
        last_outcome=last.get("outcome") if row.kind == "task" else None,
        last_error_code=last.get("error_code") or None if row.kind == "task" else None,
        last_task_id=last.get("task_id") or None if row.kind == "task" else None,
    )


def _next_note(spec: str, timezone: str, now: int, *, first: bool, anchor: int) -> int | None:
    kind = spec.partition(":")[0]
    if kind not in {"interval", "cron", "date"}:
        raise OctopError.localized(ErrorCode.CRON_TRIGGER_INVALID)
    trigger = build_trigger(spec, timezone=timezone)
    assert not isinstance(trigger, AgentlyMailTrigger)
    at = datetime.fromtimestamp(now / 1000, UTC)
    if isinstance(trigger, IntervalTrigger):
        if trigger.interval.total_seconds() < 60:
            raise OctopError.localized(ErrorCode.CRON_TRIGGER_INVALID)
        # Persisted anchor prevents a restarted process from resetting interval timing.
        trigger.start_date = datetime.fromtimestamp(anchor / 1000, UTC) + trigger.interval
    if not first and kind == "date":
        return None
    upcoming = trigger.get_next_fire_time(
        None, at if first and kind != "cron" else at + timedelta(milliseconds=1)
    )
    if upcoming is None:
        return None
    value = int(upcoming.timestamp() * 1000)
    if first and value <= now:
        raise OctopError.localized(ErrorCode.CRON_TRIGGER_INVALID)
    return value


def _task_snapshot(
    services: SharedServices, conn: Any, owner_id: int, task_id: str
) -> dict[str, Any]:
    task = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
    if task is None:
        return {"task_id": task_id, "status": "deleted", "task_turn_id": "", "text": ""}
    turns = services.project_task_turn_repo.page(conn, task_id, 1)
    turn = turns[-1] if turns else None
    status = public_status(task.status, task.wait_kind)
    if turn is not None:
        wait_kind = (
            "user" if turn.pause_requested and turn.wait_kind != "interrupted" else turn.wait_kind
        )
        status = public_status(turn.status, wait_kind if turn.is_waiting else "")
    return {
        "task_id": task_id,
        "status": status,
        "task_turn_id": turn.turn_id if turn is not None else "",
        "text": turn.reply[:8000]
        if turn is not None and status in {"completed", "failed", "cancelled"}
        else "",
    }


def _validated_fields(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    body: FollowupFields,
    now: int,
    *,
    thread_id: str,
    timezone: str,
    paused: bool = False,
) -> tuple[str, int | None, str]:
    if body.kind in {"note", "task"}:
        assert body.schedule is not None
        if body.kind == "task":
            assert isinstance(body.payload, FollowupTaskPayload)
            if not services.resident_followup_repo.source_exists(
                conn, owner_id, thread_id, body.payload.source_turn_id
            ):
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            if body.payload.project_id is not None:
                project = services.project_repo.get_in_transaction(
                    conn, body.payload.project_id, owner_id
                )
                if project is None:
                    raise OctopError.localized(ErrorCode.NOT_FOUND)
                if project.archived:
                    raise OctopError.localized(ErrorCode.PROJECT_ARCHIVED)
        return (
            body.schedule,
            _next_note(body.schedule, timezone, now, first=not paused, anchor=now),
            "",
        )
    assert body.subject_task_id is not None
    if services.project_task_repo.get_in_transaction(conn, body.subject_task_id, owner_id) is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    observed = _task_snapshot(services, conn, owner_id, body.subject_task_id)
    # A watch explicitly created after completion returns the existing outcome once.
    baseline = "" if observed["status"] in {"completed", "failed", "cancelled"} else _json(observed)
    return "event:task_changed", now, baseline


def create_followup(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    body: FollowupCreate,
) -> ResidentFollowup:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    fingerprint = hashlib.sha256(_json(body.model_dump(mode="json")).encode()).hexdigest()
    now = _now()
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        prior = services.resident_followup_repo.by_request(conn, owner_id, body.request_id)
        if prior is not None:
            if (
                prior.fingerprint != fingerprint
                or prior.agent_id != agent_id
                or prior.thread_id != thread_id
            ):
                raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT)
            return _view(prior)
        _require_authorizable(services, conn, owner_id, agent_id)
        timezone = services.config.default_timezone
        schedule, next_fire_at, observed = _validated_fields(
            services, conn, owner_id, body, now, thread_id=thread_id, timezone=timezone
        )
        row = ResidentFollowupRow(
            followup_id="followup_" + new_ulid(),
            owner_id=owner_id,
            agent_id=agent_id,
            thread_id=thread_id,
            request_id=body.request_id,
            fingerprint=fingerprint,
            purpose=body.purpose,
            kind=body.kind,
            schedule=schedule,
            timezone=timezone,
            subject_task_id=body.subject_task_id or "",
            payload_json=body.payload.model_dump_json(),
            capabilities_json=_json(body.capabilities),
            notification_target="conversation",
            authorization_version=1,
            revision=1,
            status="enabled",
            next_fire_at=next_fire_at,
            last_observed_json=observed,
            created_at=now,
            updated_at=now,
        )
        services.resident_followup_repo.insert(conn, row)
        return _view(row)


def _require_authorizable(
    services: SharedServices, conn: Any, owner_id: int, agent_id: str
) -> None:
    profile = services.resident_profile_repo.get_by_agent_in_transaction(conn, owner_id, agent_id)
    if profile is not None and profile.archived:
        raise OctopError.localized(ErrorCode.RESIDENT_PROFILE_ARCHIVED)


def _require_row(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    followup_id: str,
    *,
    lock: bool = False,
) -> ResidentFollowupRow:
    repo = services.resident_followup_repo
    row = (
        repo.lock(conn, owner_id, followup_id)
        if lock
        else repo.get_in_transaction(conn, owner_id, followup_id)
    )
    if row is None or row.agent_id != agent_id or row.thread_id != thread_id:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return row


def get_followup(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    followup_id: str,
) -> ResidentFollowup:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    with services.db.read_snapshot() as conn:
        return _view(_require_row(services, conn, owner_id, agent_id, thread_id, followup_id))


def list_followups(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    limit: int = 50,
    page_cursor: str | None = None,
) -> ResidentFollowupPage:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    codec = cursor_codec(services, owner_id, "resident_followups:" + thread_id)
    before = codec.decode(page_cursor, "conversation").position if page_cursor else None
    with services.db.read_snapshot() as conn:
        rows = services.resident_followup_repo.page(conn, owner_id, thread_id, limit + 1, before)
    more = len(rows) > limit
    rows = rows[:limit]
    return ResidentFollowupPage(
        items=[_view(row) for row in rows],
        next_page_cursor=codec.encode("conversation", position=rows[-1].pk) if more else None,
    )


def update_followup(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    followup_id: str,
    body: FollowupUpdate,
) -> ResidentFollowup:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    now = _now()
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        _require_authorizable(services, conn, owner_id, agent_id)
        row = _require_row(services, conn, owner_id, agent_id, thread_id, followup_id, lock=True)
        retry_failed_admission = (
            row.kind == "task"
            and row.status == "completed"
            and bool(row.last_observed_json)
            and json.loads(row.last_observed_json).get("outcome") == "admission_failed"
        )
        if (
            row.revision != body.expected_revision
            or row.status == "cancelled"
            or (row.status == "completed" and not retry_failed_admission)
        ):
            raise OctopError.localized(ErrorCode.RESIDENT_FOLLOWUP_CONFLICT)
        schedule, next_fire_at, observed = _validated_fields(
            services,
            conn,
            owner_id,
            body,
            now,
            thread_id=thread_id,
            timezone=row.timezone,
            paused=body.status == "paused",
        )
        updated = replace(
            row,
            purpose=body.purpose,
            kind=body.kind,
            schedule=schedule,
            subject_task_id=body.subject_task_id or "",
            payload_json=body.payload.model_dump_json(),
            capabilities_json=_json(body.capabilities),
            status=body.status,
            next_fire_at=next_fire_at,
            last_observed_json=observed,
            revision=row.revision + 1,
            authorization_version=row.authorization_version + 1,
            updated_at=now,
        )
        assert services.resident_followup_repo.replace(conn, updated, row.revision)
        return _view(updated)


def cancel_followup(
    services: SharedServices,
    owner_id: int,
    agent_id: str,
    thread_id: str,
    followup_id: str,
    body: FollowupCancel,
) -> ResidentFollowup:
    require_resident_binding(services, owner_id, agent_id, thread_id)
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = _require_row(services, conn, owner_id, agent_id, thread_id, followup_id, lock=True)
        if row.status == "cancelled" and body.expected_revision in {row.revision, row.revision - 1}:
            return _view(row)
        if row.revision != body.expected_revision:
            raise OctopError.localized(ErrorCode.RESIDENT_FOLLOWUP_CONFLICT)
        updated = replace(
            row,
            status="cancelled",
            next_fire_at=None,
            revision=row.revision + 1,
            authorization_version=row.authorization_version + 1,
            updated_at=_now(),
        )
        assert services.resident_followup_repo.replace(conn, updated, row.revision)
        return _view(updated)


def _task_request(
    row: ResidentFollowupRow,
) -> tuple[FollowupTaskPayload, tasks.TaskMessage, str]:
    payload = FollowupTaskPayload.model_validate_json(row.payload_json)
    digest = hashlib.sha256(
        f"{row.followup_id}:{row.authorization_version}:{row.next_fire_at}".encode()
    ).hexdigest()
    message = payload.message.model_copy(update={"request_id": "followup_" + digest})
    return payload, message, "scheduled_" + digest


def _prepare_task(
    services: SharedServices, row: ResidentFollowupRow
) -> tuple[tasks.PreparedSubmission | None, tuple[str, str] | None]:
    payload, message, delegation_request = _task_request(row)
    with services.db.connect() as conn:
        if services.resident_delegation_repo.by_request(conn, row.owner_id, delegation_request):
            return None, None
    prepared = tasks.prepare_submission(
        services, row.owner_id, message, project_id=payload.project_id
    )
    configuration = (
        tasks.submission_configuration(services, row.owner_id, prepared)
        if prepared.previous_turn_id is None
        else None
    )
    return prepared, configuration


def _admit_task(
    services: SharedServices,
    conn: Any,
    row: ResidentFollowupRow,
    now: int,
    prepared: tasks.PreparedSubmission | None,
    configuration: tuple[str, str] | None,
) -> dict[str, Any]:
    payload, message, delegation_request = _task_request(row)
    if not services.resident_followup_repo.source_exists(
        conn, row.owner_id, row.thread_id, payload.source_turn_id
    ):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    fingerprint = hashlib.sha256(
        _json(
            {
                "source_turn_id": payload.source_turn_id,
                "project_id": payload.project_id,
                "message": message.model_dump(mode="json"),
            }
        ).encode()
    ).hexdigest()
    previous = services.resident_delegation_repo.by_request(conn, row.owner_id, delegation_request)
    if previous is not None:
        if (
            previous.fingerprint != fingerprint
            or previous.origin_agent_id != row.agent_id
            or previous.origin_thread_id != row.thread_id
        ):
            raise OctopError.localized(ErrorCode.RESIDENT_REQUEST_CONFLICT)
        return {
            "outcome": "admitted",
            "task_id": previous.task_id,
            "task_turn_id": previous.initial_turn_id,
            "delegation_id": previous.delegation_id,
        }
    if services.project_task_repo.by_request(conn, row.owner_id, message.request_id) is not None:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    assert prepared is not None
    task_id, turn_id = tasks.send_in_transaction(
        services,
        conn,
        row.owner_id,
        message,
        project_id=payload.project_id,
        prepared=prepared,
        prepared_configuration=configuration,
    )
    delegation_id = "delegation_" + new_ulid()
    services.resident_delegation_repo.insert(
        conn,
        ResidentDelegationRow(
            delegation_id=delegation_id,
            owner_id=row.owner_id,
            origin_agent_id=row.agent_id,
            origin_thread_id=row.thread_id,
            source_turn_id=payload.source_turn_id,
            task_id=task_id,
            initial_turn_id=turn_id,
            request_id=delegation_request,
            fingerprint=fingerprint,
            timezone=row.timezone,
            created_at=now,
            updated_at=now,
        ),
    )
    services.resident_followup_repo.bump_conversation(conn, row.owner_id, row.thread_id, now)
    return {
        "outcome": "admitted",
        "task_id": task_id,
        "task_turn_id": turn_id,
        "delegation_id": delegation_id,
    }


def poll_followups(services: SharedServices, now: int | None = None) -> int:
    """Capture due observations and their outbox entries atomically, without model/tool calls."""
    now = _now() if now is None else now
    captured = 0
    for pending in services.resident_followup_repo.due(now):
        prepared, configuration = None, None
        preparation_error: ErrorCode | None = None
        if pending.kind == "task":
            try:
                prepared, configuration = _prepare_task(services, pending)
            except OctopError as exc:
                preparation_error = exc.code
            except Exception:
                logger.exception(
                    "Scheduled resident task preparation failed: %s", pending.followup_id
                )
                preparation_error = ErrorCode.INTERNAL_ERROR
        with services.db.transaction() as conn:
            if not services.project_task_repo.lock_owner(conn, pending.owner_id):
                continue
            row = services.resident_followup_repo.lock(conn, pending.owner_id, pending.followup_id)
            if (
                row is None
                or row.status != "enabled"
                or row.authorization_version != pending.authorization_version
                or row.payload_json != pending.payload_json
                or row.kind != pending.kind
                or row.next_fire_at != pending.next_fire_at
                or row.next_fire_at is None
                or row.next_fire_at > now
            ):
                continue
            binding = services.resident_conversation_repo.get_by_thread(row.thread_id)
            agent = services.agent_repo.get(row.agent_id)
            if (
                binding is None
                or binding.owner_id != row.owner_id
                or binding.agent_id != row.agent_id
                or binding.status != "ready"
                or agent is None
                or not agent.enabled
                or agent.is_shared
                or agent.user_id != row.owner_id
            ):
                continue
            payload: dict[str, Any]
            observed = row.last_observed_json
            status = "enabled"
            if row.kind == "note":
                text = FollowupPayload.model_validate_json(row.payload_json).text
                payload = {"purpose": row.purpose, "scheduled_for": row.next_fire_at}
                source_key = (
                    f"followup:{row.followup_id}:{row.authorization_version}:due:{row.next_fire_at}"
                )
                next_fire_at = _next_note(
                    row.schedule, row.timezone, now, first=False, anchor=row.next_fire_at
                )
                if next_fire_at is None:
                    status = "completed"
            elif row.kind == "task":
                user = services.user_repo.get(row.owner_id)
                locale = user.locale if user else "en"
                code = preparation_error
                try:
                    if code is None:
                        with services.resident_followup_repo.task_admission(conn):
                            payload = _admit_task(services, conn, row, now, prepared, configuration)
                except OctopError as exc:
                    code = exc.code
                except Exception:
                    logger.exception(
                        "Scheduled resident task admission failed: %s", row.followup_id
                    )
                    code = ErrorCode.INTERNAL_ERROR
                if code is not None:
                    payload = {"outcome": "admission_failed", "error_code": code.value}
                    try:
                        failure_text = error_message(code.value, locale)
                    except KeyError:
                        failure_text = error_message(ErrorCode.INTERNAL_ERROR.value, locale)
                    text = tr(
                        "agents.resident.followup_task_failed",
                        locale,
                        purpose=row.purpose,
                        error=failure_text,
                    )
                    status, next_fire_at = "completed", None
                else:
                    text = tr(
                        "agents.resident.followup_task_admitted",
                        locale,
                        purpose=row.purpose,
                        task_id=payload["task_id"],
                    )
                    next_fire_at = _next_note(
                        row.schedule, row.timezone, now, first=False, anchor=row.next_fire_at
                    )
                    if next_fire_at is None:
                        status = "completed"
                observed = _json(payload)
                payload = {**payload, "purpose": row.purpose, "scheduled_for": row.next_fire_at}
                source_key = f"followup:{row.followup_id}:{row.authorization_version}:task:{row.next_fire_at}"
            else:
                payload = _task_snapshot(services, conn, row.owner_id, row.subject_task_id)
                observed = _json(payload)
                next_fire_at = now + 1000
                if observed == row.last_observed_json:
                    services.resident_followup_repo.record_poll(
                        conn,
                        row,
                        next_fire_at=next_fire_at,
                        last_observed_json=observed,
                        status=status,
                        now=now,
                    )
                    continue
                state = str(payload["status"])
                if state in {"completed", "failed", "cancelled", "deleted"}:
                    status, next_fire_at = "completed", None
                user = services.user_repo.get(row.owner_id)
                locale = user.locale if user else "en"
                text = tr(
                    "agents.resident.followup_task_changed",
                    locale,
                    purpose=row.purpose,
                    task_id=row.subject_task_id,
                    status=tr("agents.resident.task_status_" + state, locale),
                    text=payload["text"],
                )
                source_key = f"followup:{row.followup_id}:{row.authorization_version}:observed:{row.next_fire_at}"
                payload["purpose"] = row.purpose
            when = datetime.fromtimestamp(now / 1000, UTC)
            captured += int(
                services.resident_notification_repo.enqueue(
                    conn,
                    notification_id="notification_" + new_ulid(),
                    owner_id=row.owner_id,
                    agent_id=row.agent_id,
                    thread_id=row.thread_id,
                    followup_id=row.followup_id,
                    authorization_version=row.authorization_version,
                    source_key=source_key,
                    kind=row.kind,
                    text=text,
                    payload_json=_json(payload),
                    day_key=when.astimezone(ZoneInfo(row.timezone)).date().isoformat(),
                    timezone=row.timezone,
                    now=now,
                )
            )
            services.resident_followup_repo.record_poll(
                conn,
                row,
                next_fire_at=next_fire_at,
                last_observed_json=observed,
                status=status,
                now=now,
            )
    return captured
