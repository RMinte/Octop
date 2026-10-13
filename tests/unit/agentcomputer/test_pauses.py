"""Run-bound private approval/question facts without a production resume hook."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_effect_runner import effect as effect
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.config import OctopConfig
from octop.infra.agentcomputer import approvals, dispatch, pauses, questions
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.models import (
    ApprovalDecisionCommand,
    QuestionAnswerCommand,
    RunPauseContent,
)
from octop.infra.agentcomputer.runs import get_run
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services


def ready(effect, monkeypatch):
    monkeypatch.setattr(pauses, "get_capabilities", dispatch.get_capabilities)
    with effect.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_runs SET state='running' WHERE run_id=?",
            (str(effect.invocation.intent.runID),),
        )
        run = effect.services.ac_run_repo.get_in_transaction(
            conn, str(effect.invocation.intent.runID), 1
        )
        assert run is not None
        effect.invocation = effect.invocation.model_copy(
            update={"expectedRunRevision": run.revision}
        )
    payload = {"toolCallID": "fixture-call-1", "action": "ask before effect"}
    return RunPauseContent(
        schemaVersion=1,
        payloadJSON=canonical_json(payload),
        payloadDigest=content_digest(payload),
        workspaceDigest=effect.action.workspaceDigest,
        policyVersion=effect.action.policyVersion,
        capabilityVersion=effect.action.capabilityVersion,
    )


def expiry():
    return datetime.now(UTC) + timedelta(minutes=10)


def approval_command(record, *, decision="approveOnce", command_id=None, key=None):
    return ApprovalDecisionCommand(
        commandID=command_id or uuid4(),
        idempotencyKey=key or str(uuid4()),
        expectedRestoreEpoch=record.intent.restoreEpoch,
        approvalID=record.intent.pauseID,
        runID=record.intent.runID,
        expectedRevision=record.revision,
        decision=decision,
    )


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_pause_status_by_id_is_owned_typed_and_redacted(effect, monkeypatch, kind):
    content = ready(effect, monkeypatch)
    record = (
        approvals.request_approval(
            effect.services, effect.invocation, action=content, expires_at=expiry()
        )
        if kind == "approval"
        else questions.ask_question(
            effect.services, effect.invocation, question=content, expires_at=expiry()
        )
    )
    found, run_status = pauses.get_run_pause_status(
        effect.services, owner_id=1, run_id=record.intent.runID
    )
    status = pauses.get_pause_status(
        effect.services, owner_id=1, pause_id=record.intent.pauseID, kind=kind
    )
    assert found and status == run_status
    assert status is not None and "payloadJSON" not in status.model_dump_json()

    def identity_must_not_be_read(_services):
        pytest.fail("missing, foreign, and wrong-kind pauses must not read identity")

    monkeypatch.setattr(pauses, "_existing_recovery_identity", identity_must_not_be_read)
    for owner_id, pause_id, requested_kind in (
        (2, record.intent.pauseID, kind),
        (1, uuid4(), kind),
        (1, record.intent.pauseID, "question" if kind == "approval" else "approval"),
    ):
        assert (
            pauses.get_pause_status(
                effect.services, owner_id=owner_id, pause_id=pause_id, kind=requested_kind
            )
            is None
        )


async def test_approval_wait_event_decision_replay_and_restart_fence(effect, monkeypatch):
    action = ready(effect, monkeypatch)
    record = approvals.request_approval(
        effect.services, effect.invocation, action=action, expires_at=expiry()
    )
    assert record.phase == "pending" and record.revision == 1
    assert (
        get_run(effect.services, owner_id=1, run_id=record.intent.runID).state == "waitingApproval"
    )
    before = [tuple(row) for row in effect.events()]
    calls = []

    def authorize(intent):
        calls.append(intent)
        return True

    command = approval_command(record)
    receipt = approvals.decide_approval(
        effect.services, owner_id=1, command=command, current_action=action, authorize=authorize
    )
    assert receipt.state == "adjudicatedOnly" and receipt.pauseRevision == 2
    assert len(calls) == 1
    assert (
        approvals.decide_approval(
            effect.services,
            owner_id=1,
            command=command,
            current_action=action,
            authorize=lambda _: pytest.fail("replay called authorization"),
        )
        == receipt
    )
    assert len(calls) == 1
    assert (
        get_run(effect.services, owner_id=1, run_id=record.intent.runID).state == "waitingApproval"
    )
    assert len(effect.events()) == len(before) + 1
    assert pauses.expire_pause(effect.services, owner_id=1, pause_id=record.intent.pauseID)
    assert not pauses.expire_pause(effect.services, owner_id=1, pause_id=record.intent.pauseID)
    historical = pauses.get_pause(effect.services, owner_id=1, pause_id=record.intent.pauseID)
    assert (
        historical is not None and historical.phase == "expired" and historical.receipt == receipt
    )
    view = get_run(effect.services, owner_id=1, run_id=record.intent.runID)
    assert view.state == "interrupted" and view.reconciliationRequired
    task = effect.services.ac_task_repo.get(str(record.intent.taskID), 1)
    project = effect.services.ac_project_repo.get(str(record.intent.projectID), 1)
    assert task.active_run_id == project.active_run_id == str(record.intent.runID)


async def test_question_answer_and_cross_owner_are_scoped_to_exact_run(effect, monkeypatch):
    content = ready(effect, monkeypatch)
    record = questions.ask_question(
        effect.services, effect.invocation, question=content, expires_at=expiry()
    )
    assert get_run(effect.services, owner_id=1, run_id=record.intent.runID).state == "waitingInput"
    assert pauses.get_pause(effect.services, owner_id=2, pause_id=record.intent.pauseID) is None
    command = QuestionAnswerCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=record.intent.restoreEpoch,
        questionID=record.intent.pauseID,
        runID=record.intent.runID,
        expectedRevision=record.revision,
        answer="继续",
    )
    receipt = questions.answer_question(
        effect.services,
        owner_id=1,
        command=command,
        current_question=content,
        authorize=lambda _: True,
    )
    assert receipt.kind == "question" and receipt.state == "adjudicatedOnly"
    with pytest.raises(CommandAdmissionError, match="resourceNotFound"):
        questions.answer_question(
            effect.services,
            owner_id=2,
            command=command,
            current_question=content,
            authorize=lambda _: True,
        )


async def test_changed_action_or_revoked_auth_does_not_adjudicate(effect, monkeypatch):
    content = ready(effect, monkeypatch)
    record = approvals.request_approval(
        effect.services, effect.invocation, action=content, expires_at=expiry()
    )
    command = approval_command(record)
    changed = content.model_copy(update={"workspaceDigest": content_digest({"changed": True})})
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        approvals.decide_approval(
            effect.services,
            owner_id=1,
            command=command,
            current_action=changed,
            authorize=lambda _: True,
        )
    with pytest.raises(CommandAdmissionError, match="accessDenied"):
        approvals.decide_approval(
            effect.services,
            owner_id=1,
            command=command,
            current_action=content,
            authorize=lambda _: False,
        )
    assert (
        pauses.get_pause(effect.services, owner_id=1, pause_id=record.intent.pauseID).phase
        == "pending"
    )
    approvals.decide_approval(
        effect.services,
        owner_id=1,
        command=command,
        current_action=content,
        authorize=lambda _: True,
    )
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        approvals.decide_approval(
            effect.services,
            owner_id=1,
            command=approval_command(record, decision="deny", command_id=command.commandID),
            current_action=content,
            authorize=lambda _: True,
        )


async def test_missing_pause_event_or_row_is_never_recreated(effect, monkeypatch):
    content = ready(effect, monkeypatch)
    record = approvals.request_approval(
        effect.services, effect.invocation, action=content, expires_at=expiry()
    )
    with effect.services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_run_pauses WHERE pause_id=?", (str(record.intent.pauseID),))
    with pytest.raises(StorageDataError):
        get_run(effect.services, owner_id=1, run_id=record.intent.runID)
    with pytest.raises(StorageDataError):
        approvals.request_approval(
            effect.services, effect.invocation, action=content, expires_at=expiry()
        )


async def test_same_key_and_business_decision_replays_original_across_connections(
    effect, monkeypatch
):
    content = ready(effect, monkeypatch)
    record = approvals.request_approval(
        effect.services, effect.invocation, action=content, expires_at=expiry()
    )
    first = approval_command(record)
    receipt = approvals.decide_approval(
        effect.services,
        owner_id=1,
        command=first,
        current_action=content,
        authorize=lambda _: True,
    )
    before = [tuple(row) for row in effect.events()]
    other_pool = SqlitePool(effect.services.paths.db)
    other = build_shared_services(db=other_pool, paths=effect.services.paths, config=OctopConfig())
    try:
        retry = approval_command(record, command_id=uuid4(), key=first.idempotencyKey)
        assert (
            approvals.decide_approval(
                other,
                owner_id=1,
                command=retry,
                current_action=content,
                authorize=lambda _: pytest.fail("retry called authorization"),
            )
            == receipt
        )
        assert [tuple(row) for row in effect.events()] == before
        with pytest.raises(CommandAdmissionError, match="revisionConflict"):
            approvals.decide_approval(
                other,
                owner_id=1,
                command=approval_command(record, decision="deny", key=first.idempotencyKey),
                current_action=content,
                authorize=lambda _: True,
            )
        with pytest.raises(CommandAdmissionError, match="revisionConflict"):
            approvals.decide_approval(
                other,
                owner_id=1,
                command=approval_command(record, key=str(uuid4())),
                current_action=content,
                authorize=lambda _: True,
            )
        with other.db.connect() as conn:
            old_command = conn.execute("SELECT command_id FROM ac_commands LIMIT 1").fetchone()[0]
        with pytest.raises(CommandAdmissionError, match="revisionConflict"):
            approvals.decide_approval(
                other,
                owner_id=1,
                command=approval_command(record, command_id=old_command, key=first.idempotencyKey),
                current_action=content,
                authorize=lambda _: True,
            )
    finally:
        other_pool.close()


@pytest.mark.parametrize("stage", ["request", "decision"])
async def test_pause_event_insert_failure_rolls_back_all_facts(effect, monkeypatch, stage):
    content = ready(effect, monkeypatch)
    record = None
    if stage == "decision":
        record = approvals.request_approval(
            effect.services, effect.invocation, action=content, expires_at=expiry()
        )
    before_events = [tuple(row) for row in effect.events()]
    before_run = get_run(effect.services, owner_id=1, run_id=effect.invocation.intent.runID)
    insert = effect.services.ac_event_repo.insert

    def fail(*args, **kwargs):
        insert(*args, **kwargs)
        raise RuntimeError("fixture pause event unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(effect.services.ac_event_repo, "insert", fail)
        with pytest.raises(RuntimeError, match="pause event unavailable"):
            if record is None:
                approvals.request_approval(
                    effect.services, effect.invocation, action=content, expires_at=expiry()
                )
            else:
                approvals.decide_approval(
                    effect.services,
                    owner_id=1,
                    command=approval_command(record),
                    current_action=content,
                    authorize=lambda _: True,
                )
    assert [tuple(row) for row in effect.events()] == before_events
    assert get_run(effect.services, owner_id=1, run_id=effect.invocation.intent.runID) == before_run
    with effect.services.db.connect() as conn:
        rows = conn.execute("SELECT phase FROM ac_run_pauses").fetchall()
    assert [row[0] for row in rows] == ([] if record is None else ["pending"])


@pytest.mark.parametrize("damage", ["pretty", "duplicate"])
async def test_noncanonical_stored_pause_intent_is_rejected(effect, monkeypatch, damage):
    content = ready(effect, monkeypatch)
    record = approvals.request_approval(
        effect.services, effect.invocation, action=content, expires_at=expiry()
    )
    with effect.services.db.transaction() as conn:
        original = conn.execute(
            "SELECT intent_json FROM ac_run_pauses WHERE pause_id=?",
            (str(record.intent.pauseID),),
        ).fetchone()[0]
        bad_json = (
            json.dumps(json.loads(original), indent=2, ensure_ascii=False)
            if damage == "pretty"
            else '{"schemaVersion":1,' + original[1:]
        )
        conn.execute(
            "UPDATE ac_run_pauses SET intent_json=? WHERE pause_id=?",
            (bad_json, str(record.intent.pauseID)),
        )
    with pytest.raises(StorageDataError):
        get_run(effect.services, owner_id=1, run_id=record.intent.runID)
