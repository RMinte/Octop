"""A repeated exact cancellation must let the first cleanup persist its receipt."""

from __future__ import annotations

import asyncio
import threading

import pytest

from octop.infra.projects import resident_tasks
from tests.integration.test_resident_runtime_recovery import _setup, _wait_status
from tests.support.harness import patch_resident_stream


@pytest.mark.parametrize("reentry", ["replay", "shutdown"])
async def test_cancel_reentry_does_not_interrupt_resident_receipt_cleanup(
    env_with_provider, monkeypatch, reentry
):
    client, server, auth, binding, base = await _setup(env_with_provider)
    runtime = server.app_runtime.resident_conversations
    entered, release = threading.Event(), threading.Event()
    calls = []
    waiting = asyncio.Event()

    async def stream(message):
        calls.append(message)
        if len(calls) == 1:
            yield {"type": "token", "content": "Persisted partial"}
            await waiting.wait()
        else:
            yield {"type": "token", "content": "Followup completed"}
            yield {"type": "done"}

    patch_resident_stream(monkeypatch, stream)
    accepted = await client.post(
        f"{base}/messages",
        headers=auth,
        json={"request_id": "original-send", "prompt": "Wait for cancellation"},
    )
    assert accepted.status_code == 202, accepted.text
    turn_id = accepted.json()["turn_id"]
    async with asyncio.timeout(5):
        while True:
            receipt = await client.get(f"{base}/requests/original-send", headers=auth)
            if receipt.json()["reply"] == "Persisted partial":
                break
            await asyncio.sleep(0.01)
    execution = runtime._active[turn_id]
    original = resident_tasks.cancel
    count = 0
    counter_lock = threading.Lock()

    def slow_cleanup(*args):
        nonlocal count
        with counter_lock:
            count += 1
            current = count
        # 1: HTTP cancel; 2: CancelledError handler; 3: final cleanup before receipt.finish.
        if current == 3:
            entered.set()
            assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(resident_tasks, "cancel", slow_cleanup)
    cancellation = {"request_id": "cancel-original", "turn_id": turn_id}
    closing = None
    try:
        first = await client.post(f"{base}/cancel", headers=auth, json=cancellation)
        assert first.status_code == 200, first.text
        assert await asyncio.to_thread(entered.wait, 5)
        assert execution.cancelling() == 1
        if reentry == "replay":
            replay = await client.post(f"{base}/cancel", headers=auth, json=cancellation)
            assert replay.status_code == 200, replay.text
            assert replay.json()["turn_id"] == turn_id
        else:
            closing = asyncio.create_task(runtime.close())
            await asyncio.sleep(0.01)
            assert not closing.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(execution, return_exceptions=True), 5)
    if closing is not None:
        await asyncio.wait_for(closing, 5)
        await runtime.start()
    final = await client.get(f"{base}/requests/original-send", headers=auth)
    assert final.json()["status"] == "cancelled"
    assert final.json()["reply"] == "Persisted partial"
    assert execution.cancelling() == 1
    assert services_row(server, binding, "original-send").worker_id == ""
    following = await client.post(
        f"{base}/messages",
        headers=auth,
        json={"request_id": "followup-send", "prompt": "Continue after stop"},
    )
    assert following.status_code == 202, following.text
    completed = await _wait_status(client, auth, base, "followup-send", "completed")
    assert completed["reply"] == "Followup completed"
    assert len(calls) == 2


def services_row(server, binding, request_id):
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    return server.services.resident_request_repo.get(owner, request_id)
