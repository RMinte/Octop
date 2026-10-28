"""Scheduled task preparation leaves authorization changes free to commit."""
# ruff: noqa: F811

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.unit.agents.test_resident_followups import (
    NOW,
    _create,
    _notifications,
    _scheduled,
    _update_body,
    followed,  # noqa: F401
)

from octop.infra.agents import resident_followups as followups
from octop.infra.projects import tasks


@pytest.mark.parametrize("action", ["cancel", "update", "reschedule"])
def test_authorization_can_change_while_content_preparation_waits(followed, monkeypatch, action):
    services, binding = followed
    row = _create(followed, _scheduled(followed))
    original = tasks._base_configuration
    entered = threading.Event()
    release = threading.Event()

    def delayed(*args, **kwargs):
        assert not services.db._conn.in_transaction
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(tasks, "_base_configuration", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        polling = pool.submit(followups.poll_followups, services, NOW + 60_000)
        try:
            assert entered.wait(5)
            if action == "cancel":
                modifying = pool.submit(
                    followups.cancel_followup,
                    services,
                    1,
                    binding.agent_id,
                    binding.thread_id,
                    row.followup_id,
                    followups.FollowupCancel(expected_revision=row.revision),
                )
            elif action == "update":
                payload = row.payload.model_dump()
                payload["message"]["prompt"] = "Updated authorized work"
                modifying = pool.submit(
                    followups.update_followup,
                    services,
                    1,
                    binding.agent_id,
                    binding.thread_id,
                    row.followup_id,
                    _update_body(row, payload=payload),
                )
            else:

                def reschedule():
                    with services.db.transaction() as conn:
                        conn.execute(
                            "UPDATE resident_followups SET next_fire_at=? WHERE followup_id=?",
                            (NOW + 120_000, row.followup_id),
                        )

                modifying = pool.submit(reschedule)
            modifying.result(5)
        finally:
            release.set()
        assert polling.result(5) == 0
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM resident_delegations").fetchone()[0] == 0
    assert _notifications(services) == []
    if action == "update":
        assert followups.poll_followups(services, NOW + 60_000) == 1
        with services.db.connect() as conn:
            assert (
                conn.execute("SELECT prompt FROM project_task_turns").fetchone()[0]
                == "Updated authorized work"
            )


def test_existing_delegation_replays_without_preparing_content(followed, monkeypatch):
    services, binding = followed
    row = _create(followed, _scheduled(followed))
    assert followups.poll_followups(services, NOW + 60_000) == 1
    saved = followups.get_followup(
        services, 1, binding.agent_id, binding.thread_id, row.followup_id
    )
    with services.db.transaction() as conn:
        # Replay the same due occurrence, whose delegation is already durable.
        conn.execute(
            "UPDATE resident_followups SET next_fire_at=? WHERE followup_id=?",
            (NOW + 60_000, row.followup_id),
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("An existing receipt must not prepare content")

    monkeypatch.setattr(tasks, "prepare_submission", forbidden)
    assert followups.poll_followups(services, NOW + 60_000) == 0
    replayed = followups.get_followup(
        services, 1, binding.agent_id, binding.thread_id, row.followup_id
    )
    assert replayed.last_outcome == "admitted"
    assert replayed.last_task_id == saved.last_task_id
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 1
