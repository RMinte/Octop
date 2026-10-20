"""Worker shutdown and lease loss retain receipts without replaying a turn."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from octop.infra.agents.resident_conversation import (
    ResidentConversationRuntime,
    now_ms,
    turn_view,
)
from octop.infra.agents.resident_worker_lock import ResidentWorkerLock
from octop.infra.errors import ErrorCode, OctopError


async def _setup(env):
    client, server, auth = env
    response = await client.put("/api/agents/resident-conversation", headers=auth, json={})
    assert response.status_code == 200, response.text
    binding = response.json()
    base = f"/api/agents/{binding['agent_id']}/threads/{binding['thread_id']}"
    return client, server, auth, binding, base


async def _wait_status(client, auth, base, request_id, expected):
    async with asyncio.timeout(10):
        while True:
            result = await client.get(f"{base}/requests/{request_id}", headers=auth)
            assert result.status_code == 200, result.text
            if result.json()["status"] == expected:
                return result.json()
            await asyncio.sleep(0.01)


async def test_shutdown_holds_process_ownership_until_followup_admission_settles(
    env_with_provider, monkeypatch
):
    from octop.infra.agents import resident_followups

    _, server, _, _, _ = await _setup(env_with_provider)
    runtime = server.app_runtime.resident_conversations
    await runtime.close()
    entered, release = threading.Event(), threading.Event()

    def blocked_poll(*args):
        entered.set()
        assert release.wait(10)
        return 0

    monkeypatch.setattr(resident_followups, "poll_followups", blocked_poll)
    runtime._last_followup_poll = 0
    contender = ResidentWorkerLock(runtime._process_lock.path)
    try:
        await runtime.start()
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.01)
        assert not closing.done()
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(contender.acquire)
        release.set()
        await closing
        await asyncio.to_thread(contender.acquire)
    finally:
        release.set()
        await asyncio.to_thread(contender.release)
        await runtime.close()


async def test_runtime_restart_keeps_partial_reply_and_does_not_replay_running_work(
    env_with_provider, monkeypatch
):
    client, server, auth, binding, base = await _setup(env_with_provider)
    runtime = server.app_runtime.resident_conversations
    invocations = []
    blocked = asyncio.Event()

    async def stream(message):
        invocations.append(message)
        yield {"type": "token", "content": "Persisted partial"}
        await blocked.wait()

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    response = await client.post(
        f"{base}/messages", headers=auth, json={"request_id": "restart", "prompt": "Wait"}
    )
    assert response.status_code == 202
    async with asyncio.timeout(10):
        while True:
            row = await client.get(f"{base}/requests/restart", headers=auth)
            if row.json()["reply"] == "Persisted partial":
                break
            await asyncio.sleep(0.01)
    await runtime.close()
    interrupted = await _wait_status(client, auth, base, "restart", "interrupted")
    assert interrupted["reply"] == "Persisted partial"
    assert interrupted["error_code"] == ErrorCode.RESIDENT_INTERRUPTED.value
    await runtime.start()
    await runtime.start()  # Startup is idempotent as well as identity initialization.
    retry = await client.post(
        f"{base}/messages", headers=auth, json={"request_id": "restart", "prompt": "Wait"}
    )
    assert retry.status_code == 202
    assert retry.json()["turn_id"] == interrupted["turn_id"]
    assert retry.json()["status"] == "interrupted"
    assert len(invocations) == 1
    same = (await client.get("/api/agents/resident-conversation", headers=auth)).json()
    assert same["agent_id"] == binding["agent_id"]
    assert same["thread_id"] == binding["thread_id"]


async def test_new_worker_claims_queued_work_but_never_replays_expired_running_work(
    env_with_provider, monkeypatch
):
    client, server, auth, binding, base = await _setup(env_with_provider)
    runtime = server.app_runtime.resident_conversations
    await runtime.close()
    repo = server.services.resident_request_repo
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    timestamp = now_ms()
    repo.create(owner, binding["thread_id"], "crashed", "crashed-turn", "Old", timestamp)
    assert repo.mark_running("crashed-turn", "dead-worker", timestamp - 1000, timestamp - 1)
    # Simulate a persisted partial response from a process that never shut down cleanly.
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE resident_requests SET reply='Before crash' WHERE turn_id='crashed-turn'"
        )
    invocations = []

    async def stream(message):
        invocations.append(message)
        yield {"type": "token", "content": "Recovered queue"}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    replacement = ResidentConversationRuntime(server.services, runtime.registry, runtime.gateway)
    server.app_runtime.resident_conversations = replacement
    try:
        await replacement.start()
        interrupted = await _wait_status(client, auth, base, "crashed", "interrupted")
        assert interrupted["reply"] == "Before crash" and not invocations
        assert not repo.update_reply("crashed-turn", "dead-worker", "Late", now_ms())
        await replacement.close()
        accepted = await client.post(
            f"{base}/messages", headers=auth, json={"request_id": "queued", "prompt": "New"}
        )
        assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
        await replacement.start()
        completed = await _wait_status(client, auth, base, "queued", "completed")
        assert completed["reply"] == "Recovered queue" and len(invocations) == 1
    finally:
        await replacement.close()
        server.app_runtime.resident_conversations = runtime


async def test_shutdown_waits_for_an_inflight_database_claim_before_finishing(
    env_with_provider, monkeypatch
):
    client, server, auth, binding, base = await _setup(env_with_provider)
    runtime = server.app_runtime.resident_conversations
    await runtime.close()
    repo = server.services.resident_request_repo
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    row, _ = repo.create(owner, binding["thread_id"], "claim", "claim-turn", "Hello", now_ms())
    entered, release = threading.Event(), threading.Event()
    original = repo.mark_running

    def slow_claim(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(repo, "mark_running", slow_claim)
    task = asyncio.create_task(runtime._execute(row))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(task, 5)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    result = await _wait_status(client, auth, base, "claim", "interrupted")
    assert result["reply"] == ""


async def test_lease_failure_stops_execution_and_unknown_provider_error_stays_readable(
    env_with_provider, monkeypatch
):
    client, server, auth, binding, base = await _setup(env_with_provider)
    entered = asyncio.Event()

    async def stream(_message):
        entered.set()
        yield {"type": "token", "content": "Partial"}
        await asyncio.Event().wait()

    def fail_renew(*args):
        raise OSError("database temporarily unavailable")

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    with monkeypatch.context() as patch:
        patch.setattr(server.services.resident_request_repo, "renew", fail_renew)
        accepted = await client.post(
            f"{base}/messages", headers=auth, json={"request_id": "lease", "prompt": "Hello"}
        )
        assert accepted.status_code == 202
        await asyncio.wait_for(entered.wait(), 5)
        await _wait_status(client, auth, base, "lease", "interrupted")

    async def unknown_error(_message):
        yield {"type": "error", "error_code": "PROVIDER_SPECIFIC_UNKNOWN"}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", unknown_error)
    accepted = await client.post(
        f"{base}/messages", headers=auth, json={"request_id": "error", "prompt": "Again"}
    )
    assert accepted.status_code == 202
    result = await _wait_status(client, auth, base, "error", "failed")
    assert result["error_code"] == ErrorCode.INTERNAL_ERROR.value and result["error_message"]
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    original = server.services.resident_request_repo.get(owner, "error")
    projected = turn_view(replace(original, error_code="OLD_UNKNOWN_CODE"), "zh")
    assert projected.error_code == ErrorCode.INTERNAL_ERROR.value


@pytest.mark.parametrize("request_id", ["a/b", "a%2Fb", "a?b", "a#b", ".", "..", "非路径键"])
async def test_request_keys_that_cannot_be_polled_are_rejected(env_with_provider, request_id):
    client, _server, auth, _binding, base = await _setup(env_with_provider)
    response = await client.post(
        f"{base}/messages", headers=auth, json={"request_id": request_id, "prompt": "Hello"}
    )
    assert response.status_code == 422, response.text
    response = await client.post(
        f"{base}/cancel", headers=auth, json={"request_id": request_id, "turn_id": "turn"}
    )
    assert response.status_code == 422, response.text


async def test_lost_lease_does_not_dispatch_a_followup_before_the_old_graph_stops(
    env_with_provider, monkeypatch
):
    client, server, auth, _binding, base = await _setup(env_with_provider)
    release = asyncio.Event()
    entered = asyncio.Event()
    calls = []

    async def stream(message):
        calls.append(message)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        yield {"type": "token", "content": "Finished"}
        yield {"type": "done"}

    monkeypatch.setattr(server.app_runtime.gateway.processor, "iter_turn_chunks", stream)
    accepted = await client.post(
        f"{base}/messages", headers=auth, json={"request_id": "expired", "prompt": "First"}
    )
    assert accepted.status_code == 202
    await asyncio.wait_for(entered.wait(), 5)
    repo = server.services.resident_request_repo
    assert repo.interrupt_active(now_ms() + 60_000) == 1
    try:
        next_turn = await client.post(
            f"{base}/messages", headers=auth, json={"request_id": "after-expiry", "prompt": "Next"}
        )
        assert next_turn.status_code == 202 and next_turn.json()["status"] == "queued"
        # Allow more than one worker poll to run while the old graph still owns its thread.
        await asyncio.sleep(0.3)
        current = await client.get(f"{base}/requests/after-expiry", headers=auth)
        assert current.json()["status"] == "queued" and len(calls) == 1
    finally:
        release.set()
    await _wait_status(client, auth, base, "after-expiry", "completed")
    assert len(calls) == 2


async def test_runtime_rejects_a_second_writer_until_the_owner_has_stopped(env_with_provider):
    _client, server, _auth, _binding, _base = await _setup(env_with_provider)
    runtime = server.app_runtime.resident_conversations
    contender = ResidentConversationRuntime(server.services, runtime.registry, runtime.gateway)
    try:
        with pytest.raises(OctopError) as error:
            await contender.start()
        assert error.value.code == ErrorCode.RESIDENT_WORKER_CONFLICT
        assert contender._loop_task is None
        await runtime.close()
        await contender.start()
    finally:
        await contender.close()
        await runtime.start()


async def test_identity_is_available_without_a_model_and_send_reports_a_durable_failure(env):
    client, _server, auth, binding, base = await _setup(env)
    assert binding["status"] == "ready"
    accepted = await client.post(
        f"{base}/messages", headers=auth, json={"request_id": "no-model", "prompt": "Hello"}
    )
    assert accepted.status_code == 202
    failed = await _wait_status(client, auth, base, "no-model", "failed")
    assert failed["error_code"] == ErrorCode.RESIDENT_MODEL_UNAVAILABLE
