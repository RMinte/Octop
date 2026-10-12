"""Durable dispatch windows without a production consumer or provider."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_submission_runs import control as control
from tests.unit.agentcomputer.test_submission_runs import request

from octop.config import OctopConfig
from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    admit_new_message,
    get_command_receipt,
)
from octop.infra.agentcomputer.runs import get_run
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services


def allow(monkeypatch):
    def profile(services):
        original = get_capabilities(services)
        return original.model_copy(
            update={
                "supportedPermissionModes": ["askForApproval"],
                "capabilities": original.capabilities.model_copy(
                    update={
                        "runs": CapabilityStatus(supported=True, available=True),
                    }
                ),
            }
        )

    monkeypatch.setattr(dispatch, "get_capabilities", profile)


def counts(services):
    with services.db.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ac_turns", "ac_runs", "ac_commands", "ac_events", "ac_run_dispatches")
        }


def seed(control, monkeypatch):
    services, _, task_id = control
    allow(monkeypatch)
    receipt = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=receipt.runID, claimant_id=uuid4(), expected_revision=1
    )
    return services, receipt, claim


def test_production_claim_gate_does_not_change_original_run_or_sequence(control):
    services, _, task_id = control
    receipt = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable"):
        dispatch.claim_run(
            services, owner_id=1, run_id=receipt.runID, claimant_id=uuid4(), expected_revision=1
        )
    assert counts(services) == before
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.revision == 1 and view.executionEpoch == 0 and view.dispatchPhase == "notDispatched"


async def test_claim_dispatch_entry_are_durable_and_duplicate_never_invokes_again(
    control, monkeypatch
):
    services, receipt, claim = seed(control, monkeypatch)
    original = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
    calls = []

    async def invoke(invocation):
        calls.append(invocation)
        # A second connection observes dispatching before the callback starts.
        pool = SqlitePool(services.paths.db)
        other = build_shared_services(db=pool, paths=services.paths, config=OctopConfig())
        try:
            assert get_run(other, owner_id=1, run_id=receipt.runID).dispatchPhase == "dispatching"
        finally:
            pool.close()
        accepted = await asyncio.to_thread(dispatch.accept_engine_entry, services, invocation)
        assert not accepted.replayed

    entry = await dispatch.dispatch_run(services, claim=claim, invoke=invoke)
    before = counts(services)
    assert await dispatch.dispatch_run(services, claim=claim, invoke=invoke) == entry
    assert len(calls) == 1 and counts(services) == before
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "queued" and view.dispatchPhase == "engineAccepted" and view.revision == 4
    assert view.executionEpoch == 1 and not view.reconciliationRequired
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == original
    with services.db.connect() as conn:
        assert [
            row[0] for row in conn.execute("SELECT event_type FROM ac_events ORDER BY sequence")
        ] == ["run.accepted", "run.claimed", "run.dispatching", "run.engineAccepted"]
    assert dispatch.get_claim(services, owner_id=2, run_id=receipt.runID) is None


def test_two_connections_have_one_claimant_and_one_fixed_intent(control, monkeypatch):
    services, _, task_id = control
    allow(monkeypatch)
    receipt = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    pool = SqlitePool(services.paths.db)
    other = build_shared_services(db=pool, paths=services.paths, config=OctopConfig())
    barrier = Barrier(2)

    def claim(target):
        barrier.wait(timeout=10)
        try:
            return dispatch.claim_run(
                target, owner_id=1, run_id=receipt.runID, claimant_id=uuid4(), expected_revision=1
            )
        except CommandAdmissionError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (services, other)))
        assert sum(result == "revisionConflict" for result in results) == 1
        assert counts(services)["ac_run_dispatches"] == 1
        assert get_run(other, owner_id=1, run_id=receipt.runID).executionEpoch == 1
    finally:
        pool.close()


@pytest.mark.parametrize("method", ["start_dispatch", "insert_event"])
async def test_predispatch_failure_rolls_back_and_never_calls_engine(control, monkeypatch, method):
    services, receipt, claim = seed(control, monkeypatch)
    before = counts(services)
    repo, name = (
        (services.ac_event_repo, "insert")
        if method == "insert_event"
        else (services.ac_run_dispatch_repo, "start_dispatch")
    )
    original = getattr(repo, name)

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected predispatch write failure")

    monkeypatch.setattr(repo, name, fail)
    invoke = Mock(side_effect=AssertionError("engine must not be called"))
    with pytest.raises(RuntimeError, match="predispatch"):
        await dispatch.dispatch_run(services, claim=claim, invoke=invoke)
    invoke.assert_not_called()
    assert counts(services) == before
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.dispatchPhase == "claimed" and view.revision == 2


@pytest.mark.parametrize("accepted", [False, True])
async def test_lost_confirmation_or_failure_after_entry_keeps_both_lanes(
    control, monkeypatch, accepted
):
    services, receipt, claim = seed(control, monkeypatch)
    calls = []

    async def invoke(invocation):
        calls.append(invocation)
        if accepted:
            await asyncio.to_thread(dispatch.accept_engine_entry, services, invocation)
        raise RuntimeError("injected uncertain handoff")

    with pytest.raises(RuntimeError, match="handoff"):
        await dispatch.dispatch_run(services, claim=claim, invoke=invoke)
    current = dispatch.get_claim(services, owner_id=1, run_id=receipt.runID)
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "interrupted" and view.reconciliationRequired
    assert view.dispatchPhase == ("engineAccepted" if accepted else "dispatching")
    assert (current.engineReceipt is not None) == accepted
    task = services.ac_task_repo.get(str(receipt.taskID), 1)
    assert task.active_run_id == str(receipt.runID)
    assert services.ac_project_repo.get(task.project_id, 1).active_run_id == str(receipt.runID)
    before = counts(services)
    assert dispatch.interrupt_uncertain_run(services, claim=current) == current
    assert counts(services) == before
    with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
        dispatch.claim_run(
            services,
            owner_id=1,
            run_id=receipt.runID,
            claimant_id=uuid4(),
            expected_revision=view.revision,
        )
    assert len(calls) == 1
    if not accepted:
        with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
            dispatch.accept_engine_entry(services, calls[0])


async def test_cancel_during_handoff_commits_uncertainty_before_return(control, monkeypatch):
    services, receipt, claim = seed(control, monkeypatch)
    started = asyncio.Event()

    async def invoke(invocation):
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(dispatch.dispatch_run(services, claim=claim, invoke=invoke))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "interrupted" and view.reconciliationRequired
    assert view.dispatchPhase == "dispatching"


@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancel_at_dispatch_commit_waits_and_marks_uncertain(
    control, monkeypatch, cancellations
):
    services, receipt, claim = seed(control, monkeypatch)
    committed, release, finished = Event(), Event(), Event()
    begin = dispatch._begin_dispatch
    invoke = Mock(side_effect=AssertionError("cancelled handoff must not start an engine"))

    def delayed(services, claim):
        prepared = begin(services, claim)
        committed.set()
        try:
            assert release.wait(timeout=10)
            return prepared
        finally:
            finished.set()

    monkeypatch.setattr(dispatch, "_begin_dispatch", delayed)
    task = asyncio.create_task(dispatch.dispatch_run(services, claim=claim, invoke=invoke))
    try:
        assert await asyncio.to_thread(committed.wait, 10)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 10)
    invoke.assert_not_called()
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "interrupted" and view.reconciliationRequired
    assert view.dispatchPhase == "dispatching"
    assert dispatch.get_claim(services, owner_id=1, run_id=receipt.runID).engineReceipt is None
    task_row = services.ac_task_repo.get(str(receipt.taskID), 1)
    assert task_row.active_run_id == str(receipt.runID)
    assert services.ac_project_repo.get(task_row.project_id, 1).active_run_id == str(receipt.runID)


async def test_late_acceptance_during_fence_preserves_receipt_and_marks_uncertain(
    control, monkeypatch
):
    services, receipt, claim = seed(control, monkeypatch)
    interrupt = dispatch.interrupt_uncertain_run
    captured, entries = {}, []

    async def invoke(invocation):
        captured["invocation"] = invocation
        raise RuntimeError("injected handoff lost")

    def fence_after_late_acceptance(services, *, claim):
        # The fence's earlier read has revision 3. A legitimate late entry
        # commits revision 4 before the fence acquires the Run for tightening.
        entries.append(dispatch.accept_engine_entry(services, captured["invocation"]).receipt)
        return interrupt(services, claim=claim)

    monkeypatch.setattr(dispatch, "interrupt_uncertain_run", fence_after_late_acceptance)
    with pytest.raises(RuntimeError, match="handoff lost"):
        await dispatch.dispatch_run(services, claim=claim, invoke=invoke)
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    current = dispatch.get_claim(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "interrupted" and view.reconciliationRequired
    assert view.dispatchPhase == "engineAccepted"
    assert current.engineReceipt == entries[0]
    with services.db.connect() as conn:
        assert [
            row[0] for row in conn.execute("SELECT event_type FROM ac_events ORDER BY sequence")
        ] == [
            "run.accepted",
            "run.claimed",
            "run.dispatching",
            "run.engineAccepted",
            "run.interrupted",
        ]


@pytest.mark.parametrize("new_writer", [False, True])
async def test_terminal_commit_wins_over_uncertainty_and_preserves_new_writer(
    control, monkeypatch, new_writer
):
    services, receipt, claim = seed(control, monkeypatch)
    winner = {}

    async def invoke(invocation):
        winner["entry"] = await asyncio.to_thread(
            dispatch.accept_engine_entry, services, invocation
        )
        # A separate definitive terminal committer is simulated only in this
        # fixture. Intake does not implement or claim production completion.
        with services.db.transaction() as conn:
            conn.execute(
                "UPDATE ac_runs SET state='succeeded',revision=revision+1,updated_at=updated_at+1 "
                "WHERE run_id=?",
                (str(receipt.runID),),
            )
            conn.execute(
                "UPDATE ac_tasks SET active_run_id=NULL WHERE task_id=?",
                (str(receipt.taskID),),
            )
            conn.execute(
                "UPDATE ac_projects SET active_run_id=NULL WHERE project_id=?",
                (str(claim.intent.projectID),),
            )
        if new_writer:
            winner["next"] = admit_new_message(
                services, owner_id=1, task_id=receipt.taskID, command=request()
            )
        raise RuntimeError("injected handoff lost after terminal commit")

    with pytest.raises(RuntimeError, match="after terminal commit"):
        await dispatch.dispatch_run(services, claim=claim, invoke=invoke)
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "succeeded" and not view.reconciliationRequired
    assert view.dispatchPhase == "engineAccepted" and view.revision == 5
    current = dispatch.get_claim(services, owner_id=1, run_id=receipt.runID)
    assert current.engineReceipt == winner["entry"].receipt
    active = str(winner["next"].runID) if new_writer else None
    assert services.ac_task_repo.get(str(receipt.taskID), 1).active_run_id == active
    assert services.ac_project_repo.get(str(claim.intent.projectID), 1).active_run_id == active
    with services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ac_events WHERE run_id=? AND event_type='run.interrupted'",
                (str(receipt.runID),),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("change", ["claimant", "epoch", "snapshot", "revision"])
async def test_wrong_claim_cannot_handoff_or_write(control, monkeypatch, change):
    services, _, claim = seed(control, monkeypatch)
    if change == "claimant":
        bad = claim.model_copy(update={"claimantID": uuid4()})
    elif change == "revision":
        bad = claim.model_copy(update={"runRevision": 999})
    else:
        intent = claim.intent.model_copy(
            update={"executionEpoch": 9}
            if change == "epoch"
            else {"snapshotDigest": "sha256:wrong"}
        )
        bad = claim.model_copy(update={"intent": intent})
    before = counts(services)
    invoke = Mock(side_effect=AssertionError("no handoff"))
    with pytest.raises(CommandAdmissionError):
        await dispatch.dispatch_run(services, claim=bad, invoke=invoke)
    assert counts(services) == before
    invoke.assert_not_called()


def test_missing_persisted_intent_field_is_not_defaulted(control, monkeypatch):
    services, receipt, _ = seed(control, monkeypatch)
    row = services.ac_run_dispatch_repo.get(str(receipt.runID), 1)
    raw = json.loads(row.intent_json)
    del raw["schemaVersion"]
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_run_dispatches SET intent_json=? WHERE run_id=?",
            (json.dumps(raw), str(receipt.runID)),
        )
    with pytest.raises(StorageDataError):
        dispatch.get_claim(services, owner_id=1, run_id=receipt.runID)
    with pytest.raises(StorageDataError):
        get_run(services, owner_id=1, run_id=receipt.runID)


def test_missing_progressed_run_journal_is_not_rebuilt(control, monkeypatch):
    services, receipt, _ = seed(control, monkeypatch)
    with services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_run_dispatches WHERE run_id=?", (str(receipt.runID),))
    before = counts(services)
    with pytest.raises(StorageDataError):
        get_run(services, owner_id=1, run_id=receipt.runID)
    assert counts(services) == before
