"""Trusted no-tool terminal transaction and its conservative failure windows."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_dispatch import allow
from tests.unit.agentcomputer.test_submission_runs import control as control
from tests.unit.agentcomputer.test_submission_runs import request

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.active_runs import ActiveRunRegistry
from octop.infra.agentcomputer.cancellations import CancelRunCommand, cancel_run
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    admit_new_message,
    content_digest,
    get_command_receipt,
)
from octop.infra.agentcomputer.completions import CompletionEvidence, complete_run
from octop.infra.agentcomputer.engine import _checkpoint_metadata
from octop.infra.agentcomputer.runs import get_run
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool


@pytest.fixture
async def accepted(control, monkeypatch):
    services, _, task_id = control
    allow(monkeypatch)
    receipt = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=receipt.runID, claimant_id=uuid4(), expected_revision=1
    )
    invocation_holder = []

    async def invoke(invocation):
        invocation_holder.append(invocation)
        await asyncio.to_thread(dispatch.accept_engine_entry, services, invocation)

    await dispatch.dispatch_run(
        services, claim=claim, invoke=invoke, active_runs=ActiveRunRegistry()
    )
    return services, receipt, invocation_holder[0]


def _evidence(invocation) -> CompletionEvidence:
    intent = invocation.intent
    answer_id = str(uuid4())
    answer_digest = content_digest("fixture final answer")
    checkpoint_id = uuid4()
    return CompletionEvidence(
        schemaVersion=1,
        historyTurnID=intent.turnID,
        historyThreadID=intent.snapshot.octopThreadID,
        historyAgentID=intent.snapshot.agentID,
        historyStatus="complete",
        historyDigest=content_digest(
            {
                "schemaVersion": 1,
                "turnID": str(intent.turnID),
                "threadID": intent.snapshot.octopThreadID,
                "agentID": intent.snapshot.agentID,
                "status": "complete",
                "assistantMessageID": answer_id,
                "assistantContentDigest": answer_digest,
            }
        ),
        historyAssistantMessageID=answer_id,
        historyAssistantContentDigest=answer_digest,
        terminalCheckpointID=checkpoint_id,
        terminalCheckpointDigest=content_digest(
            {
                "schemaVersion": 1,
                "checkpointID": str(checkpoint_id),
                "checkpointNamespace": "",
                "metadata": _checkpoint_metadata(invocation),
                "assistantMessageID": answer_id,
                "assistantContentDigest": answer_digest,
            }
        ),
        terminalAssistantMessageID=answer_id,
        terminalAssistantContentDigest=answer_digest,
        engineStreamEndedAt=datetime.now(UTC),
    )


def _lanes(services, receipt):
    task = services.ac_task_repo.get(str(receipt.taskID), 1)
    assert task is not None
    project = services.ac_project_repo.get(task.project_id, 1)
    assert project is not None
    return task.active_run_id, project.active_run_id


def test_natural_completion_is_atomic_replayable_and_preserves_original_receipt(accepted):
    services, original, invocation = accepted
    original_json = services.ac_command_repo.get(str(original.commandID), 1).receipt_json
    evidence = _evidence(invocation)
    completed = complete_run(services, invocation, evidence)
    assert completed.resultRunRevision == 5 and not completed.replayed
    assert _lanes(services, original) == (None, None)
    view = get_run(services, owner_id=1, run_id=original.runID)
    assert view.state == "succeeded" and view.revision == 5
    assert not view.reconciliationRequired and not view.cancelRequested
    with services.db.connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM ac_events WHERE run_id=? ORDER BY sequence",
            (str(original.runID),),
        ).fetchall()
        assert [event[0] for event in events] == [
            "run.accepted",
            "run.claimed",
            "run.dispatching",
            "run.engineAccepted",
            "run.finished",
        ]
        assert conn.execute("SELECT COUNT(*) FROM ac_run_terminals").fetchone()[0] == 1
    assert complete_run(services, invocation, evidence) == completed.model_copy(
        update={"replayed": True}
    )
    assert get_command_receipt(services, 1, original.commandID) == original
    assert services.ac_command_repo.get(str(original.commandID), 1).receipt_json == original_json
    next_receipt = admit_new_message(
        services, owner_id=1, task_id=original.taskID, command=request(prompt="next")
    )
    assert _lanes(services, next_receipt) == (str(next_receipt.runID),) * 2
    assert get_run(services, owner_id=1, run_id=original.runID).state == "succeeded"


@pytest.mark.parametrize(
    "mismatch",
    ["turn", "agent", "assistant_id", "assistant_digest", "history_digest", "checkpoint_digest"],
)
def test_mismatched_external_evidence_never_releases_lanes(accepted, mismatch):
    services, original, invocation = accepted
    evidence = _evidence(invocation)
    changes = {
        "turn": {"historyTurnID": uuid4()},
        "agent": {"historyAgentID": "wrong-agent"},
        "assistant_id": {"terminalAssistantMessageID": "different"},
        "assistant_digest": {"terminalAssistantContentDigest": content_digest("different")},
        "history_digest": {"historyDigest": content_digest("wrong-history")},
        "checkpoint_digest": {"terminalCheckpointDigest": content_digest("wrong-checkpoint")},
    }
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        complete_run(services, invocation, evidence.model_copy(update=changes[mismatch]))
    assert _lanes(services, original) == (str(original.runID),) * 2
    assert get_run(services, owner_id=1, run_id=original.runID).state == "queued"


@pytest.mark.parametrize(
    "operation", ["cas", "receipt", "event", "task_release", "project_release"]
)
def test_any_terminal_write_failure_rolls_back_both_lanes(accepted, monkeypatch, operation):
    services, original, invocation = accepted
    target = {
        "cas": (services.ac_run_repo, "mark_succeeded_without_effects"),
        "receipt": (services.ac_run_terminal_repo, "insert"),
        "event": (services.ac_event_repo, "insert"),
        "task_release": (services.ac_task_repo, "release_run"),
        "project_release": (services.ac_project_repo, "release_run"),
    }[operation]
    repo, method = target
    real = getattr(repo, method)

    def fail_after_write(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("injected completion write failure")

    monkeypatch.setattr(repo, method, fail_after_write)
    with pytest.raises(RuntimeError, match="injected completion"):
        complete_run(services, invocation, _evidence(invocation))
    assert _lanes(services, original) == (str(original.runID),) * 2
    assert get_run(services, owner_id=1, run_id=original.runID).state == "queued"
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_run_terminals").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ac_events WHERE event_type='run.finished'"
            ).fetchone()[0]
            == 0
        )


def test_cancellation_accepted_first_blocks_completion(accepted):
    services, original, invocation = accepted
    cancelled = cancel_run(
        services,
        owner_id=1,
        run_id=original.runID,
        command=CancelRunCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            expectedRevision=4,
        ),
    )
    assert cancelled.status == "cancelRequested"
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        complete_run(services, invocation, _evidence(invocation))
    view = get_run(services, owner_id=1, run_id=original.runID)
    assert view.cancelRequested and view.state == "queued"
    assert _lanes(services, original) == (str(original.runID),) * 2


@pytest.mark.parametrize("corruption", ["receipt", "event", "task_lane", "project_lane"])
def test_terminal_binding_corruption_is_not_a_successful_run_read(accepted, corruption):
    services, original, invocation = accepted
    complete_run(services, invocation, _evidence(invocation))
    with services.db.transaction() as conn:
        if corruption == "receipt":
            conn.execute("UPDATE ac_run_terminals SET receipt_digest='sha256:wrong'")
        elif corruption == "event":
            conn.execute(
                "UPDATE ac_events SET payload_json='{}' "
                "WHERE run_id=? AND event_type='run.finished'",
                (str(original.runID),),
            )
        elif corruption == "task_lane":
            conn.execute(
                "UPDATE ac_tasks SET active_run_id=? WHERE task_id=?",
                (str(original.runID), str(original.taskID)),
            )
        else:
            conn.execute(
                "UPDATE ac_projects SET active_run_id=? WHERE project_id=?",
                (str(original.runID), str(invocation.intent.projectID)),
            )
    with pytest.raises(StorageDataError, match="natural completion"):
        get_run(services, owner_id=1, run_id=original.runID)


def test_cancel_and_complete_transactions_have_one_winner(accepted):
    services, original, invocation = accepted
    evidence = _evidence(invocation)
    barrier = Barrier(2)

    def finish():
        barrier.wait(timeout=10)
        try:
            return complete_run(services, invocation, evidence)
        except CommandAdmissionError as exc:
            return exc.code

    def cancel():
        barrier.wait(timeout=10)
        try:
            return cancel_run(
                services,
                owner_id=1,
                run_id=original.runID,
                command=CancelRunCommand(
                    commandID=uuid4(),
                    idempotencyKey=str(uuid4()),
                    expectedRestoreEpoch=1,
                    expectedRevision=4,
                ),
            )
        except CommandAdmissionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        finished = executor.submit(finish)
        cancelled = executor.submit(cancel)
        outcomes = [finished.result(timeout=10), cancelled.result(timeout=10)]
    view = get_run(services, owner_id=1, run_id=original.runID)
    assert (view.state == "succeeded") != view.cancelRequested
    assert "revisionConflict" in outcomes
    assert _lanes(services, original) == (
        (None, None) if view.state == "succeeded" else (str(original.runID),) * 2
    )


def test_missing_v26_table_repairs_only_without_completed_run(tmp_path):
    pool = SqlitePool(tmp_path / "repaired.sqlite")
    try:
        run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute("DROP TABLE ac_run_terminals")
        run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 26
            assert conn.execute("SELECT COUNT(*) FROM ac_run_terminals").fetchone()[0] == 0
    finally:
        pool.close()


def test_missing_v26_table_never_reconstructs_lost_success_receipts(accepted):
    services, _, invocation = accepted
    complete_run(services, invocation, _evidence(invocation))
    with services.db.transaction() as conn:
        conn.execute("DROP TABLE ac_run_terminals")
    with pytest.raises(RuntimeError, match="Missing AgentComputer terminal receipts"):
        run_migrations(services.db)
