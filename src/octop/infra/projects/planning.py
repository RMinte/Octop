"""Host-owned ordered plans, durable step transitions, and replayable tool receipts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
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
    PROGRESS_TEXT_LIMIT,
    PlanIdentity,
    TaskPlan,
    TaskPlanStep,
)
from octop.infra.utils.ulid import new_ulid

PLANNING = "planning"
ORDERED_PLAN_STEP_LIMIT = 12
PLAN_TITLE_LIMIT = 200
PlanningPhase = Literal["unplanned", "executing", "blocked", "completed"]
PlanPersist = Callable[[TaskPlan, str, dict[str, Any]], Awaitable[None]]
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
_Command = Literal["create", "get", "mark_step"]
_EndStatus = Literal["completed", "blocked", "skipped"]
_ToolResult = tuple[str, dict[str, Any]]


class _PlanningArgs(ProjectModel):
    command: _Command
    call_id: Annotated[str, InjectedToolCallId]
    expected_revision: Annotated[StrictInt, Field(ge=1)] | None = None
    title: (
        Annotated[
            str,
            StringConstraints(
                strict=True, strip_whitespace=True, min_length=1, max_length=PLAN_TITLE_LIMIT
            ),
        ]
        | None
    ) = None
    steps: list[_Content] | None = Field(
        default=None, min_length=1, max_length=ORDERED_PLAN_STEP_LIMIT
    )
    step_id: PlanIdentity | None = None
    status: _EndStatus | None = None
    result: _Result | None = None


class TaskPlanning:
    def __init__(self, restored: TaskPlan | None, locale: str, persist: PlanPersist | None = None):
        self._plan = restored.model_copy(deep=True) if restored is not None else None
        self.locale = locale
        self._lock = asyncio.Lock()
        self._calls: dict[str, tuple[str, _ToolResult]] = {}
        self._persist = persist

    @property
    def phase(self) -> PlanningPhase:
        if self._plan is None:
            return "unplanned"
        if any(step.status == "blocked" for step in self._plan.steps):
            return "blocked"
        if self._plan.steps and all(
            step.status in {"completed", "skipped"} for step in self._plan.steps
        ):
            return "completed"
        return "executing"

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
            if plan is not None and self.phase == "executing":
                raise ValueError(tr("projects.planning_step_not_current", self.locale))
            return None, None
        if plan is None or plan.source != "octop_planning" or plan.plan_id is None:
            raise ValueError(tr("projects.planning_step_unavailable", self.locale))
        step = next((step for step in plan.steps if step.step_id == selected), None)
        if step is None:
            raise ValueError(tr("projects.planning_step_unavailable", self.locale))
        if step.status != "in_progress":
            raise ValueError(tr("projects.planning_step_not_running", self.locale))
        if selected != plan.active_step_id or not self._is_ordered(plan):
            raise ValueError(tr("projects.planning_step_not_current", self.locale))
        return plan.plan_id, selected

    @staticmethod
    def _is_ordered(plan: TaskPlan) -> bool:
        reached_unfinished = False
        for step in plan.steps:
            if reached_unfinished and step.status != "pending":
                return False
            if step.status not in {"completed", "skipped"}:
                reached_unfinished = True
        current = next((step for step in plan.steps if step.status == "in_progress"), None)
        return plan.active_step_id == (current.step_id if current else None)

    def _managed(self, expected_revision: int | None) -> TaskPlan:
        if self._plan is None:
            raise self._error("missing")
        if self._plan.source != "octop_planning":
            raise self._error("legacy")
        if not self._is_ordered(self._plan):
            raise self._error("step_not_current")
        if expected_revision != self._plan.revision:
            raise self._error("conflict")
        return self._plan.model_copy(deep=True)

    def _step(self, plan: TaskPlan, step_id: str | None) -> TaskPlanStep:
        step = next((step for step in plan.steps if step.step_id == step_id), None)
        if step is None:
            raise self._error("step_unavailable")
        return step

    def _candidate(self, args: _PlanningArgs) -> TaskPlan:
        if args.command == "get":
            if any(
                value is not None
                for value in (
                    args.expected_revision,
                    args.title,
                    args.steps,
                    args.step_id,
                    args.status,
                    args.result,
                )
            ):
                raise self._error("invalid_arguments")
            if self._plan is None:
                raise self._error("missing")
            return self._plan.model_copy(deep=True)
        now = datetime.now(UTC)
        if args.command == "create":
            if self._plan is not None:
                raise self._error("already_created")
            if (
                args.title is None
                or not args.steps
                or any(
                    value is not None
                    for value in (args.expected_revision, args.step_id, args.status, args.result)
                )
            ):
                raise self._error("invalid_arguments")
            steps = [
                TaskPlanStep(step_id=new_ulid(), content=step, status="pending")
                for step in args.steps
            ]
            steps[0].status = "in_progress"
            steps[0].started_at = now
            return TaskPlan(
                source="octop_planning",
                plan_id=new_ulid(),
                title=args.title,
                revision=1,
                active_step_id=steps[0].step_id,
                steps=steps,
            )
        if args.title is not None or args.steps is not None:
            raise self._error("invalid_arguments")
        if args.step_id is None or args.status is None or args.result is None:
            raise self._error("invalid_arguments")
        plan = self._managed(args.expected_revision)
        step = self._step(plan, args.step_id)
        if plan.active_step_id != args.step_id or step.status != "in_progress":
            raise self._error("step_not_current")
        step.status = args.status
        step.result = args.result
        step.finished_at = now
        plan.active_step_id = None
        if args.status != "blocked":
            following = next((step for step in plan.steps if step.status == "pending"), None)
            if following is not None:
                following.status = "in_progress"
                following.started_at = now
                plan.active_step_id = following.step_id
        plan.revision += 1
        return plan

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
            steps: list[str] | None = None,
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
                candidate = self._candidate(args)
                if args.command != "get":
                    if self._persist is not None:
                        await self._persist(
                            candidate.model_copy(deep=True),
                            call_id,
                            args.model_dump(exclude={"call_id"}, exclude_none=True),
                        )
                    self._plan = candidate
                response = self._snapshot_result(candidate)
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
