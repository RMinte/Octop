"""Interrupted resumes retain completed facts and renew only expired approvals."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_tool_calls import ProjectTaskToolCallRow
from octop.infra.db.repos.user_task_approval_policies import UserTaskApprovalPolicyRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_runtime, checkpoints, client_tools, interactions
from octop.infra.projects.client_tool_models import ClientToolResult
from octop.infra.projects.interaction_models import ApprovalPayload, InteractionResolution
from octop.infra.utils.paths import PathLayout

TOKEN = "original-claim-token-001"


@pytest.fixture
def services(tmp_path, monkeypatch):
    now = int(time.time() * 1000)
    monkeypatch.setattr(client_tools, "_now", lambda: now)
    db = SqlitePool(tmp_path / "resume-receipts.db")
    run_migrations(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,created_at,updated_at) "
            "VALUES('task_one',1,'Resume fixture','request_one','turn_one',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,configuration_json,"
            "model_ref,worker_id,checkpoint_id,pause_json,created_at,updated_at) "
            "VALUES('turn_one','task_one','request_one','Continue tests','running',?,"
            "'chosen/model','worker_one','checkpoint_one',?,?,?)",
            (
                json.dumps({"execution_options": {"client_id": "mac_one_001"}}),
                json.dumps({"checkpoint_id": "checkpoint_one", "calls": [], "groups": []}),
                now,
                now,
            ),
        )
    try:
        yield SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    finally:
        db.close()


def expired_approval(services):
    turn = services.project_task_turn_repo.get("turn_one")
    payload = ApprovalPayload(
        tool_name="execute",
        arguments={"command": "run focused tests"},
        reason="Test the existing changes",
        action_digest="original-proposal",
        allowed_decisions=["allow_once", "deny"],
    )
    old = interactions.new(turn, payload, "native_call_one")
    with services.db.transaction() as conn:
        approval_runtime.freeze_server(services, conn, 1, turn, old, [], client_tools._now())
    old.status = "expired"
    old.resolution = InteractionResolution(outcome="deny", source="timeout")
    old.resolved_at = datetime.now(UTC)
    old.expires_at = old.resolved_at - timedelta(seconds=1)
    wait = {
        "groups": [
            {
                "interrupt_id": "native_wait",
                "actions": [{"type": "approval", "id": old.interaction_id}],
            }
        ]
    }
    return turn, old, wait


def test_explicit_rearm_retains_timeout_record_and_exact_call_identity(services):
    turn, old, wait = expired_approval(services)
    original = old.model_dump(mode="json")
    items = [old]
    with services.db.transaction() as conn:
        assert approval_runtime.rearm_expired(
            services, conn, 1, turn, items, wait, client_tools._now()
        )
        assert not approval_runtime.rearm_expired(
            services, conn, 1, turn, items, wait, client_tools._now()
        )
    fresh = items[1]
    assert old.model_dump(mode="json") == original
    assert fresh.interaction_id != old.interaction_id
    assert fresh.supersedes_interaction_id == old.interaction_id
    assert fresh.tool_call_id == old.tool_call_id
    assert fresh.status == "pending" and fresh.resolution is None and not fresh.dispatch_started
    assert fresh.payload.arguments == old.payload.arguments
    assert fresh.payload.action_digest != old.payload.action_digest
    assert wait["groups"][0]["actions"][0]["id"] == fresh.interaction_id


@pytest.mark.parametrize("source", ["user", "auto_reviewer"])
def test_rearm_cannot_bypass_an_explicit_denial(services, source):
    turn, old, wait = expired_approval(services)
    denied = old.model_copy(deep=True)
    denied.interaction_id = "explicit_denial"
    denied.status = "resolved"
    denied.resolution = InteractionResolution(outcome="deny", source=source)
    items = [denied, old]
    with services.db.transaction() as conn:
        assert approval_runtime.rearm_expired(
            services, conn, 1, turn, items, wait, client_tools._now()
        )
    fresh = items[-1]
    assert fresh.resolution == InteractionResolution(outcome="deny", source="previous_denial")


def test_rearm_never_renews_an_already_dispatched_action(services):
    turn, old, wait = expired_approval(services)
    old.dispatch_started = True
    items = [old]
    with services.db.transaction() as conn:
        assert not approval_runtime.rearm_expired(
            services, conn, 1, turn, items, wait, client_tools._now()
        )
    assert items == [old] and wait["groups"][0]["actions"][0]["id"] == old.interaction_id


def test_normal_model_reproposal_does_not_renew_a_timeout_approval(services):
    turn, old, _ = expired_approval(services)
    fresh = interactions.new(turn, old.payload.model_copy(deep=True), "another_native_call")
    with services.db.transaction() as conn:
        approval_runtime.freeze_server(services, conn, 1, turn, fresh, [old], client_tools._now())
    assert fresh.resolution == InteractionResolution(outcome="deny", source="previous_denial")


@pytest.mark.parametrize("mandatory_human", [False, True])
def test_rearm_rechecks_the_current_policy_and_mandatory_human_rules(services, mandatory_human):
    turn, old, wait = expired_approval(services)
    with services.db.transaction() as conn:
        services.user_task_approval_policy_repo.save(
            conn, UserTaskApprovalPolicyRow(1, "full_access", 2, client_tools._now(), "[]")
        )
    if mandatory_human:
        services.settings_repo.set("task_approval_policy", '{"require_human_tools":["execute"]}')
    items = [old]
    with services.db.transaction() as conn:
        approval_runtime.rearm_expired(services, conn, 1, turn, items, wait, client_tools._now())
    fresh = items[-1]
    assert fresh.payload.approval_revision == 2
    assert fresh.payload.approval_mode == "full_access"
    assert fresh.status == ("pending" if mandatory_human else "resolved")
    assert (fresh.resolution is None) == mandatory_human


def client_call(services, *, claimed=True, expired=False, call_id="call_one"):
    now = client_tools._now()
    row = ProjectTaskToolCallRow(
        call_id,
        "task_one",
        "turn_one",
        1,
        "mac_one_001",
        "read_note",
        '{"path":"note.md"}',
        "pending",
        "",
        "",
        "",
        "",
        now,
        now,
        now - 1 if expired else now + 600_000,
    )
    with services.db.transaction() as conn:
        services.project_task_tool_call_repo.insert(conn, row)
        if claimed:
            services.project_task_tool_call_repo.claim(
                conn, row.call_id, "claim_request", TOKEN, now
            )
    return row


def get_call(services):
    return client_tools.get_call(services, 1, "mac_one_001", "task_one", "turn_one", "call_one")


def submit_result(services, *, token=TOKEN, output="original result", call_id="call_one"):
    return client_tools.submit_result(
        services,
        1,
        "mac_one_001",
        "task_one",
        "turn_one",
        call_id,
        ClientToolResult(claim_token=token, output=output),
    )


@pytest.mark.parametrize("checkpoint", ["", "checkpoint_one"])
def test_interrupted_claim_retains_receipt_authority_without_dispatch(services, checkpoint):
    client_call(services)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET wait_kind='interrupted',worker_id='',checkpoint_id=?",
            (checkpoint,),
        )
    call = get_call(services)
    assert call.status == "claimed" and not call.dispatch_allowed
    with pytest.raises(OctopError) as rejected:
        submit_result(services, token="a-different-claim-token")
    assert rejected.value.code == ErrorCode.FORBIDDEN
    assert submit_result(services).status == "completed"
    assert submit_result(services).status == "completed"
    with pytest.raises(OctopError) as conflict:
        submit_result(services, output="changed result")
    assert conflict.value.code == ErrorCode.TASK_CLIENT_TOOL_RESULT_CONFLICT


def test_interrupted_unclaimed_action_cannot_be_newly_claimed(services):
    client_call(services, claimed=False)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET wait_kind='interrupted',worker_id='',checkpoint_id=''"
        )
    assert not get_call(services).dispatch_allowed
    with pytest.raises(OctopError) as rejected:
        client_tools.claim_call(
            services, 1, "mac_one_001", "task_one", "turn_one", "call_one", "new_claim"
        )
    assert rejected.value.code == ErrorCode.TASK_CLIENT_TOOL_CLOSED


def test_expired_claim_can_only_reconcile_its_original_result(services):
    client_call(services, expired=True)
    call = get_call(services)
    assert call.status == "claimed" and not call.dispatch_allowed
    assert call.error_code == "TASK_CLIENT_TOOL_TIMEOUT"
    with pytest.raises(OctopError) as rejected:
        client_tools.claim_call(
            services, 1, "mac_one_001", "task_one", "turn_one", "call_one", "another_claim"
        )
    assert rejected.value.code == ErrorCode.TASK_CLIENT_TOOL_CLAIMED
    completed = submit_result(services)
    assert completed.status == "completed" and completed.error_code == ""


def test_expired_unclaimed_action_stays_closed(services):
    client_call(services, claimed=False, expired=True)
    call = get_call(services)
    assert call.status == "expired" and not call.dispatch_allowed


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_terminal_turn_rejects_a_new_result_from_an_old_claim(services, status):
    client_call(services)
    with services.db.transaction() as conn:
        conn.execute("UPDATE project_task_turns SET status=?", (status,))
    with pytest.raises(OctopError) as rejected:
        submit_result(services)
    assert rejected.value.code == ErrorCode.TASK_CLIENT_TOOL_CLOSED


def test_stale_turn_fences_dispatch_without_destroying_a_claim(services):
    client_call(services)
    with services.db.transaction() as conn:
        conn.execute("UPDATE project_task_turns SET updated_at=?", (client_tools._now() - 91_000,))
    call = get_call(services)
    assert call.status == "claimed" and not call.dispatch_allowed
    assert submit_result(services).status == "completed"


def test_checkpoint_reports_an_expired_claim_for_recovery(services):
    client_call(services, expired=True)
    turn = services.project_task_turn_repo.get("turn_one")
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET pause_json=?",
            (
                json.dumps(
                    {"checkpoint_id": turn.checkpoint_id, "calls": ["call_one"], "groups": []}
                ),
            ),
        )
    with pytest.raises(OctopError) as interrupted:
        checkpoints.resume_decisions(services, 1, turn)
    assert interrupted.value.code == ErrorCode.TASK_CLIENT_TOOL_TIMEOUT
    assert get_call(services).status == "claimed"


def test_older_turn_payloads_default_to_no_recovery(services):
    from octop.infra.projects.tasks import TaskTurn, turn_view

    payload = turn_view(services.project_task_turn_repo.get("turn_one")).model_dump(
        exclude={"recovery"}
    )
    assert TaskTurn.model_validate(payload).recovery is None


def interrupt_client_wait(services, calls, *, mode="wait"):
    recovery = {
        "mode": mode,
        "can_resume": False,
        "needs_reconciliation": True,
        "phase": "client_tool",
        "reason_code": "TASK_CLIENT_TOOL_TIMEOUT",
        "cause_type": "OctopError",
        "diagnostic_id": "diag_original",
    }
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET wait_kind='interrupted',worker_id='',pause_json=?,recovery_json=?",
            (json.dumps({"calls": calls, "groups": []}), json.dumps(recovery)),
        )
    return recovery


def test_last_original_result_makes_interrupted_wait_resumable_without_starting_it(services):
    client_call(services, expired=True)
    client_call(services, expired=True, call_id="call_two")
    original = interrupt_client_wait(services, ["call_one", "call_two"])
    submit_result(services)
    turn = services.project_task_turn_repo.get("turn_one")
    assert json.loads(turn.recovery_json) == original
    submit_result(services, call_id="call_two")
    turn = services.project_task_turn_repo.get("turn_one")
    assert json.loads(turn.recovery_json) == {
        **original,
        "can_resume": True,
        "needs_reconciliation": False,
    }
    assert turn.wait_kind == "interrupted" and turn.worker_id == ""


@pytest.mark.parametrize("blocker", ["server_unknown", "unrelated_call", "graph_mode"])
def test_client_result_does_not_clear_other_unknown_execution(services, blocker):
    client_call(services, expired=True)
    original = interrupt_client_wait(
        services,
        ["another_call"] if blocker == "unrelated_call" else ["call_one"],
        mode="graph" if blocker == "graph_mode" else "wait",
    )
    if blocker == "server_unknown":
        with services.db.transaction() as conn:
            services.project_task_execution_receipt_repo.start(
                conn,
                "receipt_unknown",
                "task_one",
                "turn_one",
                "server_call",
                "execute",
                "fixed-arguments",
                "previous_worker",
                client_tools._now(),
            )
            services.project_task_execution_receipt_repo.unknown(
                conn,
                "turn_one",
                "server_call",
                "previous_worker",
                client_tools._now(),
            )
    assert submit_result(services).status == "completed"
    turn = services.project_task_turn_repo.get("turn_one")
    assert json.loads(turn.recovery_json) == original


def test_completed_result_replay_does_not_rewrite_later_recovery_state(services):
    client_call(services)
    submit_result(services)
    original = interrupt_client_wait(services, ["call_one"])
    assert submit_result(services).status == "completed"
    turn = services.project_task_turn_repo.get("turn_one")
    assert json.loads(turn.recovery_json) == original
