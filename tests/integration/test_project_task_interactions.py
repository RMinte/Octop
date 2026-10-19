"""Human decisions and cooperative control resume real on-disk Harness graphs."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.i18n import tr
from octop.infra.agents.security.policy_store import SecuritySettingsStore
from octop.infra.projects import interactions, runtime
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected
from tests.support.auth import create_user


@pytest.fixture
async def interaction_env(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    effects, generations = [], []
    entered, released = asyncio.Event(), asyncio.Event()
    policy = SecuritySettingsStore(settings_repo=server.services.settings_repo)
    value = policy.load().to_dict()
    value["hitl"] = {"enabled": True, "tools": ["guarded_action", "guarded_second"]}
    policy.save(value)

    async def action(value: str) -> str:
        effects.append(value)
        if value == "blocking":
            entered.set()
            await released.wait()
        return "executed:" + value

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            generations.append(messages)
            if isinstance(messages[-1], HumanMessage):
                prompt = messages[-1].content
                if prompt == "ordinary":
                    message = AIMessage(content="Finished ordinary")
                elif prompt == "questions":
                    message = AIMessage(
                        content="Before questions. ",
                        tool_calls=[
                            {
                                "id": "ask-1",
                                "name": "ask_user_question",
                                "args": {
                                    "questions": [
                                        {
                                            "question": "Which format?",
                                            "options": [{"label": "Markdown"}, {"label": "PDF"}],
                                        },
                                        {"question": "Any extra instructions?"},
                                    ]
                                },
                            }
                        ],
                    )
                else:
                    message = AIMessage(
                        content="Before approval. ",
                        tool_calls=[
                            {
                                "id": "action-" + str(i),
                                "name": name,
                                "args": {"value": "blocking" if prompt == "blocking" else str(i)},
                            }
                            for i, name in enumerate(
                                ["guarded_action", "guarded_second"]
                                if prompt == "approval-batch"
                                else ["guarded_action"]
                            )
                        ],
                    )
            else:
                assert isinstance(messages[-1], ToolMessage)
                message = AIMessage(content="After waiting.")
            return ChatResult(generations=[ChatGeneration(message=message)])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return Model(responses=[])

    def create(cfg, **kwargs):
        cfg.tools = [
            *cfg.tools,
            *[
                StructuredTool.from_function(name=name, description="Test action", coroutine=action)
                for name in ["guarded_action", "guarded_second"]
            ],
        ]
        return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

    monkeypatch.setattr(runtime, "HarnessAgent", create)
    yield client, server, auth, effects, generations, entered, released
    released.set()


async def start(env, prompt):
    client, server, auth, *_ = env
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": prompt,
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    task = response.json()
    await parked(server, task["active_turn_id"])
    detail = (await client.get("/api/project-tasks/" + task["task_id"], headers=auth)).json()
    return detail, detail["turns"][0]


def body(item, response, request_id=None):
    return {
        "request_id": request_id or str(uuid4()),
        "expected_revision": item["revision"],
        "response": response,
    }


async def respond(env, task, item, payload):
    client, _, auth, *_ = env
    return await client.post(
        f"/api/project-tasks/{task['task_id']}/turns/{item['turn_id']}/interactions/{item['interaction_id']}/respond",
        headers=auth,
        json=payload,
    )


def approval(item, decision="allow_once"):
    return body(
        item,
        {
            "kind": "approval",
            "decision": decision,
            "action_digest": item["payload"]["action_digest"],
        },
    )


async def test_parallel_approvals_release_slot_and_survive_restart(interaction_env):
    env = interaction_env
    client, server, auth, effects, generations, *_ = env
    task, turn = await start(env, "approval-batch")
    assert task["status"] == turn["status"] == "waiting_approval"
    items = turn["interactions"]
    assert len(items) == 2 and effects == []
    ordinary = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": str(uuid4()), "prompt": "ordinary"},
        )
    ).json()
    assert (await settled(client, auth, ordinary["task_id"]))["status"] == "completed"
    await server.project_task_worker.close()
    first = approval(items[0])
    assert (await respond(env, task, items[0], first)).status_code == 200
    assert (await respond(env, task, items[0], first)).status_code == 200
    assert (await respond(env, task, items[1], approval(items[1]))).status_code == 200
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed", final
    assert sorted(effects) == ["0", "1"]
    assert len(final["turns"]) == 1 and final["turns"][0]["turn_id"] == turn["turn_id"]
    assert final["turns"][0]["reply"] == "After waiting."
    assert final["turns"][0]["reasoning"].count("Before approval. ") == 1
    assert all(item["dispatch_started"] for item in final["turns"][0]["interactions"])
    assert (await respond(env, task, items[0], first)).status_code == 200
    assert len(generations) == 3


async def test_question_answers_are_information_and_commands_are_idempotent(interaction_env):
    env = interaction_env
    client, _, auth, effects, generations, *_ = env
    task, turn = await start(env, "questions")
    assert task["status"] == "waiting_answer"
    first, second = turn["interactions"]
    answer = body(first, {"kind": "question", "action": "answer", "answer": "Custom text"})
    assert (await respond(env, task, first, answer)).status_code == 200
    assert (await respond(env, task, first, answer)).status_code == 200
    conflict = await respond(
        env, task, first, {**answer, "response": {"kind": "question", "action": "skip"}}
    )
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "TASK_REQUEST_CONFLICT"
    )
    assert len(generations) == 1
    assert (
        await respond(env, task, second, body(second, {"kind": "question", "action": "skip"}))
    ).status_code == 200
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed" and effects == []
    tool = next(m for m in generations[-1] if isinstance(m, ToolMessage))
    value = json.loads(tool.content)
    assert value["authorization"] is False
    assert [a["result"]["outcome"] for a in value["answers"]] == ["answered", "skipped"]
    assert value["answers"][0]["result"]["answer"] == "Custom text"


@pytest.mark.parametrize("default_source", ["user_default", "timeout"])
async def test_question_defaults_timeout_and_late_reply(interaction_env, default_source):
    env = interaction_env
    client, server, auth, _, generations, *_ = env
    task, turn = await start(env, "questions")
    first, second = turn["interactions"]
    if default_source == "user_default":
        assert (
            await respond(
                env, task, first, body(first, {"kind": "question", "action": "use_default"})
            )
        ).status_code == 200
    with server.services.db.transaction() as conn:
        current = server.services.project_task_turn_repo.get_in_transaction(conn, turn["turn_id"])
        items = interactions.load(current)
        if default_source == "timeout":
            items[0].expires_at = items[0].created_at
        items[1].expires_at = items[1].created_at
        server.services.project_task_turn_repo.save_interactions(
            conn, turn["turn_id"], interactions.dump(items), runtime.now_ms()
        )
    final = await settled(client, auth, task["task_id"])
    resolutions = [i["resolution"] for i in final["turns"][0]["interactions"]]
    assert resolutions[0] == {
        "outcome": "defaulted",
        "source": default_source,
        "answer": "Markdown",
    }
    assert resolutions[1] == {"outcome": "unanswered", "source": "timeout", "answer": None}
    assert (
        await respond(
            env,
            task,
            second,
            body(second, {"kind": "question", "action": "answer", "answer": "late"}),
        )
    ).status_code == 409
    assert len(generations) == 2


@pytest.mark.parametrize("decision", ["deny", "expire"])
async def test_denial_and_expiry_cannot_become_permission_after_policy_change(
    interaction_env, decision
):
    env = interaction_env
    client, server, auth, effects, *_ = env
    task, turn = await start(env, "approval")
    item = turn["interactions"][0]
    await server.project_task_worker.close()
    policy = SecuritySettingsStore(settings_repo=server.services.settings_repo)
    value = policy.load().to_dict()
    value["hitl"]["enabled"] = False
    policy.save(value)
    if decision == "deny":
        assert (await respond(env, task, item, approval(item, "deny"))).status_code == 200
    else:
        with server.services.db.transaction() as conn:
            current = server.services.project_task_turn_repo.get_in_transaction(
                conn, turn["turn_id"]
            )
            items = interactions.load(current)
            items[0].expires_at = items[0].created_at
            server.services.project_task_turn_repo.save_interactions(
                conn, turn["turn_id"], interactions.dump(items), runtime.now_ms()
            )
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed", final
    assert effects == []


async def test_pause_wait_answer_then_resume_exact_turn_over_ws(interaction_env):
    env = interaction_env
    client, server, auth, effects, *_ = env
    task, turn = await start(env, "questions")
    session, ws = await connected(client, auth)
    try:
        payload = {"turn_id": turn["turn_id"], "request_id": "pause-original"}
        receipt = await command(
            ws, "task.pause", "pause-command", task_id=task["task_id"], data=payload
        )
        assert receipt["data"]["status"] == "paused"
        for item in turn["interactions"]:
            receipt = await command(
                ws,
                "task.interaction.respond",
                str(uuid4()),
                task_id=task["task_id"],
                turn_id=turn["turn_id"],
                interaction_id=item["interaction_id"],
                data=body(item, {"kind": "question", "action": "use_default"}),
            )
            assert receipt["type"] == "result"
        await asyncio.sleep(0.05)
        assert effects == []
        receipt = await command(
            ws,
            "task.resume",
            "resume-command",
            task_id=task["task_id"],
            data={"turn_id": turn["turn_id"], "request_id": "resume-original"},
        )
        assert receipt["type"] == "result"
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed" and len(final["turns"]) == 1
        # A repeated old pause receipt never pauses a completed or later turn.
        assert (
            await command(ws, "task.pause", "pause-repeated", task_id=task["task_id"], data=payload)
        )["data"]["status"] == "completed"
    finally:
        await session.__aexit__(None, None, None)


async def test_manual_pause_before_model_and_after_started_tool(interaction_env):
    env = interaction_env
    client, server, auth, effects, _, entered, released = env
    await server.project_task_worker.close()
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    task = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": str(uuid4()), "prompt": "ordinary"},
        )
    ).json()
    base = "/api/project-tasks/" + task["task_id"]
    turn_id = task["active_turn_id"]
    assert (
        await client.post(
            base + "/pause",
            headers=auth,
            json={"turn_id": turn_id, "request_id": "pause-before-model"},
        )
    ).status_code == 200
    await server.project_task_worker.start()
    await parked(server, turn_id)
    assert (await client.get(base, headers=auth)).json()["status"] == "paused"
    assert (
        await client.post(
            base + "/resume",
            headers=auth,
            json={"turn_id": turn_id, "request_id": "resume-before-model"},
        )
    ).status_code == 200
    assert (await settled(client, auth, task["task_id"]))["status"] == "completed"
    task, turn = await start(env, "blocking")
    assert (
        await respond(env, task, turn["interactions"][0], approval(turn["interactions"][0]))
    ).status_code == 200
    await asyncio.wait_for(entered.wait(), 3)
    base = "/api/project-tasks/" + task["task_id"]
    assert (
        await client.post(
            base + "/pause",
            headers=auth,
            json={"turn_id": turn["turn_id"], "request_id": "pause-during-tool"},
        )
    ).status_code == 200
    released.set()
    await parked(server, turn["turn_id"])
    assert effects == ["blocking"]
    assert (await client.get(base, headers=auth)).json()["status"] == "paused"
    assert (
        await client.post(
            base + "/resume",
            headers=auth,
            json={"turn_id": turn["turn_id"], "request_id": "resume-after-tool"},
        )
    ).status_code == 200
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed", final
    assert effects == ["blocking"]


async def test_ownership_revision_digest_cancel_and_late_decision(interaction_env):
    env = interaction_env
    client, server, auth, effects, *_ = env
    task, turn = await start(env, "approval")
    item = turn["interactions"][0]
    alice = await create_user(client, auth, username="alice")
    assert (await respond((client, server, alice), task, item, approval(item))).status_code == 404
    wrong = approval(item)
    wrong["expected_revision"] += 1
    assert (await respond(env, task, item, wrong)).status_code == 409
    wrong = approval(item)
    wrong["response"]["action_digest"] = "different"
    assert (await respond(env, task, item, wrong)).status_code == 409
    assert (
        await respond(env, task, item, body(item, {"kind": "question", "action": "skip"}))
    ).status_code == 409
    base = "/api/project-tasks/" + task["task_id"]
    cancelled = await client.post(base + "/cancel", headers=auth, json={"turn_id": turn["turn_id"]})
    assert cancelled.json()["turns"][0]["interactions"][0]["status"] == "cancelled"
    assert (await respond(env, task, item, approval(item))).status_code == 409
    assert (
        await client.post(
            base + "/resume",
            headers=auth,
            json={"turn_id": turn["turn_id"], "request_id": "resume-cancelled"},
        )
    ).status_code == 409
    assert effects == []


async def test_separate_parallel_tool_interrupts_resume_by_native_identity(
    interaction_env, monkeypatch
):
    from langchain.agents.middleware import AgentMiddleware
    from langgraph.types import interrupt

    original = runtime.HarnessAgent

    class Gate(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            call = request.tool_call
            if call["name"].startswith("guarded_"):
                response = interrupt(
                    {
                        "action_requests": [{"name": call["name"], "args": call["args"]}],
                        "review_configs": [
                            {
                                "action_name": call["name"],
                                "allowed_decisions": ["approve", "reject"],
                            }
                        ],
                    }
                )
                if response["decisions"][0]["type"] != "approve":
                    return ToolMessage(content="Denied", tool_call_id=call["id"], status="error")
            return await handler(request)

    def create(cfg, **kwargs):
        cfg.interrupt_on = {
            name: value
            for name, value in cfg.interrupt_on.items()
            if not name.startswith("guarded_")
        }
        cfg.middleware = [*cfg.middleware, Gate()]
        return original(cfg, **kwargs)

    monkeypatch.setattr(runtime, "HarnessAgent", create)
    client, server, auth, effects, *_ = interaction_env
    task, turn = await start(interaction_env, "approval-batch")
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert len(json.loads(saved.pause_json)["groups"]) == 2
    first, second = turn["interactions"]
    assert (await respond(interaction_env, task, first, approval(first))).status_code == 200
    assert effects == []
    assert (await respond(interaction_env, task, second, approval(second))).status_code == 200
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed", final
    assert sorted(effects) == ["0", "1"]


async def test_another_device_cannot_overwrite_a_saved_decision(interaction_env):
    env = interaction_env
    client, server, auth, effects, *_ = env
    task, turn = await start(env, "approval-batch")
    first, second = turn["interactions"]
    results = await asyncio.gather(
        respond(env, task, first, approval(first)),
        respond(env, task, first, approval(first, "deny")),
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    await client.post(
        "/api/project-tasks/" + task["task_id"] + "/cancel",
        headers=auth,
        json={"turn_id": turn["turn_id"]},
    )
    assert effects == []


async def test_question_budget_survives_multiple_native_waits(env_with_provider, monkeypatch):
    from tests.integration.test_project_capabilities import harness_models

    questions = {"questions": [{"question": "One?"}, {"question": "Two?"}]}
    observed, _ = harness_models(
        monkeypatch,
        [
            AIMessage(
                content="First. ",
                tool_calls=[{"id": "ask-first", "name": "ask_user_question", "args": questions}],
            ),
            AIMessage(
                content="More. ",
                tool_calls=[{"id": "ask-second", "name": "ask_user_question", "args": questions}],
            ),
            AIMessage(content="Done."),
        ],
    )
    task, turn = await start(env_with_provider, "questions")
    client, server, auth = env_with_provider
    for item in turn["interactions"]:
        assert (
            await respond(
                env_with_provider, task, item, body(item, {"kind": "question", "action": "skip"})
            )
        ).status_code == 200
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed", final
    assert len(final["turns"][0]["interactions"]) == 2
    results = [message for message in observed[-1][1] if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in results] == ["ask-first", "ask-second"]
    assert results[-1].status == "error"
    assert results[-1].content == tr("projects.question_limit", "zh")
    assert "User rejected" not in results[-1].content
    block = next(
        block
        for block in final["turns"][0]["content_blocks"]
        if block["type"] == "tool" and block["tool_call_id"] == "ask-second"
    )
    assert block["status"] == "failed" and block["started_at"] is None
    assert block["output"] == results[-1].content
