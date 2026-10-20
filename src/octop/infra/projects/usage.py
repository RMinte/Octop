"""Durable context-window snapshots, separate from turn-summed billing tokens."""

from __future__ import annotations

import json
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, messages_from_dict
from octop_harness.context_usage import (
    CONTEXT_USAGE_KEY,
    SEGMENT_KEYS,
    ContextUsage,
    extract_usage_from_message,
    shrink_context_usage,
)
from pydantic import Field

from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.checkpoint_saver import checkpoint_fence
from octop.infra.projects.models import ProjectModel


class TaskContextSegment(ProjectModel):
    key: str = Field(
        description="上下文组成：system_prompt/tool_definitions/rules/skills/mcp/subagent_definitions/conversation。"
    )
    tokens: int = Field(ge=0, description="该部分的估算 token 数，不强制缩放为供应商计量总数。")


class TaskContextUsage(ProjectModel):
    max_tokens: int = Field(
        default=128_000, gt=0, description="最近模型请求所用模型的输入窗口上限。"
    )
    used_tokens: int = Field(
        default=0, ge=0, description="最近请求的上下文占用；压缩后为更新后的估算。"
    )
    input_tokens: int = Field(
        default=0, ge=0, description="最近一次模型请求的输入量，不是本轮累计计费。"
    )
    output_tokens: int = Field(
        default=0, ge=0, description="最近一次模型请求的输出量，压缩估算时为零。"
    )
    segments: list[TaskContextSegment] = Field(default_factory=list)
    source: Literal["model_request", "empty"] = Field(
        default="empty", description="empty 表示还没有模型请求快照，不表示已知零占用。"
    )
    estimated: bool = Field(default=True, description="占用是否为估算；各组成部分始终是估算。")


def message_usage(message: BaseMessage) -> TaskContextUsage | None:
    payload = message.additional_kwargs.get(CONTEXT_USAGE_KEY)
    if not isinstance(payload, dict):
        return None
    usage = ContextUsage.from_dict(payload)
    reported = extract_usage_from_message(message)
    return _snapshot(
        usage,
        estimated=bool(payload.get("estimated", reported is None or reported["input_tokens"] <= 0)),
    )


def _snapshot(usage: ContextUsage, *, estimated: bool) -> TaskContextUsage:
    return TaskContextUsage(
        max_tokens=usage.max_tokens,
        used_tokens=usage.used_tokens,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        segments=[
            TaskContextSegment(key=key, tokens=usage.segments[key])
            for key in SEGMENT_KEYS
            if usage.segments.get(key, 0) > 0
        ],
        source=usage.source,
        estimated=estimated,
    )


def latest_usage(messages: list[BaseMessage]) -> TaskContextUsage:
    for message in reversed(messages):
        snapshot = message_usage(message)
        if snapshot is not None:
            return snapshot
    return TaskContextUsage()


def compact_usage(snapshot: TaskContextUsage, removed_tokens: int) -> TaskContextUsage:
    if snapshot.source == "empty":
        return snapshot
    usage = ContextUsage.from_dict(
        {
            **snapshot.model_dump(),
            "segments": {segment.key: segment.tokens for segment in snapshot.segments},
        }
    )
    return _snapshot(shrink_context_usage(usage, removed_tokens), estimated=True).model_copy(
        update={"output_tokens": 0}
    )


def stamp_usage(messages: list[BaseMessage], snapshot: TaskContextUsage) -> list[BaseMessage]:
    """Keep the post-compaction estimate in the durable effective context too."""
    result = list(messages)
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if isinstance(message, AIMessage):
            payload = snapshot.model_dump()
            payload["segments"] = {segment.key: segment.tokens for segment in snapshot.segments}
            result[index] = message.model_copy(
                update={
                    "additional_kwargs": {**message.additional_kwargs, CONTEXT_USAGE_KEY: payload}
                }
            )
            break
    return result


def read_usage(services: SharedServices, owner_id: int, task_id: str) -> TaskContextUsage:
    if services.project_task_repo.get(task_id, owner_id) is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    path = services.paths.root / "project-tasks" / task_id / "context-usage.json"
    try:
        return TaskContextUsage.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    # A queued turn has no new model request. Use the previous completed context
    # rather than treating the absence of a snapshot as zero occupancy.
    context = services.project_task_turn_repo.context(task_id)
    return (
        latest_usage(messages_from_dict(json.loads(context[1]))) if context else TaskContextUsage()
    )


def save_usage(
    services: SharedServices,
    owner_id: int,
    turn: ProjectTaskTurnRow,
    snapshot: TaskContextUsage,
    *,
    context_messages: str | None = None,
) -> None:
    path = services.paths.root / "project-tasks" / turn.task_id / "context-usage.json"
    temporary = path.with_name(f"context-usage.{turn.worker_id}.tmp")
    with checkpoint_fence(services, turn), services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        current = services.project_task_turn_repo.get_in_transaction(conn, turn.turn_id)
        if (
            current is None
            or current.status != "running"
            or current.cancel_requested
            or not turn.worker_id
            or current.worker_id != turn.worker_id
        ):
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        if (
            context_messages is not None
            and not services.project_task_turn_repo.save_context_in_transaction(
                conn, turn.turn_id, turn.worker_id, context_messages
            )
        ):
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            temporary.write_text(snapshot.model_dump_json(), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
