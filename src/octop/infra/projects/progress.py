"""Validated task plans and explicit, public execution updates from the model."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import Field, StrictInt, StringConstraints, ValidationError, model_validator

from octop.i18n import tr
from octop.infra.projects.models import ProjectModel

NOTIFY_USER = "message_notify_user"
PROGRESS_TEXT_LIMIT = 2000
PLAN_STEP_LIMIT = 100
PLAN_CONTENT_LIMIT = 500
_PlanContent = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=PLAN_CONTENT_LIMIT
    ),
]
_ProgressText = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=PROGRESS_TEXT_LIMIT
    ),
]
PlanIdentity = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
PlanStatus = Literal["pending", "in_progress", "completed", "blocked", "skipped"]


class TaskPlanStep(ProjectModel):
    step_id: PlanIdentity | None = Field(
        default=None, description="宿主生成的稳定步骤标识；旧计划未记录时为空，不按文字或顺序补造。"
    )
    content: _PlanContent
    status: PlanStatus
    result: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, max_length=PROGRESS_TEXT_LIMIT)
    ] = Field(default="", description="模型显式报告的阶段结果或受阻原因；不代表工具自动验证成功。")
    started_at: datetime | None = Field(default=None, description="宿主首次启动步骤的 UTC 时间。")
    finished_at: datetime | None = Field(
        default=None, description="宿主结束步骤的 UTC 时间；重新启动后清空。"
    )
    removed_at: datetime | None = Field(
        default=None, description="步骤从当前计划移入历史的 UTC 时间。"
    )


class TaskPlan(ProjectModel):
    source: Literal["harness_todos", "octop_planning"] = "harness_todos"
    plan_id: PlanIdentity | None = Field(
        default=None, description="宿主生成的稳定计划标识；旧计划未记录时为空。"
    )
    title: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, max_length=PLAN_CONTENT_LIMIT)
    ] = ""
    revision: Annotated[StrictInt, Field(ge=1)] = Field(description="轮次内单调递增的计划版本。")
    active_step_id: PlanIdentity | None = Field(
        default=None, description="宿主明确选中的执行中步骤；为空时默认动作归属整轮。"
    )
    steps: list[TaskPlanStep] = Field(max_length=PLAN_STEP_LIMIT)
    retired_steps: list[TaskPlanStep] = Field(
        default_factory=list, description="从本计划移除的步骤，保留原身份、阶段结果和时间。"
    )

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        all_steps = [*self.steps, *self.retired_steps]
        identities = [step.step_id for step in all_steps if step.step_id is not None]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate plan step identity")
        if self.source == "octop_planning" and (
            self.plan_id is None or any(step.step_id is None for step in all_steps)
        ):
            raise ValueError("managed plans require stable plan and step identities")
        if self.active_step_id is not None and not any(
            step.step_id == self.active_step_id and step.status == "in_progress"
            for step in self.steps
        ):
            raise ValueError("active step must be an in-progress plan step")
        return self


def normalize_todos(todos: object) -> list[TaskPlanStep] | None:
    """Validate a complete replacement without turning malformed data into clearing."""
    if not isinstance(todos, list) or len(todos) > PLAN_STEP_LIMIT:
        return None
    steps: list[TaskPlanStep] = []
    for item in todos:
        if (
            not isinstance(item, dict)
            or set(item) != {"content", "status"}
            or item.get("status") not in ("pending", "in_progress", "completed")
        ):
            return None
        try:
            steps.append(TaskPlanStep.model_validate(item, strict=True))
        except ValidationError:
            return None
    return steps


class _ProgressArgs(ProjectModel):
    text: _ProgressText


def progress_prompt(locale: str) -> str:
    return tr("projects.progress_prompt", locale)


def progress_tool(locale: str) -> StructuredTool:
    async def notify(text: str) -> tuple[str, dict[str, str]]:
        text = _ProgressArgs(text=text).text
        return text, {"type": "task_progress", "text": text}

    return StructuredTool.from_function(
        name=NOTIFY_USER,
        description=tr("projects.progress_tool_description", locale),
        args_schema=_ProgressArgs,
        coroutine=notify,
        response_format="content_and_artifact",
    )


def progress_report(message: object) -> str | None:
    """Read only a successful notification tool's validated public-text artifact."""
    if (
        not isinstance(message, ToolMessage)
        or message.name != NOTIFY_USER
        or message.status == "error"
        or not isinstance(message.artifact, dict)
        or message.artifact.get("type") != "task_progress"
    ):
        return None
    try:
        return _ProgressArgs.model_validate(
            {"text": message.artifact.get("text")}, strict=True
        ).text
    except ValidationError:
        return None
