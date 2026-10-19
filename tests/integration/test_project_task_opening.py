"""Durable opening notices admit work across real Harness rounds and cold resumes."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.config import get_config

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
    return tool_call("record_fact", call_id, value=value, brief="Record the requested fact")


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
async def test_work_without_a_notice_is_rejected_before_dispatch(
    opening_harness, approval_required
):
    client, _, auth, install = opening_harness
    effects, observed, _ = install(
        [work(), AIMessage(content="The work was not executed.")],
        approval_required=approval_required,
    )
    created = await start(opening_harness)
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == []
    turn = done["turns"][0]
    assert turn["interactions"] == []
    assert_not_dispatched(tool_block(turn, "work"))
    result = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "work"
    )
    assert result.status == "error"
    assert not any(block["type"] == "progress" for block in turn["content_blocks"])


async def test_successful_notice_is_durable_before_the_next_response_can_work(opening_harness):
    client, server, auth, install = opening_harness

    def after_opening(messages):
        result = next(
            message
            for message in messages
            if isinstance(message, ToolMessage) and message.tool_call_id == "opening"
        )
        assert result.status == "success" and result.artifact["introduction"] is True
        saved = server.services.project_task_turn_repo.get(
            get_config()["configurable"]["thread_id"]
        )
        notices = [
            block for block in json.loads(saved.content_blocks_json) if block["type"] == "progress"
        ]
        assert len(notices) == 1 and notices[0]["introduction"] is True
        return work()

    effects, _, _ = install([notification(), after_opening, AIMessage(content="Recorded.")])
    created = await start(opening_harness)
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == ["proof"]
    turn = done["turns"][0]
    block = tool_block(turn, "work")
    assert block["status"] == "completed" and block["started_at"]
    assert block["arguments"] == {"value": "proof"}
    assert block["output"] == "Recorded: proof"
    assert (
        next(b for b in turn["content_blocks"] if b["type"] == "progress")["introduction"] is True
    )


async def test_notice_and_work_in_one_response_require_a_later_retry(opening_harness):
    client, _, auth, install = opening_harness
    mixed = AIMessage(
        content="",
        tool_calls=[
            *notification().tool_calls,
            *work("same-response").tool_calls,
        ],
    )
    effects, _, _ = install([mixed, work("later-response"), AIMessage(content="Recorded once.")])
    created = await start(opening_harness)
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == ["proof"]
    turn = done["turns"][0]
    assert_not_dispatched(tool_block(turn, "same-response"))
    assert tool_block(turn, "later-response")["status"] == "completed"
    notices = [block for block in turn["content_blocks"] if block["type"] == "progress"]
    assert len(notices) == 1 and notices[0]["introduction"] is True


async def test_failed_notice_does_not_admit_work(opening_harness):
    client, _, auth, install = opening_harness
    effects, observed, _ = install(
        [
            notification(text=" "),
            work(),
            AIMessage(content="The invalid notice did not admit work."),
        ]
    )
    created = await start(opening_harness)
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    assert effects == []
    turn = done["turns"][0]
    assert_not_dispatched(tool_block(turn, "work"))
    assert not any(block["type"] == "progress" for block in turn["content_blocks"])
    failure = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "opening"
    )
    assert failure.status == "error"


async def test_cold_approval_resume_reuses_only_this_turns_durable_opening(opening_harness):
    client, server, auth, install = opening_harness
    effects, _, agents = install(
        [notification(), work(), AIMessage(content="Approved work completed.")],
        approval_required=True,
    )
    created = await start(opening_harness)
    await parked(server, created["active_turn_id"])
    task = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    turn = task["turns"][0]
    notices = [block for block in turn["content_blocks"] if block["type"] == "progress"]
    assert len(notices) == 1 and notices[0]["introduction"] is True
    assert effects == [] and task["status"] == "waiting_approval"
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
    assert [block for block in final["content_blocks"] if block["type"] == "progress"] == notices
    assert tool_block(final, "work")["status"] == "completed"
    assert (await respond(opening_harness, task, item, decision)).status_code == 200
    assert effects == ["proof"]


async def test_follow_up_does_not_inherit_opening_or_replay_cache_from_prior_turn(opening_harness):
    client, _, auth, install = opening_harness
    effects, _, _ = install(
        [
            notification(text="I will record the first fact."),
            work(value="first"),
            AIMessage(content="First done."),
            work(value="second"),
            notification(text="I will now record the second fact."),
            work("new-work", "second"),
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
    assert_not_dispatched(tool_block(second, "work"))
    assert tool_block(second, "new-work")["status"] == "completed"
    notices = [block for block in second["content_blocks"] if block["type"] == "progress"]
    assert len(notices) == 1 and notices[0]["introduction"] is True
    assert notices[0]["text"] == "I will now record the second fact."


async def test_progress_persistence_failure_prevents_all_following_dispatch(
    opening_harness, monkeypatch
):
    client, _, auth, install = opening_harness
    effects, observed, _ = install([notification(), work(), AIMessage(content="Unreachable.")])
    original = runtime.progress_tool

    def failing_tool(locale, persist=None):
        async def fail(text, call_id):
            raise RuntimeError("Progress checkpoint did not confirm persistence")

        return original(locale, fail)

    monkeypatch.setattr(runtime, "progress_tool", failing_tool)
    created = await start(opening_harness)
    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "failed", done
    assert effects == [] and len(observed) == 1
    assert not any(block["tool_call_id"] == "work" for block in done["turns"][0]["content_blocks"])
