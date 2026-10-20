"""Read anchors survive retries and concurrent devices without losing new messages."""

from __future__ import annotations

import asyncio

import pytest
from tests.unit.db.test_resident_requests import services as services


def _finish(services, thread, key, timestamp):
    repo = services.resident_request_repo
    repo.create(1, thread, key, key, key, timestamp)
    assert repo.mark_running(key, "worker", timestamp + 1, timestamp + 100)
    assert repo.update_reply(key, "worker", "partial", timestamp + 2)
    assert repo.update_reply(key, "worker", "full", timestamp + 3)
    assert repo.finish(key, "worker", "completed", "full", "", timestamp + 4)


def test_repeated_progress_and_old_read_anchor_do_not_consume_new_reply(services):
    thread = services.resident_conversation_repo.get(1).thread_id
    inbox = services.resident_inbox_repo
    _finish(services, thread, "first", 1000)
    assert inbox.state(1, thread)["unread_count"] == 1
    anchor = "resident:first:assistant"
    assert inbox.mark_read(1, thread, anchor)["unread_count"] == 0
    _finish(services, thread, "second", 2000)
    assert inbox.mark_read(1, thread, anchor)["unread_count"] == 1
    assert inbox.mark_read(2, thread, anchor) is None
    assert inbox.mark_read(1, thread, "resident:first:user") is None
    inbox.initialize_existing()
    assert inbox.state(1, thread)["unread_count"] == 1


@pytest.mark.asyncio
async def test_concurrent_devices_can_only_advance_to_later_anchor(services):
    thread = services.resident_conversation_repo.get(1).thread_id
    _finish(services, thread, "first", 1000)
    _finish(services, thread, "second", 2000)
    _finish(services, thread, "third", 3000)
    repo = services.resident_inbox_repo
    await asyncio.gather(
        asyncio.to_thread(repo.mark_read, 1, thread, "resident:second:assistant"),
        asyncio.to_thread(repo.mark_read, 1, thread, "resident:first:assistant"),
    )
    assert repo.state(1, thread)["last_read_message_id"] == "resident:second:assistant"
    assert repo.state(1, thread)["unread_count"] == 1
