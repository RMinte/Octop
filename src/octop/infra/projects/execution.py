"""Freeze root-task step ownership and publish actual tool execution facts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.tools import BaseTool, ToolException
from langgraph.config import get_config, get_stream_writer
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel

from octop.i18n import tr
from octop.infra.projects.artifacts import SERVER_ARTIFACT_TOOLS
from octop.infra.projects.file_changes import (
    FILE_CHANGES_METADATA,
    change_path,
    changes,
    recorded_file_changes,
    snapshot,
)

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace

    from octop.infra.projects.artifacts import TaskArtifactPublisher
    from octop.infra.projects.receipts import TaskReceipts

STEP_METADATA = "task_step_id"
BRIEF_METADATA = "brief"
BRIEF_LIMIT = 240
PLANNING_TOOL = "planning"
_SERVER_PROJECT_WRITES = frozenset({"write_file", "edit_file", "execute"})
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


def _with_step_schema(schema: dict[str, Any], description: str) -> dict[str, Any]:
    copied = deepcopy(schema)
    properties = copied.setdefault("properties", {})
    if STEP_METADATA not in properties:
        properties[STEP_METADATA] = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": description,
        }
    return copied


def _tool_name(tool: BaseTool | dict[str, Any]) -> str | None:
    if isinstance(tool, BaseTool):
        return tool.name
    function = tool.get("function", tool)
    name = function.get("name") if isinstance(function, dict) else None
    return name if isinstance(name, str) else None


def _model_tool(tool: BaseTool | dict[str, Any], description: str) -> BaseTool | dict[str, Any]:
    if isinstance(tool, BaseTool):
        if tool.name == PLANNING_TOOL:
            return tool
        return tool.model_copy(
            update={"args_schema": _with_step_schema(_tool_schema(tool), description)}
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
    function[schema_key] = _with_step_schema(schema, description)
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
        artifact_publisher: TaskArtifactPublisher | None = None,
        client_directory: str | None = None,
    ) -> None:
        self.planning = planning
        self.locale = locale
        self.root_turn_id = root_turn_id
        self.action_timeline_enabled = action_timeline_enabled
        self.artifact_publisher = artifact_publisher
        self.client_directory = client_directory
        self.workspace: BackendWorkspace | None = None
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._wrote_files = False
        self._missing_delivery = False
        self._started: dict[str, _StartedTool] = {}

    def _record_write(self, name: str, arguments: Mapping[str, Any]) -> None:
        path = arguments.get("file_path", arguments.get("path"))
        if (
            name in {"write_file", "edit_file"}
            and isinstance(path, str)
            and path.lstrip("/").split("/", 1)[0] not in {".octop", "inbound"}
        ):
            self._wrote_files = True

    async def delivery_pending(self) -> bool:
        self._missing_delivery = (
            not self.client_directory
            and self.artifact_publisher is not None
            and self._wrote_files
            and not await self.artifact_publisher.has_artifacts()
        )
        return self._missing_delivery

    async def require_delivery(self) -> None:
        """Keep a standalone file-producing task open until a real attachment exists."""
        if await self.delivery_pending():
            raise ToolException(tr("projects.artifacts_delivery_required", self.locale))

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
                block.get("type") == "tool"
                and block.get("status") == "completed"
                and block.get("started_at")
                and isinstance(block.get("arguments"), Mapping)
            ):
                self._record_write(str(block.get("tool_name")), block["arguments"])
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
        if not isinstance(value, str) or not value.strip() or len(value) > BRIEF_LIMIT:
            return None
        return value.strip()

    def rejection(self, call: ToolCall, tool: BaseTool | None = None) -> str | None:
        if rejected := self._client_write_rejection(call["name"]):
            return rejected
        if (
            self.planning.phase in {"completed", "blocked"}
            and call["name"] != PLANNING_TOOL
            and not (
                self.planning.phase == "completed"
                and self._missing_delivery
                and call["name"] in SERVER_ARTIFACT_TOOLS
            )
        ):
            return tr("projects.planning_work_unavailable", self.locale)
        if not self.action_timeline_enabled and call["name"] == "message_notify_user":
            return tr("projects.progress_disabled", self.locale)
        return None

    def _client_write_rejection(self, name: str) -> str | None:
        if self.client_directory and name in _SERVER_PROJECT_WRITES:
            return tr("projects.client_workspace_required", self.locale)
        return None

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        if self.client_directory:
            request = request.override(
                tools=[
                    tool for tool in request.tools if _tool_name(tool) not in _SERVER_PROJECT_WRITES
                ]
            )
        if not self._is_root():
            return await handler(request)
        plan_id, step_id = self.planning.binding()
        delivery_only = self.planning.phase == "completed" and await self.delivery_pending()
        tools_disabled = self.planning.phase in {"completed", "blocked"} and not delivery_only
        get_stream_writer()(
            {
                "type": "task_step_context",
                "plan_id": plan_id,
                "step_id": step_id,
                "tools_disabled": tools_disabled,
            }
        )
        description = tr("projects.task_step_metadata", self.locale)
        if tools_disabled:
            return await handler(request.override(tools=[], tool_choice=None))
        tools = [
            _model_tool(tool, description)
            for tool in request.tools
            if _tool_name(tool) != "message_notify_user"
            and (not delivery_only or _tool_name(tool) in SERVER_ARTIFACT_TOOLS)
        ]
        actual = request.override(tools=tools)
        correction = tr("projects.artifacts_delivery_required", self.locale)
        if delivery_only:
            actual = actual.override(
                messages=[*actual.messages, HumanMessage(content=correction)],
                tool_choice="required",
            )
        response = await handler(actual)

        def has_tools(result: ModelResponse[Any]) -> bool:
            return any(
                isinstance(message, AIMessage)
                and bool(message.tool_calls or message.invalid_tool_calls)
                for message in result.result
            )

        if not has_tools(response) and self.planning.phase in {"unplanned", "completed"}:
            if delivery_only:
                await self.require_delivery()
            elif await self.delivery_pending():
                # The first answer is still buffered by TaskResponseText. Start a new
                # response so it becomes process text before asking for a real delivery.
                get_stream_writer()(
                    {
                        "type": "task_step_context",
                        "plan_id": plan_id,
                        "step_id": step_id,
                        "tools_disabled": False,
                    }
                )
                response = await handler(
                    actual.override(
                        messages=[
                            *actual.messages,
                            *[
                                message
                                for message in response.result
                                if isinstance(message, AIMessage)
                            ],
                            HumanMessage(content=correction),
                        ],
                        tool_choice="required",
                    )
                )
                if not has_tools(response):
                    await self.require_delivery()
        return response

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
                "file_changes": [
                    change.model_dump(mode="json")
                    for change in (recorded_file_changes(message) or [])
                ]
                if message is not None
                else [],
            }
        )
        self._started.pop(call_id, None)

    async def run(
        self,
        request: ToolCallRequest,
        handler: ToolHandler,
        *,
        receipts: TaskReceipts | None = None,
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        root = self._is_root()
        rejected = (
            self.rejection(call, request.tool)
            if root
            else self._client_write_rejection(call["name"])
        )
        if rejected is not None:
            return ToolMessage(
                content=rejected, name=call["name"], tool_call_id=str(call["id"]), status="error"
            )
        if not root:
            return await handler(request)
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
            brief = self.brief_for_call(call, request.tool)
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

        async def capture(authorized: ToolCallRequest) -> ToolMessage | Command[Any]:
            path = change_path(call["name"], authorized.tool_call["args"])
            if path is None:
                return await handler(authorized)
            # ToolNode dispatches batches concurrently. Keep observations of the
            # same file around exactly one authorized operation at a time.
            async with self._file_locks.setdefault(path, asyncio.Lock()):
                before = await snapshot(self.workspace, path)
                result = await handler(authorized)
                message = _tool_message(result, call_id)
                if message is not None and message.status != "error":
                    file_changes = await changes(self.workspace, before)
                    artifact = dict(message.artifact) if isinstance(message.artifact, dict) else {}
                    if message.artifact is not None and not isinstance(message.artifact, dict):
                        artifact["tool_artifact"] = message.artifact
                    artifact[FILE_CHANGES_METADATA] = [
                        change.model_dump(mode="json") for change in file_changes
                    ]
                    # ToolMessage artifacts are persisted with the receipt and
                    # are excluded from model input by LangChain adapters.
                    message.artifact = artifact
                return result

        try:
            result = (
                await receipts.run(actual, capture)
                if receipts is not None
                else await capture(actual)
            )
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
        if message is not None and message.status != "error":
            self._record_write(call["name"], actual.tool_call["args"])
        self._finish(
            call_id, started, message, failed=message is not None and message.status == "error"
        )
        return result
