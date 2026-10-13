"""Lost pause facts and daemon restart must never grant a stale decision."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tests.unit.agentcomputer.test_effect_runner import effect as effect
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.config import OctopConfig
from octop.infra.agentcomputer import (
    approvals,
    cancellations,
    dispatch,
    effects,
    pauses,
    questions,
    recovery,
)
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    canonical_json,
    content_digest,
    get_command_receipt,
)
from octop.infra.agentcomputer.identity import read_identity
from octop.infra.agentcomputer.models import (
    ApprovalDecisionCommand,
    QuestionAnswerCommand,
    RunPauseContent,
)
from octop.infra.agentcomputer.runs import get_run
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services


def _open(effect, monkeypatch, kind):
    monkeypatch.setattr(pauses, "get_capabilities", dispatch.get_capabilities)
    run_id = effect.invocation.intent.runID
    with effect.services.db.transaction() as conn:
        conn.execute("UPDATE ac_runs SET state='running' WHERE run_id=?", (str(run_id),))
    current = get_run(effect.services, owner_id=1, run_id=run_id)
    assert current is not None
    invocation = effect.invocation.model_copy(update={"expectedRunRevision": current.revision})
    payload = {"action": "fixture"} if kind == "approval" else {"question": "fixture"}
    content = RunPauseContent(
        schemaVersion=1,
        payloadJSON=canonical_json(payload),
        payloadDigest=content_digest(payload),
        workspaceDigest=effect.action.workspaceDigest,
        policyVersion=effect.action.policyVersion,
        capabilityVersion=effect.action.capabilityVersion,
    )
    expiry = datetime.now(UTC) + timedelta(hours=1)
    record = (
        approvals.request_approval(effect.services, invocation, action=content, expires_at=expiry)
        if kind == "approval"
        else questions.ask_question(
            effect.services, invocation, question=content, expires_at=expiry
        )
    )
    return record, content


def _command(record, kind):
    common = {
        "commandID": uuid4(),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": record.intent.restoreEpoch,
        "runID": record.intent.runID,
        "expectedRevision": 1,
    }
    if kind == "approval":
        return ApprovalDecisionCommand(
            **common, approvalID=record.intent.pauseID, decision="approveOnce"
        )
    return QuestionAnswerCommand(**common, questionID=record.intent.pauseID, answer="yes")


def _decide(effect, content, kind, command, *, authorize):
    if kind == "approval":
        return approvals.decide_approval(
            effect.services,
            owner_id=1,
            command=command,
            current_action=content,
            authorize=authorize,
        )
    return questions.answer_question(
        effect.services,
        owner_id=1,
        command=command,
        current_question=content,
        authorize=authorize,
    )


def _facts(effect):
    with effect.services.db.connect() as conn:
        return {
            name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY id")]
            for name in ("ac_run_pauses", "ac_runs", "ac_events", "ac_commands")
        }


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("damage", ["pauseRow", "requestEvent"])
async def test_lost_pause_fact_rejects_run_and_decision_without_rebuilding(
    effect, monkeypatch, kind, damage
):
    record, content = _open(effect, monkeypatch, kind)
    command = _command(record, kind)
    with effect.services.db.transaction() as conn:
        if damage == "pauseRow":
            conn.execute(
                "DELETE FROM ac_run_pauses WHERE pause_id=?", (str(record.intent.pauseID),)
            )
        else:
            conn.execute(
                "DELETE FROM ac_events WHERE run_id=? AND event_type=?",
                (
                    str(record.intent.runID),
                    "approval.requested" if kind == "approval" else "question.asked",
                ),
            )
    before = _facts(effect)
    with pytest.raises(StorageDataError):
        get_run(effect.services, owner_id=1, run_id=record.intent.runID)
    with pytest.raises(StorageDataError):
        _decide(effect, content, kind, command, authorize=lambda _: True)
    assert _facts(effect) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("phase", ["pending", "decided"])
async def test_restart_expires_pause_and_rejects_new_decision(effect, monkeypatch, kind, phase):
    record, content = _open(effect, monkeypatch, kind)
    command = _command(record, kind)
    original = None
    if phase == "decided":
        original = _decide(effect, content, kind, command, authorize=lambda _: True)
        assert original.state == "adjudicatedOnly"

    report = recovery.recover_dispatches(effect.services)
    assert report.scanned == report.tightened == 1
    stored = pauses.get_pause(effect.services, owner_id=1, pause_id=record.intent.pauseID)
    assert stored is not None and stored.phase == "expired"
    assert stored.receipt == original
    current = get_run(effect.services, owner_id=1, run_id=record.intent.runID)
    assert current is not None and current.state == "interrupted"
    assert current.reconciliationRequired
    effect.assert_lane(reconciliation=True)

    late = _command(record, kind)
    with pytest.raises(CommandAdmissionError):
        _decide(effect, content, kind, late, authorize=lambda _: True)
    before = _facts(effect)
    assert recovery.recover_dispatches(effect.services).scanned == 0
    assert _facts(effect) == before


async def test_cancel_request_blocks_new_model_and_tool_execution(effect):
    run_id = effect.invocation.intent.runID
    current = get_run(effect.services, owner_id=1, run_id=run_id)
    assert current is not None and current.dispatchPhase == "engineAccepted"
    receipt = cancellations.cancel_run(
        effect.services,
        owner_id=1,
        run_id=run_id,
        command=cancellations.CancelRunCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=current.restoreEpoch,
            expectedRevision=current.revision,
        ),
    )
    assert receipt.status == "cancelRequested"
    with pytest.raises(CommandAdmissionError):
        dispatch.validate_engine_execution(effect.services, effect.invocation)
    with pytest.raises(CommandAdmissionError):
        effects.prepare_effect(effect.services, effect.invocation, effect.action)
    with effect.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_old_epoch_decision_replay_is_rejected_but_history_remains_readable(
    effect, monkeypatch, kind
):
    record, content = _open(effect, monkeypatch, kind)
    command = _command(record, kind)
    original = _decide(effect, content, kind, command, authorize=lambda _: True)
    identity_path = effect.services.paths.agentcomputer_identity
    identity = read_identity(identity_path)
    identity_path.write_text(
        identity.model_copy(update={"restoreEpoch": identity.restoreEpoch + 1}).model_dump_json(),
        encoding="utf-8",
    )
    before = _facts(effect)
    historical = pauses.get_pause(effect.services, owner_id=1, pause_id=record.intent.pauseID)
    assert historical is not None and historical.receipt == original
    with pytest.raises(CommandAdmissionError, match="restoreEpochConflict"):
        _decide(effect, content, kind, command, authorize=lambda _: True)
    assert _facts(effect) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_decision_command_receipt_is_immutable_after_expiry_and_restart(
    effect, monkeypatch, kind
):
    record, content = _open(effect, monkeypatch, kind)
    command = _command(record, kind)
    original = _decide(effect, content, kind, command, authorize=lambda _: True)
    assert get_command_receipt(effect.services, 1, command.commandID) == original
    assert get_command_receipt(effect.services, 2, command.commandID) is None
    assert get_command_receipt(effect.services, 1, uuid4()) is None

    assert pauses.expire_pause(effect.services, owner_id=1, pause_id=record.intent.pauseID)
    identity_path = effect.services.paths.agentcomputer_identity
    identity = read_identity(identity_path)
    identity_path.write_text(
        identity.model_copy(update={"restoreEpoch": identity.restoreEpoch + 1}).model_dump_json(),
        encoding="utf-8",
    )
    before = _facts(effect)
    other_pool = SqlitePool(effect.services.paths.db)
    other = build_shared_services(db=other_pool, paths=effect.services.paths, config=OctopConfig())
    try:
        assert get_command_receipt(other, 1, command.commandID) == original
        assert get_command_receipt(other, 1, command.commandID) == original
    finally:
        other_pool.close()
    assert _facts(effect) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("damage", ["receipt", "decisionEvent"])
async def test_corrupt_decision_receipt_query_fails_closed_without_writes(
    effect, monkeypatch, kind, damage
):
    record, content = _open(effect, monkeypatch, kind)
    command = _command(record, kind)
    _decide(effect, content, kind, command, authorize=lambda _: True)
    with effect.services.db.transaction() as conn:
        if damage == "receipt":
            conn.execute(
                "UPDATE ac_run_pauses SET decision_receipt_json=? WHERE pause_id=?",
                ('{"private-marker":', str(record.intent.pauseID)),
            )
        else:
            conn.execute(
                "DELETE FROM ac_events WHERE run_id=? AND event_type=?",
                (
                    str(record.intent.runID),
                    "approval.decided" if kind == "approval" else "question.answered",
                ),
            )
    before = _facts(effect)
    with pytest.raises(StorageDataError):
        get_command_receipt(effect.services, 1, command.commandID)
    assert _facts(effect) == before


async def test_command_and_decision_id_collision_fails_closed_without_writes(effect, monkeypatch):
    record, content = _open(effect, monkeypatch, "approval")
    command = _command(record, "approval")
    _decide(effect, content, "approval", command, authorize=lambda _: True)
    with effect.services.db.transaction() as conn:
        existing = conn.execute(
            "SELECT command_id FROM ac_commands WHERE owner_id=1 ORDER BY id LIMIT 1"
        ).fetchone()
        assert existing is not None
        conn.execute(
            "UPDATE ac_run_pauses SET decision_command_id=? WHERE pause_id=?",
            (existing[0], str(record.intent.pauseID)),
        )
    before = _facts(effect)
    with pytest.raises(StorageDataError, match="collision"):
        get_command_receipt(effect.services, 1, UUID(existing[0]))
    assert _facts(effect) == before
