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
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command
from octop_harness.agent import HarnessAgent
from octop_harness.request import ChatRequest

from octop.infra.agentcomputer.active_runs import ActiveRunRegistry
from octop.infra.agentcomputer.bootstrap import settled_to_thread
from octop.infra.agentcomputer.commands import CommandAdmissionError, canonical_json, content_digest
from octop.infra.agentcomputer.models import (
    CheckpointUnknownReason,
    EngineCheckpointObservation,
    EngineEntryReceipt,
    EngineInvocation,
)
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
        # Native checkpoint metadata retains scalars, not the nested intent.
        metadata=_checkpoint_metadata(invocation),
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


def _checkpoint_metadata(invocation: EngineInvocation) -> dict[str, str | int]:
    intent = invocation.intent
    return {
        "ac_schema_version": 1,
        "ac_box_id": str(intent.boxID),
        "ac_owner_id": intent.ownerID,
        "ac_project_id": str(intent.projectID),
        "ac_task_id": str(intent.taskID),
        "ac_run_id": str(intent.runID),
        "ac_turn_id": str(intent.turnID),
        "ac_dispatch_id": str(intent.dispatchID),
        "ac_engine_request_id": str(intent.engineRequestID),
        "ac_restore_epoch": intent.restoreEpoch,
        "ac_execution_epoch": intent.executionEpoch,
        "ac_claimant_id": str(invocation.claimantID),
        "ac_expected_run_revision": invocation.expectedRunRevision,
        "ac_intent_digest": content_digest(intent.model_dump(mode="json")),
        "ac_snapshot_digest": intent.snapshotDigest,
        "ac_user_message_id": str(intent.userMessageID),
        "ac_agent_id": intent.snapshot.agentID,
        "ac_thread_id": intent.snapshot.octopThreadID,
        "ac_workspace_id": str(intent.snapshot.workspaceID),
    }


class EngineEntryMiddleware(AgentMiddleware[Any, Any]):
    """Commit acceptance before the model and reject all tool execution."""

    def __init__(self, services: SharedServices, invocation: EngineInvocation) -> None:
        self._services = services
        self.invocation = invocation

    def _validate_context(self, state: Any) -> None:
        expected = build_engine_request(self.invocation).to_runnable_config()["configurable"]
        actual = get_config().get("configurable") or {}
        metadata = get_config().get("metadata") or {}
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
                or not isinstance(metadata, dict)
                or any(
                    canonical_json(metadata.get(key)) != canonical_json(value)
                    for key, value in _checkpoint_metadata(self.invocation).items()
                )
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
        result = await settled_to_thread(accept_engine_entry, self._services, self.invocation)
        if result.replayed:
            raise EngineEntryReplayed(result.receipt)

    async def abefore_model(self, state: Any, runtime: Runtime[Any]) -> None:
        from octop.infra.agentcomputer.dispatch import validate_engine_execution

        self._validate_context(state)
        await settled_to_thread(validate_engine_execution, self._services, self.invocation)

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


async def invoke_engine_entry(
    agent: HarnessAgent,
    invocation: EngineInvocation,
    *,
    active_runs: ActiveRunRegistry,
) -> None:
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
    async with active_runs.track(invocation):
        await settled_to_thread(_validate_registered_workspace, agent, middleware)
        async for _ in agent.stream(build_engine_request(invocation), protocol="langgraph"):
            pass


def _checkpoint_matches(checkpoint: CheckpointTuple, invocation: EngineInvocation) -> bool:
    config = checkpoint.config.get("configurable") or {}
    expected = _checkpoint_metadata(invocation)
    return (
        config.get("thread_id") == invocation.intent.snapshot.octopThreadID
        and config.get("checkpoint_ns", "") == ""
        and checkpoint.checkpoint.get("id") == config.get("checkpoint_id")
        and all(
            canonical_json(checkpoint.metadata.get(key)) == canonical_json(value)
            for key, value in expected.items()
        )
    )


def _original_message_matches(messages: Any, invocation: EngineInvocation) -> bool:
    if not isinstance(messages, list):
        return False
    original = [
        message
        for message in messages
        if getattr(message, "id", None) == str(invocation.intent.userMessageID)
    ]
    return (
        len(original) == 1
        and isinstance(original[0], HumanMessage)
        and content_digest(original[0].content)
        == content_digest(invocation.intent.snapshot.request.prompt)
    )


async def inspect_engine_checkpoint(
    agent: HarnessAgent, invocation: EngineInvocation
) -> EngineCheckpointObservation:
    """Inspect an existing trusted runtime, without starting, resuming or repairing it.

    The supplied SQLite saver must already be initialized: its public read APIs
    otherwise run setup and can create files/tables. This probe never initializes
    a saver. A matching input checkpoint only proves persisted request correlation,
    not model start, completion, worker death or permission to replay the Run.
    """
    intent = invocation.intent

    def unknown(reason: CheckpointUnknownReason) -> EngineCheckpointObservation:
        return EngineCheckpointObservation(
            schemaVersion=1,
            engineRequestID=intent.engineRequestID,
            dispatchID=intent.dispatchID,
            runID=intent.runID,
            turnID=intent.turnID,
            restoreEpoch=intent.restoreEpoch,
            executionEpoch=intent.executionEpoch,
            status="unknown",
            reasonCode=reason,
            checkpointID=None,
            checkpointNamespace=None,
        )

    middleware = next(
        (
            item
            for item in agent.config.middleware or []
            if isinstance(item, EngineEntryMiddleware)
            and canonical_json(item.invocation.model_dump(mode="json"))
            == canonical_json(invocation.model_dump(mode="json"))
        ),
        None,
    )
    if (
        middleware is None
        or agent.agent_id != intent.snapshot.agentID
        or agent.config.default_model != intent.snapshot.projectConfiguration.modelRef
        or agent.config.system_prompt != intent.snapshot.projectConfiguration.instructions
    ):
        return unknown("runtimeContextMismatch")
    try:
        await asyncio.to_thread(_validate_registered_workspace, agent, middleware)
    except Exception:
        return unknown("runtimeContextMismatch")
    saver = agent.checkpointer
    if saver is None or saver is False:
        return unknown("checkpointerUnavailable")
    if isinstance(saver, AsyncSqliteSaver) and not saver.is_setup:
        return unknown("checkpointerNotInitialized")
    try:
        # Exhaust the bounded iterator before another saver call. SQLite alist
        # holds its lock while yielding, so nested aget_tuple would deadlock.
        candidates = [
            checkpoint
            async for checkpoint in saver.alist(
                {"configurable": {"thread_id": intent.snapshot.octopThreadID, "checkpoint_ns": ""}},
                filter={"ac_engine_request_id": str(intent.engineRequestID)},
                limit=1,
            )
        ]
        if not candidates:
            return unknown("checkpointNotObserved")
        checkpoint = await saver.aget_tuple(candidates[0].config)
        if checkpoint is None:
            return unknown("checkpointNotObserved")
        if not _checkpoint_matches(checkpoint, invocation):
            return unknown("checkpointBindingMismatch")
        checkpoint_id = UUID(checkpoint.config["configurable"]["checkpoint_id"])
        # messages can be a DeltaChannel; the public graph reader reconstructs
        # the exact saved state without invoking its model or tools.
        state = await agent.graph.aget_state(checkpoint.config)
        state_config = state.config.get("configurable") or {}
        if (
            state_config.get("checkpoint_id") != str(checkpoint_id)
            or state_config.get("thread_id") != intent.snapshot.octopThreadID
            or state_config.get("checkpoint_ns", "") != ""
            or any(
                canonical_json((state.metadata or {}).get(key)) != canonical_json(value)
                for key, value in _checkpoint_metadata(invocation).items()
            )
            or not _original_message_matches((state.values or {}).get("messages"), invocation)
        ):
            return unknown("checkpointBindingMismatch")
    except Exception:
        return unknown("checkpointReadFailed")
    from octop.infra.agentcomputer.dispatch import get_claim

    try:
        claim = await asyncio.to_thread(
            get_claim, middleware._services, owner_id=intent.ownerID, run_id=intent.runID
        )
    except Exception:
        return unknown("controlJournalUnavailable")
    if (
        claim is None
        or canonical_json(claim.intent.model_dump(mode="json"))
        != canonical_json(intent.model_dump(mode="json"))
        or claim.claimantID != invocation.claimantID
    ):
        return unknown("controlJournalMismatch")
    return EngineCheckpointObservation(
        schemaVersion=1,
        engineRequestID=intent.engineRequestID,
        dispatchID=intent.dispatchID,
        runID=intent.runID,
        turnID=intent.turnID,
        restoreEpoch=intent.restoreEpoch,
        executionEpoch=intent.executionEpoch,
        status="requestObserved",
        reasonCode=None,
        checkpointID=checkpoint_id,
        checkpointNamespace="",
    )
