"""Actual public harness hooks with an in-memory model and isolated control DB."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from octop_harness.agent import HarnessAgent
from octop_harness.config import HarnessAgentConfig
from octop_harness.llm.factory import ChatModelFactory
from tests.support.agentcomputer_history import open_history_archive
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.active_runs import ActiveRunRegistry
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageUnavailableError,
    admit_new_message,
    content_digest,
)
from octop.infra.agentcomputer.engine import (
    EngineEntryMiddleware,
    EngineEntryReplayed,
    build_engine_request,
    invoke_engine_entry,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import EngineInvocation, NewMessageCommand, RunClaim
from octop.infra.db.services import SharedServices
from octop.infra.history.service import HistoryArchive
from octop.infra.history.store import probe_bound_completion


class CountingFakeModel(FakeMessagesListChatModel):
    calls: int = 0
    seen: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> CountingFakeModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.calls += 1
        self.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


class MemoryModelFactory(ChatModelFactory):
    """Override a public injection point without providers, credentials or cache patches."""

    def __init__(self, model: CountingFakeModel, model_ref: str) -> None:
        super().__init__([])
        self.model = model
        self.model_ref = model_ref

    def get_chat_model(self, model_ref: str) -> CountingFakeModel:
        assert model_ref == self.model_ref
        return self.model


@dataclass
class EngineFixture:
    services: SharedServices
    claim: RunClaim
    model: CountingFakeModel
    history_archive: HistoryArchive
    agent: HarnessAgent | None = None
    invocation: EngineInvocation | None = None
    failure: Exception | None = None
    tools: list[Any] | None = None
    checkpointer: Any = None
    active_runs: ActiveRunRegistry = field(default_factory=ActiveRunRegistry)

    async def prepare(self, invocation: EngineInvocation) -> HarnessAgent:
        self.invocation = invocation
        if self.agent is None:
            snapshot = invocation.intent.snapshot
            row = await asyncio.to_thread(self.services.agent_repo.get, snapshot.agentID)
            assert row is not None and row.config_json is not None
            workspace_config = json.loads(row.config_json)
            saver = self.checkpointer if self.checkpointer is not None else InMemorySaver()
            cfg = HarnessAgentConfig(
                name="managed-engine-fixture",
                workspace_dir=workspace_config["workspace_dir"],
                system_files_path=workspace_config["system_files_path"],
                backend=workspace_config["backend"],
                default_model=snapshot.projectConfiguration.modelRef,
                system_prompt=snapshot.projectConfiguration.instructions,
                checkpointer=saver,
                middleware=[EngineEntryMiddleware(self.services, invocation, self.history_archive)],
                tools=self.tools or [],
                bootstrap_enabled=False,
                memory_enabled=False,
                memory_jsonl_enabled=False,
                memory=(),
                session_log_enabled=False,
                subagents=[],
                subagents_auto_load=False,
                todos_enabled=False,
                team_enabled=False,
                ask_user_enabled=False,
                media_offload_enabled=False,
                model_retry_enabled=False,
                web_search_tools=False,
                mcp_server_configs={},
                acp_delegate_enabled=False,
            )
            factory = MemoryModelFactory(self.model, snapshot.projectConfiguration.modelRef)
            self.agent = await asyncio.to_thread(
                HarnessAgent,
                cfg,
                agent_id=snapshot.agentID,
                model_factory=factory,
            )
            assert self.agent.config.providers == []
            assert self.agent.model_factory.provider_configs() == []
            assert self.agent.checkpointer is saver
        return self.agent

    async def invoke(self, invocation: EngineInvocation) -> None:
        agent = await self.prepare(invocation)
        try:
            await invoke_engine_entry(
                agent,
                invocation,
                active_runs=self.active_runs,
                history_archive=self.history_archive,
            )
        except Exception as exc:
            self.failure = exc
            raise

    def current_claim(self) -> RunClaim:
        claim = dispatch.get_claim(self.services, owner_id=1, run_id=self.claim.intent.runID)
        assert claim is not None
        return claim


def _open_fixture_gate(services: SharedServices):
    profile = get_capabilities(services)
    return profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
            "supportedPermissionModes": ["askForApproval"],
        }
    )


@pytest.fixture
async def entry(control, monkeypatch, request):
    services, _, task_id = control
    monkeypatch.setattr(dispatch, "get_capabilities", _open_fixture_gate)
    command = NewMessageCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        contextMode="newMessage",
        userMessageID=uuid4(),
        expectedProjectRevision=1,
        prompt=getattr(request, "param", "  原文\n第二行  "),
        permissionMode="askForApproval",
    )
    receipt = admit_new_message(services, owner_id=1, task_id=task_id, command=command)
    claim = dispatch.claim_run(
        services,
        owner_id=1,
        run_id=receipt.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    model = CountingFakeModel(
        responses=[AIMessage(content="fixture response", id=str(receipt.assistantMessageID))]
    )
    fixture = EngineFixture(services, claim, model, open_history_archive(services))
    try:
        yield fixture
    finally:
        if fixture.agent is not None:
            await fixture.agent.aclose()
        fixture.history_archive.store.close()


async def test_actual_harness_accepts_before_first_model_and_preserves_exact_input(entry):
    receipt = await dispatch.dispatch_run(
        entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
    )
    assert entry.model.calls == 1
    assert entry.current_claim().engineReceipt == receipt
    assert entry.failure is None
    assert entry.invocation is not None
    request = build_engine_request(entry.invocation)
    original = request.normalize_messages()[0]
    assert isinstance(original, HumanMessage)
    assert original.id == str(entry.claim.intent.userMessageID)
    assert original.content == entry.claim.intent.snapshot.request.prompt
    observed = [message for message in entry.model.seen[0] if isinstance(message, HumanMessage)]
    assert [(message.id, message.content) for message in observed] == [
        (original.id, original.content)
    ]
    thread_id = entry.claim.intent.snapshot.octopThreadID
    turn = entry.history_archive.store.turn(thread_id)
    assert turn is not None
    assert turn["id"] == str(entry.claim.intent.turnID)
    assert turn["format"] == "v2" and turn["status"] == "complete"
    documents = entry.history_archive.store.documents(
        thread_id, "message", turn_id=str(entry.claim.intent.turnID)
    )
    wires = [document["value"] for document in documents]
    assert [(wire["type"], wire["data"]["content"]) for wire in wires] == [
        ("human", entry.claim.intent.snapshot.request.prompt),
        ("ai", "fixture response"),
    ]
    assert [wire["data"]["id"] for wire in wires] == [
        str(entry.claim.intent.userMessageID),
        str(entry.claim.intent.assistantMessageID),
    ]
    assert [document["turn_id"] for document in documents] == [str(entry.claim.intent.turnID)] * 2


async def test_actual_harness_turn_passes_strict_bound_completion_probe(entry):
    await dispatch.dispatch_run(
        entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
    )
    intent = entry.claim.intent
    documents = entry.history_archive.store.documents(
        intent.snapshot.octopThreadID, "message", turn_id=str(intent.turnID)
    )
    assert len(documents) == 2
    native_answer = documents[-1]["value"]
    assert native_answer["type"] == "ai"

    proof = probe_bound_completion(
        entry.history_archive.store.path,
        identity=str(entry.services.paths.db.resolve()),
        turn_id=str(intent.turnID),
        thread_id=intent.snapshot.octopThreadID,
        agent_id=intent.snapshot.agentID,
        user_message_id=str(intent.userMessageID),
        prompt=intent.snapshot.request.prompt,
    )
    assert proof.status == "complete"
    assert proof.assistant_message_id == native_answer["data"]["id"]
    assert proof.assistant_message_id == str(intent.assistantMessageID)
    assert proof.assistant_content_digest == content_digest(native_answer["data"]["content"])


async def test_existing_entry_receipt_blocks_another_model_loop(entry):
    receipt = await dispatch.dispatch_run(
        entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
    )
    assert entry.agent is not None and entry.invocation is not None
    with entry.history_archive.store.db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    with pytest.raises(EngineEntryReplayed) as replay:
        await invoke_engine_entry(
            entry.agent,
            entry.invocation,
            active_runs=entry.active_runs,
            history_archive=entry.history_archive,
        )
    assert replay.value.receipt == receipt
    assert entry.current_claim().engineReceipt == receipt
    assert entry.model.calls == 1
    with entry.history_archive.store.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == before


async def test_disabled_archive_rejects_before_receipt_or_model(entry):
    entry.history_archive.enabled = False
    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable") as error:
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    assert error.value.reason == "historyUnavailable"
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None
    assert entry.history_archive.store.turn(entry.claim.intent.snapshot.octopThreadID) is None


async def test_model_output_before_bound_history_hook_fails_closed(entry, monkeypatch):
    async def premature_output(invocation):
        agent = await entry.prepare(invocation)

        async def stream(_request, *, protocol):
            assert protocol == "langgraph"
            yield {"type": "token", "content": "must not be captured before entry"}

        monkeypatch.setattr(agent, "stream", stream)
        await invoke_engine_entry(
            agent,
            invocation,
            active_runs=entry.active_runs,
            history_archive=entry.history_archive,
        )

    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable") as error:
        await dispatch.dispatch_run(
            entry.services,
            claim=entry.claim,
            invoke=premature_output,
            active_runs=entry.active_runs,
        )
    assert error.value.reason == "historyTurnUnavailable"
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None
    assert entry.history_archive.store.turn(entry.claim.intent.snapshot.octopThreadID) is None


async def test_receipt_survives_archive_begin_failure_without_claiming_model_execution(
    entry, monkeypatch
):
    def fail_archive(*_args, **_kwargs):
        raise OSError("simulated separate archive write failure")

    monkeypatch.setattr(entry.history_archive, "begin_bound", fail_archive)
    with pytest.raises(OSError, match="separate archive"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is not None
    assert entry.history_archive.store.turn(entry.claim.intent.snapshot.octopThreadID) is None
    with entry.services.db.connect() as conn:
        row = conn.execute(
            "SELECT dispatch_phase,reconciliation_required FROM ac_runs WHERE run_id=?",
            (str(entry.claim.intent.runID),),
        ).fetchone()
    assert tuple(row) == ("engineAccepted", 1)


async def test_missing_archive_after_receipt_fails_closed_without_recreating_it(entry):
    path = entry.history_archive.store.path
    entry.history_archive.store.close()
    path.unlink()
    with pytest.raises(FileNotFoundError):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    assert not path.exists()
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is not None
    with entry.services.db.connect() as conn:
        row = conn.execute(
            "SELECT state,reconciliation_required FROM ac_runs WHERE run_id=?",
            (str(entry.claim.intent.runID),),
        ).fetchone()
    assert tuple(row) == ("interrupted", 1)


async def test_seed_capture_failure_keeps_bound_turn_but_blocks_model(entry, monkeypatch):
    def fail_seed(*_args, **_kwargs):
        raise OSError("simulated archive seed write failure")

    monkeypatch.setattr(entry.history_archive, "save_message_updates", fail_seed)
    with pytest.raises(OSError, match="seed write failure"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is not None
    turn = entry.history_archive.store.turn(entry.claim.intent.snapshot.octopThreadID)
    assert turn is not None
    assert turn["id"] == str(entry.claim.intent.turnID)
    assert turn["status"] == "failed"
    assert turn["error"] == "initial_capture_failed"


@pytest.mark.parametrize("entry", ["/model other/unsafe", "/cancel"], indirect=True)
async def test_runtime_slash_is_original_human_text_and_cannot_bypass_entry(entry):
    receipt = await dispatch.dispatch_run(
        entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
    )
    assert entry.current_claim().engineReceipt == receipt
    assert entry.model.calls == 1
    assert entry.agent is not None
    assert entry.agent.get_thread_model(entry.claim.intent.snapshot.octopThreadID) is None
    human = [message for message in entry.model.seen[0] if isinstance(message, HumanMessage)]
    assert human[0].content == entry.claim.intent.snapshot.request.prompt


async def test_acceptance_storage_failure_prevents_model_and_receipt(entry, monkeypatch):
    def fail(*args):
        raise StorageUnavailableError("fixture acceptance transaction unavailable")

    monkeypatch.setattr(dispatch, "accept_engine_entry", fail)
    with pytest.raises(StorageUnavailableError):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    assert isinstance(entry.failure, StorageUnavailableError)
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None


@pytest.mark.parametrize(
    "change", ["restoreEpoch", "reconciliation", "closedGate", "disabledOwner"]
)
async def test_fencing_after_acceptance_prevents_first_model(entry, monkeypatch, change):
    accept = dispatch.accept_engine_entry

    def accept_then_fence(services, invocation):
        result = accept(services, invocation)
        if change == "restoreEpoch":
            identity = get_or_create_identity(services.paths.agentcomputer_identity)
            services.paths.agentcomputer_identity.write_text(
                identity.model_copy(
                    update={"restoreEpoch": identity.restoreEpoch + 1}
                ).model_dump_json()
            )
        elif change == "reconciliation":
            with services.db.transaction() as conn:
                conn.execute(
                    "UPDATE ac_runs SET state='interrupted',reconciliation_required=1 WHERE run_id=?",
                    (str(invocation.intent.runID),),
                )
        elif change == "disabledOwner":
            with services.db.transaction() as conn:
                conn.execute("UPDATE users SET disabled=1 WHERE id=1")
        else:
            monkeypatch.setattr(dispatch, "get_capabilities", get_capabilities)
        return result

    monkeypatch.setattr(dispatch, "accept_engine_entry", accept_then_fence)
    with pytest.raises(CommandAdmissionError):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    assert isinstance(entry.failure, CommandAdmissionError)
    assert entry.model.calls == 0
    # The durable earlier entry is retained even when later execution is fenced.
    with entry.services.db.connect() as conn:
        row = conn.execute(
            "SELECT reconciliation_required FROM ac_runs WHERE run_id=?",
            (str(entry.claim.intent.runID),),
        ).fetchone()
        assert row[0] == 1


@pytest.mark.parametrize(
    "field",
    [
        "thread_id",
        "user",
        "model",
        "agent_id",
        "source",
        "mcp_servers",
        "skills",
        "mcp_use_default",
    ],
)
async def test_configurable_override_is_rejected_before_acceptance(entry, field):
    async def altered(invocation):
        agent = await entry.prepare(invocation)
        request = build_engine_request(invocation)
        request.configurable[field] = "different-identity"
        try:
            async for _ in agent.stream(request, protocol="langgraph"):
                pass
        except Exception as exc:
            entry.failure = exc
            raise

    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=altered, active_runs=entry.active_runs
        )
    assert isinstance(entry.failure, CommandAdmissionError)
    assert entry.failure.reason == "engineContextMismatch"
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None


async def test_tool_hook_never_calls_side_effect_handler(entry):
    effects = []

    @tool
    def fixture_effect(value: str) -> str:
        """Record a fixture effect to prove the handler was never reached."""
        effects.append(value)
        return value

    entry.tools = [fixture_effect]
    entry.model.responses = [
        AIMessage(
            content="",
            tool_calls=[{"name": "fixture_effect", "args": {"value": "write"}, "id": "effect-1"}],
        )
    ]
    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
    assert isinstance(entry.failure, CommandAdmissionError)
    assert entry.failure.reason == "toolExecutionUnavailable"
    assert effects == []
    assert entry.model.calls == 1


async def test_missing_bound_middleware_cannot_call_the_model(entry):
    async def missing(invocation):
        agent = await entry.prepare(invocation)
        agent.config.middleware = []
        try:
            await invoke_engine_entry(
                agent,
                invocation,
                active_runs=entry.active_runs,
                history_archive=entry.history_archive,
            )
        except Exception as exc:
            entry.failure = exc
            raise

    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=missing, active_runs=entry.active_runs
        )
    assert isinstance(entry.failure, CommandAdmissionError)
    assert entry.failure.reason == "engineContextMismatch"
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None


@pytest.mark.parametrize("change", ["schemaVersion", "ownerID", "nonJson", "nonFinite"])
async def test_nested_untyped_metadata_is_rejected_before_acceptance(entry, change):
    async def altered(invocation):
        agent = await entry.prepare(invocation)
        request = build_engine_request(invocation)
        if change in {"schemaVersion", "ownerID"}:
            request.configurable["agentcomputer"]["intent"][change] = True
        elif change == "nonJson":
            request.configurable["agentcomputer"] = object()
        else:
            request.configurable["agentcomputer"] = float("nan")
        try:
            async for _ in agent.stream(request, protocol="langgraph"):
                pass
        except Exception as exc:
            entry.failure = exc
            raise

    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=altered, active_runs=entry.active_runs
        )
    assert entry.failure.reason == "engineContextMismatch"
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None


@pytest.mark.parametrize("field", ["backend", "workspace_dir", "system_files_path"])
async def test_runtime_workspace_config_must_match_registered_managed_metadata(entry, field):
    async def altered(invocation):
        agent = await entry.prepare(invocation)
        if field == "backend":
            agent.config.backend = {
                "type": "filesystem",
                "root_dir": str(entry.services.paths.root),
                "virtual_mode": True,
            }
        elif field == "workspace_dir":
            agent.config.workspace_dir = entry.services.paths.root
        else:
            agent.config.system_files_path = ".other-system"
        try:
            await invoke_engine_entry(
                agent,
                invocation,
                active_runs=entry.active_runs,
                history_archive=entry.history_archive,
            )
        except Exception as exc:
            entry.failure = exc
            raise

    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=altered, active_runs=entry.active_runs
        )
    assert entry.failure.reason == "engineContextMismatch"
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None


@pytest.mark.parametrize(
    "field", ["ac_schema_version", "ac_owner_id", "ac_run_id", "ac_intent_digest"]
)
async def test_native_metadata_cannot_override_server_binding_before_acceptance(entry, field):
    async def altered(invocation):
        agent = await entry.prepare(invocation)
        request = build_engine_request(invocation)
        request.metadata[field] = True if field in {"ac_schema_version", "ac_owner_id"} else "wrong"
        try:
            async for _ in agent.stream(request, protocol="langgraph"):
                pass
        except Exception as exc:
            entry.failure = exc
            raise

    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=altered, active_runs=entry.active_runs
        )
    assert entry.failure.reason == "engineContextMismatch"
    assert entry.model.calls == 0
    assert entry.current_claim().engineReceipt is None
