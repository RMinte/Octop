"""Public harness boundary for a single, durably fenced managed invocation.

This module neither creates a runtime nor enables production execution. The
caller supplies a trusted harness with the bound middleware; receiving a Run
is a separate durable fact from completing it or performing tool effects.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command
from octop_harness.agent import HarnessAgent
from octop_harness.request import ChatRequest

from octop.infra.agentcomputer.active_runs import ActiveRunRegistry
from octop.infra.agentcomputer.bootstrap import settle_task, settled_to_thread
from octop.infra.agentcomputer.commands import CommandAdmissionError, canonical_json, content_digest
from octop.infra.agentcomputer.completions import (
    CompletionEvidence,
    ToolCompletionEvidence,
    complete_run,
)
from octop.infra.agentcomputer.effect_runner import execute_effect
from octop.infra.agentcomputer.models import (
    CheckpointUnknownReason,
    EngineCheckpointObservation,
    EngineEntryReceipt,
    EngineInvocation,
    ToolEffectAction,
    ToolEffectOutput,
    ToolEffectReceipt,
)
from octop.infra.agentcomputer.project_document_tool import (
    TOOL_NAME,
    document_read_action,
    execute_document_read,
)
from octop.infra.db.services import SharedServices
from octop.infra.gateway.process.message_keys import STREAM_ERROR_FLAG
from octop.infra.history.recorder import RecordingTracker
from octop.infra.history.service import HistoryArchive
from octop.infra.history.store import BoundToolExpectation, probe_bound_completion

if TYPE_CHECKING:
    from octop.infra.agents.manager import AgentManager


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
    """Commit acceptance before the model and journal one scoped document read."""

    def __init__(
        self,
        services: SharedServices,
        invocation: EngineInvocation,
        history_archive: HistoryArchive,
        registry: AgentManager | None = None,
    ) -> None:
        self._services = services
        self.invocation = invocation
        self.history_archive = history_archive
        self.registry = registry
        self.history_tracker: RecordingTracker | None = None
        self.tool_action: ToolEffectAction | None = None
        self.tool_receipt: ToolEffectReceipt | None = None
        self.tool_message_id: str | None = None
        self._tool_attempted = False

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
        if not self.history_archive.enabled:
            raise CommandAdmissionError("runtimeUnavailable", reason="historyUnavailable")
        result = await settled_to_thread(accept_engine_entry, self._services, self.invocation)
        if result.replayed:
            raise EngineEntryReplayed(result.receipt)
        intent = self.invocation.intent
        turn, cancelled = await settle_task(
            asyncio.create_task(
                asyncio.to_thread(
                    self.history_archive.begin_bound,
                    intent.snapshot.agentID,
                    intent.snapshot.octopThreadID,
                    turn_id=str(intent.turnID),
                )
            )
        )
        if cancelled:
            await settled_to_thread(self.history_archive.finish, turn["id"], "interrupted")
            raise asyncio.CancelledError
        try:
            tracker = RecordingTracker(
                self.history_archive,
                turn,
                [
                    HumanMessage(
                        content=intent.snapshot.request.prompt, id=str(intent.userMessageID)
                    )
                ],
            )
            self.history_tracker = tracker
            await tracker.flush()
        except BaseException as exc:
            status = "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
            await settle_task(
                asyncio.create_task(
                    asyncio.to_thread(
                        self.history_archive.finish,
                        turn["id"],
                        status,
                        error="initial_capture_failed",
                    )
                )
            )
            self.history_tracker = None
            raise

    async def abefore_model(self, state: Any, runtime: Runtime[Any]) -> None:
        from octop.infra.agentcomputer.dispatch import validate_engine_execution

        self._validate_context(state)
        if self.history_tracker is None:
            raise CommandAdmissionError("runtimeUnavailable", reason="historyTurnUnavailable")
        await settled_to_thread(validate_engine_execution, self._services, self.invocation)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        state = request.state
        messages = state.get("messages") if isinstance(state, dict) else None
        if (
            self.registry is None
            or self.history_tracker is None
            or self._tool_attempted
            or not isinstance(call, dict)
            or call.get("name") != TOOL_NAME
            or not isinstance(call.get("id"), str)
            or not isinstance(call.get("args"), dict)
            or not isinstance(messages, list)
            or not messages
            or not isinstance(messages[-1], AIMessage)
            or len(messages[-1].tool_calls) != 1
            or messages[-1].invalid_tool_calls
            or canonical_json(messages[-1].tool_calls[0]) != canonical_json(call)
        ):
            raise CommandAdmissionError("runtimeUnavailable", reason="toolExecutionUnavailable")
        self._validate_context(state)
        self._tool_attempted = True
        action = await settled_to_thread(
            document_read_action,
            self.registry,
            self.invocation,
            tool_call_id=call["id"],
            arguments=call["args"],
        )

        async def authorize() -> bool:
            from octop.infra.agentcomputer.dispatch import validate_engine_execution

            await settled_to_thread(validate_engine_execution, self._services, self.invocation)
            return True

        async def execute() -> ToolEffectOutput:
            assert self.registry is not None
            return await execute_document_read(self.registry, self.invocation, action)

        receipt = await execute_effect(
            self._services,
            invocation=self.invocation,
            action=action,
            authorize=authorize,
            handler=execute,
        )
        if receipt.outcome != "succeeded" or receipt.output is None:
            raise CommandAdmissionError("runtimeUnavailable", reason="toolExecutionUnavailable")
        message_id = str(uuid4())
        self.tool_action = action
        self.tool_receipt = receipt
        self.tool_message_id = message_id
        return ToolMessage(
            content=receipt.output.resultJSON,
            tool_call_id=action.toolCallID,
            name=TOOL_NAME,
            id=message_id,
            status="success",
        )


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


def _is_pre_entry_event(chunk: Any, invocation: EngineInvocation) -> bool:
    """Ignore only input and setup events emitted before the bound hook runs."""
    if not isinstance(chunk, dict):
        return False
    if chunk.get("type") == "state_update":
        node = chunk.get("node")
        data = chunk.get("data")
        return (
            isinstance(node, str)
            and node.endswith(".before_agent")
            and (data is None or (isinstance(data, dict) and "messages" not in data))
        )
    if chunk.get("type") != "state_snapshot":
        return False
    data = chunk.get("data")
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    intent = invocation.intent
    return (
        isinstance(last, HumanMessage)
        and last.id == str(intent.userMessageID)
        and last.content == intent.snapshot.request.prompt
    )


async def invoke_engine_entry(
    agent: HarnessAgent,
    invocation: EngineInvocation,
    *,
    active_runs: ActiveRunRegistry,
    history_archive: HistoryArchive,
) -> None:
    """Capture the bound native turn while consuming the public harness stream."""
    from octop.infra.agentcomputer.dispatch import get_claim

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
        or middleware.history_archive is not history_archive
    ):
        raise CommandAdmissionError("accessDenied", reason="engineContextMismatch")
    async with active_runs.track(invocation):
        await settled_to_thread(_validate_registered_workspace, agent, middleware)
        current = await settled_to_thread(
            get_claim,
            middleware._services,
            owner_id=invocation.intent.ownerID,
            run_id=invocation.intent.runID,
        )
        if (
            current is None
            or current.intent != invocation.intent
            or current.claimantID != invocation.claimantID
        ):
            raise CommandAdmissionError("revisionConflict")
        if current.engineReceipt is not None:
            raise EngineEntryReplayed(current.engineReceipt)
        completed = False
        stream_ended_at: datetime | None = None
        try:
            async for chunk in agent.stream(build_engine_request(invocation), protocol="langgraph"):
                tracker = middleware.history_tracker
                if tracker is None:
                    if _is_pre_entry_event(chunk, invocation):
                        continue
                    raise CommandAdmissionError(
                        "runtimeUnavailable", reason="historyTurnUnavailable"
                    )
                tracker.observe(chunk)
                await tracker.flush()
            completed = True
            stream_ended_at = datetime.now(UTC)
        except Exception:
            if middleware.history_tracker is not None:
                middleware.history_tracker.failed = True
            raise
        finally:
            tracker = middleware.history_tracker
            if tracker is not None:
                try:
                    _, cancelled = await settle_task(
                        asyncio.create_task(tracker.finish(completed=completed))
                    )
                finally:
                    middleware.history_tracker = None
                if cancelled:
                    raise asyncio.CancelledError
        # A transient in-memory saver cannot supply durable terminal evidence.
        # The trusted persistent path must either settle an exact, tool-free
        # answer or let dispatch fence the handoff as uncertain.
        if (
            completed
            and stream_ended_at is not None
            and isinstance(agent.checkpointer, AsyncSqliteSaver)
        ):
            await _complete_persisted_entry(
                agent, invocation, middleware, history_archive, stream_ended_at
            )


async def _complete_persisted_entry(
    agent: HarnessAgent,
    invocation: EngineInvocation,
    middleware: EngineEntryMiddleware,
    history_archive: HistoryArchive,
    stream_ended_at: datetime,
) -> None:
    """Join independent archive and checkpoint proof before the final DB CAS."""
    action = middleware.tool_action
    tool_receipt = middleware.tool_receipt
    tool_message_id = middleware.tool_message_id
    if any(value is not None for value in (action, tool_receipt, tool_message_id)) and not all(
        value is not None for value in (action, tool_receipt, tool_message_id)
    ):
        raise CommandAdmissionError("runtimeUnavailable", reason="completionEvidenceUnavailable")
    expected_tool: BoundToolExpectation | None = None
    if action is not None and tool_receipt is not None and tool_message_id is not None:
        if tool_receipt.outcome != "succeeded" or tool_receipt.output is None:
            raise CommandAdmissionError(
                "runtimeUnavailable", reason="completionEvidenceUnavailable"
            )
        expected_tool = BoundToolExpectation(
            tool_call_id=action.toolCallID,
            tool_name=action.toolName,
            tool_arguments_digest=action.argumentsDigest,
            tool_content_digest=content_digest(tool_receipt.output.resultJSON),
        )
    terminal = await inspect_terminal_checkpoint(agent, invocation, expected_tool=expected_tool)
    if terminal is None:
        raise CommandAdmissionError("runtimeUnavailable", reason="completionEvidenceUnavailable")
    services = middleware._services
    if not services.config.database.is_sqlite:
        raise CommandAdmissionError("runtimeUnavailable", reason="historyUnavailable")
    intent = invocation.intent
    history = await settled_to_thread(
        probe_bound_completion,
        history_archive.store.path,
        identity=str(services.config.database.resolve_sqlite_path(services.paths.root).resolve()),
        turn_id=str(intent.turnID),
        thread_id=intent.snapshot.octopThreadID,
        agent_id=intent.snapshot.agentID,
        user_message_id=str(intent.userMessageID),
        prompt=intent.snapshot.request.prompt,
        expected_tool=expected_tool,
    )
    if (
        history.status != "complete"
        or history.assistant_message_id is None
        or history.assistant_content_digest is None
    ):
        raise CommandAdmissionError("runtimeUnavailable", reason="completionEvidenceUnavailable")
    tool_fields: dict[str, str] = {}
    if expected_tool is not None:
        assert tool_message_id is not None
        if (
            history.tool_call_id != expected_tool.tool_call_id
            or history.tool_name != expected_tool.tool_name
            or history.tool_arguments_digest != expected_tool.tool_arguments_digest
            or history.tool_content_digest != expected_tool.tool_content_digest
            or history.tool_message_id != tool_message_id
            or terminal.tool_message_id != tool_message_id
            or terminal.tool_call_id != expected_tool.tool_call_id
            or terminal.tool_content_digest != expected_tool.tool_content_digest
        ):
            raise CommandAdmissionError(
                "runtimeUnavailable", reason="completionEvidenceUnavailable"
            )
        tool_fields = {
            "toolCallID": expected_tool.tool_call_id,
            "toolName": expected_tool.tool_name,
            "toolArgumentsDigest": expected_tool.tool_arguments_digest,
            "toolMessageID": tool_message_id,
            "toolContentDigest": expected_tool.tool_content_digest,
        }
    evidence_fields: dict[str, Any] = {
        "schemaVersion": 2 if expected_tool is not None else 1,
        "historyTurnID": intent.turnID,
        "historyThreadID": intent.snapshot.octopThreadID,
        "historyAgentID": intent.snapshot.agentID,
        "historyStatus": "complete",
        "historyDigest": content_digest(
            {
                "schemaVersion": 2 if expected_tool is not None else 1,
                "turnID": str(intent.turnID),
                "threadID": intent.snapshot.octopThreadID,
                "agentID": intent.snapshot.agentID,
                "status": "complete",
                "assistantMessageID": history.assistant_message_id,
                "assistantContentDigest": history.assistant_content_digest,
                **tool_fields,
            }
        ),
        "historyAssistantMessageID": history.assistant_message_id,
        "historyAssistantContentDigest": history.assistant_content_digest,
        "terminalCheckpointID": terminal.checkpoint_id,
        "terminalCheckpointDigest": terminal.checkpoint_digest,
        "terminalAssistantMessageID": terminal.assistant_message_id,
        "terminalAssistantContentDigest": terminal.assistant_content_digest,
        "engineStreamEndedAt": stream_ended_at,
    }
    if expected_tool is not None:
        assert action is not None and tool_receipt is not None and tool_message_id is not None
        evidence_fields.update(
            toolEffectID=tool_receipt.effectID,
            toolReceiptDigest=content_digest(tool_receipt.model_dump(mode="json")),
            **tool_fields,
        )
        evidence: CompletionEvidence | ToolCompletionEvidence = (
            ToolCompletionEvidence.model_validate(evidence_fields)
        )
    else:
        evidence = CompletionEvidence.model_validate(evidence_fields)
    await settled_to_thread(complete_run, services, invocation, evidence)


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


@dataclass(frozen=True)
class TerminalCheckpointEvidence:
    """A bound, persisted graph terminal state for one invocation.

    This is checkpoint evidence, not permission to finish a Run or release its
    lanes. The control transaction must independently fence cancellation and
    any tool effect before using it.
    """

    checkpoint_id: UUID
    assistant_message_id: str
    assistant_content_digest: str
    checkpoint_digest: str
    tool_call_id: str | None = None
    tool_message_id: str | None = None
    tool_content_digest: str | None = None


async def inspect_terminal_checkpoint(
    agent: HarnessAgent,
    invocation: EngineInvocation,
    *,
    expected_tool: BoundToolExpectation | None = None,
) -> TerminalCheckpointEvidence | None:
    """Read one fixed native checkpoint; never infer completion from stream EOF.

    Harness cancellation can end a stream normally while its graph still has
    work to do. For now only an already initialized, persistent SQLite saver
    is accepted as natural-completion evidence.
    """
    saver = agent.checkpointer
    if not isinstance(saver, AsyncSqliteSaver) or not saver.is_setup:
        return None
    observation = await inspect_engine_checkpoint(agent, invocation)
    if observation.status != "requestObserved" or observation.checkpointID is None:
        return None
    middleware = next(
        (
            item
            for item in agent.config.middleware or []
            if isinstance(item, EngineEntryMiddleware) and item.invocation == invocation
        ),
        None,
    )
    if middleware is None:
        return None
    checkpoint_id = observation.checkpointID
    try:
        config: RunnableConfig = {
            "configurable": {
                "thread_id": invocation.intent.snapshot.octopThreadID,
                "checkpoint_ns": "",
                "checkpoint_id": str(checkpoint_id),
            }
        }
        checkpoint = await saver.aget_tuple(config)
        if (
            checkpoint is None
            or not _checkpoint_matches(checkpoint, invocation)
            or checkpoint.pending_writes != []
            or checkpoint.metadata.get("source") != "loop"
            or type(checkpoint.metadata.get("step")) is not int
            or checkpoint.metadata["step"] < 0
        ):
            return None
        state = await agent.graph.aget_state(checkpoint.config)
        state_config = state.config.get("configurable") or {}
        messages = (state.values or {}).get("messages")
        if (
            state_config.get("checkpoint_id") != str(checkpoint_id)
            or state_config.get("thread_id") != invocation.intent.snapshot.octopThreadID
            or state_config.get("checkpoint_ns", "") != ""
            or state.next != ()
            or state.tasks != ()
            or not isinstance(messages, list)
            or not _original_message_matches(messages, invocation)
            or any(
                canonical_json((state.metadata or {}).get(key)) != canonical_json(value)
                for key, value in _checkpoint_metadata(invocation).items()
            )
        ):
            return None
        user_id = str(invocation.intent.userMessageID)
        original_index = next(i for i, message in enumerate(messages) if message.id == user_id)
        answer = messages[original_index + 1 :]
        tool_message: ToolMessage | None = None
        if expected_tool is None:
            if len(answer) != 1 or not isinstance(answer[0], AIMessage):
                return None
        else:
            if (
                len(answer) != 3
                or not isinstance(answer[0], AIMessage)
                or not isinstance(answer[1], ToolMessage)
                or not isinstance(answer[2], AIMessage)
            ):
                return None
            call_message = answer[0]
            tool_message = answer[1]
            calls = call_message.tool_calls
            if (
                not isinstance(call_message.id, str)
                or not call_message.id
                or len(calls) != 1
                or call_message.invalid_tool_calls
                or calls[0].get("id") != expected_tool.tool_call_id
                or calls[0].get("name") != expected_tool.tool_name
                or not isinstance(calls[0].get("args"), dict)
                or content_digest(calls[0]["args"]) != expected_tool.tool_arguments_digest
                or tool_message.tool_call_id != expected_tool.tool_call_id
                or tool_message.name != expected_tool.tool_name
                or tool_message.status != "success"
                or not isinstance(tool_message.id, str)
                or not tool_message.id
                or not isinstance(tool_message.content, str)
                or content_digest(tool_message.content) != expected_tool.tool_content_digest
            ):
                return None
        assistant = answer[-1]
        if (
            not isinstance(assistant.id, str)
            or not assistant.id
            or assistant.id == user_id
            or not isinstance(assistant.content, str)
            or not assistant.content
            or assistant.tool_calls
            or assistant.invalid_tool_calls
            or STREAM_ERROR_FLAG in assistant.additional_kwargs
            or (
                tool_message is not None
                and len(
                    {
                        user_id,
                        answer[0].id,
                        tool_message.id,
                        assistant.id,
                    }
                )
                != 4
            )
        ):
            return None
    except Exception:
        return None

    # Native input can predate the durable application ACK. Recheck that exact
    # claim without opening execution; a later cancellation still needs the
    # final control-DB transaction to win its own CAS race.
    from octop.infra.agentcomputer.dispatch import get_claim

    try:
        claim = await asyncio.to_thread(
            get_claim,
            middleware._services,
            owner_id=invocation.intent.ownerID,
            run_id=invocation.intent.runID,
        )
    except Exception:
        return None
    if (
        claim is None
        or claim.intent != invocation.intent
        or claim.claimantID != invocation.claimantID
        or claim.engineReceipt is None
    ):
        return None
    assistant_content_digest = content_digest(assistant.content)
    tool_fields = (
        {
            "toolCallID": expected_tool.tool_call_id,
            "toolName": expected_tool.tool_name,
            "toolArgumentsDigest": expected_tool.tool_arguments_digest,
            "toolMessageID": tool_message.id,
            "toolContentDigest": expected_tool.tool_content_digest,
        }
        if expected_tool is not None and tool_message is not None
        else {}
    )
    return TerminalCheckpointEvidence(
        checkpoint_id=checkpoint_id,
        assistant_message_id=assistant.id,
        assistant_content_digest=assistant_content_digest,
        checkpoint_digest=content_digest(
            {
                "schemaVersion": 2 if expected_tool is not None else 1,
                "checkpointID": str(checkpoint_id),
                "checkpointNamespace": "",
                "metadata": _checkpoint_metadata(invocation),
                "assistantMessageID": assistant.id,
                "assistantContentDigest": assistant_content_digest,
                **tool_fields,
            }
        ),
        tool_call_id=expected_tool.tool_call_id if expected_tool is not None else None,
        tool_message_id=tool_message.id if tool_message is not None else None,
        tool_content_digest=(
            expected_tool.tool_content_digest if expected_tool is not None else None
        ),
    )
