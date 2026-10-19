"""A failed heartbeat must not prevent the worker's final output checkpoint."""

import asyncio
import json

from octop.infra.projects import runtime
from tests.integration.test_project_tasks import settled


async def test_failed_heartbeat_still_finalizes_and_preserves_received_output(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    loop = asyncio.get_running_loop()
    heartbeat_failed = asyncio.Event()
    reply = "This public answer arrived before the heartbeat write failed."
    progress_calls = []
    repo = server.services.project_task_turn_repo
    original_progress = repo.progress

    def fail_first_progress(*args, **kwargs):
        progress_calls.append(args[2])
        if len(progress_calls) == 1:
            loop.call_soon_threadsafe(heartbeat_failed.set)
            raise RuntimeError("Injected heartbeat write failure")
        return original_progress(*args, **kwargs)

    async def fake_run(services, owner, turn, history):
        heartbeat = next(
            task
            for task in asyncio.all_tasks()
            if task.get_coro().__qualname__ == "ProjectTaskWorker._execute.<locals>.heartbeat"
        )
        yield {"type": "token", "content": reply}
        await asyncio.wait_for(heartbeat_failed.wait(), 2)
        # The thread's signal can arrive before its exception reaches the Task.
        # Finish the script only after that failed heartbeat has actually ended.
        done, _pending = await asyncio.wait({heartbeat}, timeout=2)
        assert done == {heartbeat}

    monkeypatch.setattr(repo, "progress", fail_first_progress)
    monkeypatch.setattr(runtime, "run_turn", fake_run)
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "heartbeat-failure", "prompt": "Preserve received public output."},
    )
    assert created.status_code == 202, created.text
    task = await settled(client, auth, created.json()["task_id"])
    turn = task["turns"][0]
    assert task["active_turn_id"] is None
    assert task["status"] == turn["status"] == "failed"
    assert turn["error_code"] == "TASK_EXECUTION_FAILED"
    assert turn["reply"] == reply
    assert progress_calls == [reply]
    text = [block for block in turn["content_blocks"] if block["type"] == "text"]
    assert len(text) == 1 and text[0]["text"] == reply
    assert text[0]["status"] == "interrupted"
    saved = repo.get(turn["turn_id"])
    assert saved.status == "failed" and saved.reply == reply
    assert json.loads(saved.content_blocks_json) == turn["content_blocks"]
