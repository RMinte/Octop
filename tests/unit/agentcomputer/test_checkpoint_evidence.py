"""Native checkpoint request correlation without a provider or recovery execution."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import aiosqlite
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import ValidationError
from tests.unit.agentcomputer.test_engine_entry import (
    CountingFakeModel,
    EngineFixture,
)
from tests.unit.agentcomputer.test_engine_entry import (
    entry as entry,
)
from tests.unit.agentcomputer.test_submission_runs import control as control
from tests.unit.agentcomputer.test_submission_runs import request

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageUnavailableError,
    admit_new_message,
    content_digest,
)
from octop.infra.agentcomputer.engine import (
    inspect_engine_checkpoint,
    inspect_terminal_checkpoint,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import EngineCheckpointObservation
from octop.infra.agentcomputer.runs import get_run


def _counts(services):
    with services.db.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "ac_commands",
                "ac_turns",
                "ac_runs",
                "ac_events",
                "ac_run_dispatches",
                "ac_run_terminals",
            )
        }


@pytest.fixture
async def persisted(entry, tmp_path):
    path = tmp_path / "native-checkpoints.sqlite"
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        entry.checkpointer = saver
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
        yield entry, path, saver


async def test_existing_native_checkpoint_correlates_exact_request_and_does_not_write_control(
    persisted,
):
    entry, _, saver = persisted
    before = _counts(entry.services)
    observation = await asyncio.wait_for(
        inspect_engine_checkpoint(entry.agent, entry.invocation), timeout=10
    )
    assert observation.status == "requestObserved" and observation.reasonCode is None, observation
    assert observation.checkpointID is not None and observation.checkpointNamespace == ""
    checkpoint = await saver.aget_tuple(
        {
            "configurable": {
                "thread_id": entry.claim.intent.snapshot.octopThreadID,
                "checkpoint_ns": "",
                "checkpoint_id": str(observation.checkpointID),
            }
        }
    )
    assert checkpoint.metadata["ac_engine_request_id"] == str(entry.claim.intent.engineRequestID)
    assert checkpoint.metadata["ac_owner_id"] == 1
    serialized = observation.model_dump_json()
    assert entry.claim.intent.snapshot.request.prompt not in serialized
    assert str(entry.services.paths.root) not in serialized
    assert "modelStarted" not in serialized and "historyTurnID" not in serialized
    assert _counts(entry.services) == before
    assert entry.model.calls == 1


async def test_persisted_no_tool_terminal_checkpoint_exposes_only_bound_answer_digest(persisted):
    entry, _, _ = persisted
    before = _counts(entry.services)
    evidence = await inspect_terminal_checkpoint(entry.agent, entry.invocation)
    assert evidence is not None
    assert (
        evidence.checkpoint_id
        == (await inspect_engine_checkpoint(entry.agent, entry.invocation)).checkpointID
    )
    assert evidence.assistant_message_id == str(entry.claim.intent.assistantMessageID)
    assert evidence.assistant_content_digest == content_digest("fixture response")
    assert evidence.checkpoint_digest.startswith("sha256:")
    assert entry.claim.intent.snapshot.request.prompt not in repr(evidence)
    assert _counts(entry.services) == before and entry.model.calls == 1


async def test_persisted_harness_completion_finishes_run_and_releases_both_lanes(persisted):
    entry, _, _ = persisted
    intent = entry.claim.intent
    view = get_run(entry.services, owner_id=1, run_id=intent.runID)
    assert view is not None and view.state == "succeeded"
    assert not view.reconciliationRequired and view.dispatchPhase == "engineAccepted"
    with entry.services.db.connect() as conn:
        terminal = conn.execute(
            "SELECT receipt_digest FROM ac_run_terminals WHERE run_id=?",
            (str(intent.runID),),
        ).fetchone()
        task = conn.execute(
            "SELECT active_run_id FROM ac_tasks WHERE task_id=?", (str(intent.taskID),)
        ).fetchone()
        project = conn.execute(
            "SELECT active_run_id FROM ac_projects WHERE project_id=?",
            (str(intent.projectID),),
        ).fetchone()
        finished = conn.execute(
            "SELECT COUNT(*) FROM ac_events WHERE run_id=? AND event_type='run.finished'",
            (str(intent.runID),),
        ).fetchone()[0]
    assert terminal is not None and terminal["receipt_digest"].startswith("sha256:")
    assert task is not None and task["active_run_id"] is None
    assert project is not None and project["active_run_id"] is None
    assert finished == 1 and entry.model.calls == 1


async def test_in_memory_terminal_state_is_not_persistent_completion_evidence(entry):
    entry.checkpointer = InMemorySaver()
    await dispatch.dispatch_run(
        entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
    )
    assert (
        await inspect_engine_checkpoint(entry.agent, entry.invocation)
    ).status == "requestObserved"
    assert await inspect_terminal_checkpoint(entry.agent, entry.invocation) is None
    assert entry.model.calls == 1


async def test_empty_final_ai_does_not_prove_text_answer(entry, tmp_path):
    entry.model.responses = [AIMessage(content="", id=str(entry.claim.intent.assistantMessageID))]
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "empty-answer.sqlite")) as saver:
        entry.checkpointer = saver
        with pytest.raises(CommandAdmissionError) as error:
            await dispatch.dispatch_run(
                entry.services,
                claim=entry.claim,
                invoke=entry.invoke,
                active_runs=entry.active_runs,
            )
        assert error.value.reason == "completionEvidenceUnavailable"
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": entry.claim.intent.snapshot.octopThreadID}}
        )
        assert checkpoint is not None
        state = await entry.agent.graph.aget_state(checkpoint.config)
        assert state.next == () and state.tasks == ()
        assert await inspect_terminal_checkpoint(entry.agent, entry.invocation) is None
        view = get_run(entry.services, owner_id=1, run_id=entry.claim.intent.runID)
        assert view is not None and view.state == "interrupted" and view.reconciliationRequired


@pytest.mark.parametrize("cancel_at", ["token", "model_update", "last_after_model"])
async def test_harness_cancel_normal_eof_does_not_create_terminal_evidence(
    entry, tmp_path, monkeypatch, cancel_at
):
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "cancel-checkpoints.sqlite")
    ) as saver:
        entry.checkpointer = saver
        cancelled = False

        async def invoke(invocation):
            nonlocal cancelled
            agent = await entry.prepare(invocation)
            stream = agent.stream

            async def stop_at_checkpoint(*args, **kwargs):
                nonlocal cancelled
                async for chunk in stream(*args, **kwargs):
                    yield chunk
                    kind, node = chunk.get("type"), chunk.get("node")
                    matches = (
                        (cancel_at == "token" and kind == "token" and node == "model")
                        or (
                            cancel_at == "model_update"
                            and kind == "state_update"
                            and node == "model"
                        )
                        or (
                            cancel_at == "last_after_model"
                            and kind == "state_update"
                            and node == "PIIMiddleware[api_key].after_model"
                        )
                    )
                    if matches:
                        cancelled = True
                        agent.cancel(invocation.intent.snapshot.octopThreadID)

            monkeypatch.setattr(agent, "stream", stop_at_checkpoint)
            await entry.invoke(invocation)

        with pytest.raises(CommandAdmissionError) as error:
            await dispatch.dispatch_run(
                entry.services, claim=entry.claim, invoke=invoke, active_runs=entry.active_runs
            )
        assert error.value.reason == "completionEvidenceUnavailable"
        assert cancelled and entry.model.calls == 1
        assert entry.current_claim().engineReceipt is not None
        checkpoint = await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": entry.claim.intent.snapshot.octopThreadID,
                    "checkpoint_ns": "",
                }
            }
        )
        assert checkpoint is not None
        state = await entry.agent.graph.aget_state(checkpoint.config)
        assert state.next and state.tasks
        before = _counts(entry.services)
        assert await inspect_terminal_checkpoint(entry.agent, entry.invocation) is None
        assert _counts(entry.services) == before
        view = get_run(entry.services, owner_id=1, run_id=entry.claim.intent.runID)
        assert view is not None and view.state == "interrupted" and view.reconciliationRequired


async def test_reopen_existing_saver_preserves_request_correlation_without_model_call(
    entry, tmp_path
):
    path = tmp_path / "persisted-checkpoints.sqlite"
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        entry.checkpointer = saver
        await dispatch.dispatch_run(
            entry.services, claim=entry.claim, invoke=entry.invoke, active_runs=entry.active_runs
        )
        original = await inspect_engine_checkpoint(entry.agent, entry.invocation)
        await entry.agent.aclose()
        entry.agent = None
    assert path.exists() and original.status == "requestObserved"
    async with AsyncSqliteSaver.from_conn_string(str(path)) as reopened:
        entry.checkpointer = reopened
        agent = await entry.prepare(entry.invocation)
        # Initialization belongs to this fixture's trusted existing saver,
        # never to the inspection function. No SDK model call is made here.
        assert not reopened.is_setup
        unavailable = await inspect_engine_checkpoint(agent, entry.invocation)
        assert unavailable.status == "unknown"
        assert unavailable.reasonCode == "checkpointerNotInitialized"
        assert not reopened.is_setup
        await reopened.setup()
        observed = await inspect_engine_checkpoint(agent, entry.invocation)
        assert observed == original
        assert entry.model.calls == 1


async def test_later_checkpoint_on_same_thread_does_not_replace_original_request_evidence(
    persisted,
):
    entry, _, saver = persisted
    original = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    completed = get_run(entry.services, owner_id=1, run_id=entry.claim.intent.runID)
    assert completed is not None and completed.state == "succeeded"
    receipt = admit_new_message(
        entry.services,
        owner_id=1,
        task_id=entry.claim.intent.taskID,
        command=request(prompt="A later request on the same fixed thread"),
    )
    claim = dispatch.claim_run(
        entry.services,
        owner_id=1,
        run_id=receipt.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    later = EngineFixture(
        entry.services,
        claim,
        CountingFakeModel(responses=[AIMessage(content="later fixture response")]),
        entry.history_archive,
        checkpointer=saver,
    )
    try:
        await dispatch.dispatch_run(
            entry.services, claim=claim, invoke=later.invoke, active_runs=later.active_runs
        )
        latest = await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": claim.intent.snapshot.octopThreadID,
                    "checkpoint_ns": "",
                }
            }
        )
        assert latest.metadata["ac_engine_request_id"] == str(claim.intent.engineRequestID)
        later_observation = await inspect_engine_checkpoint(later.agent, later.invocation)
        old_observation = await inspect_engine_checkpoint(entry.agent, entry.invocation)
        assert old_observation == original
        assert later_observation.status == "requestObserved"
        assert later_observation.checkpointID != old_observation.checkpointID
        old_terminal = await inspect_terminal_checkpoint(entry.agent, entry.invocation)
        later_terminal = await inspect_terminal_checkpoint(later.agent, later.invocation)
        assert (
            old_terminal is not None and old_terminal.checkpoint_id == old_observation.checkpointID
        )
        assert later_terminal is not None
        assert later_terminal.checkpoint_id == later_observation.checkpointID
        assert old_terminal.assistant_content_digest == content_digest("fixture response")
        assert later_terminal.assistant_content_digest == content_digest("later fixture response")
        assert entry.model.calls == later.model.calls == 1
    finally:
        if later.agent is not None:
            await later.agent.aclose()


@pytest.mark.parametrize(
    "field", ["ac_schema_version", "ac_owner_id", "ac_run_id", "ac_intent_digest"]
)
async def test_corrupted_native_metadata_never_returns_verified_reference(persisted, field):
    entry, _, saver = persisted
    checkpoint = await saver.aget_tuple(
        {
            "configurable": {
                "thread_id": entry.claim.intent.snapshot.octopThreadID,
                "checkpoint_ns": "",
            }
        }
    )
    metadata = dict(checkpoint.metadata)
    metadata[field] = True if field in {"ac_schema_version", "ac_owner_id"} else "wrong"
    await saver.conn.execute(
        "UPDATE checkpoints SET metadata=? WHERE thread_id=? AND checkpoint_ns='' AND checkpoint_id=?",
        (
            json.dumps(metadata),
            entry.claim.intent.snapshot.octopThreadID,
            checkpoint.config["configurable"]["checkpoint_id"],
        ),
    )
    await saver.conn.commit()
    observed = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    assert observed.status == "unknown" and observed.reasonCode == "checkpointBindingMismatch"
    assert observed.checkpointID is observed.checkpointNamespace is None
    assert entry.model.calls == 1


async def test_app_receipt_without_native_checkpoint_remains_unknown(persisted):
    entry, _, _ = persisted
    assert entry.current_claim().engineReceipt is not None
    entry.agent.config.checkpointer = InMemorySaver()
    # Reconstruct a trusted existing runtime with an empty saver, without invoking it.
    await entry.agent.aclose()
    entry.agent = None
    entry.checkpointer = InMemorySaver()
    agent = await entry.prepare(entry.invocation)
    observed = await inspect_engine_checkpoint(agent, entry.invocation)
    assert observed.status == "unknown" and observed.reasonCode == "checkpointNotObserved"
    assert observed.checkpointID is None
    assert entry.current_claim().engineReceipt is not None and entry.model.calls == 1


async def test_native_input_can_exist_when_app_acceptance_failed(entry, tmp_path, monkeypatch):
    def failed_accept(*args):
        raise StorageUnavailableError("fixture app acceptance failed")

    monkeypatch.setattr(dispatch, "accept_engine_entry", failed_accept)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "input-only.sqlite")) as saver:
        entry.checkpointer = saver
        with pytest.raises(StorageUnavailableError):
            await dispatch.dispatch_run(
                entry.services,
                claim=entry.claim,
                invoke=entry.invoke,
                active_runs=entry.active_runs,
            )
        assert entry.current_claim().engineReceipt is None
        observed = await inspect_engine_checkpoint(entry.agent, entry.invocation)
        assert observed.status == "requestObserved"
        assert entry.model.calls == 0
        assert await inspect_terminal_checkpoint(entry.agent, entry.invocation) is None


async def test_saver_read_failure_remains_unknown_and_never_repairs_or_invokes(persisted):
    entry, _, saver = persisted
    before = _counts(entry.services)
    await saver.conn.close()
    observed = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    assert observed.status == "unknown" and observed.reasonCode == "checkpointReadFailed"
    assert observed.checkpointID is None
    assert _counts(entry.services) == before and entry.model.calls == 1


async def test_mismatched_control_claimant_rejects_native_evidence(persisted, monkeypatch):
    entry, _, _ = persisted
    original = dispatch.get_claim

    def changed(*args, **kwargs):
        return original(*args, **kwargs).model_copy(update={"claimantID": uuid4()})

    monkeypatch.setattr(dispatch, "get_claim", changed)
    observed = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    assert observed.status == "unknown" and observed.reasonCode == "controlJournalMismatch"
    assert observed.checkpointID is None and entry.model.calls == 1


@pytest.mark.parametrize("field", ["backend", "workspace_dir", "system_files_path"])
async def test_checkpoint_probe_rejects_runtime_workspace_mismatch(persisted, field):
    entry, _, _ = persisted
    before = _counts(entry.services)
    if field == "backend":
        entry.agent.config.backend = {
            "type": "filesystem",
            "root_dir": str(entry.services.paths.root),
            "virtual_mode": True,
        }
    elif field == "workspace_dir":
        entry.agent.config.workspace_dir = entry.services.paths.root
    else:
        entry.agent.config.system_files_path = ".wrong-system"
    observed = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    assert observed.status == "unknown" and observed.reasonCode == "runtimeContextMismatch"
    assert observed.checkpointID is None
    assert _counts(entry.services) == before and entry.model.calls == 1


async def test_uninitialized_saver_probe_cannot_create_missing_checkpoint_file(persisted, tmp_path):
    entry, _, _ = persisted
    await entry.agent.aclose()
    entry.agent = None
    missing = tmp_path / "not-created.sqlite"
    connection = aiosqlite.connect(str(missing))
    saver = AsyncSqliteSaver(connection)
    try:
        entry.checkpointer = saver
        agent = await entry.prepare(entry.invocation)
        assert not missing.exists() and not saver.is_setup
        observed = await inspect_engine_checkpoint(agent, entry.invocation)
        assert observed.status == "unknown" and observed.reasonCode == "checkpointerNotInitialized"
        assert not missing.exists() and not saver.is_setup
        assert entry.model.calls == 1
    finally:
        await connection.close()


@pytest.mark.parametrize("change", ["disabledOwner", "historicalEpoch"])
async def test_native_observation_can_read_historical_facts_without_execution_gate(
    persisted, monkeypatch, change
):
    entry, _, _ = persisted
    if change == "disabledOwner":
        with entry.services.db.transaction() as conn:
            conn.execute("UPDATE users SET disabled=1 WHERE id=1")
    else:
        path = entry.services.paths.agentcomputer_identity
        identity = get_or_create_identity(path)
        path.write_text(identity.model_copy(update={"restoreEpoch": 2}).model_dump_json())

    def forbidden_gate(*args):
        raise AssertionError("read-only native correlation cannot authorize execution")

    monkeypatch.setattr(dispatch, "get_capabilities", forbidden_gate)
    before = _counts(entry.services)
    observed = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    assert observed.status == "requestObserved" and observed.restoreEpoch == 1
    assert _counts(entry.services) == before and entry.model.calls == 1


async def test_missing_control_identity_probe_does_not_recreate_it(persisted):
    entry, _, _ = persisted
    path = entry.services.paths.agentcomputer_identity
    path.unlink()
    before = _counts(entry.services)
    observed = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    assert observed.status == "unknown" and observed.reasonCode == "controlJournalUnavailable"
    assert not path.exists()
    assert _counts(entry.services) == before and entry.model.calls == 1


async def test_observation_shape_is_strict_and_cannot_add_model_start_or_paths(persisted):
    entry, _, _ = persisted
    observation = await inspect_engine_checkpoint(entry.agent, entry.invocation)
    payload = observation.model_dump(mode="json")
    for changes in (
        {"modelStarted": False},
        {"path": str(entry.services.paths.root)},
        {"schemaVersion": True},
        {"restoreEpoch": True},
        {"status": "requestObserved", "checkpointID": None},
        {"status": "unknown", "reasonCode": None},
    ):
        with pytest.raises(ValidationError):
            EngineCheckpointObservation.model_validate({**payload, **changes})
