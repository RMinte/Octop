"""Explicit Run cancellation never borrows the thread-wide harness stop."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_effect_runner import effect as effect
from tests.unit.agentcomputer.test_effect_runner import output
from tests.unit.agentcomputer.test_pauses import approval_command, expiry, ready
from tests.unit.agentcomputer.test_submission_runs import allow_fixture, request
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.config import OctopConfig
from octop.infra.agentcomputer import approvals, cancellations, dispatch, effects
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    admit_new_message,
    get_command_receipt,
)
from octop.infra.agentcomputer.identity import read_identity
from octop.infra.agentcomputer.recovery import recover_dispatches
from octop.infra.agentcomputer.runs import get_run
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services


def _command(revision: int = 1) -> cancellations.CancelRunCommand:
    return cancellations.CancelRunCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        expectedRevision=revision,
    )


def _events(services) -> list[str]:
    with services.db.connect() as conn:
        return [row[0] for row in conn.execute("SELECT event_type FROM ac_events ORDER BY id")]


def _counts(services) -> tuple[int, int, int]:
    with services.db.connect() as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ac_commands", "ac_run_cancellations", "ac_events")
        )


def test_queued_unstarted_cancel_stops_and_releases_both_lanes(control):
    services, _, task_id = control
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    original = services.ac_command_repo.get(str(accepted.commandID), 1).receipt_json
    command = _command()
    receipt = cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=command)
    assert receipt.status == "stopped"
    assert receipt.requestRunRevision == 2 and receipt.resultRunRevision == 3
    assert not receipt.replayed
    view = get_run(services, owner_id=1, run_id=accepted.runID)
    assert view.state == "stopped" and view.revision == 3
    assert view.dispatchPhase == "notDispatched" and not view.reconciliationRequired
    task = services.ac_task_repo.get(str(task_id), 1)
    project = services.ac_project_repo.get(task.project_id, 1)
    assert task.active_run_id is None and project.active_run_id is None
    assert _events(services)[-3:] == ["run.accepted", "run.cancelRequested", "run.finished"]
    assert services.ac_command_repo.get(str(accepted.commandID), 1).receipt_json == original
    assert get_command_receipt(services, 1, accepted.commandID) == accepted
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    # A later Turn acquires the same Task and Project after this proved stop.
    next_run = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    assert next_run.runID != accepted.runID
    assert get_run(services, owner_id=1, run_id=accepted.runID).state == "stopped"


def test_exact_cancel_replay_preserves_original_bytes_and_sequence(control):
    services, _, task_id = control
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    command = _command()
    receipt = cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=command)
    original = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
    before = _counts(services)
    replay = cancellations.cancel_run(
        services,
        owner_id=1,
        run_id=accepted.runID,
        command=command.model_copy(update={"commandID": uuid4()}),
    )
    assert replay == receipt.model_copy(update={"replayed": True})
    assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == original
    assert _counts(services) == before
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        cancellations.cancel_run(
            services,
            owner_id=1,
            run_id=accepted.runID,
            command=command.model_copy(update={"expectedRevision": 2}),
        )
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=_command(3))
    assert _counts(services) == before


def test_claimed_unstarted_cancel_stops_without_dispatch_and_releases_both_lanes(
    control, monkeypatch
):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    original = services.ac_command_repo.get(str(accepted.commandID), 1).receipt_json
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=accepted.runID, claimant_id=uuid4(), expected_revision=1
    )
    claim_row = services.ac_run_dispatch_repo.get(str(accepted.runID), 1)
    assert claim_row is not None
    assert claim_row.revision == 1 and claim_row.dispatch_started_at is None
    assert claim_row.engine_receipt_json is None
    command = _command(2)
    receipt = cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=command)
    assert receipt.status == "stopped" and receipt.requestRunRevision == 3
    assert receipt.resultRunRevision == 4 and receipt.executionEpoch == 1
    view = get_run(services, owner_id=1, run_id=accepted.runID)
    assert view.state == "stopped" and view.dispatchPhase == "claimed"
    assert view.executionEpoch == 1 and not view.reconciliationRequired
    assert view.cancelRequested is True
    task = services.ac_task_repo.get(str(task_id), 1)
    project = services.ac_project_repo.get(task.project_id, 1)
    assert task.active_run_id is None and project.active_run_id is None
    assert _events(services) == [
        "run.accepted",
        "run.claimed",
        "run.cancelRequested",
        "run.finished",
    ]
    assert services.ac_run_dispatch_repo.get(str(accepted.runID), 1) == claim_row
    assert services.ac_command_repo.get(str(accepted.commandID), 1).receipt_json == original
    assert get_command_receipt(services, 1, accepted.commandID) == accepted
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        dispatch._begin_dispatch(services, claim)
    assert _events(services)[-1] == "run.finished"
    assert recover_dispatches(services).scanned == 0
    next_run = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    assert next_run.runID != accepted.runID
    assert services.ac_task_repo.get(str(task_id), 1).active_run_id == str(next_run.runID)
    assert services.ac_project_repo.get(task.project_id, 1).active_run_id == str(next_run.runID)


def test_claimed_stop_replay_and_historic_receipt_keep_original_bytes(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    dispatch.claim_run(
        services, owner_id=1, run_id=accepted.runID, claimant_id=uuid4(), expected_revision=1
    )
    command = _command(2)
    receipt = cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=command)
    original = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
    before = _counts(services)
    replay = cancellations.cancel_run(
        services,
        owner_id=1,
        run_id=accepted.runID,
        command=command.model_copy(update={"commandID": uuid4()}),
    )
    assert replay == receipt.model_copy(update={"replayed": True})
    assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == original
    assert _counts(services) == before

    identity = read_identity(services.paths.agentcomputer_identity)
    services.paths.agentcomputer_identity.write_text(
        identity.model_copy(update={"restoreEpoch": 2}).model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(CommandAdmissionError, match="restoreEpochConflict"):
        cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=command)
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == original
    assert _counts(services) == before


@pytest.mark.parametrize("winner", ["cancel", "dispatch"])
def test_claimed_cancel_and_dispatch_commit_have_one_ordered_winner(control, monkeypatch, winner):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=accepted.runID, claimant_id=uuid4(), expected_revision=1
    )
    other_pool = SqlitePool(services.paths.db)
    other = build_shared_services(db=other_pool, paths=services.paths, config=OctopConfig())
    entered, attempted, release = Event(), Event(), Event()
    method = "mark_cancel_requested" if winner == "cancel" else "mark_dispatching"
    original = getattr(services.ac_run_repo, method)

    def hold_winner(*args, **kwargs):
        row = original(*args, **kwargs)
        assert row is not None
        entered.set()
        assert release.wait(timeout=10)
        return row

    monkeypatch.setattr(services.ac_run_repo, method, hold_winner)

    def begin_on_other():
        attempted.set()
        try:
            return dispatch._begin_dispatch(other, claim)
        except CommandAdmissionError as exc:
            return exc.code

    def cancel_on_other():
        attempted.set()
        return cancellations.cancel_run(
            other, owner_id=1, run_id=accepted.runID, command=_command(3)
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            if winner == "cancel":
                first = executor.submit(
                    cancellations.cancel_run,
                    services,
                    owner_id=1,
                    run_id=accepted.runID,
                    command=_command(2),
                )
                second_action = begin_on_other
            else:
                first = executor.submit(dispatch._begin_dispatch, services, claim)
                second_action = cancel_on_other
            try:
                assert entered.wait(timeout=10)
                second = executor.submit(second_action)
                assert attempted.wait(timeout=10)
            finally:
                release.set()
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)
    finally:
        release.set()
        other_pool.close()

    events = _events(services)
    view = get_run(services, owner_id=1, run_id=accepted.runID)
    task = services.ac_task_repo.get(str(task_id), 1)
    project = services.ac_project_repo.get(task.project_id, 1)
    dispatch_row = services.ac_run_dispatch_repo.get(str(accepted.runID), 1)
    assert dispatch_row is not None and dispatch_row.engine_receipt_json is None
    if winner == "cancel":
        assert first_result.status == "stopped" and second_result == "revisionConflict"
        assert view.state == "stopped" and view.dispatchPhase == "claimed"
        assert dispatch_row.dispatch_started_at is None
        assert task.active_run_id is None and project.active_run_id is None
        assert events == ["run.accepted", "run.claimed", "run.cancelRequested", "run.finished"]
    else:
        assert isinstance(first_result, dispatch.EngineInvocation)
        assert second_result.status == "cancelRequested"
        assert view.state == "queued" and view.dispatchPhase == "dispatching"
        assert dispatch_row.dispatch_started_at is not None
        assert task.active_run_id == str(accepted.runID)
        assert project.active_run_id == str(accepted.runID)
        assert events == ["run.accepted", "run.claimed", "run.dispatching", "run.cancelRequested"]


def test_cancel_after_engine_ack_blocks_new_model_step_without_stop_claim(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=accepted.runID, claimant_id=uuid4(), expected_revision=1
    )
    invocation = dispatch._begin_dispatch(services, claim)
    assert not isinstance(invocation, dispatch.EngineEntryReceipt)
    dispatch.accept_engine_entry(services, invocation)
    receipt = cancellations.cancel_run(
        services, owner_id=1, run_id=accepted.runID, command=_command(4)
    )
    assert receipt.status == "cancelRequested"
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        dispatch.validate_engine_execution(services, invocation)
    view = get_run(services, owner_id=1, run_id=accepted.runID)
    assert view.state == "queued" and view.dispatchPhase == "engineAccepted"
    assert services.ac_task_repo.get(str(task_id), 1).active_run_id == str(accepted.runID)


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
async def test_inflight_tool_result_can_finish_after_cancel_without_new_handoff(effect, outcome):
    prepared = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    effects.begin_effect(
        effect.services, effect.invocation, prepared.intent.effectID, prepared.revision
    )
    run_id = effect.invocation.intent.runID
    receipt = cancellations.cancel_run(
        effect.services, owner_id=1, run_id=run_id, command=_command(4)
    )
    assert receipt.status == "cancelRequested"
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        effects.prepare_effect(
            effect.services,
            effect.invocation,
            effect.action.model_copy(update={"toolCallID": "new-call-after-cancel"}),
        )
    late = effects.complete_effect(
        effect.services, effect.invocation, prepared.intent.effectID, output(outcome=outcome)
    )
    assert late.outcome == outcome
    view = get_run(effect.services, owner_id=1, run_id=run_id)
    assert view.state == ("interrupted" if outcome == "failed" else "queued")
    assert view.reconciliationRequired is (outcome == "failed")
    assert effect.services.ac_task_repo.get(
        str(effect.invocation.intent.taskID), 1
    ).active_run_id == str(run_id)


async def test_cancel_waiting_approval_retains_pause_but_blocks_old_decision(effect, monkeypatch):
    action = ready(effect, monkeypatch)
    pause = approvals.request_approval(
        effect.services, effect.invocation, action=action, expires_at=expiry()
    )
    before = get_run(effect.services, owner_id=1, run_id=pause.intent.runID)
    receipt = cancellations.cancel_run(
        effect.services,
        owner_id=1,
        run_id=pause.intent.runID,
        command=_command(before.revision),
    )
    assert receipt.status == "cancelRequested"
    after = get_run(effect.services, owner_id=1, run_id=pause.intent.runID)
    assert after.state == "waitingApproval" and not after.reconciliationRequired
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        approvals.decide_approval(
            effect.services,
            owner_id=1,
            command=approval_command(pause),
            current_action=action,
            authorize=lambda _: pytest.fail("cancelled Run must not authorize a decision"),
        )
    assert effect.services.ac_task_repo.get(str(pause.intent.taskID), 1).active_run_id == str(
        pause.intent.runID
    )


@pytest.mark.parametrize("collision", ["key", "commandID"])
async def test_cancel_cannot_reuse_pause_decision_identity(effect, monkeypatch, collision):
    action = ready(effect, monkeypatch)
    pause = approvals.request_approval(
        effect.services, effect.invocation, action=action, expires_at=expiry()
    )
    decision = approval_command(pause)
    approvals.decide_approval(
        effect.services,
        owner_id=1,
        command=decision,
        current_action=action,
        authorize=lambda _: True,
    )
    run = get_run(effect.services, owner_id=1, run_id=pause.intent.runID)
    candidate = _command(run.revision)
    if collision == "key":
        candidate = candidate.model_copy(update={"idempotencyKey": decision.idempotencyKey})
    else:
        candidate = candidate.model_copy(update={"commandID": decision.commandID})
    before = _counts(effect.services)
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        cancellations.cancel_run(
            effect.services, owner_id=1, run_id=pause.intent.runID, command=candidate
        )
    assert _counts(effect.services) == before


def test_stale_or_cross_owner_cancel_changes_nothing(control):
    services, _, task_id = control
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    before = _counts(services)
    with pytest.raises(CommandAdmissionError, match="resourceNotFound"):
        cancellations.cancel_run(services, owner_id=2, run_id=accepted.runID, command=_command())
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=_command(2))
    assert _counts(services) == before
    assert get_run(services, owner_id=1, run_id=accepted.runID).state == "queued"


@pytest.mark.parametrize("lost", ["row", "command", "event", "finished"])
def test_lost_cancellation_fact_is_not_recreated(control, lost):
    services, _, task_id = control
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    command = _command()
    cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=command)
    if lost == "command":
        with services.db.connect() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.execute("DELETE FROM ac_commands WHERE operation='run.cancel'")
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
    else:
        with services.db.transaction() as conn:
            if lost == "row":
                conn.execute("DELETE FROM ac_run_cancellations")
            elif lost == "event":
                conn.execute("DELETE FROM ac_events WHERE event_type='run.cancelRequested'")
            else:
                conn.execute("DELETE FROM ac_events WHERE event_type='run.finished'")
    with pytest.raises(StorageDataError):
        cancellations.cancel_run(services, owner_id=1, run_id=accepted.runID, command=command)
    with pytest.raises(StorageDataError):
        get_run(services, owner_id=1, run_id=accepted.runID)


@pytest.mark.parametrize("claimed", [False, True], ids=["queued", "claimed"])
@pytest.mark.parametrize("failure", ["finished_event", "task_release", "project_release"])
def test_failed_stop_commit_rolls_back_request_and_both_lanes(
    control, monkeypatch, claimed, failure
):
    services, _, task_id = control
    if claimed:
        allow_fixture(services, monkeypatch)
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = (
        dispatch.claim_run(
            services, owner_id=1, run_id=accepted.runID, claimant_id=uuid4(), expected_revision=1
        )
        if claimed
        else None
    )
    before = _counts(services)
    before_events = _events(services)
    before_view = get_run(services, owner_id=1, run_id=accepted.runID)
    before_dispatch = services.ac_run_dispatch_repo.get(str(accepted.runID), 1)
    before_task = services.ac_task_repo.get(str(task_id), 1)
    before_project = services.ac_project_repo.get(before_task.project_id, 1)
    if failure == "finished_event":
        insert = services.ac_event_repo.insert

        def fail_event(*args, **kwargs):
            result = insert(*args, **kwargs)
            if kwargs.get("event_type") == "run.finished":
                raise RuntimeError("injected finish event failure")
            return result

        monkeypatch.setattr(services.ac_event_repo, "insert", fail_event)
        expected = pytest.raises(RuntimeError, match="finish event")
    else:
        repo = services.ac_task_repo if failure == "task_release" else services.ac_project_repo
        release = repo.release_run

        def fail_release(*args, **kwargs):
            assert release(*args, **kwargs)
            return False

        monkeypatch.setattr(repo, "release_run", fail_release)
        expected = pytest.raises(CommandAdmissionError, match="activeRunConflict")

    with expected:
        cancellations.cancel_run(
            services, owner_id=1, run_id=accepted.runID, command=_command(before_view.revision)
        )
    assert _counts(services) == before
    assert _events(services) == before_events
    assert get_run(services, owner_id=1, run_id=accepted.runID) == before_view
    assert services.ac_run_dispatch_repo.get(str(accepted.runID), 1) == before_dispatch
    assert services.ac_task_repo.get(str(task_id), 1) == before_task
    assert services.ac_project_repo.get(before_task.project_id, 1) == before_project
    if claim is not None:
        assert isinstance(dispatch._begin_dispatch(services, claim), dispatch.EngineInvocation)


def test_two_connections_accept_one_cancel_and_release_once(control):
    services, _, task_id = control
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    other_pool = SqlitePool(services.paths.db)
    other = build_shared_services(db=other_pool, paths=services.paths, config=OctopConfig())
    barrier = Barrier(2)
    commands = (_command(), _command())

    def submit(args):
        target, command = args
        barrier.wait(timeout=10)
        try:
            return cancellations.cancel_run(
                target, owner_id=1, run_id=accepted.runID, command=command
            ).status
        except CommandAdmissionError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, zip((services, other), commands, strict=True)))
        assert results.count("stopped") == 1
        assert results.count("idempotencyConflict") == 1
        assert _counts(services) == (4, 1, 3)
        assert get_run(other, owner_id=1, run_id=accepted.runID).state == "stopped"
    finally:
        other_pool.close()
