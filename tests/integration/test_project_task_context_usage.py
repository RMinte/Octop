"""Context occupancy reflects the latest request, not cumulative task billing."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.messages import AIMessage

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import compaction, tasks
from octop.infra.projects.usage import TaskContextUsage, compact_usage, save_usage
from tests.integration.test_project_task_artifacts import real_model as real_model
from tests.integration.test_project_task_compaction import conversation
from tests.integration.test_project_tasks import settled
from tests.support.auth import create_user, resolve_user_id


def reply(text, input_tokens, output_tokens, **kwargs):
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        **kwargs,
    )


async def test_context_usage_empty_is_unknown_and_owner_scoped(env_with_provider):
    client, server, auth = env_with_provider
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    created = tasks.send(
        server.services,
        owner,
        tasks.TaskMessage(request_id="empty-context-first", prompt="Waiting"),
    )
    task_id = created.task_id
    response = await client.get(f"/api/project-tasks/{task_id}/context-usage", headers=auth)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["source"] == "empty" and response.json()["segments"] == []
    alice = await create_user(client, auth, username="context-alice")
    for invisible in (task_id, "missing-context-task"):
        assert (
            await client.get(f"/api/project-tasks/{invisible}/context-usage", headers=alice)
        ).status_code == 404


async def test_latest_request_occupancy_is_separate_from_multi_call_billing(
    env_with_provider, real_model
):
    client, server, auth = env_with_provider
    observed, _ = real_model(
        [
            reply(
                "",
                111,
                11,
                tool_calls=[{"id": "context-read", "name": "ls", "args": {"path": "."}}],
            ),
            reply("Inspected.", 222, 22),
        ]
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "context-two-model-calls",
            "prompt": "List the current directory",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    task = await settled(client, auth, created.json()["task_id"])
    assert task["status"] == "completed", task
    assert len(observed) == 2
    turn = task["turns"][-1]
    assert (turn["input_tokens"], turn["output_tokens"]) == (333, 33)
    response = await client.get(f"/api/project-tasks/{task['task_id']}/context-usage", headers=auth)
    snapshot = response.json()
    assert response.status_code == 200, response.text
    assert snapshot["source"] == "model_request" and snapshot["estimated"] is False
    assert (snapshot["used_tokens"], snapshot["input_tokens"], snapshot["output_tokens"]) == (
        222,
        222,
        22,
    )
    assert snapshot["max_tokens"] > 0
    assert {segment["key"] for segment in snapshot["segments"]} >= {
        "system_prompt",
        "conversation",
        "tool_definitions",
    }
    # The endpoint reads runtime persistence, without a live harness or worker.
    await server.project_task_worker.close()
    again = await client.get(f"/api/project-tasks/{task['task_id']}/context-usage", headers=auth)
    assert again.json() == snapshot


async def test_active_request_snapshot_survives_cancellation_and_rejects_stale_writer(
    env_with_provider, real_model
):
    client, server, auth = env_with_provider
    gate = asyncio.Event()
    real_model(
        [
            reply(
                "",
                512,
                16,
                tool_calls=[{"id": "live-context-read", "name": "ls", "args": {"path": "."}}],
            ),
            reply("Completed", 800, 20),
        ],
        gate,
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "live-context-first",
            "prompt": "Inspect the workspace",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    task = created.json()
    url = f"/api/project-tasks/{task['task_id']}/context-usage"
    try:
        for _ in range(200):
            snapshot = (await client.get(url, headers=auth)).json()
            if snapshot["source"] != "empty":
                break
            await asyncio.sleep(0.01)
        assert snapshot["input_tokens"] == 512
        turn = server.services.project_task_turn_repo.get(task["active_turn_id"])
        assert turn is not None
        owner = await resolve_user_id(client, auth, "admin")
        with pytest.raises(OctopError) as stale:
            await asyncio.to_thread(
                save_usage,
                server.services,
                owner,
                replace(turn, worker_id="stale-context-worker"),
                TaskContextUsage(used_tokens=999),
            )
        assert stale.value.code == ErrorCode.TASK_INTERRUPTED
        assert (await client.get(url, headers=auth)).json() == snapshot
        response = await client.post(
            f"/api/project-tasks/{task['task_id']}/cancel",
            headers=auth,
            json={"turn_id": turn.turn_id},
        )
        assert response.status_code == 200, response.text
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "cancelled", done
        assert (await client.get(url, headers=auth)).json() == snapshot
        await server.project_task_worker.close()
        tasks.send(
            server.services,
            owner,
            tasks.TaskMessage(request_id="queued-after-context-cancel", prompt="Continue"),
            task_id=task["task_id"],
        )
        assert (await client.get(url, headers=auth)).json() == snapshot
    finally:
        gate.set()


async def test_manual_compaction_updates_and_persists_estimated_occupancy(
    env_with_provider, monkeypatch, real_model
):
    client, server, auth = env_with_provider
    real_model([reply(f"Long reply {i} " + "history " * 500, 10_000, 100) for i in range(4)])
    task = await conversation(client, auth, 4)
    base = f"/api/project-tasks/{task['task_id']}"
    before = (await client.get(base + "/context-usage", headers=auth)).json()
    assert before["used_tokens"] == 10_000 and before["estimated"] is False
    monkeypatch.setattr(
        SummarizationMiddleware, "_acreate_summary", AsyncMock(return_value="Earlier exchanges.")
    )
    response = await client.post(
        base + "/messages",
        headers=auth,
        json={"request_id": "shrink-context-on-demand", "prompt": "/compact"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    after = (await client.get(base + "/context-usage", headers=auth)).json()
    assert 0 < after["used_tokens"] < before["used_tokens"]
    assert after["estimated"] is True and after["output_tokens"] == 0
    # The effective message stamp carries the new estimate as well, so a later
    # queued turn does not resurrect the old pre-compaction occupancy.
    context = server.services.project_task_turn_repo.context(task["task_id"])
    assert context is not None
    assert '"estimated": true' in context[1]
    path = server.services.paths.root / "project-tasks" / task["task_id"] / "context-usage.json"
    assert json.loads(path.read_text())["used_tokens"] == after["used_tokens"]


async def test_compaction_cancelled_before_publication_preserves_context_and_snapshot(
    env_with_provider, monkeypatch, real_model
):
    client, server, auth = env_with_provider
    real_model([reply(f"Long reply {i} " + "history " * 500, 10_000, 100) for i in range(4)])
    task = await conversation(client, auth, 4)
    services = server.services
    task_id = task["task_id"]
    before_context = services.project_task_turn_repo.context(task_id)
    url = f"/api/project-tasks/{task_id}/context-usage"
    before_snapshot = (await client.get(url, headers=auth)).json()
    monkeypatch.setattr(
        SummarizationMiddleware, "_acreate_summary", AsyncMock(return_value="Earlier exchanges.")
    )
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    publish = compaction.save_usage

    def wait_before_publish(*args, **kwargs):
        try:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("compaction publication gate timed out")
            return publish(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(compaction, "save_usage", wait_before_publish)
    response = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={"request_id": "cancel-before-compact-publication", "prompt": "/compact"},
    )
    assert response.status_code == 202, response.text
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert services.project_task_turn_repo.context(task_id) == before_context
        cancelled = await client.post(
            f"/api/project-tasks/{task_id}/cancel",
            headers=auth,
            json={"turn_id": response.json()["active_turn_id"]},
        )
        assert cancelled.status_code == 200, cancelled.text
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        done = await settled(client, auth, task_id)
        assert done["status"] == "cancelled", done
        assert services.project_task_turn_repo.context(task_id) == before_context
        assert (await client.get(url, headers=auth)).json() == before_snapshot
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 5)


def test_zero_reduction_compaction_clears_prior_model_output_usage():
    before = TaskContextUsage(
        max_tokens=10_000,
        used_tokens=900,
        input_tokens=900,
        output_tokens=5,
        source="model_request",
        estimated=False,
    )
    after = compact_usage(before, 0)
    assert after.estimated is True and after.output_tokens == 0
    assert (after.max_tokens, after.used_tokens, after.input_tokens) == (10_000, 900, 900)
    assert after.source == "model_request" and before.output_tokens == 5


async def test_root_request_snapshot_is_available_while_delegated_model_waits(
    env_with_provider, real_model
):
    client, server, auth = env_with_provider
    gate = asyncio.Event()
    observed, _ = real_model(
        [
            reply(
                "",
                1234,
                12,
                tool_calls=[
                    {
                        "id": "delegate-context-work",
                        "name": "task",
                        "args": {
                            "subagent_type": "engineering-code-reviewer",
                            "description": "Inspect the task context",
                        },
                    }
                ],
            ),
            reply("Delegated work complete", 4000, 40),
            reply("Root complete", 1500, 15),
        ],
        gate,
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "context-while-delegating",
            "prompt": "Delegate the review",
            "options": {
                "action_timeline_enabled": False,
                "subagents": ["engineering-code-reviewer"],
                "team_enabled": False,
            },
        },
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    try:
        for _ in range(200):
            snapshot = (
                await client.get(f"/api/project-tasks/{task_id}/context-usage", headers=auth)
            ).json()
            if snapshot["source"] != "empty":
                break
            await asyncio.sleep(0.01)
        assert len(observed) == 1
        assert snapshot["source"] == "model_request" and snapshot["estimated"] is False
        assert (snapshot["input_tokens"], snapshot["output_tokens"]) == (1234, 12)
        turn = server.services.project_task_turn_repo.get(response.json()["active_turn_id"])
        assert turn is not None and turn.status == "running"
    finally:
        gate.set()
        done = await settled(client, auth, task_id)
    assert done["status"] == "completed", done
