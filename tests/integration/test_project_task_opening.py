"""Display notifications never gate real work, approval, or cold resume."""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from octop.infra.projects import runtime
from tests.integration.test_project_capabilities import follow_up
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_task_planning import planning_models, tool_call
from tests.integration.test_project_tasks import settled


@pytest.fixture
def opening_harness(env_with_provider, monkeypatch):
    def install(responses, *, approval_required=False):
        effects = []
        observed, agents = planning_models(monkeypatch, responses)
        build = runtime.HarnessAgent

        async def record_fact(value: str) -> str:
            effects.append(value)
            return "Recorded: " + value

        def create(cfg, **kwargs):
            cfg.tools = [
                *(cfg.tools or []),
                StructuredTool.from_function(
                    name="record_fact",
                    description="Record the requested fact.",
                    coroutine=record_fact,
                ),
            ]
            if approval_required:
                cfg.interrupt_on = {
                    **(cfg.interrupt_on or {}),
                    "record_fact": {"allowed_decisions": ["approve", "reject"]},
                }
            return build(cfg, **kwargs)

        monkeypatch.setattr(runtime, "HarnessAgent", create)
        return effects, observed, agents

    return *env_with_provider, install


async def start(env):
    client, _, auth, _ = env
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "Record the requested fact.",
            "options": {"action_timeline_enabled": True},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def notification(call_id="opening", text="I will record the requested fact."):
    return tool_call("message_notify_user", call_id, text=text)


def work(call_id="work", value="proof"):
    return tool_call("record_fact", call_id, value=value)


def tool_block(turn, call_id):
    return next(
        block
        for block in turn["content_blocks"]
        if block["type"] == "tool" and block["tool_call_id"] == call_id
    )


def assert_not_dispatched(block):
    assert block["status"] == "failed"
    assert block["started_at"] is None
    assert block["finished_at"] is None
    assert block["duration_ms"] is None


@pytest.mark.parametrize("approval_required", [False, True])
async def test_work_without_a_model_notice_reaches_execution_or_approval(
    opening_harness, approval_required
):
    client, server, auth, install = opening_harness
    effects, observed, _ = install(
        [work(), AIMessage(content="Recorded.")], approval_required=approval_required
    )
    created = await start(opening_harness)
    if approval_required:
        await parked(server, created["active_turn_id"])
        waiting = (
            await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)
        ).json()
        assert waiting["status"] == "waiting_approval" and effects == []
        item = waiting["turns"][0]["interactions"][0]
        assert item["payload"]["arguments"] == {"value": "proof"}
        assert (await respond(opening_harness, waiting, item, approval(item))).status_code == 200
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == ["proof"]
    turn = done["turns"][0]
    block = tool_block(turn, "work")
    assert block["status"] == "completed" and block["arguments"] == {"value": "proof"}
    assert block["brief"] is None
    result = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "work"
    )
    assert result.status == "success"
    root_requests = [tools for role, _, tools in observed if role == "project-task"]
    assert len(root_requests) == 2 and all(
        "message_notify_user" not in tools for tools in root_requests
    )


async def test_existing_notice_and_work_in_one_response_run_without_a_text_retry(opening_harness):
    client, _, auth, install = opening_harness
    mixed = AIMessage(
        content="",
        tool_calls=[*notification().tool_calls, *work("same-response").tool_calls],
    )
    effects, observed, _ = install([mixed, AIMessage(content="Recorded once.")])
    created = await start(opening_harness)
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == ["proof"]
    turn = done["turns"][0]
    assert tool_block(turn, "same-response")["status"] == "completed"
    notices = [
        block
        for block in turn["content_blocks"]
        if block["type"] == "progress" and block["tool_call_id"] == "opening"
    ]
    assert len(notices) == 1
    tools = [message for message in observed[-1][1] if isinstance(message, ToolMessage)]
    assert {message.tool_call_id for message in tools} == {"opening", "same-response"}
    assert all(message.status == "success" for message in tools)
    assert len(observed) == 2


async def test_invalid_existing_notice_does_not_block_following_work(opening_harness):
    client, _, auth, install = opening_harness
    effects, observed, _ = install([notification(text=" "), work(), AIMessage(content="Recorded.")])
    created = await start(opening_harness)
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == ["proof"]
    turn = done["turns"][0]
    assert tool_block(turn, "work")["status"] == "completed"
    failure = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "opening"
    )
    assert failure.status == "error"
    assert not any(
        block["type"] == "progress" and block["tool_call_id"] == "opening"
        for block in turn["content_blocks"]
    )


async def test_cold_approval_without_model_notice_dispatches_once(opening_harness):
    client, server, auth, install = opening_harness
    effects, _, agents = install(
        [work(), AIMessage(content="Approved work completed.")], approval_required=True
    )
    created = await start(opening_harness)
    await parked(server, created["active_turn_id"])
    task = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    turn = task["turns"][0]
    assert effects == [] and task["status"] == "waiting_approval"
    assert tool_block(turn, "work")["started_at"] is None
    item = turn["interactions"][0]
    decision = approval(item)
    await server.project_task_worker.close()
    assert (await respond(opening_harness, task, item, decision)).status_code == 200
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == ["proof"] and len(agents) == 2
    final = done["turns"][0]
    assert tool_block(final, "work")["status"] == "completed"
    assert len([b for b in final["content_blocks"] if b["type"] == "tool"]) == 1
    assert (await respond(opening_harness, task, item, decision)).status_code == 200
    assert effects == ["proof"]


async def test_follow_up_work_keeps_turns_isolated_without_model_notices(opening_harness):
    client, _, auth, install = opening_harness
    effects, _, _ = install(
        [
            work(value="first"),
            AIMessage(content="First done."),
            work(value="second"),
            AIMessage(content="Second done."),
        ]
    )
    created = await start(opening_harness)
    first = await settled(client, auth, created["task_id"])
    assert first["status"] == "completed" and effects == ["first"]
    done = await follow_up(
        client,
        auth,
        created["task_id"],
        prompt="Record the second fact.",
        options={"action_timeline_enabled": True},
    )
    assert done["status"] == "completed", done
    assert effects == ["first", "second"]
    assert done["turns"][0]["content_blocks"] == first["turns"][0]["content_blocks"]
    second = done["turns"][1]
    assert tool_block(second, "work")["status"] == "completed"
    assert tool_block(second, "work")["output"] == "Recorded: second"
