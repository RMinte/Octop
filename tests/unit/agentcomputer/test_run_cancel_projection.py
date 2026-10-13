"""The current Run view reflects validated cancellation facts, not an inferred stop."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_submission_runs import allow_fixture, request
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.config import OctopConfig
from octop.infra.agentcomputer import cancellations, dispatch
from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.runs import get_run
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services


def _command(revision: int) -> cancellations.CancelRunCommand:
    return cancellations.CancelRunCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        expectedRevision=revision,
    )


def test_unstarted_run_projection_distinguishes_cancel_request_from_stopped(control) -> None:
    services, _, task_id = control
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    before = get_run(services, owner_id=1, run_id=accepted.runID)
    assert before is not None and before.state == "queued" and before.cancelRequested is False

    receipt = cancellations.cancel_run(
        services, owner_id=1, run_id=accepted.runID, command=_command(before.revision)
    )
    after = get_run(services, owner_id=1, run_id=accepted.runID)
    assert receipt.status == "stopped"
    assert after is not None and after.state == "stopped" and after.cancelRequested is True


@pytest.mark.parametrize(
    ("phase", "expected_status", "expected_state"),
    [
        ("claimed", "stopped", "stopped"),
        ("engineAccepted", "cancelRequested", "queued"),
    ],
)
def test_claimed_run_stops_but_started_run_keeps_request(
    control,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_status: str,
    expected_state: str,
) -> None:
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = dispatch.claim_run(
        services,
        owner_id=1,
        run_id=accepted.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    if phase == "engineAccepted":
        invocation = dispatch._begin_dispatch(services, claim)
        assert isinstance(invocation, dispatch.EngineInvocation)
        dispatch.accept_engine_entry(services, invocation)
    before = get_run(services, owner_id=1, run_id=accepted.runID)
    assert before is not None and before.cancelRequested is False
    receipt = cancellations.cancel_run(
        services, owner_id=1, run_id=accepted.runID, command=_command(before.revision)
    )

    replacement_pool = SqlitePool(services.paths.db)
    replacement = build_shared_services(
        db=replacement_pool, paths=services.paths, config=OctopConfig()
    )
    try:
        after = get_run(replacement, owner_id=1, run_id=accepted.runID)
    finally:
        replacement_pool.close()
    assert receipt.status == expected_status
    assert after is not None and after.state == expected_state
    assert after.dispatchPhase == phase and after.cancelRequested is True
    task = services.ac_task_repo.get(str(task_id), 1)
    project = services.ac_project_repo.get(task.project_id, 1)
    active = None if phase == "claimed" else str(accepted.runID)
    assert task.active_run_id == active and project.active_run_id == active
