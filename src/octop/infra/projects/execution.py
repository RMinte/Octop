"""Freeze root-task step ownership and publish actual tool execution facts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.tools import BaseTool
from langgraph.config import get_config, get_stream_writer
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel

from octop.i18n import tr

STEP_METADATA = "task_step_id"
BRIEF_METADATA = "brief"
BRIEF_LIMIT = 240
PLANNING_TOOL = "planning"
_BRIEF_EXEMPT_TOOLS = frozenset({PLANNING_TOOL, "message_notify_user", "ask_user_question"})
ToolHandler = Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]


class StepPlanning(Protocol):
    @property
    def phase(self) -> str: ...

    def binding(self, step_id: str | None = None) -> tuple[str | None, str | None]: ...


def _tool_schema(tool: BaseTool) -> dict[str, Any]:
    schema = tool.tool_call_schema
    if isinstance(schema, dict):
        return schema
    return schema.model_json_schema() if issubclass(schema, BaseModel) else schema.schema()


def _business_field(tool: BaseTool | None, key: str) -> bool:
    return tool is not None and key in _tool_schema(tool).get("properties", {})


def strip_step_metadata(call: ToolCall, tool: BaseTool | None = None) -> ToolCall:
    """Copy a call, retaining an identically named parameter owned by the tool."""
    arguments = dict(call["args"])
    if call["name"] != PLANNING_TOOL:
        for key in (STEP_METADATA, BRIEF_METADATA):
            if not _business_field(tool, key):
                arguments.pop(key, None)
    return {**call, "args": arguments}


def _with_step_schema(
    schema: dict[str, Any], description: str, brief: str | None = None
) -> dict[str, Any]:
    copied = deepcopy(schema)
    properties = copied.setdefault("properties", {})
    if STEP_METADATA not in properties:
        properties[STEP_METADATA] = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": description,
        }
    if brief is not None and BRIEF_METADATA not in properties:
        properties[BRIEF_METADATA] = {
            "type": "string",
            "minLength": 1,
            "maxLength": BRIEF_LIMIT,
            "description": brief,
        }
        required = copied.setdefault("required", [])
        if BRIEF_METADATA not in required:
            required.append(BRIEF_METADATA)
    return copied


def _model_tool(
    tool: BaseTool | dict[str, Any], description: str, brief: str | None = None
) -> BaseTool | dict[str, Any]:
    if isinstance(tool, BaseTool):
        if tool.name == PLANNING_TOOL:
            return tool
        return tool.model_copy(
            update={
                "args_schema": _with_step_schema(
                    _tool_schema(tool),
                    description,
                    None if tool.name in _BRIEF_EXEMPT_TOOLS else brief,
                )
            }
        )
    # Preserve provider-native tool dictionaries rather than changing their format.
    copied = deepcopy(tool)
    function = copied.get("function", copied)
    if not isinstance(function, dict) or function.get("name") == PLANNING_TOOL:
        return tool
    schema_key = "input_schema" if "input_schema" in function else "parameters"
    schema = function.get(schema_key)
    if not isinstance(schema, dict):
        return tool
    function[schema_key] = _with_step_schema(
        schema, description, None if function.get("name") in _BRIEF_EXEMPT_TOOLS else brief
    )
    return copied


def _tool_message(result: ToolMessage | Command[Any], call_id: str) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result if result.tool_call_id == call_id else None
    update = result.update
    messages = update.get("messages", []) if isinstance(update, dict) else []
    if isinstance(messages, ToolMessage):
        messages = [messages]
    return next(
        (
            message
            for message in messages
            if isinstance(message, ToolMessage) and message.tool_call_id == call_id
        ),
        None,
    )


@dataclass(frozen=True)
class _StartedTool:
    plan_id: str | None
    step_id: str | None
    started_at: datetime
    clock: float | None


class TaskExecution(AgentMiddleware[Any, Any]):
    """Model-only schemas and facts emitted after the controller authorizes a call.

    This class never authorizes or retries tools. The controller must validate the
    original call before invoking ``run``; only its handler receives stripped args.
    """

    def __init__(
        self,
        planning: StepPlanning,
        locale: str,
        root_turn_id: str,
        *,
        action_timeline_enabled: bool = True,
    ) -> None:
        self.planning = planning
        self.locale = locale
        self.root_turn_id = root_turn_id
        self.action_timeline_enabled = action_timeline_enabled
        self._started: dict[str, _StartedTool] = {}
        self._opening_accepted = False
        self._response_opening: bool | None = None
        self._introduction_calls: set[str] = set()

    def accept_opening(self) -> None:
        """The caller must first confirm that this turn's progress is durable."""
        self._opening_accepted = True

    def restore_model_calls(self, calls: Sequence[ToolCall]) -> None:
        """An opening in this interrupted batch cannot authorize its sibling work."""
        self._response_opening = self._opening_accepted and not any(
            call.get("id") in self._introduction_calls for call in calls
        )

    def _is_root(self) -> bool:
        try:
            configurable = get_config().get("configurable") or {}
        except RuntimeError:
            return False
        return (
            configurable.get("thread_id") == self.root_turn_id
            and configurable.get("ls_agent_type") != "subagent"
        )

    def is_root(self) -> bool:
        """Let the controller reject inherited planning tools before authorization."""
        return self._is_root()

    def restore(self, blocks: Sequence[Mapping[str, Any]]) -> None:
        """Restore only real, unfinished starts; unknown client-tool times stay unknown."""
        for block in blocks:
            if (
                block.get("type") == "progress"
                and block.get("status") == "completed"
                and block.get("introduction") is True
            ):
                self.accept_opening()
                if isinstance(block.get("tool_call_id"), str):
                    self._introduction_calls.add(block["tool_call_id"])
            call_id, started_at = block.get("tool_call_id"), block.get("started_at")
            if (
                block.get("type") != "tool"
                or block.get("status") != "streaming"
                or not isinstance(call_id, str)
                or not isinstance(started_at, str)
            ):
                continue
            try:
                started = datetime.fromisoformat(started_at)
            except ValueError:
                continue
            if started.tzinfo is None:
                continue
            self._started[call_id] = _StartedTool(
                block.get("plan_id"), block.get("step_id"), started, None
            )

    def binding_for_call(
        self, call: ToolCall, tool: BaseTool | None = None
    ) -> tuple[str | None, str | None]:
        step_id = None if _business_field(tool, STEP_METADATA) else call["args"].get(STEP_METADATA)
        if step_id is not None and (not isinstance(step_id, str) or not step_id.strip()):
            raise ValueError("invalid task step metadata")
        return self.planning.binding(step_id)

    def brief_for_call(self, call: ToolCall, tool: BaseTool | None = None) -> str | None:
        if not self.action_timeline_enabled or _business_field(tool, BRIEF_METADATA):
            return None
        value = call["args"].get(BRIEF_METADATA)
        if value is None and call["name"] in _BRIEF_EXEMPT_TOOLS:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > BRIEF_LIMIT:
            raise ValueError("invalid action brief")
        return value.strip()

    def rejection(self, call: ToolCall, tool: BaseTool | None = None) -> str | None:
        if self.planning.phase in {"completed", "blocked"} and call["name"] != PLANNING_TOOL:
            return tr("projects.planning_work_unavailable", self.locale)
        if not self.action_timeline_enabled and call["name"] == "message_notify_user":
            return tr("projects.progress_disabled", self.locale)
        exempt = call["name"] in {"message_notify_user", "ask_user_question"} or (
            call["name"] == PLANNING_TOOL and call["args"].get("command") in ("create", "get")
        )
        opening = (
            self._opening_accepted if self._response_opening is None else self._response_opening
        )
        if self.action_timeline_enabled and not opening and not exempt:
            return tr("projects.progress_opening_required", self.locale)
        if call["name"] != PLANNING_TOOL:
            try:
                self.brief_for_call(call, tool)
            except ValueError:
                return tr("projects.task_brief_invalid", self.locale)
        return None

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        if not self._is_root():
            return await handler(request)
        self._response_opening = self._opening_accepted
        plan_id, step_id = self.planning.binding()
        get_stream_writer()({"type": "task_step_context", "plan_id": plan_id, "step_id": step_id})
        description = tr("projects.task_step_metadata", self.locale)
        if self.planning.phase in {"completed", "blocked"}:
            return await handler(request.override(tools=[], tool_choice=None))
        brief = (
            tr("projects.task_brief_metadata", self.locale)
            if self.action_timeline_enabled
            else None
        )
        tools = [_model_tool(tool, description, brief) for tool in request.tools]
        return await handler(request.override(tools=tools))

    def _finish(
        self, call_id: str, started: _StartedTool, message: ToolMessage | None, *, failed: bool
    ) -> None:
        finished = datetime.now(UTC)
        elapsed = (
            time.monotonic() - started.clock
            if started.clock is not None
            else (finished - started.started_at).total_seconds()
        )
        get_stream_writer()(
            {
                "type": "task_tool_end",
                "call_id": call_id,
                "plan_id": started.plan_id,
                "step_id": started.step_id,
                "finished_at": finished.isoformat(),
                "duration_ms": max(0, int(elapsed * 1000)),
                "failed": failed,
                # Artifacts and additional_kwargs can contain private provider context.
                "output": deepcopy(message.content) if message is not None else None,
            }
        )
        self._started.pop(call_id, None)

    async def run(
        self, request: ToolCallRequest, handler: ToolHandler
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        if not self._is_root():
            return await handler(request)
        rejected = self.rejection(call, request.tool)
        if rejected is not None:
            return ToolMessage(
                content=rejected, name=call["name"], tool_call_id=str(call["id"]), status="error"
            )
        if call["name"] == PLANNING_TOOL:
            return await handler(request)
        call_id = str(call["id"])
        started = self._started.get(call_id)
        if started is None:
            try:
                plan_id, step_id = self.binding_for_call(call, request.tool)
            except ValueError:
                return ToolMessage(
                    content=tr("projects.task_step_invalid", self.locale),
                    name=call["name"],
                    tool_call_id=call_id,
                    status="error",
                )
            try:
                brief = self.brief_for_call(call, request.tool)
            except ValueError:
                return ToolMessage(
                    content=tr("projects.task_brief_invalid", self.locale),
                    name=call["name"],
                    tool_call_id=call_id,
                    status="error",
                )
            started = _StartedTool(plan_id, step_id, datetime.now(UTC), time.monotonic())
            self._started[call_id] = started
            get_stream_writer()(
                {
                    "type": "task_tool_start",
                    "call_id": call_id,
                    "name": call["name"],
                    "arguments": deepcopy(strip_step_metadata(call, request.tool)["args"]),
                    "plan_id": plan_id,
                    "step_id": step_id,
                    "started_at": started.started_at.isoformat(),
                    "brief": brief,
                }
            )
        actual = request.override(tool_call=strip_step_metadata(call, request.tool))
        try:
            result = await handler(actual)
        except GraphBubbleUp:
            # Interruptions are durable waits, not failed or completed tool executions.
            raise
        except asyncio.CancelledError:
            self._finish(call_id, started, None, failed=True)
            raise
        except Exception:
            self._finish(call_id, started, None, failed=True)
            raise
        message = _tool_message(result, call_id)
        self._finish(
            call_id, started, message, failed=message is not None and message.status == "error"
        )
        return result
