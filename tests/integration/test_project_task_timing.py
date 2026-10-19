"""The HTTP turn exposes the worker's recorded wall-time boundaries."""

import asyncio
from datetime import UTC, datetime

from octop.infra.projects import runtime, tasks
from tests.integration.test_project_tasks import settled


async def test_http_turn_timing_excludes_first_queue_and_remains_stable_after_completion(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    await server.project_task_worker.close()
    worker = server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    clock = [1_000]
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def fake_run(services, owner, turn, history):
        calls.append(turn.turn_id)
        entered.set()
        await finish.wait()
        yield {"type": "token", "content": "Timing verified"}

    monkeypatch.setattr(runtime, "run_turn", fake_run)
    monkeypatch.setattr(runtime, "now_ms", lambda: clock[0])
    monkeypatch.setattr(tasks, "now_ms", lambda: clock[0])
    body = {"request_id": "timing-first", "prompt": "Verify turn timing"}
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 202, response.text
    queued = response.json()
    turn = queued["turns"][0]
    assert turn["status"] == "queued"
    assert turn["started_at"] is turn["finished_at"] is turn["processing_duration_ms"] is None
    base = f"/api/project-tasks/{queued['task_id']}"

    clock[0] = 5_000
    await worker.start()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        running = (await client.get(base, headers=auth)).json()["turns"][0]
        assert running["status"] == "running"
        assert datetime.fromisoformat(running["started_at"]) == datetime.fromtimestamp(5, UTC)
        assert running["finished_at"] is running["processing_duration_ms"] is None
        clock[0] = 12_000
        finish.set()
        done = await settled(client, auth, queued["task_id"])
        completed = done["turns"][0]
        assert completed["status"] == "completed" and completed["reply"] == "Timing verified"
        assert completed["started_at"] == running["started_at"]
        assert datetime.fromisoformat(completed["finished_at"]) == datetime.fromtimestamp(12, UTC)
        assert completed["processing_duration_ms"] == 7_000

        clock[0] = 20_000
        assert (await client.get(base, headers=auth)).json() == done
        replay = await client.post("/api/project-tasks", headers=auth, json=body)
        assert replay.status_code == 202 and replay.json() == done
        assert calls == [completed["turn_id"]]
    finally:
        finish.set()
