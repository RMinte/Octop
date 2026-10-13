"""Durable internal effect windows with isolated handlers and no model requests."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from functools import partial
from threading import Event
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tests.unit.agentcomputer.test_dispatch import allow
from tests.unit.agentcomputer.test_submission_runs import control as control
from tests.unit.agentcomputer.test_submission_runs import request

from octop.config import OctopConfig
from octop.infra.agentcomputer import dispatch, effects
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    admit_new_message,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.effect_runner import execute_effect
from octop.infra.agentcomputer.identity import read_identity
from octop.infra.agentcomputer.models import EngineInvocation, ToolEffectAction, ToolEffectOutput
from octop.infra.agentcomputer.runs import get_run
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import SharedServices, build_shared_services


@dataclass
class EffectFixture:
    services: SharedServices
    invocation: EngineInvocation
    action: ToolEffectAction

    def record(self):
        with self.services.db.connect() as conn:
            row = conn.execute("SELECT effect_id FROM ac_tool_effects").fetchone()
        assert row is not None
        return effects.get_effect(self.services, owner_id=1, effect_id=UUID(row[0]))

    def assert_lane(self, *, reconciliation):
        view = get_run(self.services, owner_id=1, run_id=self.invocation.intent.runID)
        assert view.reconciliationRequired is reconciliation
        task = self.services.ac_task_repo.get(str(self.invocation.intent.taskID), 1)
        project = self.services.ac_project_repo.get(str(self.invocation.intent.projectID), 1)
        assert task.active_run_id == project.active_run_id == str(self.invocation.intent.runID)

    def events(self):
        with self.services.db.connect() as conn:
            return list(conn.execute("SELECT event_type,sequence FROM ac_events ORDER BY sequence"))


def output(*, outcome="succeeded", reported="reported"):
    result = {"fixture": "result", "partialEffectReported": outcome == "failed"}
    return ToolEffectOutput(
        schemaVersion=1,
        outcome=outcome,
        resultJSON=canonical_json(result),
        resultDigest=content_digest(result),
        exitCode=0 if outcome == "succeeded" else 1,
        fileChangeIDs=(),
        artifactIDs=(),
        effects=reported,
    )


@pytest.fixture
async def effect(control, monkeypatch):
    services, _, task_id = control
    allow(monkeypatch)
    monkeypatch.setattr(effects, "get_capabilities", dispatch.get_capabilities)
    receipt = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=receipt.runID, claimant_id=uuid4(), expected_revision=1
    )
    invocations = []

    async def accept(invocation):
        invocations.append(invocation)
        await asyncio.get_running_loop().run_in_executor(
            None, partial(dispatch.accept_engine_entry, services, invocation)
        )

    await dispatch.dispatch_run(services, claim=claim, invoke=accept)
    assert len(invocations) == 1
    invocation = invocations[0]
    arguments = {"value": "fixture effect"}
    action = ToolEffectAction(
        schemaVersion=1,
        executionAttempt=1,
        toolCallID="fixture-call-1",
        toolName="fixture_effect",
        argumentsJSON=canonical_json(arguments),
        argumentsDigest=content_digest(arguments),
        capabilityVersion="fixture-capability-v1",
        policyVersion="fixture-policy-v1",
        workspaceDigest=content_digest(
            {"workspaceID": str(invocation.intent.snapshot.workspaceID), "scope": "fixture-only"}
        ),
    )
    yield EffectFixture(services, invocation, action)
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] == 0


async def execute(effect, *, authorize=None, handler=None):
    return await execute_effect(
        effect.services,
        invocation=effect.invocation,
        action=effect.action,
        authorize=authorize if authorize is not None else AsyncMock(return_value=True),
        handler=handler if handler is not None else AsyncMock(return_value=output()),
    )


async def database(operation):
    return await asyncio.get_running_loop().run_in_executor(None, operation)


async def test_success_and_terminal_replay_never_repeat_the_handler(effect):
    authorize = AsyncMock(return_value=True)
    handler = AsyncMock(return_value=output())
    receipt = await execute(effect, authorize=authorize, handler=handler)
    before = [tuple(row) for row in effect.events()]
    assert await execute(effect, authorize=authorize, handler=handler) == receipt
    authorize.assert_awaited_once()
    handler.assert_awaited_once()
    record = effect.record()
    assert record.phase == "terminal" and record.receipt == receipt
    assert receipt.outcome == "succeeded" and not receipt.reconciliationRequired
    assert [tuple(row) for row in effect.events()] == before
    assert [row[1] for row in effect.events()] == list(range(1, len(before) + 1))
    effect.assert_lane(reconciliation=False)


@pytest.mark.parametrize("decision", [False, None, 1])
async def test_authorization_requires_true_and_records_not_dispatched(effect, decision):
    handler = AsyncMock(return_value=output())
    receipt = await execute(effect, authorize=AsyncMock(return_value=decision), handler=handler)
    handler.assert_not_awaited()
    assert receipt.outcome == "notDispatched" and receipt.reasonCode == "authorizationDenied"
    assert effect.record().phase == "terminal"
    effect.assert_lane(reconciliation=False)


async def test_authorization_exception_preserves_not_dispatched_fact(effect):
    handler = AsyncMock(return_value=output())
    with pytest.raises(RuntimeError, match="authorization fixture failed"):
        await execute(
            effect,
            authorize=AsyncMock(side_effect=RuntimeError("authorization fixture failed")),
            handler=handler,
        )
    handler.assert_not_awaited()
    assert effect.record().receipt.reasonCode == "authorizationDenied"
    effect.assert_lane(reconciliation=False)


async def test_cancel_during_authorization_rejects_without_calling_handler(effect):
    entered = asyncio.Event()

    async def authorize():
        entered.set()
        await asyncio.Future()

    handler = AsyncMock(return_value=output())
    task = asyncio.create_task(execute(effect, authorize=authorize, handler=handler))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    handler.assert_not_awaited()
    assert effect.record().receipt.reasonCode == "callerCancelled"
    effect.assert_lane(reconciliation=False)


async def test_prepare_storage_failure_never_calls_callbacks(effect, monkeypatch):
    def fail(*args):
        raise StorageUnavailableError("fixture prepare failed")

    monkeypatch.setattr(effects, "prepare_effect", fail)
    authorize, handler = AsyncMock(return_value=True), AsyncMock(return_value=output())
    with pytest.raises(StorageUnavailableError):
        await execute(effect, authorize=authorize, handler=handler)
    authorize.assert_not_awaited()
    handler.assert_not_awaited()
    with effect.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0


async def test_begin_event_failure_rolls_back_handoff_then_marks_unknown(effect, monkeypatch):
    insert = effect.services.ac_event_repo.insert

    def fail_dispatch(conn, **kwargs):
        if kwargs["event_type"] == "tool.dispatching":
            raise sqlite3.OperationalError("fixture dispatch event unavailable")
        return insert(conn, **kwargs)

    monkeypatch.setattr(effect.services.ac_event_repo, "insert", fail_dispatch)
    handler = AsyncMock(return_value=output())
    with pytest.raises(StorageUnavailableError):
        await execute(effect, handler=handler)
    handler.assert_not_awaited()
    assert effect.record().phase == "outcomeUnknown"
    assert effect.record().dispatchStartedAt is None and effect.record().receipt is None
    assert "tool.dispatching" not in [row[0] for row in effect.events()]
    effect.assert_lane(reconciliation=True)


async def test_begin_commit_confirmation_lost_never_hands_off_and_records_unknown(
    effect, monkeypatch
):
    begin = effects.begin_effect

    def lost_confirmation(*args):
        begin(*args)
        raise StorageUnavailableError("fixture dispatch confirmation lost")

    monkeypatch.setattr(effects, "begin_effect", lost_confirmation)
    handler = AsyncMock(return_value=output())
    with pytest.raises(StorageUnavailableError, match="fixture dispatch confirmation lost"):
        await execute(effect, handler=handler)
    handler.assert_not_awaited()
    assert effect.record().phase == "outcomeUnknown"
    assert effect.record().dispatchStartedAt is not None and effect.record().receipt is None
    effect.assert_lane(reconciliation=True)


@pytest.mark.parametrize("stage", ["prepare_effect", "begin_effect", "complete_effect"])
@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancel_during_database_commit_observes_the_final_fact(
    effect, monkeypatch, stage, cancellations
):
    committed, release, finished = Event(), Event(), Event()
    original = getattr(effects, stage)

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        committed.set()
        try:
            assert release.wait(timeout=10)
            return result
        finally:
            finished.set()

    monkeypatch.setattr(effects, stage, delayed)
    handler = AsyncMock(return_value=output())
    task = asyncio.create_task(execute(effect, handler=handler))
    try:
        assert await database(partial(committed.wait, 10))
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await database(partial(finished.wait, 10))
    record = effect.record()
    if stage == "prepare_effect":
        handler.assert_not_awaited()
        assert record.phase == "terminal" and record.receipt.reasonCode == "callerCancelled"
        effect.assert_lane(reconciliation=False)
    elif stage == "begin_effect":
        handler.assert_not_awaited()
        assert record.phase == "outcomeUnknown" and record.receipt is None
        effect.assert_lane(reconciliation=True)
    else:
        handler.assert_awaited_once()
        assert record.phase == "terminal" and record.receipt.outcome == "succeeded"
        effect.assert_lane(reconciliation=False)


async def test_handler_exception_after_partial_effect_never_fabricates_a_receipt(effect):
    observed = []

    async def handler():
        observed.append("partial effect happened")
        raise RuntimeError("fixture partial failure")

    with pytest.raises(RuntimeError, match="fixture partial failure"):
        await execute(effect, handler=handler)
    assert observed == ["partial effect happened"]
    assert effect.record().phase == "outcomeUnknown" and effect.record().receipt is None
    effect.assert_lane(reconciliation=True)
    retry = AsyncMock(return_value=output())
    with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
        await execute(effect, handler=retry)
    retry.assert_not_awaited()


async def test_cancel_during_handler_preserves_unknown_and_both_lanes(effect):
    started = asyncio.Event()
    observed = []

    async def handler():
        observed.append("possible external effect")
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(execute(effect, handler=handler))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed == ["possible external effect"]
    assert effect.record().phase == "outcomeUnknown" and effect.record().receipt is None
    effect.assert_lane(reconciliation=True)


async def test_terminal_event_failure_rolls_back_receipt_then_records_unknown(effect, monkeypatch):
    insert = effect.services.ac_event_repo.insert

    def fail_completion(conn, **kwargs):
        if kwargs["event_type"] == "tool.terminal":
            raise sqlite3.OperationalError("fixture terminal event unavailable")
        return insert(conn, **kwargs)

    monkeypatch.setattr(effect.services.ac_event_repo, "insert", fail_completion)
    handler = AsyncMock(return_value=output())
    with pytest.raises(StorageUnavailableError):
        await execute(effect, handler=handler)
    handler.assert_awaited_once()
    assert effect.record().phase == "outcomeUnknown" and effect.record().receipt is None
    assert "tool.terminal" not in [row[0] for row in effect.events()]
    effect.assert_lane(reconciliation=True)


async def test_unknown_storage_failure_keeps_durable_dispatch_and_never_succeeds(
    effect, monkeypatch
):
    def fail_completion(*args):
        raise StorageUnavailableError("fixture completion unavailable")

    def fail_interrupt(*args, **kwargs):
        raise StorageUnavailableError("fixture uncertainty unavailable")

    monkeypatch.setattr(effects, "complete_effect", fail_completion)
    monkeypatch.setattr(effects, "interrupt_effect", fail_interrupt)
    with pytest.raises(StorageUnavailableError, match="fixture uncertainty unavailable"):
        await execute(effect)
    assert effect.record().phase == "dispatching" and effect.record().receipt is None
    retry = AsyncMock(return_value=output())
    with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
        await execute(effect, handler=retry)
    retry.assert_not_awaited()
    effect.assert_lane(reconciliation=False)


async def test_duplicate_during_handler_does_not_interrupt_the_winner(effect):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler():
        calls.append("winner")
        started.set()
        await release.wait()
        return output()

    task = asyncio.create_task(execute(effect, handler=handler))
    await started.wait()
    loser = AsyncMock(return_value=output())
    try:
        with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
            await execute(effect, handler=loser)
        loser.assert_not_awaited()
        assert effect.record().phase == "dispatching"
        effect.assert_lane(reconciliation=False)
    finally:
        release.set()
    assert (await task).outcome == "succeeded"
    assert calls == ["winner"]


async def test_two_connections_have_only_one_handler_winner(effect):
    pool = SqlitePool(effect.services.paths.db)
    other = build_shared_services(db=pool, paths=effect.services.paths, config=OctopConfig())
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler():
        calls.append("winner")
        started.set()
        await release.wait()
        return output()

    tasks = [
        asyncio.create_task(
            execute_effect(
                services,
                invocation=effect.invocation,
                action=effect.action,
                authorize=AsyncMock(return_value=True),
                handler=handler,
            )
        )
        for services in (effect.services, other)
    ]
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        done, pending = await asyncio.wait(tasks, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == len(pending) == 1
        failure = next(iter(done)).exception()
        assert (
            isinstance(failure, CommandAdmissionError) and failure.code == "reconciliationRequired"
        )
        assert effect.record().phase == "dispatching"
        effect.assert_lane(reconciliation=False)
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        pool.close()
    assert calls == ["winner"]
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert effect.record().phase == "terminal"


async def test_terminal_commit_confirmation_lost_keeps_receipt_and_replay_is_safe(
    effect, monkeypatch
):
    complete = effects.complete_effect

    def lost_confirmation(*args):
        complete(*args)
        raise StorageUnavailableError("fixture terminal confirmation lost")

    monkeypatch.setattr(effects, "complete_effect", lost_confirmation)
    handler = AsyncMock(return_value=output())
    with pytest.raises(StorageUnavailableError, match="fixture terminal confirmation lost"):
        await execute(effect, handler=handler)
    record = effect.record()
    assert record.phase == "terminal" and record.receipt.outcome == "succeeded"
    assert await execute(effect, handler=handler) == record.receipt
    handler.assert_awaited_once()
    effect.assert_lane(reconciliation=False)


async def test_same_call_with_changed_arguments_conflicts_without_another_handler(effect):
    await execute(effect)
    changed = {"value": "different operation"}
    effect.action = effect.action.model_copy(
        update={
            "argumentsJSON": canonical_json(changed),
            "argumentsDigest": content_digest(changed),
        }
    )
    handler = AsyncMock(return_value=output())
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        await execute(effect, handler=handler)
    handler.assert_not_awaited()
    assert effect.record().receipt.outcome == "succeeded"


async def test_replayed_prepared_record_does_not_call_authorization_or_handler(effect):
    prepared = await database(
        partial(effects.prepare_effect, effect.services, effect.invocation, effect.action)
    )
    authorize, handler = AsyncMock(return_value=True), AsyncMock(return_value=output())
    with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
        await execute(effect, authorize=authorize, handler=handler)
    authorize.assert_not_awaited()
    handler.assert_not_awaited()
    assert effect.record() == prepared.record
    effect.assert_lane(reconciliation=False)


async def test_competing_begin_winner_is_never_interrupted_by_runner_cas_failure(effect):
    async def authorize():
        prepared = effect.record()
        await database(
            partial(
                effects.begin_effect,
                effect.services,
                effect.invocation,
                prepared.intent.effectID,
                prepared.revision,
            )
        )
        return True

    handler = AsyncMock(return_value=output())
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        await execute(effect, authorize=authorize, handler=handler)
    handler.assert_not_awaited()
    assert effect.record().phase == "dispatching"
    effect.assert_lane(reconciliation=False)


@pytest.mark.parametrize("fence", ["disabledOwner", "restoreEpoch", "closedGate"])
async def test_begin_rechecks_realtime_fences_and_records_no_handoff(effect, monkeypatch, fence):
    async def authorize():
        if fence == "disabledOwner":

            def disable():
                with effect.services.db.transaction() as conn:
                    conn.execute("UPDATE users SET disabled=1 WHERE id=1")

            await database(disable)
        elif fence == "restoreEpoch":

            def advance():
                identity = read_identity(effect.services.paths.agentcomputer_identity)
                effect.services.paths.agentcomputer_identity.write_text(
                    identity.model_copy(
                        update={"restoreEpoch": identity.restoreEpoch + 1}
                    ).model_dump_json()
                )

            await database(advance)
        else:
            from octop.infra.agentcomputer.capabilities import get_capabilities

            monkeypatch.setattr(dispatch, "get_capabilities", get_capabilities)
            monkeypatch.setattr(effects, "get_capabilities", get_capabilities)
        return True

    handler = AsyncMock(return_value=output())
    with pytest.raises(CommandAdmissionError):
        await execute(effect, authorize=authorize, handler=handler)
    handler.assert_not_awaited()
    record = effect.record()
    assert record.phase == "terminal" and record.receipt.reasonCode == "authorizationDenied"
    effect.assert_lane(reconciliation=False)


async def test_late_handler_result_cannot_overwrite_a_committed_unknown(effect):
    started, release = asyncio.Event(), asyncio.Event()

    async def handler():
        started.set()
        await release.wait()
        return output()

    task = asyncio.create_task(execute(effect, handler=handler))
    await started.wait()
    record = effect.record()
    try:
        interrupted = await database(
            partial(
                effects.interrupt_effect,
                effect.services,
                owner_id=1,
                effect_id=record.intent.effectID,
            )
        )
    finally:
        release.set()
    with pytest.raises(CommandAdmissionError):
        await task
    assert effect.record() == interrupted
    assert effect.record().receipt is None
    effect.assert_lane(reconciliation=True)


async def test_authorizer_suppressing_cancel_cannot_hand_off(effect):
    entered = asyncio.Event()

    async def authorize():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return True

    handler = AsyncMock(return_value=output())
    task = asyncio.create_task(execute(effect, authorize=authorize, handler=handler))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    handler.assert_not_awaited()
    assert effect.record().receipt.reasonCode == "callerCancelled"
    effect.assert_lane(reconciliation=False)


async def test_handler_suppressing_cancel_preserves_result_but_propagates_cancellation(effect):
    entered = asyncio.Event()

    async def handler():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return output()

    task = asyncio.create_task(execute(effect, handler=handler))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert effect.record().phase == "terminal" and effect.record().receipt.outcome == "succeeded"
    effect.assert_lane(reconciliation=False)


@pytest.mark.parametrize("outcome,reported", [("failed", "reported"), ("succeeded", "unknown")])
async def test_adapter_reports_reconciliation_without_claiming_rollback(effect, outcome, reported):
    receipt = await execute(
        effect, handler=AsyncMock(return_value=output(outcome=outcome, reported=reported))
    )
    assert receipt.outcome == outcome and receipt.reconciliationRequired
    assert effect.record().phase == "terminal" and effect.record().receipt == receipt
    effect.assert_lane(reconciliation=True)


@pytest.mark.parametrize("phase", ["prepared", "terminal", "outcomeUnknown"])
async def test_missing_effect_row_with_persisted_events_cannot_register_another_identity(
    effect, phase
):
    if phase == "prepared":
        await database(
            partial(effects.prepare_effect, effect.services, effect.invocation, effect.action)
        )
    elif phase == "terminal":
        await execute(effect)
    else:
        with pytest.raises(RuntimeError, match="partial effect occurred"):
            await execute(
                effect,
                handler=AsyncMock(side_effect=RuntimeError("partial effect occurred")),
            )
    original = effect.record()
    assert original.phase == phase
    with effect.services.db.transaction() as conn:
        event_facts = [
            tuple(row)
            for row in conn.execute(
                "SELECT event_id,event_type,payload_json,sequence FROM ac_events ORDER BY sequence"
            )
        ]
        conn.execute(
            "DELETE FROM ac_tool_effects WHERE effect_id=?", (str(original.intent.effectID),)
        )
    authorize, handler = AsyncMock(return_value=True), AsyncMock(return_value=output())
    with pytest.raises(StorageDataError):
        await execute(effect, authorize=authorize, handler=handler)
    authorize.assert_not_awaited()
    handler.assert_not_awaited()
    with effect.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT event_id,event_type,payload_json,sequence FROM ac_events ORDER BY sequence"
            )
        ] == event_facts
        run = effect.services.ac_run_repo.get_in_transaction(
            conn, str(effect.invocation.intent.runID), 1
        )
        task = effect.services.ac_task_repo.get_in_transaction(
            conn, str(effect.invocation.intent.taskID), 1
        )
        project = effect.services.ac_project_repo.get_in_transaction(
            conn, str(effect.invocation.intent.projectID), 1
        )
    assert task.active_run_id == project.active_run_id == str(effect.invocation.intent.runID)
    assert run.reconciliation_required is (phase == "outcomeUnknown")
    assert run.state == ("interrupted" if phase == "outcomeUnknown" else "queued")
