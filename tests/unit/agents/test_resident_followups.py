"""Explicit follow-up authorization, quiet observation, and durable scheduling."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from octop.config import OctopConfig
from octop.infra.agents import resident_followups as followups
from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.agents.resident_notifications import (
    ResidentNotificationDelivery,
    get_notification,
    list_notifications,
)
from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout

NOW = 1_800_000_000_000


@pytest.fixture
def followed(tmp_path, monkeypatch):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    monkeypatch.setenv("OCTOP_HOME", str(paths.root))
    monkeypatch.setattr(followups, "_now", lambda: NOW)
    db = SqlitePool(paths.db)
    migrate.run_migrations(db)
    services = build_shared_services(
        db=db, paths=paths, config=OctopConfig(default_timezone="Asia/Shanghai")
    )
    services.user_repo.create(username="owner", password_hash="x", role="user")
    services.user_repo.create(username="other", password_hash="x", role="user")
    services.secret_repo.get_or_create("jwt", lambda: b"test-followup-cursor-key")
    binding = asyncio.run(ensure_resident_identity(services, 1))
    yield services, binding
    db.close()


def _note(request="request", schedule="interval:60", text="Drink water"):
    return followups.FollowupCreate(
        request_id=request,
        purpose="Daily care",
        kind="note",
        schedule=schedule,
        payload={"text": text},
    )


def _create(followed, body=None):
    services, binding = followed
    return followups.create_followup(
        services, 1, binding.agent_id, binding.thread_id, body or _note()
    )


def _notifications(services):
    with services.db.connect() as conn:
        return conn.execute("SELECT * FROM resident_notifications ORDER BY id").fetchall()


def _task(services, task_id="task", *, owner_id=1):
    with services.db.transaction() as conn:
        services.project_task_repo.insert(
            conn,
            ProjectTaskRow(
                task_id,
                owner_id,
                None,
                "Task",
                "task-request-" + task_id,
                "first-" + task_id,
                NOW,
                NOW,
            ),
        )
        services.project_task_turn_repo.insert(
            conn,
            ProjectTaskTurnRow(
                "first-" + task_id,
                task_id,
                "turn-request",
                "Work",
                "",
                "queued",
                "",
                "",
                "{}",
                "provider/model",
                "",
                False,
                NOW,
                NOW,
            ),
        )


def _watch(followed, task_id="task"):
    return _create(
        followed,
        followups.FollowupCreate(
            request_id="watch", purpose="Get result", kind="task_watch", subject_task_id=task_id
        ),
    )


def _update_body(row, **changes):
    body = row.model_dump(
        include={
            "purpose",
            "kind",
            "schedule",
            "subject_task_id",
            "payload",
            "capabilities",
            "notification_target",
        }
    )
    body.update({"expected_revision": row.revision, "status": "enabled", **changes})
    return followups.FollowupUpdate.model_validate(body)


def test_no_implicit_followup_or_notification(followed):
    services, binding = followed
    assert followups.list_followups(services, 1, binding.agent_id, binding.thread_id).items == []
    assert followups.poll_followups(services, NOW + 99_000_000) == 0
    assert _notifications(services) == []


def test_parallel_same_request_authorizes_once_and_content_conflicts(followed):
    with ThreadPoolExecutor(max_workers=2) as workers:
        rows = list(workers.map(lambda _: _create(followed), range(2)))
    assert rows[0].followup_id == rows[1].followup_id
    assert rows[0].next_fire_at == datetime.fromtimestamp((NOW + 60_000) / 1000, UTC)
    assert rows[0].timezone == "Asia/Shanghai"
    assert rows[0].capabilities == ["conversation_notify"]
    with pytest.raises(OctopError) as error:
        _create(followed, _note(text="Different text"))
    assert error.value.code == ErrorCode.RESIDENT_REQUEST_CONFLICT


def test_interval_restart_replay_is_durable_and_missed_ticks_coalesce(followed):
    services, _ = followed
    row = _create(followed)
    assert followups.poll_followups(services, NOW + 59_999) == 0
    assert followups.poll_followups(services, NOW + 600_000) == 1
    assert followups.poll_followups(services, NOW + 600_000) == 0
    notices = _notifications(services)
    assert len(notices) == 1 and notices[0]["text"] == "Drink water"
    saved = services.resident_followup_repo.get(1, row.followup_id)
    assert saved.next_fire_at == NOW + 660_000
    # Recreating service objects does not reset the persisted schedule or source key.
    reopened = build_shared_services(db=services.db, paths=services.paths, config=services.config)
    assert followups.poll_followups(reopened, NOW + 600_001) == 0
    assert followups.poll_followups(reopened, NOW + 660_000) == 1


def test_one_time_note_completes_with_single_stable_outbox_record(followed):
    services, _ = followed
    at = datetime.fromtimestamp((NOW + 90_000) / 1000, UTC).isoformat()
    row = _create(followed, _note(schedule="date:" + at))
    assert followups.poll_followups(services, NOW + 100_000) == 1
    saved = services.resident_followup_repo.get(1, row.followup_id)
    assert saved.status == "completed" and saved.next_fire_at is None
    assert followups.poll_followups(services, NOW + 999_999_999) == 0
    assert _notifications(services)[0]["authorization_version"] == 1


@pytest.mark.parametrize(
    "schedule",
    ["interval:1", "interval:0", "agently:mail", "cron:bad", "date:2000-01-01T00:00:00Z"],
)
def test_invalid_or_external_event_schedules_are_rejected(followed, schedule):
    with pytest.raises(OctopError) as error:
        _create(followed, _note(schedule=schedule))
    assert error.value.code == ErrorCode.CRON_TRIGGER_INVALID


@pytest.mark.parametrize(
    "values",
    [
        {
            "kind": "note",
            "schedule": "interval:60",
            "payload": {"text": "Hello"},
            "capabilities": ["execute_task"],
        },
        {
            "kind": "note",
            "schedule": "interval:60",
            "payload": {"text": "Hello"},
            "notification_target": "email",
        },
        {"kind": "task_watch", "subject_task_id": "task", "payload": {"text": "Execute this"}},
        {"kind": "note", "schedule": "interval:60", "payload": {"text": " "}},
    ],
)
def test_unauthorized_capabilities_and_mixed_kinds_are_rejected(values):
    with pytest.raises(ValidationError):
        followups.FollowupCreate(request_id="request", purpose="Purpose", **values)


def test_update_pause_cancel_revision_and_no_resurrection(followed):
    services, binding = followed
    row = _create(followed)
    paused = followups.update_followup(
        services,
        1,
        binding.agent_id,
        binding.thread_id,
        row.followup_id,
        _update_body(row, status="paused"),
    )
    assert paused.authorization_version == 2 and paused.revision == 2
    assert followups.poll_followups(services, NOW + 120_000) == 0
    with pytest.raises(OctopError) as error:
        followups.cancel_followup(
            services,
            1,
            binding.agent_id,
            binding.thread_id,
            row.followup_id,
            followups.FollowupCancel(expected_revision=1),
        )
    assert error.value.code == ErrorCode.RESIDENT_FOLLOWUP_CONFLICT
    cancelled = followups.cancel_followup(
        services,
        1,
        binding.agent_id,
        binding.thread_id,
        row.followup_id,
        followups.FollowupCancel(expected_revision=2),
    )
    assert cancelled.status == "cancelled" and cancelled.authorization_version == 3
    repeated = followups.cancel_followup(
        services,
        1,
        binding.agent_id,
        binding.thread_id,
        row.followup_id,
        followups.FollowupCancel(expected_revision=2),
    )
    assert repeated == cancelled
    with pytest.raises(OctopError):
        followups.update_followup(
            services,
            1,
            binding.agent_id,
            binding.thread_id,
            row.followup_id,
            _update_body(cancelled),
        )


def test_quiet_task_watch_reports_state_then_outcome_once_and_stops(followed):
    services, _ = followed
    _task(services)
    row = _watch(followed)
    assert followups.poll_followups(services, NOW) == 0
    assert followups.poll_followups(services, NOW + 1000) == 0
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET status='running',worker_id='worker' WHERE task_id='task'"
        )
    assert followups.poll_followups(services, NOW + 2000) == 1
    assert followups.poll_followups(services, NOW + 3000) == 0
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET status='completed',reply='Exact outcome' WHERE task_id='task'"
        )
    assert followups.poll_followups(services, NOW + 4000) == 1
    assert followups.poll_followups(services, NOW + 5000) == 0
    saved = services.resident_followup_repo.get(1, row.followup_id)
    assert saved.status == "completed" and saved.next_fire_at is None
    assert "Exact outcome" in _notifications(services)[-1]["text"]


def test_task_watch_deletion_is_final_and_foreign_task_cannot_be_watched(followed):
    services, _ = followed
    _task(services, "foreign", owner_id=2)
    with pytest.raises(OctopError) as error:
        _watch(followed, "foreign")
    assert error.value.code == ErrorCode.NOT_FOUND
    _task(services)
    row = _watch(followed)
    with services.db.transaction() as conn:
        task = services.project_task_repo.get_in_transaction(conn, "task", 1)
        services.project_task_repo.delete(conn, task)
    assert followups.poll_followups(services, NOW) == 1
    assert services.resident_followup_repo.get(1, row.followup_id).status == "completed"


def test_followup_read_and_mutation_are_private_even_to_other_owner(followed):
    services, binding = followed
    row = _create(followed)
    with pytest.raises(OctopError) as error:
        followups.get_followup(services, 2, binding.agent_id, binding.thread_id, row.followup_id)
    assert error.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(OctopError):
        followups.cancel_followup(
            services,
            2,
            binding.agent_id,
            binding.thread_id,
            row.followup_id,
            followups.FollowupCancel(expected_revision=1),
        )


def test_naive_date_and_cron_use_saved_server_timezone(followed):
    services, _ = followed
    naive = "date:2027-01-16T09:00:00"
    row = _create(followed, _note(schedule=naive))
    assert row.next_fire_at == datetime(2027, 1, 16, 1, 0, tzinfo=UTC)
    cron = _create(followed, _note(request="cron", schedule="cron:0 9 * * *"))
    assert cron.next_fire_at.astimezone(UTC).hour == 1
    changed = build_shared_services(
        db=services.db, paths=services.paths, config=OctopConfig(default_timezone="UTC")
    )
    assert changed.resident_followup_repo.get(1, row.followup_id).timezone == "Asia/Shanghai"


def test_outbox_and_schedule_advance_rollback_together(followed, monkeypatch):
    services, _ = followed
    row = _create(followed)
    original = services.resident_notification_repo.enqueue

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(services.resident_notification_repo, "enqueue", fail_after_insert)
    with pytest.raises(RuntimeError, match="simulated"):
        followups.poll_followups(services, NOW + 60_000)
    assert _notifications(services) == []
    assert services.resident_followup_repo.get(1, row.followup_id).next_fire_at == NOW + 60_000
    monkeypatch.setattr(services.resident_notification_repo, "enqueue", original)
    assert followups.poll_followups(services, NOW + 60_000) == 1


def test_profile_archive_helper_invalidates_even_completed_pending_notice(followed):
    services, binding = followed
    at = datetime.fromtimestamp((NOW + 90_000) / 1000, UTC).isoformat()
    row = _create(followed, _note(schedule="date:" + at))
    assert followups.poll_followups(services, NOW + 100_000) == 1
    with services.db.transaction() as conn:
        assert (
            services.resident_followup_repo.cancel_for_agent_in_transaction(
                conn, 1, binding.agent_id, NOW + 100_001
            )
            == 1
        )
    saved = services.resident_followup_repo.get(1, row.followup_id)
    assert saved.status == "cancelled" and saved.authorization_version == 2
    assert _notifications(services)[0]["authorization_version"] == 1


class _NotificationHarness:
    def __init__(self):
        self.messages = {}
        self.append_calls = 0
        self.fail_after_append = False
        self.entered = None
        self.release = None

    async def aappend_messages(self, thread_id, messages):
        self.append_calls += 1
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        for message in messages:
            self.messages[message.id] = message
        if self.fail_after_append:
            self.fail_after_append = False
            raise RuntimeError("simulated context receipt failure")


def _notification_setup(followed):
    services, _ = followed
    _create(followed)
    assert followups.poll_followups(services, NOW + 60_000) == 1
    row = services.resident_notification_repo.pending()[0]
    harness = _NotificationHarness()
    registry = SimpleNamespace(get_agent=lambda agent_id: harness)
    return row, harness, ResidentNotificationDelivery(services, registry, None)


async def test_notification_appends_without_model_call_and_records_one_unread(followed):
    services, binding = followed
    row, harness, delivery = _notification_setup(followed)
    await delivery.deliver(row)
    await delivery.deliver(row)
    saved = services.resident_notification_repo.get(1, row.notification_id)
    assert saved.status == "delivered"
    assert saved.archive_written_at is not None and saved.context_written_at is not None
    assert services.thread_message_repo.contains_message(binding.thread_id, row.message_id)
    assert len(harness.messages) == 1 and harness.append_calls == 1
    message = harness.messages[row.message_id]
    assert message.content == "Drink water"
    assert message.additional_kwargs["resident_notification"]["followup_id"] == row.followup_id
    with services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM resident_inbox WHERE source='notification'"
            ).fetchone()["n"]
            == 1
        )


async def test_notification_replays_frozen_message_after_context_receipt_failure(followed):
    services, binding = followed
    row, harness, delivery = _notification_setup(followed)
    harness.fail_after_append = True
    with pytest.raises(RuntimeError, match="simulated context"):
        await delivery.deliver(row)
    failed = services.resident_notification_repo.get(1, row.notification_id)
    assert failed.archive_written_at is not None and failed.context_written_at is None
    assert failed.status == "pending" and failed.message_json
    # The stored transport rendering cannot replace the already archived message.
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE resident_notifications SET text='Changed later' WHERE notification_id=?",
            (row.notification_id,),
        )
    await delivery.deliver(row)
    assert len(harness.messages) == 1 and harness.append_calls == 2
    assert harness.messages[row.message_id].content == "Drink water"
    assert services.thread_message_repo.head(binding.thread_id) == 1


async def test_notification_archive_receipt_failure_replays_same_message_id(followed, monkeypatch):
    services, binding = followed
    row, harness, delivery = _notification_setup(followed)
    original = services.resident_notification_repo.mark_stage

    def fail_receipt(notification_id, stage, now):
        if stage == "archive_written_at":
            raise RuntimeError("simulated archive receipt failure")
        return original(notification_id, stage, now)

    monkeypatch.setattr(services.resident_notification_repo, "mark_stage", fail_receipt)
    with pytest.raises(RuntimeError, match="simulated archive"):
        await delivery.deliver(row)
    assert services.thread_message_repo.head(binding.thread_id) == 1
    assert harness.append_calls == 0
    monkeypatch.setattr(services.resident_notification_repo, "mark_stage", original)
    await delivery.deliver(row)
    assert services.thread_message_repo.head(binding.thread_id) == 1
    assert harness.append_calls == 1


async def test_cancelled_authorization_never_appends_pending_notification(followed):
    services, binding = followed
    row, harness, delivery = _notification_setup(followed)
    followups.cancel_followup(
        services,
        1,
        binding.agent_id,
        binding.thread_id,
        row.followup_id,
        followups.FollowupCancel(expected_revision=1),
    )
    assert list_notifications(services, 1, binding.agent_id, binding.thread_id).items == []
    await delivery.deliver(row)
    assert services.resident_notification_repo.get(1, row.notification_id).status == "discarded"
    assert services.thread_message_repo.head(binding.thread_id) == 0
    assert harness.append_calls == 0
    assert (
        get_notification(
            services, 1, binding.agent_id, binding.thread_id, row.notification_id
        ).status
        == "discarded"
    )


async def test_disabled_owner_suppresses_pending_notification_and_future_due(followed):
    services, binding = followed
    row, harness, delivery = _notification_setup(followed)
    with services.db.transaction() as conn:
        conn.execute("UPDATE users SET disabled=1 WHERE id=1")
    assert not services.resident_notification_repo.is_authorized(row)
    assert list_notifications(services, 1, binding.agent_id, binding.thread_id).items == []
    assert followups.poll_followups(services, NOW + 120_000) == 0
    await delivery.deliver(row)
    assert services.resident_notification_repo.get(1, row.notification_id).status == "discarded"
    assert harness.append_calls == 0
    assert services.thread_message_repo.head(binding.thread_id) == 0


async def test_cancelled_delivery_drains_before_releasing_caller_scope(followed):
    services, _ = followed
    row, harness, delivery = _notification_setup(followed)
    harness.entered, harness.release = asyncio.Event(), asyncio.Event()
    operation = asyncio.create_task(delivery.deliver(row))
    await asyncio.wait_for(harness.entered.wait(), timeout=2)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    harness.release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert services.resident_notification_repo.get(1, row.notification_id).status == "delivered"
    assert len(harness.messages) == 1


def test_notification_keyset_pages_are_private_and_do_not_repeat(followed):
    services, binding = followed
    _create(followed)
    for when in (60_000, 120_000, 180_000):
        assert followups.poll_followups(services, NOW + when) == 1
    seen, cursor = [], None
    for _ in range(3):
        page = list_notifications(
            services, 1, binding.agent_id, binding.thread_id, limit=1, page_cursor=cursor
        )
        assert len(page.items) == 1
        seen.append(page.items[0].notification_id)
        cursor = page.next_page_cursor
    assert cursor is None and len(set(seen)) == 3
    with pytest.raises(OctopError) as error:
        get_notification(services, 2, binding.agent_id, binding.thread_id, seen[0])
    assert error.value.code == ErrorCode.NOT_FOUND


def _scheduled(followed, *, schedule="interval:60", attachment_ids=None, project_id=None):
    services, binding = followed
    services.provider_repo.create(
        name="scheduled",
        kind="openai",
        base_url="https://model.example.test/v1",
        api_key="test-only-key",
        models_json='[{"id":"model","name":"model"}]',
    )
    source, _ = services.resident_request_repo.create(
        1, binding.thread_id, "source-request", "source", "Prepare this work daily", NOW
    )
    services.resident_request_repo.cancel(
        1, binding.thread_id, "cancel-source", source.turn_id, NOW + 1
    )
    return followups.FollowupCreate(
        request_id="scheduled-work",
        purpose="Prepare the authorized work",
        kind="task",
        schedule=schedule,
        payload={
            "source_turn_id": "source",
            "project_id": project_id,
            "message": {
                "request_id": "user-task-input",
                "prompt": "Prepare report",
                "attachment_ids": attachment_ids or [],
                "options": {
                    "model_ref": "scheduled/model",
                    "skills": [],
                    "mcp_servers": [],
                    "memory_enabled": False,
                },
            },
        },
        capabilities=["conversation_notify", "task_create"],
    )


def test_scheduled_task_requires_explicit_exact_capabilities_and_no_recurring_uploads(followed):
    body = _scheduled(followed)
    assert body.capabilities == ["task_create", "conversation_notify"]
    values = body.model_dump()
    values.pop("capabilities")
    with pytest.raises(ValidationError):
        followups.FollowupCreate.model_validate(values)
    values["capabilities"] = ["task_create", "conversation_notify"]
    values["payload"]["message"]["attachment_ids"] = ["one-time-upload"]
    with pytest.raises(ValidationError):
        followups.FollowupCreate.model_validate(values)


def test_scheduled_task_admission_and_result_return_are_atomic_and_replay_safe(followed):
    services, binding = followed
    row = _create(followed, _scheduled(followed))
    assert followups.poll_followups(services, NOW + 60_000) == 1
    assert followups.poll_followups(services, NOW + 60_000) == 0
    saved = followups.get_followup(
        services, 1, binding.agent_id, binding.thread_id, row.followup_id
    )
    assert saved.last_outcome == "admitted" and saved.last_task_id
    task = services.project_task_repo.get(saved.last_task_id, 1)
    turn = services.project_task_turn_repo.get(task.active_turn_id)
    assert turn.prompt == "Prepare report" and task.project_id is None
    configuration = json.loads(turn.configuration_json)
    assert configuration["approval_policy_at_start"]["mode"] == "ask_for_approval"
    assert configuration["execution_options"]["skills"] == []
    assert turn.request_id != "user-task-input"
    notice = _notifications(services)[0]
    assert (
        notice["kind"] == "task" and json.loads(notice["payload_json"])["task_id"] == task.task_id
    )
    claimed = services.project_task_turn_repo.claim("scheduled-worker", NOW + 60_001)
    with services.db.transaction() as conn:
        assert services.project_task_turn_repo.finish(
            conn,
            claimed.turn_id,
            "scheduled-worker",
            "completed",
            "Scheduled task outcome",
            "",
            NOW + 60_002,
        )
        services.project_task_repo.finish(conn, task.task_id, claimed.turn_id, NOW + 60_002)
    report = services.resident_task_report_repo.pending()[0]
    assert report.thread_id == binding.thread_id and report.task_id == task.task_id
    assert report.text == "Scheduled task outcome"


def test_scheduled_task_savepoint_rolls_back_partial_admission_and_reports_failure(followed):
    services, binding = followed
    at = datetime.fromtimestamp((NOW + 60_000) / 1000, UTC).isoformat()
    row = _create(
        followed, _scheduled(followed, schedule="date:" + at, attachment_ids=["missing-upload"])
    )
    assert followups.poll_followups(services, NOW + 60_000) == 1
    saved = followups.get_followup(
        services, 1, binding.agent_id, binding.thread_id, row.followup_id
    )
    assert saved.status == "completed" and saved.last_outcome == "admission_failed"
    assert saved.last_error_code == ErrorCode.TASK_ATTACHMENT_UNAVAILABLE.value
    assert saved.last_task_id is None and saved.next_fire_at is None
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM resident_delegations").fetchone()["n"] == 0
    notice = services.resident_notification_repo.pending()[0]
    assert services.resident_notification_repo.is_authorized(notice)
    assert json.loads(notice.payload_json)["outcome"] == "admission_failed"
    assert followups.poll_followups(services, NOW + 600_000) == 0


def test_cancelling_schedule_does_not_cancel_already_accepted_task(followed):
    services, binding = followed
    row = _create(followed, _scheduled(followed))
    assert followups.poll_followups(services, NOW + 60_000) == 1
    accepted = services.resident_followup_repo.get(1, row.followup_id)
    task_id = json.loads(accepted.last_observed_json)["task_id"]
    followups.cancel_followup(
        services,
        1,
        binding.agent_id,
        binding.thread_id,
        row.followup_id,
        followups.FollowupCancel(expected_revision=accepted.revision),
    )
    assert followups.poll_followups(services, NOW + 600_000) == 0
    assert services.project_task_repo.get(task_id, 1).status == "queued"
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 1


def test_failed_schedule_requires_new_full_authorization_to_retry(followed):
    services, binding = followed
    body = _scheduled(followed)
    # Provider permission changes after authorization are checked at dispatch.
    row = _create(followed, body)
    provider = services.provider_repo.get_by_name("scheduled")
    services.provider_repo.update(provider.id, enabled=False)
    assert followups.poll_followups(services, NOW + 60_000) == 1
    failed = followups.get_followup(
        services, 1, binding.agent_id, binding.thread_id, row.followup_id
    )
    assert failed.last_error_code == ErrorCode.TASK_MODEL_UNAVAILABLE.value
    services.provider_repo.update(provider.id, enabled=True)
    renewed = followups.update_followup(
        services, 1, binding.agent_id, binding.thread_id, row.followup_id, _update_body(failed)
    )
    assert renewed.authorization_version == 2 and renewed.status == "enabled"
    assert followups.poll_followups(services, NOW + 60_000) == 1
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 1


def test_overdue_scheduled_work_coalesces_and_keeps_persisted_next_due(followed):
    services, _ = followed
    row = _create(followed, _scheduled(followed))
    assert followups.poll_followups(services, NOW + 600_000) == 1
    saved = services.resident_followup_repo.get(1, row.followup_id)
    assert saved.next_fire_at == NOW + 660_000
    reopened = build_shared_services(db=services.db, paths=services.paths, config=services.config)
    assert followups.poll_followups(reopened, NOW + 600_001) == 0
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 1


def test_task_and_delegation_roll_back_if_outbox_cannot_commit(followed, monkeypatch):
    services, _ = followed
    row = _create(followed, _scheduled(followed))
    original = services.resident_notification_repo.enqueue

    def fail_outbox(*args, **kwargs):
        raise RuntimeError("simulated outbox unavailable")

    monkeypatch.setattr(services.resident_notification_repo, "enqueue", fail_outbox)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        followups.poll_followups(services, NOW + 60_000)
    assert services.resident_followup_repo.get(1, row.followup_id).next_fire_at == NOW + 60_000
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM resident_delegations").fetchone()["n"] == 0
    monkeypatch.setattr(services.resident_notification_repo, "enqueue", original)
    assert followups.poll_followups(services, NOW + 60_000) == 1
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 1


async def test_archived_profile_returns_prior_followup_receipt_without_reauthorizing(
    followed, tmp_path
):
    from octop.infra.agents.experts.catalog import ExpertCatalog
    from octop.infra.agents.resident_profiles import (
        ResidentProfileArchive,
        ResidentProfileCreate,
        archive_profile,
        create_profile,
    )

    services, _ = followed
    directory = tmp_path / "templates" / "writer"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": "writer",
                "label": {"en": "Writer", "zh": "写作助手"},
                "prompt_files": ["SOUL.md"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "SOUL.md").write_text("Use clear evidence.", encoding="utf-8")
    catalog = ExpertCatalog(directory.parent)
    catalog.refresh()
    profile = await create_profile(
        services,
        catalog,
        1,
        ResidentProfileCreate(
            request_id="writer-profile",
            name="Writer",
            template_kind="builtin",
            template_id="writer",
        ),
    )
    body = _note()
    row = followups.create_followup(services, 1, profile.agent_id, profile.thread_id, body)
    archive_profile(
        services,
        1,
        profile.profile_id,
        ResidentProfileArchive(expected_revision=profile.revision, archived=True),
    )
    replay = followups.create_followup(services, 1, profile.agent_id, profile.thread_id, body)
    assert replay.followup_id == row.followup_id and replay.status == "cancelled"
    with pytest.raises(OctopError) as error:
        followups.create_followup(
            services, 1, profile.agent_id, profile.thread_id, _note(request="new-followup")
        )
    assert error.value.code == ErrorCode.RESIDENT_PROFILE_ARCHIVED


@pytest.mark.parametrize("action", ["cancel", "update"])
async def test_repeated_http_cancellation_keeps_authorization_lock_until_write_finishes(
    followed, monkeypatch, action
):
    from fastapi import Response

    from octop.api.routers import resident_followups as routes

    services, binding = followed
    row = _create(followed)
    locked = asyncio.Lock()

    @asynccontextmanager
    async def scope(followup_id):
        async with locked:
            yield

    runtime = SimpleNamespace(followup_scope=scope)
    server = SimpleNamespace(
        database_bound=True,
        services=services,
        app_runtime=SimpleNamespace(resident_conversations=runtime),
    )
    entered, release = threading.Event(), threading.Event()
    original = getattr(followups, action + "_followup")

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(followups, action + "_followup", blocked_write)
    body = (
        followups.FollowupCancel(expected_revision=1)
        if action == "cancel"
        else _update_body(row, status="paused")
    )
    handler = getattr(routes, action + "_followup")
    operation = asyncio.create_task(
        handler(
            binding.agent_id,
            binding.thread_id,
            row.followup_id,
            body,
            Response(),
            server,
            SimpleNamespace(id=1),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            operation.cancel()
            await asyncio.sleep(0)
            assert locked.locked() and not operation.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not locked.locked()
    saved = services.resident_followup_repo.get(1, row.followup_id)
    assert saved.status == ("cancelled" if action == "cancel" else "paused")
