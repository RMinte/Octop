"""Cooperative task pause boundaries and one-shot execution of approved actions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command, interrupt

from octop.i18n import tr
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_policy, interactions
from octop.infra.projects.execution import TaskExecution
from octop.infra.projects.interaction_models import ApprovalPayload, AskQuestions
from octop.infra.projects.planning import PLANNING

ASK_USER = "ask_user_question"


def question_tool(locale: str) -> StructuredTool:
    async def unavailable(**arguments: Any) -> str:
        return tr("projects.question_unavailable", locale)

    return StructuredTool.from_function(
        name=ASK_USER,
        description=tr("projects.question_tool_description", locale),
        args_schema=AskQuestions,
        coroutine=unavailable,
    )


class TaskControlMiddleware(AgentMiddleware[Any, Any]):
    def __init__(
        self,
        services: SharedServices,
        owner: int,
        turn: ProjectTaskTurnRow,
        locale: str,
        execution: TaskExecution | None = None,
    ):
        self.services, self.owner, self.turn, self.locale = services, owner, turn, locale
        self.execution = execution

    async def _pause(self) -> None:
        current = await asyncio.to_thread(
            self.services.project_task_turn_repo.get, self.turn.turn_id
        )
        if current is not None and current.cancel_requested:
            raise asyncio.CancelledError
        if (
            current is None
            or current.status != "running"
            or current.worker_id != self.turn.worker_id
        ):
            raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
        if current.pause_requested:
            interrupt({"type": "user_pause"})

    async def abefore_model(self, state: Any, runtime: Any) -> None:
        await self._pause()

    async def aafter_model(self, state: Any, runtime: Any) -> None:
        await self._pause()

    def _authorize(self, call: ToolCall) -> bool | None:
        with self.services.db.transaction() as conn:
            if not self.services.project_task_repo.lock_owner(conn, self.owner):
                return False
            current = self.services.project_task_turn_repo.get_in_transaction(
                conn, self.turn.turn_id
            )
            if (
                current is None
                or current.status != "running"
                or current.cancel_requested
                or current.worker_id != self.turn.worker_id
            ):
                return False
            if current.pause_requested:
                return None
            items = interactions.load(current)
            approvals = [i for i in items if i.kind == "approval" and i.tool_call_id == call["id"]]
            settings = approval_policy.settings(self.services)
            if not approvals:
                return (
                    call["name"] != "execute" and call["name"] not in settings.require_human_tools
                )
            for item in approvals:
                payload = item.payload
                if (
                    not isinstance(payload, ApprovalPayload)
                    or payload.tool_name != call["name"]
                    or payload.arguments != call["args"]
                    or item.dispatch_started
                    or item.status != "resolved"
                    or item.resolution is None
                    or item.resolution.outcome != "allow_once"
                ):
                    return False
                if (
                    item.resolution.source in {"auto_reviewer", "full_access_mode"}
                    and payload.tool_name in settings.require_human_tools
                ):
                    return False
            for item in approvals:
                item.dispatch_started = True
                item.revision += 1
            self.services.project_task_turn_repo.save_interactions(
                conn, current.turn_id, interactions.dump(items), int(time.time() * 1000)
            )
            return True

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        await self._pause()
        if (
            request.tool_call["name"] == PLANNING
            and self.execution is not None
            and not self.execution.is_root()
        ):
            return ToolMessage(
                content=tr("projects.planning_root_only", self.locale),
                name=PLANNING,
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
        messages = request.state.get("messages", []) if isinstance(request.state, dict) else []
        latest = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if (
            latest is not None
            and len(latest.tool_calls) > 1
            and any(call["name"] == PLANNING for call in latest.tool_calls)
        ):
            # ToolNode executes batches concurrently. Reject the whole batch before
            # any plan mutation or business action can race with a change of step.
            return ToolMessage(
                content=tr("projects.planning_mixed_batch", self.locale),
                name=request.tool_call["name"],
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
        rejected = (
            self.execution.rejection(request.tool_call, request.tool)
            if self.execution is not None and self.execution.is_root()
            else None
        )
        if rejected is not None:
            return ToolMessage(
                content=rejected,
                name=request.tool_call["name"],
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
        authorized = await asyncio.to_thread(self._authorize, request.tool_call)
        if authorized is None:
            interrupt({"type": "user_pause"})
        if not authorized:
            return ToolMessage(
                content=tr("projects.approval_not_granted", self.locale),
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
        if self.execution is not None:
            return await self.execution.run(request, handler)
        return await handler(request)
