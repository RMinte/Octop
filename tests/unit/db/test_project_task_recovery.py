"""Recover interrupted turns without losing identity or replaying known effects."""

import json

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_execution_receipts import ProjectTaskExecutionReceiptRepo
from octop.infra.db.repos.project_task_tool_calls import ProjectTaskToolCallRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRepo, ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRepo
from octop.infra.db.services import RepoBundle


@pytest.fixture
def recovery_db(tmp_path):
    db = SqlitePool(tmp_path / "recovery.db")
    run_migrations(db)
    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) "
            "VALUES('recovery-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,"
            "created_at,updated_at) VALUES('recovery-task',?,'Keep task','task-request',"
            "'recovery-turn',100,100)",
            (owner,),
        )
        ProjectTaskTurnRepo.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id="recovery-turn",
                task_id="recovery-task",
                request_id="turn-request",
                prompt="Keep prompt",
                reply="Keep completed work",
                status="queued",
                error_code="",
                activity="",
                configuration_json="{}",
                model_ref="fixture/model",
                worker_id="",
                cancel_requested=False,
                created_at=100,
                updated_at=100,
            ),
        )
    try:
        yield db, RepoBundle.from_pool(db), owner
    finally:
        db.close()


def start_receipt(conn, repo, *, call_id="tool-one", receipt_id="receipt-one"):
    return repo.start(
        conn,
        receipt_id,
        "recovery-task",
        "recovery-turn",
        call_id,
        "execute",
        "arguments-digest",
        "worker-one",
        1_000,
    )


def test_completed_receipt_is_immutable_and_deduplicates_dispatch(recovery_db):
    db, repos, _owner = recovery_db
    repo = repos.project_task_execution_receipt_repo
    with db.transaction() as conn:
        assert start_receipt(conn, repo)
        before = repo.get(conn, "recovery-turn", "tool-one")
        assert before.state == "dispatched" and before.pk > 0
        assert not start_receipt(conn, repo, receipt_id="another-receipt")
        assert repo.get(conn, "recovery-turn", "tool-one") == before
        assert not repo.complete(conn, "recovery-turn", "tool-one", "stale-worker", "wrong", 2_000)
        assert not repo.unknown(conn, "recovery-turn", "tool-one", "stale-worker", 2_000)
        assert repo.complete(conn, "recovery-turn", "tool-one", "worker-one", '{"output":0}', 3_000)
        completed = repo.get(conn, "recovery-turn", "tool-one")
        assert completed.state == "completed" and completed.result_json == '{"output":0}'
        assert completed.created_at == 1_000 and completed.updated_at == 3_000
        assert not repo.complete(
            conn, "recovery-turn", "tool-one", "worker-one", "overwrite", 4_000
        )
        assert not repo.unknown(conn, "recovery-turn", "tool-one", "worker-one", 4_000)
        assert repo.get(conn, "recovery-turn", "tool-one") == completed
        assert repo.unfinished(conn, "recovery-turn") == []


def test_unknown_receipt_cannot_be_replayed_or_overwritten_by_late_result(recovery_db):
    db, repos, _owner = recovery_db
    repo = repos.project_task_execution_receipt_repo
    with db.transaction() as conn:
        assert start_receipt(conn, repo)
        assert start_receipt(conn, repo, call_id="tool-two", receipt_id="receipt-two")
        assert repo.unknown(conn, "recovery-turn", "tool-one", "worker-one", 2_000)
        assert not repo.complete(conn, "recovery-turn", "tool-one", "worker-one", "late", 3_000)
        assert not start_receipt(conn, repo)
        assert [row.state for row in repo.unfinished(conn, "recovery-turn")] == [
            "unknown",
            "dispatched",
        ]
        assert repo.get(conn, "other-turn", "tool-one") is None


@pytest.mark.parametrize("checkpoint", ["", "checkpoint-one"])
def test_recovery_preserves_turn_and_requires_fresh_worker_lease(recovery_db, checkpoint):
    db, repos, _owner = recovery_db
    repo = repos.project_task_turn_repo
    original = repo.claim("worker-one", 1_000)
    recovery = json.dumps({"stage": "model", "can_resume": True})
    with db.transaction() as conn:
        assert not repo.park_recovery(
            conn, "recovery-turn", "stale-worker", checkpoint, recovery, 2_000
        )
        assert repo.park_recovery(conn, "recovery-turn", "worker-one", checkpoint, recovery, 2_000)
        assert not repo.park_recovery(conn, "recovery-turn", "", checkpoint, recovery, 2_000)
        assert not repo.resume_wait(conn, "recovery-turn", 2_500)
    parked = repo.get("recovery-turn")
    assert parked.is_waiting and parked.wait_kind == "interrupted" and not parked.worker_id
    assert parked.recovery_json == recovery and parked.checkpoint_id == checkpoint
    assert parked.started_at == original.started_at == 1_000 and parked.finished_at is None
    assert (parked.turn_id, parked.request_id, parked.prompt, parked.reply) == (
        original.turn_id,
        original.request_id,
        original.prompt,
        original.reply,
    )
    assert repo.waiting() == [] and repo.stale(99_000) == []
    with db.transaction() as conn:
        assert not repo.interrupt(conn, "recovery-turn", 99_000, 99_001)
        assert repo.resume_recovery(conn, "recovery-turn", 3_000)
        assert not repo.resume_recovery(conn, "recovery-turn", 3_000)
    resumed = repo.claim("worker-two", 4_000)
    assert resumed.turn_id == original.turn_id and resumed.worker_id == "worker-two"
    assert resumed.recovery_json == recovery and resumed.started_at == 1_000
    assert repo.progress("recovery-turn", "worker-one", "stale output", "", 4_500)
    with db.transaction() as conn:
        assert not repo.park_recovery(
            conn, "recovery-turn", "worker-one", checkpoint, recovery, 5_000
        )
        assert not repo.finish(conn, "recovery-turn", "worker-one", "failed", "wrong", "", 5_000)


def test_cancel_recovery_without_checkpoint_is_immediate_and_cannot_resume(recovery_db):
    db, repos, _owner = recovery_db
    repo = repos.project_task_turn_repo
    repo.claim("worker-one", 1_000)
    with db.transaction() as conn:
        assert repo.park_recovery(conn, "recovery-turn", "worker-one", "", "{}", 2_000)
        repo.cancel(conn, "recovery-turn", 3_000)
        assert not repo.resume_recovery(conn, "recovery-turn", 4_000)
    row = repo.get("recovery-turn")
    assert row.status == "cancelled" and row.finished_at == 3_000 and row.cancel_requested


def test_checkpoint_updates_are_fenced_and_keep_live_turn_state(recovery_db):
    db, repos, _owner = recovery_db
    repo = repos.project_task_turn_repo
    repo.claim("worker-one", 1_000)
    before = repo.get("recovery-turn")
    with db.transaction() as conn:
        assert not repo.save_checkpoint(conn, "recovery-turn", "stale-worker", "wrong", 2_000)
        assert repo.save_checkpoint(conn, "recovery-turn", "worker-one", "checkpoint-one", 3_000)
    saved = repo.get("recovery-turn")
    assert saved.checkpoint_id == "checkpoint-one" and saved.updated_at == 3_000
    assert saved.status == before.status and saved.wait_kind == before.wait_kind
    assert saved.worker_id == before.worker_id and saved.reply == before.reply
    with db.transaction() as conn:
        repo.cancel(conn, "recovery-turn", 4_000)
        assert not repo.save_checkpoint(conn, "recovery-turn", "worker-one", "after-cancel", 5_000)


def test_wait_interruption_requires_exact_checkpoint_and_preserves_wait_output(recovery_db):
    db, repos, _owner = recovery_db
    repo = repos.project_task_turn_repo
    repo.claim("worker-one", 1_000)
    recovery = '{"stage":"client_tool","retryable":true}'
    with db.transaction() as conn:
        assert not repo.interrupt_wait(conn, "recovery-turn", "", recovery, 2_000)
        assert repo.pause(
            conn,
            "recovery-turn",
            "worker-one",
            "checkpoint-one",
            '{"calls":[]}',
            "Keep output",
            "Keep reasoning",
            "[]",
            0,
            3_000,
        )
        conn.execute("UPDATE project_task_turns SET activity='read_note'")
        assert not repo.interrupt_wait(conn, "recovery-turn", "old-checkpoint", recovery, 4_000)
        assert repo.interrupt_wait(conn, "recovery-turn", "checkpoint-one", recovery, 5_000)
        assert not repo.interrupt_wait(conn, "recovery-turn", "checkpoint-one", "wrong", 6_000)
    row = repo.get("recovery-turn")
    assert row.wait_kind == "interrupted" and row.recovery_json == recovery
    assert row.checkpoint_id == "checkpoint-one" and row.activity == "read_note"
    assert row.reply == "Keep output" and row.reasoning == "Keep reasoning"
    assert row.finished_at is None and row.updated_at == 5_000


def test_recovery_evidence_can_update_only_a_parked_interruption(recovery_db):
    db, repos, _owner = recovery_db
    repo = repos.project_task_turn_repo
    repo.claim("worker-one", 1_000)
    original = '{"can_resume":false,"needs_reconciliation":true}'
    reconciled = '{"can_resume":true,"needs_reconciliation":false}'
    initial_revision = repo.get("recovery-turn").interaction_revision
    with db.transaction() as conn:
        assert not repo.update_recovery(conn, "recovery-turn", reconciled, 2_000)
        assert repo.park_recovery(
            conn, "recovery-turn", "worker-one", "checkpoint", original, 3_000
        )
        assert repo.update_recovery(conn, "recovery-turn", reconciled, 4_000)
    row = repo.get("recovery-turn")
    assert row.recovery_json == reconciled and row.updated_at == 4_000
    assert row.interaction_revision == initial_revision + 2
    assert row.wait_kind == "interrupted" and row.checkpoint_id == "checkpoint"
    with db.transaction() as conn:
        repo.cancel(conn, "recovery-turn", 5_000)
        assert not repo.update_recovery(conn, "recovery-turn", original, 6_000)


def test_stale_recovery_cas_rejects_a_worker_that_refreshed_its_heartbeat(recovery_db):
    db, repos, _owner = recovery_db
    repo = repos.project_task_turn_repo
    repo.claim("worker-one", 1_000)
    assert not repo.progress("recovery-turn", "worker-one", "Fresh output", "execute", 3_000)
    with db.transaction() as conn:
        assert not repo.park_recovery(
            conn, "recovery-turn", "worker-one", "checkpoint", "{}", 4_000, before=2_000
        )
    fresh = repo.get("recovery-turn")
    assert fresh.worker_id == "worker-one" and fresh.wait_kind == ""
    assert fresh.updated_at == 3_000 and fresh.reply == "Fresh output"
    with db.transaction() as conn:
        assert repo.park_recovery(
            conn, "recovery-turn", "worker-one", "checkpoint", "{}", 5_000, before=4_000
        )


@pytest.mark.parametrize("diagnostics", [None, '{"stage":"model","retryable":false}'])
def test_terminal_turn_keeps_or_updates_recovery_diagnostics(recovery_db, diagnostics):
    db, repos, _owner = recovery_db
    repo = repos.project_task_turn_repo
    repo.claim("worker-one", 1_000)
    saved = '{"stage":"prepare","retryable":true}'
    with db.transaction() as conn:
        conn.execute("UPDATE project_task_turns SET recovery_json=?", (saved,))
        assert repo.finish(
            conn,
            "recovery-turn",
            "worker-one",
            "failed",
            "Keep output",
            "TASK_EXECUTION_FAILED",
            2_000,
            recovery_json=diagnostics,
        )
    row = repo.get("recovery-turn")
    assert row.recovery_json == (diagnostics if diagnostics is not None else saved)
    assert row.status == "failed" and row.finished_at == 2_000


def test_completed_client_result_clears_previous_timeout_error(recovery_db):
    db, repos, owner = recovery_db
    repo = repos.project_task_tool_call_repo
    with db.transaction() as conn:
        repo.insert(
            conn,
            ProjectTaskToolCallRow(
                call_id="client-call",
                task_id="recovery-task",
                turn_id="recovery-turn",
                owner_id=owner,
                client_id="client-one",
                name="read_note",
                arguments_json="{}",
                status="claimed",
                claim_request_id="claim-one",
                claim_token="token-one",
                result_json="",
                error_code="TASK_CLIENT_TOOL_TIMEOUT",
                created_at=1_000,
                updated_at=2_000,
                expires_at=3_000,
            ),
        )
        # insert deliberately persists only the proposal; model a retained claim error.
        conn.execute(
            "UPDATE project_task_tool_calls SET error_code='TASK_CLIENT_TOOL_TIMEOUT' "
            "WHERE call_id='client-call'"
        )
        repo.complete(conn, "client-call", '{"output":"recovered"}', 4_000)
        row = repo.get(conn, "client-call")
        assert row.status == "completed" and row.error_code == ""
        assert row.result_json == '{"output":"recovered"}' and row.updated_at == 4_000


@pytest.mark.parametrize("recorded_version", [45, 46, 99])
def test_recovery_migration_repairs_missing_schema_and_preserves_saved_facts(
    recovery_db, recorded_version
):
    db, _repos, _owner = recovery_db
    with db.transaction() as conn:
        conn.execute("DROP TABLE project_task_execution_receipts")
        conn.execute("ALTER TABLE project_task_turns DROP COLUMN recovery_json")
        conn.execute("UPDATE _schema_version SET version=?", (recorded_version,))
        before = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
    run_migrations(db)
    run_migrations(db)
    with db.connect() as conn:
        after = dict(conn.execute("SELECT * FROM project_task_turns").fetchone())
        assert after.pop("recovery_json") == "" and after == before
        assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 56
        assert start_receipt(conn, ProjectTaskExecutionReceiptRepo())
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_task_deletion_removes_receipts_before_their_parent_turn(recovery_db):
    db, repos, owner = recovery_db
    with db.transaction() as conn:
        assert start_receipt(conn, repos.project_task_execution_receipt_repo)
        task = repos.project_task_repo.get_in_transaction(conn, "recovery-task", owner)
        ProjectTaskRepo.delete(conn, task)
        assert (
            conn.execute("SELECT COUNT(*) FROM project_task_execution_receipts").fetchone()[0] == 0
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
