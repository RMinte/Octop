"""A cancelled sidecar's database thread cannot erase an answer already streaming."""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

from octop.infra.projects import runtime
from tests.integration.test_project_task_narration import visible
from tests.integration.test_project_tasks import settled


async def test_delayed_narration_database_write_cannot_overwrite_first_answer(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    attempted, answer_release = asyncio.Event(), asyncio.Event()
    database_release, database_done = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    repo = server.services.project_task_turn_repo
    original = repo.progress
    results = []
    late = "This cancelled narration must never replace the answer."

    def progress(*args, **kwargs):
        if kwargs.get("require_active"):
            loop.call_soon_threadsafe(attempted.set)
            assert database_release.wait(5)
            try:
                result = original(*args, **kwargs)
                results.append(result)
                return result
            finally:
                database_done.set()
        return original(*args, **kwargs)

    async def narrate(self, snapshot):
        return late

    async def run(services, owner, turn, history):
        await attempted.wait()
        yield {"type": "token", "content": "First answer segment."}
        await answer_release.wait()
        yield {"type": "token", "content": " Final segment."}

    monkeypatch.setattr(repo, "progress", progress)
    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    monkeypatch.setattr(runtime, "run_turn", run)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "Explain the result."},
    )
    assert response.status_code == 202, response.text
    task = response.json()
    try:
        first = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: turn["reply"] == "First answer segment.",
        )
        turn_id = first["turns"][0]["turn_id"]
        before = repo.get(turn_id)
        assert before.status == "running"
        database_release.set()
        assert await asyncio.to_thread(database_done.wait, 3)
        after = repo.get(turn_id)
        assert results == [True]  # SQL rejected the cancelled thread's stale snapshot.
        assert after.reply == before.reply
        assert after.content_revision == before.content_revision
        assert after.content_blocks_json == before.content_blocks_json
        assert late not in after.content_blocks_json
        answer_release.set()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed"
        assert final["turns"][0]["reply"] == "First answer segment. Final segment."
    finally:
        database_release.set()
        answer_release.set()
