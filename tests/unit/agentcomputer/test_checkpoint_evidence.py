"""Native request correlation without a provider, recovery execution or archive binding."""

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
from octop.infra.agentcomputer.commands import StorageUnavailableError, admit_new_message
from octop.infra.agentcomputer.engine import inspect_engine_checkpoint
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import EngineCheckpointObservation


def _counts(services):
    with services.db.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ac_commands", "ac_turns", "ac_runs", "ac_events", "ac_run_dispatches")
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
    with entry.services.db.transaction() as conn:
        # Fixture-only definitive completion permits a subsequent admission.
        conn.execute(
            "UPDATE ac_runs SET state='succeeded',revision=revision+1 WHERE run_id=?",
            (str(entry.claim.intent.runID),),
        )
        conn.execute(
            "UPDATE ac_tasks SET active_run_id=NULL WHERE task_id=?",
            (str(entry.claim.intent.taskID),),
        )
        conn.execute(
            "UPDATE ac_projects SET active_run_id=NULL WHERE project_id=?",
            (str(entry.claim.intent.projectID),),
        )
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
