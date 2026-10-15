"""Persistent task conversations independent of experts and legacy Bridge projects."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import Field, StringConstraints

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.models import ProjectFields, ProjectModel
from octop.infra.projects.service import _validate, get_project
from octop.infra.users.resource_policy import assert_token_quota_available
from octop.infra.utils.ulid import new_ulid

RequestID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32768)]
TurnStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class TaskMessage(ProjectModel):
    request_id: RequestID = Field(
        description="本次发送的唯一标识；网络重试必须复用，相同标识不会重复执行。"
    )
    prompt: Prompt = Field(description="本轮用户消息。发送后开始一次执行。")


class TaskCreate(TaskMessage):
    project_id: str | None = Field(default=None, description="所属独立项目；null 创建无项目任务。")


class TaskTurn(ProjectModel):
    turn_id: str
    request_id: str
    prompt: str
    reply: str
    status: TurnStatus
    error_code: str
    activity: str = Field(description="当前执行的工具名称；不包含参数、凭据或工具输出。")
    model_ref: str
    input_tokens: int = Field(description="本轮模型输入 Token 数，独立于专家用量记录。")
    output_tokens: int = Field(description="本轮模型输出 Token 数。")
    project_revision: int | None = Field(description="本轮开始时采用的项目配置版本。")
    created_at: datetime
    updated_at: datetime


class TaskSummary(ProjectModel):
    task_id: str
    project_id: str | None
    title: str
    active_turn_id: str | None
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskSummary):
    turns: list[TaskTurn]


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC)


def summary(row: ProjectTaskRow) -> TaskSummary:
    return TaskSummary(
        task_id=row.task_id,
        project_id=row.project_id,
        title=row.title,
        active_turn_id=row.active_turn_id,
        created_at=_datetime(row.created_at),
        updated_at=_datetime(row.updated_at),
    )


def detail(services: SharedServices, owner_id: int, task_id: str) -> TaskDetail:
    # A read snapshot prevents a completed turn appearing beside a stale busy flag.
    with services.db.read_snapshot() as conn:
        row = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        turns = services.project_task_turn_repo.list_in_transaction(conn, task_id)
    return TaskDetail(
        **summary(row).model_dump(),
        turns=[
            TaskTurn(
                turn_id=t.turn_id,
                request_id=t.request_id,
                prompt=t.prompt,
                reply=t.reply,
                status=cast(TurnStatus, t.status),
                error_code=t.error_code,
                activity=t.activity,
                model_ref=t.model_ref,
                input_tokens=t.input_tokens,
                output_tokens=t.output_tokens,
                project_revision=json.loads(t.configuration_json).get("revision"),
                created_at=_datetime(t.created_at),
                updated_at=_datetime(t.updated_at),
            )
            for t in turns
        ],
    )


def list_tasks(
    services: SharedServices, owner_id: int, project_id: str | None
) -> list[TaskSummary]:
    if project_id is not None:
        get_project(services, owner_id, project_id)
    return [summary(row) for row in services.project_task_repo.list(owner_id, project_id)]


def _configuration(
    services: SharedServices, owner_id: int, project_id: str | None
) -> tuple[str, str]:
    fields = (
        get_project(services, owner_id, project_id) if project_id else ProjectFields(name="Task")
    )
    _validate(services, owner_id, fields)
    assert_token_quota_available(services.user_policy_repo, services.usage_repo, owner_id)
    ref = resolve_model_ref(services)
    if not ref:
        raise OctopError.localized(ErrorCode.TASK_MODEL_UNAVAILABLE, status=409)
    return fields.model_dump_json(), ref


def resolve_model_ref(services: SharedServices) -> str | None:
    """Resolve the same current model for discovery and task admission."""
    providers = ProviderStore(services.provider_repo)
    provider, model = services.settings_repo.get_active_model()
    ref = f"{provider}/{model}" if provider and model else None
    if not ref or not providers.is_model_ref_usable(ref):
        configured = services.config.agentcomputer_text_model_ref
        ref = (
            configured
            if configured and providers.is_model_ref_usable(configured)
            else providers.resolve_first_model_ref()
        )
    return ref


def _same_prompt(old: ProjectTaskTurnRow, body: TaskMessage) -> None:
    if old.prompt != body.prompt:
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)


def send(
    services: SharedServices,
    owner_id: int,
    body: TaskMessage,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
) -> TaskDetail:
    with services.db.transaction() as conn:
        tasks, turns = services.project_task_repo, services.project_task_turn_repo
        if not tasks.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        task = (
            tasks.get_in_transaction(conn, task_id, owner_id)
            if task_id
            else tasks.by_request(conn, owner_id, body.request_id)
        )
        if task_id and task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if task:
            if not task_id and task.project_id != project_id:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)
            previous = turns.by_request(conn, task.task_id, body.request_id)
            if previous:
                _same_prompt(previous, body)
                task_id = task.task_id
            else:
                if task.active_turn_id is not None:
                    raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
                configuration, model_ref = _configuration(services, owner_id, task.project_id)
                turn_id, now = "turn_" + new_ulid(), now_ms()
                if not tasks.activate(conn, task.task_id, turn_id, now):
                    raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
                turns.insert(
                    conn,
                    ProjectTaskTurnRow(
                        turn_id,
                        task.task_id,
                        body.request_id,
                        body.prompt,
                        "",
                        "queued",
                        "",
                        "",
                        configuration,
                        model_ref,
                        "",
                        False,
                        now,
                        now,
                    ),
                )
                task_id = task.task_id
        else:
            configuration, model_ref = _configuration(services, owner_id, project_id)
            task_id, turn_id, now = "task_" + new_ulid(), "turn_" + new_ulid(), now_ms()
            tasks.insert(
                conn,
                ProjectTaskRow(
                    task_id,
                    owner_id,
                    project_id,
                    body.prompt[:80],
                    body.request_id,
                    turn_id,
                    now,
                    now,
                ),
            )
            turns.insert(
                conn,
                ProjectTaskTurnRow(
                    turn_id,
                    task_id,
                    body.request_id,
                    body.prompt,
                    "",
                    "queued",
                    "",
                    "",
                    configuration,
                    model_ref,
                    "",
                    False,
                    now,
                    now,
                ),
            )
    assert task_id is not None
    return detail(services, owner_id, task_id)


def cancel(services: SharedServices, owner_id: int, task_id: str) -> TaskDetail:
    with services.db.transaction() as conn:
        tasks, turns = services.project_task_repo, services.project_task_turn_repo
        if not tasks.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        task = tasks.get_in_transaction(conn, task_id, owner_id)
        if task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if task.active_turn_id:
            turns.cancel(conn, task.active_turn_id, now_ms())
            turn = turns.get_in_transaction(conn, task.active_turn_id)
            if turn is not None and turn.status == "cancelled":
                tasks.finish(conn, task_id, turn.turn_id, now_ms())
    return detail(services, owner_id, task_id)
