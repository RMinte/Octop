"""Public harness boundary for a single, durably fenced managed invocation.

This module neither creates a runtime nor enables production execution. The
caller supplies a trusted harness with the bound middleware; receiving a Run
is a separate durable fact from completing it or performing tool effects.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command
from octop_harness.agent import HarnessAgent
from octop_harness.request import ChatRequest

from octop.infra.agentcomputer.commands import CommandAdmissionError, canonical_json
from octop.infra.agentcomputer.models import EngineEntryReceipt, EngineInvocation
from octop.infra.db.services import SharedServices


class EngineEntryReplayed(Exception):
    """An earlier durable receipt stops a duplicate before another model loop."""

    def __init__(self, receipt: EngineEntryReceipt) -> None:
        super().__init__("engine entry already accepted")
        self.receipt = receipt


def build_engine_request(invocation: EngineInvocation) -> ChatRequest:
    """Build every request field from the fixed server-side dispatch intent."""
    intent = invocation.intent
    snapshot = intent.snapshot
    messages: list[BaseMessage] = [
        HumanMessage(content=snapshot.request.prompt, id=str(intent.userMessageID))
    ]
    request = ChatRequest(
        # BaseMessage input intentionally treats runtime slash syntax as text.
        messages=messages,
        thread_id=snapshot.octopThreadID,
        user=str(intent.ownerID),
        source="agentcomputer",
        model=snapshot.projectConfiguration.modelRef,
        agent_id=snapshot.agentID,
        skills=[],
        mcp_servers=[],
        mcp_use_default=False,
        conversation_mode="craft",
        configurable={"agentcomputer": invocation.model_dump(mode="json")},
    )
    expected = {
        "thread_id": snapshot.octopThreadID,
        "user": str(intent.ownerID),
        "source": "agentcomputer",
        "model": snapshot.projectConfiguration.modelRef,
        "agent_id": snapshot.agentID,
        "skills": [],
        "mcp_servers": [],
        "conversation_mode": "craft",
        "agentcomputer": invocation.model_dump(mode="json"),
    }
    if request.to_runnable_config().get("configurable") != expected:
        raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
    return request


class EngineEntryMiddleware(AgentMiddleware[Any, Any]):
    """Commit acceptance before the model and reject all tool execution."""

    def __init__(self, services: SharedServices, invocation: EngineInvocation) -> None:
        self._services = services
        self.invocation = invocation

    def _validate_context(self, state: Any) -> None:
        expected = build_engine_request(self.invocation).to_runnable_config()["configurable"]
        actual = get_config().get("configurable") or {}
        try:
            if (
                not isinstance(actual, dict)
                or any(
                    canonical_json(actual.get(key)) != canonical_json(value)
                    for key, value in expected.items()
                )
                or actual.get("mcp_use_default", False) is not False
                or actual.get("team_peers")
                or actual.get("conversation_mode_extra_read_tools")
            ):
                raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
        except (TypeError, ValueError) as exc:
            raise CommandAdmissionError("accessDenied", reason="engineContextMismatch") from exc
        message_id = str(self.invocation.intent.userMessageID)
        messages = state.get("messages", []) if isinstance(state, dict) else []
        original = [message for message in messages if getattr(message, "id", None) == message_id]
        if (
            len(original) != 1
            or not isinstance(original[0], HumanMessage)
            or original[0].content != self.invocation.intent.snapshot.request.prompt
        ):
            raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")

    async def abefore_agent(self, state: Any, runtime: Runtime[Any]) -> None:
        from octop.infra.agentcomputer.dispatch import accept_engine_entry

        self._validate_context(state)
        result = await asyncio.to_thread(accept_engine_entry, self._services, self.invocation)
        if result.replayed:
            raise EngineEntryReplayed(result.receipt)

    async def abefore_model(self, state: Any, runtime: Runtime[Any]) -> None:
        from octop.infra.agentcomputer.dispatch import validate_engine_execution

        self._validate_context(state)
        await asyncio.to_thread(validate_engine_execution, self._services, self.invocation)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        raise CommandAdmissionError("runtimeUnavailable", reason="toolExecutionUnavailable")


def _validate_registered_workspace(agent: HarnessAgent, middleware: EngineEntryMiddleware) -> None:
    intent = middleware.invocation.intent
    try:
        with middleware._services.db.transaction() as conn:
            row = middleware._services.agent_repo.get_in_transaction(conn, intent.snapshot.agentID)
            if (
                row is None
                or row.user_id != intent.ownerID
                or row.managed_by != "agentcomputer"
                or row.managed_project_id != str(intent.projectID)
                or row.enabled
                or row.last_state != "stopped"
                or row.config_json is None
            ):
                raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
            saved = json.loads(row.config_json)
            if not isinstance(saved, dict) or set(saved) != {
                "backend",
                "workspace_dir",
                "system_files_path",
            }:
                raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
            saved["workspace_dir"] = Path(saved["workspace_dir"]).as_posix()
            actual = {
                "backend": agent.config.backend,
                "workspace_dir": Path(agent.config.workspace_dir).as_posix(),
                "system_files_path": agent.config.system_files_path,
            }
            if canonical_json(actual) != canonical_json(saved):
                raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
    except (TypeError, ValueError) as exc:
        raise CommandAdmissionError("accessDenied", reason="engineContextMismatch") from exc


async def invoke_engine_entry(agent: HarnessAgent, invocation: EngineInvocation) -> None:
    """Consume the public stream without accepting client configuration or kwargs."""
    snapshot = invocation.intent.snapshot
    middleware = next(
        (
            item
            for item in agent.config.middleware or []
            if isinstance(item, EngineEntryMiddleware) and item.invocation == invocation
        ),
        None,
    )
    if (
        agent.agent_id != snapshot.agentID
        or agent.config.default_model != snapshot.projectConfiguration.modelRef
        or agent.config.system_prompt != snapshot.projectConfiguration.instructions
        or middleware is None
    ):
        raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
    await asyncio.to_thread(_validate_registered_workspace, agent, middleware)
    async for _ in agent.stream(build_engine_request(invocation), protocol="langgraph"):
        pass
