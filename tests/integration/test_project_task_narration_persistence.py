"""A cancelled sidecar's database thread cannot erase an answer already streaming."""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

from langchain_core.messages import AIMessage

from octop.infra.projects import runtime
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_task_narration import visible
from tests.integration.test_project_task_planning import planning_models, tool_call
from tests.integration.test_project_tasks import settled


async def test_delayed_narration_database_write_cannot_overwrite_first_answer(
    env_with_provider, monkeypatch
):
    from deepagents.backends.protocol import ExecuteResponse
    from octop_harness.backends.bwrap_shell import BubbledLocalShellBackend
    from octop_harness.backends.local_shell import HarnessLocalShellBackend

    client, server, auth = env_with_provider
    attempted, paused, answer_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    database_release, database_done = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    repo = server.services.project_task_turn_repo
    original = repo.progress
    results = []
    late = "This paused narration must never replace the answer."

    def progress(*args, **kwargs):
        if kwargs.get("require_active") and not attempted.is_set():
            loop.call_soon_threadsafe(attempted.set)
            assert database_release.wait(10)
            try:
                result = original(*args, **kwargs)
                results.append(result)
                return result
            finally:
                database_done.set()
        return original(*args, **kwargs)

    async def narrate(self, snapshot):
        if snapshot.key == "opening":
            await paused.wait()
            return late
        return None

    original_run = runtime.run_turn

    async def hold_real_execution(services, owner, turn, history):
        stream = original_run(services, owner, turn, history)
        try:
            async for event in stream:
                if event["type"] == "pause":
                    paused.set()
                    await attempted.wait()
                yield event
                if event["type"] == "token":
                    await answer_release.wait()
        finally:
            await stream.aclose()

    def execute(self, command, *, timeout=None):
        return ExecuteResponse(output="actual-proof", exit_code=0, truncated=False)

    monkeypatch.setattr(repo, "progress", progress)
    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    monkeypatch.setattr(runtime, "run_turn", hold_real_execution)
    monkeypatch.setattr(HarnessLocalShellBackend, "execute", execute)
    monkeypatch.setattr(BubbledLocalShellBackend, "execute", execute)
    planning_models(
        monkeypatch,
        [
            tool_call("execute", "approved-work", command="echo actual-proof"),
            AIMessage(content="First answer segment."),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "Explain the result."},
    )
    assert response.status_code == 202, response.text
    task = response.json()
    try:
        await parked(server, task["active_turn_id"])
        waiting = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
        assert waiting["status"] == "waiting_approval"
        assert not database_release.is_set() and not database_done.is_set()
        item = waiting["turns"][0]["interactions"][0]
        accepted = await respond(env_with_provider, waiting, item, approval(item))
        assert accepted.status_code == 200, accepted.text
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
        assert results == [True]  # SQL rejected the paused attempt's worker and content version.
        assert after.reply == before.reply
        assert after.content_revision == before.content_revision
        assert after.content_blocks_json == before.content_blocks_json
        assert late not in after.content_blocks_json
        answer_release.set()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed"
        assert final["turns"][0]["reply"] == "First answer segment."
    finally:
        database_release.set()
        answer_release.set()
