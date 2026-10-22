"""Task finalization and result outbox commit together without changing historical facts."""

from __future__ import annotations

import asyncio
import json

import pytest

from octop.config import OctopConfig
from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_artifacts import ProjectTaskArtifactRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.resident_delegations import (
    ResidentDelegationRepo,
    ResidentDelegationRow,
    ResidentTaskReportRepo,
)
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout

NOW = 1_800_000_000_000


@pytest.fixture
def delegated(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    migrate.run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    services.user_repo.create(username="owner", password_hash="x", role="user")
    binding = asyncio.run(ensure_resident_identity(services, 1))
    with db.transaction() as conn:
        services.project_task_repo.insert(
            conn, ProjectTaskRow("task", 1, None, "Task", "task-request", "first", NOW, NOW)
        )
        services.project_task_turn_repo.insert(conn, _turn("first"))
        ResidentDelegationRepo.insert(
            conn,
            ResidentDelegationRow(
                "delegation",
                1,
                binding.agent_id,
                binding.thread_id,
                "source",
                "task",
                "first",
                "delegation-request",
                "fingerprint",
                "Asia/Shanghai",
                NOW,
                NOW,
            ),
        )
    yield services, ResidentTaskReportRepo(db), binding
    db.close()


def _turn(turn_id, retry_of=None):
    configuration = {"retry_of_turn_id": retry_of} if retry_of else {}
    return ProjectTaskTurnRow(
        turn_id,
        "task",
        "request-" + turn_id,
        "Work",
        "",
        "queued",
        "",
        "",
        json.dumps(configuration),
        "provider/model",
        "",
        False,
        NOW,
        NOW,
    )


def _finish(services, turn_id, status, reply="Result", error=""):
    claimed = services.project_task_turn_repo.claim("worker", NOW + 1)
    assert claimed.turn_id == turn_id
    with services.db.transaction() as conn:
        assert services.project_task_turn_repo.finish(
            conn, turn_id, "worker", status, reply, error, NOW + 2
        )
        services.project_task_repo.finish(conn, "task", turn_id, NOW + 2)


def _enqueue(services, turn_id, retry_of=None):
    with services.db.transaction() as conn:
        assert services.project_task_repo.activate(conn, "task", turn_id, NOW + 3)
        services.project_task_turn_repo.insert(conn, _turn(turn_id, retry_of))


def test_terminal_report_freezes_specific_reply_and_artifact_facts(delegated):
    services, reports, binding = delegated
    with services.db.transaction() as conn:
        services.project_task_artifact_repo.insert(
            conn,
            ProjectTaskArtifactRow(
                "artifact",
                "task",
                "first",
                "call",
                "request-hash",
                "result.txt",
                "text/plain",
                5,
                "hash",
                None,
                NOW,
            ),
            b"Hello",
        )
    _finish(services, "first", "completed", "Exact result")
    report = reports.pending()[0]
    assert report.text == "Exact result" and report.status == "completed"
    assert report.thread_id == binding.thread_id and report.task_turn_id == "first"
    assert report.message_id == "resident:report:first"
    assert report.timezone == "Asia/Shanghai" and report.day_key
    artifact = json.loads(report.artifacts_json)[0]
    assert artifact["artifact_id"] == "artifact" and artifact["name"] == "result.txt"
    assert artifact["sha256"] == "hash" and "content" not in artifact
    with services.db.transaction() as conn:
        conn.execute("UPDATE project_task_turns SET reply='Changed later' WHERE turn_id='first'")
        ResidentTaskReportRepo.capture_terminal(conn, "first", NOW + 100)
    assert reports.get(1, report.report_id) == report
    assert reports.get(2, report.report_id) is None


def test_task_finalization_rolls_back_if_outer_transaction_fails(delegated):
    services, reports, _ = delegated
    services.project_task_turn_repo.claim("worker", NOW + 1)
    with pytest.raises(RuntimeError, match="rollback"), services.db.transaction() as conn:
        assert services.project_task_turn_repo.finish(
            conn, "first", "worker", "completed", "Result", "", NOW + 2
        )
        assert conn.execute("SELECT COUNT(*) FROM resident_task_reports").fetchone()[0] == 1
        raise RuntimeError("rollback")
    assert services.project_task_turn_repo.get("first").status == "running"
    assert reports.pending() == []


def test_report_text_is_bounded_and_pending_reads_are_thread_scoped(delegated):
    services, reports, binding = delegated
    _finish(services, "first", "completed", "A" * 9000)
    report = reports.pending_for_thread(1, binding.thread_id)[0]
    assert report.text == "A" * 8000
    assert len(services.project_task_turn_repo.get("first").reply) == 9000
    assert reports.pending_for_thread(2, binding.thread_id) == []
    assert reports.pending_for_thread(1, "another-thread") == []


def test_explicit_retry_chain_reports_each_turn_and_ordinary_followup_does_not(delegated):
    services, reports, _ = delegated
    _finish(services, "first", "failed", "Partial", "INTERNAL_ERROR")
    _enqueue(services, "retry-1", "first")
    _finish(services, "retry-1", "failed", "Second partial", "INTERNAL_ERROR")
    _enqueue(services, "retry-2", "retry-1")
    _finish(services, "retry-2", "completed", "Recovered")
    _enqueue(services, "new-work")
    _finish(services, "new-work", "completed", "Unrelated later work")
    _enqueue(services, "unrelated-retry", "new-work")
    _finish(services, "unrelated-retry", "completed", "Unrelated retry")
    rows = reports.pending()
    assert [(row.task_turn_id, row.status) for row in rows] == [
        ("first", "failed"),
        ("retry-1", "failed"),
        ("retry-2", "completed"),
    ]
    assert [row.text for row in rows] == ["Partial", "Second partial", "Recovered"]
    with services.db.read_snapshot() as conn:
        delegation = services.resident_delegation_repo.get_in_transaction(conn, 1, "delegation")
        latest = services.resident_delegation_repo.latest_linked_turn(conn, delegation)
    assert latest["turn_id"] == "retry-2" and latest["status"] == "completed"
    assert "reply" not in dict(latest)


def test_queued_cancellation_captures_report_and_task_deletion_preserves_it(delegated):
    services, reports, _ = delegated
    with services.db.transaction() as conn:
        services.project_task_turn_repo.cancel(conn, "first", NOW + 1)
        services.project_task_repo.finish(conn, "task", "first", NOW + 1)
    report = reports.pending()[0]
    assert report.status == "cancelled" and not report.error_code
    with services.db.transaction() as conn:
        row = services.project_task_repo.get_in_transaction(conn, "task", 1)
        services.project_task_repo.delete(conn, row)
    assert reports.get(1, report.report_id) == report
    with services.db.connect() as conn:
        assert ResidentDelegationRepo.get_in_transaction(conn, 1, "delegation").task_id == "task"


def test_waiting_and_recoverable_interruption_are_not_terminal_reports(delegated):
    services, reports, _ = delegated
    services.project_task_turn_repo.claim("worker", NOW + 1)
    with services.db.transaction() as conn:
        assert services.project_task_turn_repo.pause(
            conn, "first", "worker", "checkpoint", "{}", "Partial", "", "[]", 0, NOW + 2
        )
        ResidentTaskReportRepo.capture_terminal(conn, "first", NOW + 2)
        assert services.project_task_turn_repo.interrupt_wait(
            conn, "first", "checkpoint", "{}", NOW + 3
        )
        ResidentTaskReportRepo.capture_terminal(conn, "first", NOW + 3)
    assert reports.pending() == []
    with services.db.transaction() as conn:
        services.project_task_turn_repo.cancel(conn, "first", NOW + 4)
    assert reports.pending()[0].status == "cancelled"


def test_unrecoverable_interruption_and_failed_wait_capture_terminal_reports(delegated):
    services, reports, _ = delegated
    services.project_task_turn_repo.claim("worker", NOW + 1)
    with services.db.transaction() as conn:
        assert services.project_task_turn_repo.interrupt(conn, "first", NOW + 2, NOW + 3)
        services.project_task_repo.finish(conn, "task", "first", NOW + 3)
    assert reports.pending()[0].status == "failed"
    assert reports.pending()[0].error_code == "TASK_INTERRUPTED"
    _enqueue(services, "retry", "first")
    services.project_task_turn_repo.claim("worker", NOW + 4)
    with services.db.transaction() as conn:
        assert services.project_task_turn_repo.pause(
            conn, "retry", "worker", "checkpoint", "{}", "", "", "[]", 0, NOW + 5
        )
        assert services.project_task_turn_repo.resume_wait(conn, "retry", NOW + 6, "INTERNAL_ERROR")
    assert len(reports.pending()) == 2
    assert reports.pending()[1].error_code == "INTERNAL_ERROR"


def test_outbox_stages_are_idempotent_and_delivery_requires_both_writes(delegated):
    services, reports, binding = delegated
    _finish(services, "first", "completed")
    report = reports.pending()[0]
    revision = services.resident_conversation_repo.get(1).revision
    assert (
        reports.prepare_message(report.report_id, '{"text":"First locale"}')
        == '{"text":"First locale"}'
    )
    assert (
        reports.prepare_message(report.report_id, '{"text":"Later locale"}')
        == '{"text":"First locale"}'
    )
    assert services.resident_conversation_repo.get(1).revision == revision
    with pytest.raises(ValueError, match="unavailable"):
        reports.prepare_message("missing", "{}")
    assert not reports.mark_delivered(report.report_id, NOW + 3)
    reports.record_delivery_error(report.report_id, "INTERNAL_ERROR", NOW + 4)
    assert reports.get(1, report.report_id).delivery_error == "INTERNAL_ERROR"
    assert services.resident_conversation_repo.get(1).revision > revision
    assert reports.mark_archived(report.report_id, NOW + 5)
    revision = services.resident_conversation_repo.get(1).revision
    assert not reports.mark_archived(report.report_id, NOW + 6)
    assert services.resident_conversation_repo.get(1).revision == revision
    assert not reports.mark_delivered(report.report_id, NOW + 7)
    assert reports.mark_context(report.report_id, NOW + 8)
    assert reports.mark_delivered(report.report_id, NOW + 9)
    assert not reports.mark_delivered(report.report_id, NOW + 10)
    assert reports.pending() == []
    delivered = reports.get(1, report.report_id)
    assert delivered.archive_written_at == NOW + 5 and delivered.context_written_at == NOW + 8
    assert delivered.thread_id == binding.thread_id and not delivered.delivery_error
