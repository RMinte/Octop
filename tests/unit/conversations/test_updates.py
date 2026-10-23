"""Committed wakeups are scoped, disposable and safe across awaited reads."""

import asyncio

from octop.infra.conversations.updates import CommittedUpdates, PendingUpdates


def test_committed_updates_keep_resource_scopes_separate():
    updates = CommittedUpdates[tuple[str, int, str]]()
    calls = []
    task = ("task", 1, "same-id")
    resident = ("resident", 1, "same-id")
    other_owner = ("resident", 2, "same-id")
    updates.watch(task, lambda: calls.append("task"))
    updates.watch(resident, lambda: calls.append("resident"))
    updates.watch(other_owner, lambda: calls.append("other-owner"))
    updates.notify(resident)
    assert calls == ["resident"]


def test_stopped_watch_cannot_remove_new_watch_with_the_same_key():
    updates = CommittedUpdates[str]()
    calls = []
    stop_old = updates.watch("thread", lambda: calls.append("old"))
    stop_old()
    assert updates.listeners == {}
    stop_new = updates.watch("thread", lambda: calls.append("new"))
    stop_old()
    updates.notify("thread")
    assert calls == ["new"]
    stop_new()
    stop_new()
    assert updates.listeners == {}


def test_watch_can_be_removed_during_notification():
    updates = CommittedUpdates[str]()
    calls = []

    def wake():
        calls.append("committed")
        stop()

    stop = updates.watch("thread", wake)
    updates.notify("thread")
    updates.notify("thread")
    assert calls == ["committed"] and updates.listeners == {}


async def test_pending_updates_retain_new_commit_during_authoritative_read():
    pending = PendingUpdates[str]()
    pending.add("thread")
    assert await pending.wait(0)
    assert pending.take() == {"thread"}
    reading, release_read = asyncio.Event(), asyncio.Event()

    async def read_snapshot():
        reading.set()
        await release_read.wait()

    read = asyncio.create_task(read_snapshot())
    await reading.wait()
    pending.add("thread")
    release_read.set()
    await read
    assert await pending.wait(0)
    assert pending.take() == {"thread"}
    assert not await pending.wait(0)


async def test_pending_wakeup_is_coalesced_and_unsubscribe_removes_only_its_key():
    pending = PendingUpdates[str]()
    pending.add("first")
    pending.add("first")
    pending.add("second")
    pending.discard("first")
    assert await pending.wait(0)
    assert pending.take() == {"second"}
    pending.add("first")
    pending.discard("first")
    assert not await pending.wait(0)
    assert pending.take() == set()


async def test_wait_can_wake_or_expire_for_database_polling():
    pending = PendingUpdates[str]()
    waiting = asyncio.create_task(pending.wait(1))
    pending.add("thread")
    assert await waiting
    assert pending.take() == {"thread"}
    assert not await pending.wait(0.001)
