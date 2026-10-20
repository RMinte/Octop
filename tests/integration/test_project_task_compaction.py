"""Task compaction preserves effective model context and the public transcript."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from deepagents.middleware import summarization
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import compaction, runtime, tasks
from tests.integration.test_project_task_artifacts import real_model as real_model
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected
from tests.support.auth import create_user, resolve_user_id


def context_messages(server, task_id):
    saved = server.services.project_task_turn_repo.context(task_id)
    assert saved is not None
    return messages_from_dict(json.loads(saved[1]))


async def conversation(client, auth, count):
    task_id = None
    for index in range(count):
        response = await client.post(
            f"/api/project-tasks/{task_id}/messages" if task_id else "/api/project-tasks",
            headers=auth,
            json={
                "request_id": f"compact-history-{index}",
                "prompt": f"Original detail {index}",
                "options": {"action_timeline_enabled": False},
            },
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        task = await settled(client, auth, task_id)
        assert task["status"] == "completed", task
    return task


async def test_automatic_compaction_restores_effective_context_between_turns(
    env_with_provider, monkeypatch, real_model
):
    client, server, auth = env_with_provider
    original_defaults = summarization.compute_summarization_defaults

    def low_threshold(model):
        return {
            **original_defaults(model),
            "trigger": ("messages", 4),
            "keep": ("messages", 2),
            "truncate_args_settings": None,
        }

    monkeypatch.setattr(summarization, "compute_summarization_defaults", low_threshold)
    summary = AsyncMock(return_value="The first two exchanges are summarized here.")
    monkeypatch.setattr(summarization.SummarizationMiddleware, "_acreate_summary", summary)
    observed, _ = real_model([AIMessage(content=f"Original reply {i}") for i in range(4)])
    task = await conversation(client, auth, 3)
    assert summary.await_count == 1
    saved = context_messages(server, task["task_id"])
    assert "The first two exchanges are summarized here." in saved[0].content
    assert saved[0].additional_kwargs["lc_source"] == "summarization"
    assert "Original detail 0" not in [message.content for message in saved]
    assert saved[-1].content == "Original reply 2"
    assert len(saved) < 6
    assert "The first two exchanges are summarized here." in str(observed[2])

    # The next fresh harness must use the durable summary without reconstructing
    # the original turns or requiring another summarization cycle.
    monkeypatch.setattr(summarization, "compute_summarization_defaults", original_defaults)
    response = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={
            "request_id": "after-auto-compact",
            "prompt": "Continue from the summary",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    assert summary.await_count == 1 and len(observed) == 4
    assert "The first two exchanges are summarized here." in str(observed[3])
    assert "Original detail 0" not in [message.content for message in observed[3]]
    assert [(turn["prompt"], turn["reply"]) for turn in done["turns"][:3]] == [
        (f"Original detail {i}", f"Original reply {i}") for i in range(3)
    ]
    history_file = (
        server.services.paths.root
        / "project-tasks"
        / task["task_id"]
        / "history"
        / "conversation_history"
        / f"{task['task_id']}.md"
    )
    assert "Original detail 0" in history_file.read_text()


async def test_manual_compaction_is_durable_owned_and_idempotent(
    env_with_provider, monkeypatch, real_model
):
    client, server, auth = env_with_provider
    observed, _ = real_model([AIMessage(content=f"Original reply {i}") for i in range(5)])
    capture = AsyncMock()
    monkeypatch.setattr(runtime, "capture_memory", capture)
    task = await conversation(client, auth, 4)
    base = f"/api/project-tasks/{task['task_id']}"
    before = context_messages(server, task["task_id"])
    capture_count = capture.await_count
    started, release = asyncio.Event(), asyncio.Event()

    async def summarize(messages):
        assert messages[0].content == "Original detail 0"
        started.set()
        await release.wait()
        return "The oldest exchange is summarized."

    summary = AsyncMock(side_effect=summarize)
    monkeypatch.setattr(summarization.SummarizationMiddleware, "_acreate_summary", summary)
    alice = await create_user(client, auth, username="compact-alice")
    body = {"request_id": "manual-compact-001", "prompt": "/compact"}
    assert (await client.post(base + "/messages", headers=alice, json=body)).status_code == 404
    assert summary.await_count == 0
    try:
        response = await client.post(base + "/messages", headers=auth, json=body)
        assert response.status_code == 202, response.text
        await asyncio.wait_for(started.wait(), timeout=5)
        replay = await client.post(base + "/messages", headers=auth, json=body)
        assert replay.status_code == 202
        assert replay.json()["active_turn_id"] == response.json()["active_turn_id"]
        busy = await client.post(
            base + "/messages",
            headers=auth,
            json={"request_id": "manual-compact-busy", "prompt": "/compact"},
        )
        assert busy.status_code == 409
    finally:
        release.set()
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    compact = done["turns"][-1]
    assert compact["prompt"] == "/compact" and compact["reply"]
    assert compact["status"] == "completed" and not compact["artifacts"]
    assert not any(block["type"] in {"tool", "progress"} for block in compact["content_blocks"])
    assert len(observed) == 4 and summary.await_count == 1
    assert capture.await_count == capture_count
    saved = context_messages(server, task["task_id"])
    assert len(saved) < len(before)
    assert "The oldest exchange is summarized." in saved[0].content
    assert all(message.content != "/compact" for message in saved)
    assert done["turns"][:4] == task["turns"]
    history_file = (
        server.services.paths.root
        / "project-tasks"
        / task["task_id"]
        / "history"
        / "conversation_history"
        / f"{task['task_id']}.md"
    )
    assert "Original detail 0" in history_file.read_text()

    assert (await client.post(base + "/messages", headers=auth, json=body)).status_code == 202
    assert summary.await_count == 1
    followup = await client.post(
        base + "/messages",
        headers=auth,
        json={
            "request_id": "after-manual-compact",
            "prompt": "Continue from the compacted history",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert followup.status_code == 202, followup.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed" and len(done["turns"]) == 6, done
    assert len(observed) == 5
    assert "The oldest exchange is summarized." in str(observed[4])
    assert "/compact" not in [message.content for message in observed[4]]


async def test_short_history_websocket_compact_is_a_noop_without_model_or_memory_capture(
    env_with_provider, monkeypatch, real_model
):
    client, server, auth = env_with_provider
    observed, _ = real_model([AIMessage(content="Short reply")])
    capture = AsyncMock()
    monkeypatch.setattr(runtime, "capture_memory", capture)
    task = await conversation(client, auth, 1)
    before = context_messages(server, task["task_id"])
    summary = AsyncMock(side_effect=AssertionError("short history must not be summarized"))
    monkeypatch.setattr(summarization.SummarizationMiddleware, "_acreate_summary", summary)
    session, ws = await connected(client, auth)
    payload = {"request_id": "short-ws-compact", "prompt": "/compact"}
    try:
        first = await command(
            ws, "task.message", "compact-short-first", task_id=task["task_id"], data=payload
        )
        assert first["type"] == "result", first
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "completed", done
        assert len(done["turns"]) == 2
        reply = done["turns"][-1]["reply"]
        assert "无可压缩" in reply or "Nothing to compact" in reply
        replay = await command(
            ws, "task.message", "compact-short-replay", task_id=task["task_id"], data=payload
        )
        assert replay["type"] == "result", replay
        assert len(replay["data"]["turns"]) == 2
        assert len(observed) == 1 and capture.await_count == 1
        summary.assert_not_awaited()
        assert [message.content for message in context_messages(server, task["task_id"])] == [
            message.content for message in before
        ]
    finally:
        await session.__aexit__(None, None, None)


async def test_failed_manual_compaction_keeps_history_for_next_turn(
    env_with_provider, monkeypatch, real_model
):
    client, server, auth = env_with_provider
    observed, _ = real_model([AIMessage(content=f"Original reply {i}") for i in range(5)])
    task = await conversation(client, auth, 4)
    before = server.services.project_task_turn_repo.context(task["task_id"])
    summary = AsyncMock(side_effect=RuntimeError("summary model failed: private-provider-detail"))
    monkeypatch.setattr(summarization.SummarizationMiddleware, "_acreate_summary", summary)
    base = f"/api/project-tasks/{task['task_id']}"
    response = await client.post(
        base + "/messages",
        headers=auth,
        json={"request_id": "failing-manual-compact", "prompt": "/compact"},
    )
    assert response.status_code == 202, response.text
    failed = await settled(client, auth, task["task_id"])
    assert failed["status"] == "failed", failed
    assert failed["turns"][-1]["status"] == "failed"
    assert "private-provider-detail" not in json.dumps(failed)
    assert len(observed) == 4 and summary.await_count == 1
    assert server.services.project_task_turn_repo.context(task["task_id"]) == before
    assert failed["turns"][:4] == task["turns"]
    response = await client.post(
        base + "/messages",
        headers=auth,
        json={
            "request_id": "after-failed-compact",
            "prompt": "Continue using the original history",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    assert len(observed) == 5
    assert "Original detail 0" in [message.content for message in observed[4]]
    assert "/compact" not in [message.content for message in observed[4]]


@pytest.mark.parametrize("attachment", [False, True])
async def test_compact_with_arguments_or_attachment_remains_a_normal_turn(
    env_with_provider, monkeypatch, real_model, attachment
):
    client, _, auth = env_with_provider
    observed, _ = real_model([AIMessage(content="Normal task execution")])
    summary = AsyncMock(side_effect=AssertionError("this is not a maintenance command"))
    monkeypatch.setattr(summarization.SummarizationMiddleware, "_acreate_summary", summary)
    body = {
        "request_id": "compact-literal-prompt",
        "prompt": "/compact" if attachment else "/compact explain this command",
        "options": {"action_timeline_enabled": False},
    }
    if attachment:
        uploaded = await client.post(
            "/api/project-tasks/attachments",
            headers=auth,
            files={"file": ("notes.txt", b"Attachment must be processed", "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        body["attachment_ids"] = [uploaded.json()["attachment_id"]]
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "completed", done
    assert done["turns"][0]["reply"] == "Normal task execution"
    assert len(observed) == 1
    summary.assert_not_awaited()


async def test_repeated_compaction_history_remains_readable_after_project_directory_changes(
    env_with_provider, monkeypatch, real_model, tmp_path
):
    client, server, auth = env_with_provider
    old_directory, new_directory = tmp_path / "original-workspace", tmp_path / "new-workspace"
    old_directory.mkdir()
    new_directory.mkdir()
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Compaction history", "directory": str(old_directory)},
        )
    ).json()
    read_history = AIMessage(
        content="",
        tool_calls=[{"id": "read-oldest-history", "name": "read_file", "args": {"file_path": ""}}],
    )
    observed, _ = real_model(
        [
            *[AIMessage(content=f"Original reply {i}") for i in range(5)],
            read_history,
            AIMessage(content="The original details are still available."),
        ]
    )
    summary = AsyncMock(side_effect=["First compact summary.", "Second compact summary."])
    monkeypatch.setattr(summarization.SummarizationMiddleware, "_acreate_summary", summary)
    task_id = None
    # Eight messages make the first forced compact meaningful. A later normal
    # exchange makes the already-compacted context large enough for a second.
    for index in range(5):
        body = {
            "request_id": f"repeated-compact-detail-{index}",
            "prompt": f"Original detail {index}",
            "options": {"action_timeline_enabled": False},
        }
        if task_id is None:
            body["project_id"] = project["project_id"]
        response = await client.post(
            f"/api/project-tasks/{task_id}/messages" if task_id else "/api/project-tasks",
            headers=auth,
            json=body,
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        task = await settled(client, auth, task_id)
        assert task["status"] == "completed", task
        if index in {3, 4}:
            response = await client.post(
                f"/api/project-tasks/{task_id}/messages",
                headers=auth,
                json={"request_id": f"repeated-force-compact-{index}", "prompt": "/compact"},
            )
            assert response.status_code == 202, response.text
            task = await settled(client, auth, task_id)
            assert task["status"] == "completed", task
    assert summary.await_count == 2 and len(observed) == 5
    saved = context_messages(server, task_id)
    assert "Second compact summary." in saved[0].content
    match = re.search(r"history has been saved to (\S+) should", saved[0].content)
    assert match is not None, saved[0].content
    read_history.tool_calls[0]["args"]["file_path"] = match.group(1)
    changed = await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={
            "name": "Compaction history",
            "directory": str(new_directory),
            "expected_revision": project["revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    response = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={
            "request_id": "read-repeated-compact-history",
            "prompt": "Read the full history to recover the original details",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task_id)
    assert done["status"] == "completed", done
    assert len(observed) == 7
    reread = next(
        message
        for message in observed[-1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "read-oldest-history"
    )
    assert reread.status == "success", reread.content
    assert "Original detail 0" in reread.content
    assert "Original reply 0" in reread.content
    assert "Original detail 1" in reread.content
    assert done["turns"][-1]["project_revision"] == changed.json()["revision"]


async def test_manual_compaction_records_actual_summary_model_usage(env_with_provider, real_model):
    client, server, auth = env_with_provider
    observed, _ = real_model(
        [
            *[AIMessage(content=f"Original reply {i}") for i in range(4)],
            AIMessage(
                content="Summary produced by the real summarization call.",
                response_metadata={"model_name": "test-summary-model"},
                usage_metadata={"input_tokens": 21, "output_tokens": 7, "total_tokens": 28},
            ),
        ]
    )
    task = await conversation(client, auth, 4)
    response = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={"request_id": "compact-real-summary-usage", "prompt": "/compact"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    assert len(observed) == 5
    assert done["turns"][-1]["input_tokens"] == 21
    assert done["turns"][-1]["output_tokens"] == 7
    assert "Original detail 0" in str(observed[-1])
    saved = context_messages(server, task["task_id"])
    assert "Summary produced by the real summarization call." in saved[0].content


async def test_repreparing_same_compaction_turn_replaces_partial_native_state(
    env_with_provider, monkeypatch, real_model
):
    client, server, auth = env_with_provider
    observed, _ = real_model([AIMessage(content=f"Original reply {i}") for i in range(4)])
    task = await conversation(client, auth, 4)
    services = server.services
    history = context_messages(server, task["task_id"])
    old_context = services.project_task_turn_repo.context(task["task_id"])
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    queued = tasks.send(
        services,
        owner,
        tasks.TaskMessage(request_id="compact-reprepare-same-turn", prompt="/compact"),
        task_id=task["task_id"],
    )
    turn = services.project_task_turn_repo.claim("compaction-recovery-worker", tasks.now_ms())
    assert turn is not None and turn.turn_id == queued.active_turn_id
    summary = AsyncMock(side_effect=["Interrupted compact summary.", "Reprepared compact summary."])
    monkeypatch.setattr(summarization.SummarizationMiddleware, "_acreate_summary", summary)
    native_states = []
    original_model_context = compaction.model_context

    async def inspect_native_context(agent, thread_id):
        state = await agent.protocol._graph.aget_state({"configurable": {"thread_id": thread_id}})
        native_states.append(state.values)
        return await original_model_context(agent, thread_id)

    monkeypatch.setattr(compaction, "model_context", inspect_native_context)
    with monkeypatch.context() as failed_save:
        failed_save.setattr(
            services.project_task_turn_repo, "save_context_in_transaction", lambda *args: False
        )
        with pytest.raises(OctopError) as interrupted:
            _ = [event async for event in runtime.run_turn(services, owner, turn, history)]
    assert interrupted.value.code == ErrorCode.TASK_INTERRUPTED
    assert summary.await_count == 1
    assert native_states[0]["_summarization_event"]["cutoff_index"] == 2
    assert len(native_states[0]["messages"]) == 8
    assert services.project_task_turn_repo.context(task["task_id"]) == old_context

    # Reuse the same turn and the last durable context, as a worker recovering
    # after the native event committed but before the task context was saved.
    events = [event async for event in runtime.run_turn(services, owner, turn, history)]
    assert any(event["type"] == "token" and event["content"] for event in events)
    assert summary.await_count == 2 and len(observed) == 4
    assert len(native_states[1]["messages"]) == 8
    assert native_states[1]["_summarization_event"]["cutoff_index"] == 2
    saved = context_messages(server, task["task_id"])
    assert len(saved) == 7
    assert "Reprepared compact summary." in saved[0].content
    assert [message.content for message in saved[1:]] == [
        message.content for message in history[2:]
    ]
    assert [message.content for message in native_states[1]["messages"]] == [
        message.content for message in history
    ]
