"""Persistent task conversations independent of experts and legacy Bridge projects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import Field, StringConstraints

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.cursors import cursor_codec, cursor_reset
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


class TaskRetry(ProjectModel):
    request_id: RequestID = Field(description="本次重试的唯一标识；网络重传必须复用。")


class TaskCancel(ProjectModel):
    turn_id: str = Field(
        min_length=1, max_length=256, description="用户要停止的轮次；不会取消其他轮次。"
    )


class TaskCreate(TaskMessage):
    project_id: str | None = Field(default=None, description="所属独立项目；null 创建无项目任务。")


class TaskTurn(ProjectModel):
    sequence: int = Field(description="轮次稳定排序序号，可能不连续；资源标识仍使用 turn_id。")
    turn_id: str
    request_id: str
    prompt: str
    reply: str
    status: TurnStatus
    error_code: str
    activity: str = Field(description="当前执行的工具名称；不包含参数、凭据或工具输出。")
    model_ref: str
    cancel_requested: bool = Field(description="该轮已请求停止；运行中需等待 cancelled 终态。")
    retry_of_turn_id: str | None = Field(description="手动重试的原轮次；普通消息为 null。")
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
    list_revision: int = Field(description="任务摘要版本；仅列表字段变化时递增。")
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskSummary):
    turns: list[TaskTurn]
    next_page_cursor: str | None = Field(
        default=None, description="读取更早轮次的游标；每页按时间正序。"
    )
    event_cursor: str | None = Field(
        default=None, description="首页对应的实时状态续接位置；历史页不推进。"
    )


class TaskUpdate(ProjectModel):
    task: TaskSummary
    turn: TaskTurn
    cursor: str


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
        list_revision=row.list_revision,
        created_at=_datetime(row.created_at),
        updated_at=_datetime(row.updated_at),
    )


def turn_view(t: ProjectTaskTurnRow) -> TaskTurn:
    configuration = json.loads(t.configuration_json)
    return TaskTurn(
        sequence=t.pk,
        turn_id=t.turn_id,
        request_id=t.request_id,
        prompt=t.prompt,
        reply=t.reply,
        status=cast(TurnStatus, t.status),
        error_code=t.error_code,
        activity=t.activity,
        model_ref=t.model_ref,
        cancel_requested=t.cancel_requested,
        retry_of_turn_id=configuration.get("retry_of_turn_id"),
        input_tokens=t.input_tokens,
        output_tokens=t.output_tokens,
        project_revision=configuration.get("revision"),
        created_at=_datetime(t.created_at),
        updated_at=_datetime(t.updated_at),
    )


def _fingerprint(turn: TaskTurn) -> str:
    # Lease heartbeats alone do not change the public conversation state.
    return hashlib.sha256(turn.model_dump_json(exclude={"updated_at"}).encode()).hexdigest()


def detail(
    services: SharedServices,
    owner_id: int,
    task_id: str,
    *,
    limit: int = 50,
    page_cursor: str | None = None,
) -> TaskDetail:
    codec = cursor_codec(services, owner_id, f"conversation:{task_id}:history")
    before = codec.decode(page_cursor, "conversation").position if page_cursor else None
    # Summary, page and stream baseline must describe the same database snapshot.
    with services.db.read_snapshot() as conn:
        row = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        turns = services.project_task_turn_repo.page(conn, task_id, limit + 1, before)
    more = len(turns) > limit
    turns = turns[-limit:]
    views = [turn_view(t) for t in turns]
    stream = cursor_codec(services, owner_id, f"conversation:{task_id}:events")
    return TaskDetail(
        **summary(row).model_dump(),
        turns=views,
        next_page_cursor=(codec.encode("conversation", position=turns[0].pk) if more else None),
        event_cursor=(
            stream.encode(
                "conversation",
                position=turns[-1].pk if turns else 0,
                after=_fingerprint(views[-1]) if views else "",
            )
            if page_cursor is None
            else None
        ),
    )


def updates(
    services: SharedServices, owner_id: int, task_id: str, event_cursor: str
) -> list[TaskUpdate]:
    codec = cursor_codec(services, owner_id, f"conversation:{task_id}:events")
    cursor = codec.decode(event_cursor, "conversation")
    with services.db.read_snapshot() as conn:
        task = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
        if task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        rows = services.project_task_turn_repo.since(conn, task_id, cursor.position, 50)
    if cursor.position and (not rows or rows[0].pk != cursor.position):
        raise cursor_reset()
    result = []
    for row in rows:
        view = turn_view(row)
        fingerprint = _fingerprint(view)
        if row.pk == cursor.position and fingerprint == cursor.after:
            continue
        result.append(
            TaskUpdate(
                task=summary(task),
                turn=view,
                cursor=codec.encode("conversation", position=row.pk, after=fingerprint),
            )
        )
    return result


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
    """Resolve the current model for task admission."""
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


def _same_prompt(old: ProjectTaskTurnRow, body: TaskMessage, retry_of_turn_id: str | None) -> None:
    if (
        old.prompt != body.prompt
        or json.loads(old.configuration_json).get("retry_of_turn_id") != retry_of_turn_id
    ):
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)


def send(
    services: SharedServices,
    owner_id: int,
    body: TaskMessage | TaskRetry,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    retry_of_turn_id: str | None = None,
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
        if retry_of_turn_id is not None:
            original = turns.get_in_transaction(conn, retry_of_turn_id)
            if original is None or original.task_id != task_id:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            body = TaskMessage(request_id=body.request_id, prompt=original.prompt)
        assert isinstance(body, TaskMessage)
        if task:
            if not task_id and task.project_id != project_id:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)
            previous = turns.by_request(conn, task.task_id, body.request_id)
            if previous:
                _same_prompt(previous, body, retry_of_turn_id)
                task_id = task.task_id
            else:
                if task.active_turn_id is not None:
                    raise OctopError.localized(ErrorCode.TASK_BUSY, status=409)
                if retry_of_turn_id is not None:
                    latest = turns.page(conn, task.task_id, 1)
                    if (
                        not latest
                        or latest[0].turn_id != retry_of_turn_id
                        or latest[0].status not in {"failed", "cancelled"}
                    ):
                        raise OctopError.localized(ErrorCode.TASK_RETRY_UNAVAILABLE)
                configuration, model_ref = _configuration(services, owner_id, task.project_id)
                if retry_of_turn_id is not None:
                    configuration = json.dumps(
                        {**json.loads(configuration), "retry_of_turn_id": retry_of_turn_id}
                    )
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


def cancel(services: SharedServices, owner_id: int, task_id: str, turn_id: str) -> TaskDetail:
    with services.db.transaction() as conn:
        tasks, turns = services.project_task_repo, services.project_task_turn_repo
        if not tasks.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        task = tasks.get_in_transaction(conn, task_id, owner_id)
        if task is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        turn = turns.get_in_transaction(conn, turn_id)
        if turn is None or turn.task_id != task_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if task.active_turn_id == turn_id:
            turns.cancel(conn, turn_id, now_ms())
            turn = turns.get_in_transaction(conn, turn_id)
            if turn is not None and turn.status == "cancelled":
                tasks.finish(conn, task_id, turn.turn_id, now_ms())
    return detail(services, owner_id, task_id)
