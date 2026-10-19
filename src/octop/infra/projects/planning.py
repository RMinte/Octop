"""Host-owned plan identities, revision checks, and explicit step lifecycle tools."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool, ToolException
from pydantic import Field, StrictInt, StringConstraints, ValidationError

from octop.i18n import tr
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.progress import (
    PLAN_CONTENT_LIMIT,
    PLAN_STEP_LIMIT,
    PROGRESS_TEXT_LIMIT,
    PlanIdentity,
    TaskPlan,
    TaskPlanStep,
)
from octop.infra.utils.ulid import new_ulid

PLANNING = "planning"
_Content = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=PLAN_CONTENT_LIMIT
    ),
]
_Result = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=PROGRESS_TEXT_LIMIT
    ),
]
_Command = Literal["create", "get", "update", "start_step", "mark_step", "select_step"]
_EndStatus = Literal["completed", "blocked", "skipped"]
_ToolResult = tuple[str, dict[str, Any]]


class _StepUpdate(ProjectModel):
    step_id: PlanIdentity | None = None
    content: _Content


class _PlanningArgs(ProjectModel):
    command: _Command
    call_id: Annotated[str, InjectedToolCallId]
    expected_revision: Annotated[StrictInt, Field(ge=1)] | None = None
    title: (
        Annotated[
            str,
            StringConstraints(strict=True, strip_whitespace=True, max_length=PLAN_CONTENT_LIMIT),
        ]
        | None
    ) = None
    steps: list[_Content | _StepUpdate] | None = Field(default=None, max_length=PLAN_STEP_LIMIT)
    step_id: PlanIdentity | None = None
    status: _EndStatus | None = None
    result: _Result | None = None


class TaskPlanning:
    def __init__(self, restored: TaskPlan | None, locale: str):
        self._plan = restored.model_copy(deep=True) if restored is not None else None
        self.locale = locale
        self._lock = asyncio.Lock()
        self._calls: dict[str, tuple[str, _ToolResult]] = {}

    @property
    def plan(self) -> TaskPlan | None:
        return self._plan.model_copy(deep=True) if self._plan is not None else None

    def get(self) -> TaskPlan | None:
        return self.plan

    def restore(self, plan: TaskPlan) -> None:
        if self._plan is None or plan.revision > self._plan.revision:
            self._plan = plan.model_copy(deep=True)

    def restore_call(self, call_id: str, arguments: dict[str, Any], plan: TaskPlan) -> None:
        args = _PlanningArgs.model_validate({**arguments, "call_id": call_id})
        signature = args.model_dump_json(exclude={"call_id"})
        self._calls.setdefault(call_id, (signature, deepcopy(self._snapshot_result(plan))))
        self.restore(plan)

    def _error(self, key: str) -> ToolException:
        return ToolException(tr(f"projects.planning_{key}", self.locale))

    def binding(self, step_id: str | None = None) -> tuple[str | None, str | None]:
        plan = self._plan
        selected = step_id if step_id is not None else plan.active_step_id if plan else None
        if selected is None:
            return None, None
        if plan is None or plan.source != "octop_planning" or plan.plan_id is None:
            raise ValueError(tr("projects.planning_step_unavailable", self.locale))
        step = next((step for step in plan.steps if step.step_id == selected), None)
        if step is None:
            raise ValueError(tr("projects.planning_step_unavailable", self.locale))
        if step.status != "in_progress":
            raise ValueError(tr("projects.planning_step_not_running", self.locale))
        return plan.plan_id, selected

    def _managed(self, expected_revision: int | None) -> TaskPlan:
        if self._plan is None:
            raise self._error("missing")
        if self._plan.source != "octop_planning":
            raise self._error("legacy")
        if expected_revision != self._plan.revision:
            raise self._error("conflict")
        return self._plan.model_copy(deep=True)

    def _step(self, plan: TaskPlan, step_id: str | None) -> TaskPlanStep:
        step = next((step for step in plan.steps if step.step_id == step_id), None)
        if step is None:
            raise self._error("step_unavailable")
        return step

    def _apply(self, args: _PlanningArgs) -> None:
        if args.command == "get":
            return
        now = datetime.now(UTC)
        if args.command == "create":
            if not args.steps or any(not isinstance(step, str) for step in args.steps):
                raise self._error("invalid_arguments")
            steps = [
                TaskPlanStep(step_id=new_ulid(), content=step, status="pending")
                for step in args.steps
                if isinstance(step, str)
            ]
            steps[0].status = "in_progress"
            steps[0].started_at = now
            self._plan = TaskPlan(
                source="octop_planning",
                plan_id=new_ulid(),
                title=args.title or "",
                revision=self._plan.revision + 1 if self._plan else 1,
                active_step_id=steps[0].step_id,
                steps=steps,
            )
            return
        plan = self._managed(args.expected_revision)
        if args.command == "update":
            if args.steps is None or any(not isinstance(step, _StepUpdate) for step in args.steps):
                raise self._error("invalid_arguments")
            steps = []
            retained: set[str] = set()
            for update in args.steps:
                assert isinstance(update, _StepUpdate)
                if update.step_id is None:
                    step = TaskPlanStep(
                        step_id=new_ulid(), content=update.content, status="pending"
                    )
                else:
                    if update.step_id in retained:
                        raise self._error("step_unavailable")
                    step = self._step(plan, update.step_id).model_copy(deep=True)
                    step.content = update.content
                    retained.add(update.step_id)
                steps.append(step)
            for step in plan.steps:
                if step.step_id not in retained:
                    step.removed_at = now
                    plan.retired_steps.append(step)
            plan.steps = steps
            if plan.active_step_id not in retained:
                plan.active_step_id = None
        elif args.command == "select_step":
            if args.step_id is not None and self._step(plan, args.step_id).status != "in_progress":
                raise self._error("step_not_running")
            plan.active_step_id = args.step_id
        else:
            step = self._step(plan, args.step_id)
            if args.command == "start_step":
                step.status = "in_progress"
                step.started_at = step.started_at or now
                step.finished_at = None
                plan.active_step_id = step.step_id
            elif args.command == "mark_step":
                if args.status is None or args.result is None:
                    raise self._error("invalid_arguments")
                step.status = args.status
                step.result = args.result
                step.finished_at = now
                if plan.active_step_id == step.step_id:
                    plan.active_step_id = None
        assert self._plan is not None
        if plan.model_dump(exclude={"revision"}) != self._plan.model_dump(exclude={"revision"}):
            plan.revision += 1
            self._plan = plan

    @staticmethod
    def _snapshot_result(plan: TaskPlan | None) -> _ToolResult:
        snapshot = plan.model_dump(mode="json") if plan is not None else None
        return json.dumps(snapshot, ensure_ascii=False), {"type": "task_plan", "plan": snapshot}

    def tool(self) -> StructuredTool:
        async def planning(
            command: _Command,
            call_id: str,
            expected_revision: int | None = None,
            title: str | None = None,
            steps: list[str | _StepUpdate] | None = None,
            step_id: str | None = None,
            status: _EndStatus | None = None,
            result: str | None = None,
        ) -> _ToolResult:
            args = _PlanningArgs(
                command=command,
                call_id=call_id,
                expected_revision=expected_revision,
                title=title,
                steps=steps,
                step_id=step_id,
                status=status,
                result=result,
            )
            signature = args.model_dump_json(exclude={"call_id"})
            async with self._lock:
                cached = self._calls.get(call_id)
                if cached is not None:
                    if cached[0] != signature:
                        raise self._error("call_reused")
                    return deepcopy(cached[1])
                self._apply(args)
                response = self._snapshot_result(self._plan)
                self._calls[call_id] = signature, deepcopy(response)
                return response

        return StructuredTool.from_function(
            name=PLANNING,
            description=tr("projects.planning_tool_description", self.locale),
            args_schema=_PlanningArgs,
            coroutine=planning,
            response_format="content_and_artifact",
            handle_tool_error=True,
            handle_validation_error=tr("projects.planning_invalid_arguments", self.locale),
        )

    def tools(self) -> list[StructuredTool]:
        return [self.tool()]


def plan_report(message: object) -> TaskPlan | None:
    if (
        not isinstance(message, ToolMessage)
        or message.name != PLANNING
        or message.status == "error"
        or not isinstance(message.artifact, dict)
        or message.artifact.get("type") != "task_plan"
    ):
        return None
    try:
        return TaskPlan.model_validate(message.artifact.get("plan"))
    except ValidationError:
        return None
