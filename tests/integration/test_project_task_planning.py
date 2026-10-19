"""Real harness planning, public progress, and durable per-turn task output."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from octop.infra.agents.security.policy_store import SecuritySettingsStore
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import runtime
from octop.infra.projects.artifacts import TaskArtifactPublisher
from tests.integration.test_project_capabilities import follow_up, harness_models
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until


def tool_call(tool_name, call_id, **args):
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": tool_name, "args": args}])


def planning_models(monkeypatch, responses):
    original = FakeMessagesListChatModel._generate

    def generate(model, messages, stop=None, run_manager=None, **kwargs):
        if getattr(model, "role", "") == "project-task":
            response = responses[model.i]
            if callable(response):
                model.responses[model.i] = response(messages)
        return original(model, messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(FakeMessagesListChatModel, "_generate", generate)
    return harness_models(
        monkeypatch,
        [
            AIMessage(content="Dynamic planning response") if callable(item) else item
            for item in responses
        ],
    )


def latest_plan(messages):
    return next(
        json.loads(message.content)
        for message in reversed(messages)
        if isinstance(message, ToolMessage)
        and message.name == "planning"
        and message.status != "error"
    )


def step_command(command, call_id, index=0, **arguments):
    def response(messages):
        plan = latest_plan(messages)
        return tool_call(
            "planning",
            call_id,
            command=command,
            expected_revision=plan["revision"],
            step_id=plan["steps"][index]["step_id"],
            **arguments,
        )

    return response


async def test_real_plan_progress_and_tool_results_restore_without_duplicates(
    env_with_provider, monkeypatch
):
    from octop.api.routers import project_tasks_ws

    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    initial = ["生成报告", "整理交付说明"]
    observed, agents = planning_models(
        monkeypatch,
        [
            tool_call("planning", "plan-create", command="create", steps=initial),
            tool_call(
                "message_notify_user", "report-progress", text="我会先生成报告，再整理交付说明。"
            ),
            tool_call("create_artifact", "report-create", name="report.md", content="实际报告内容"),
            step_command(
                "mark_step",
                "report-complete",
                status="completed",
                result="报告已生成并附加到任务。",
            ),
            step_command("start_step", "delivery-start", index=1),
            tool_call(
                "message_notify_user", "delivery-progress", text="报告已生成，正在整理交付说明。"
            ),
            step_command(
                "mark_step",
                "delivery-complete",
                index=1,
                status="completed",
                result="已整理交付说明。",
            ),
            AIMessage(content="报告已交付。"),
            AIMessage(content="这是普通追问的答案。"),
        ],
    )
    resume = asyncio.Event()
    original = runtime.run_turn

    async def paused_progress(services, owner, turn, history):
        stream = original(services, owner, turn, history)
        try:
            async for event in stream:
                yield event
                if event["type"] == "progress":
                    await resume.wait()
        finally:
            await stream.aclose()

    monkeypatch.setattr(runtime, "run_turn", paused_progress)
    session, ws = await connected(client, auth)
    try:
        created = (
            await command(
                ws,
                "task.create",
                "planning-create",
                data={"request_id": str(uuid4()), "prompt": "生成报告并整理交付说明"},
            )
        )["data"]
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "planning-watch",
                "task_id": created["task_id"],
                "cursor": created["event_cursor"],
            }
        )
        live = await until(
            ws,
            lambda frame: (
                frame["type"] == "turn"
                and any(
                    block["type"] == "progress" for block in frame["data"]["turn"]["content_blocks"]
                )
            ),
        )
        live_turn = live["data"]["turn"]
        assert live_turn["status"] == "running" and live_turn["reply"] == ""
        initial_plan = live_turn["plan"]
        assert initial_plan["source"] == "octop_planning"
        assert initial_plan["revision"] == 1 and initial_plan["plan_id"]
        assert [step["content"] for step in initial_plan["steps"]] == initial
        assert [step["status"] for step in initial_plan["steps"]] == ["in_progress", "pending"]
        assert initial_plan["active_step_id"] == initial_plan["steps"][0]["step_id"]
        assert live_turn["plan_revision"] == 1
        progress = next(
            block for block in live_turn["content_blocks"] if block["type"] == "progress"
        )
        assert progress["tool_call_id"] == "report-progress"
        assert progress["text"] == "我会先生成报告，再整理交付说明。"
        assert progress["status"] == "completed"
        assert progress["plan_id"] == initial_plan["plan_id"]
        assert progress["step_id"] == initial_plan["steps"][0]["step_id"]
        cursor = live["data"]["cursor"]
    finally:
        resume.set()
        await session.__aexit__(None, None, None)

    done = await settled(client, auth, created["task_id"])
    assert done["status"] == "completed", done
    turn = done["turns"][0]
    blocks = turn["content_blocks"]
    plans = [block["plan"] for block in blocks if block["type"] == "plan"]
    assert [plan["revision"] for plan in plans] == [1, 2, 3, 4]
    assert plans[0] == initial_plan
    assert {plan["plan_id"] for plan in plans} == {initial_plan["plan_id"]}
    assert all(
        [step["step_id"] for step in plan["steps"]]
        == [step["step_id"] for step in initial_plan["steps"]]
        for plan in plans
    )
    assert turn["plan"] == plans[-1] and turn["plan_revision"] == 4
    assert all(step["status"] == "completed" and step["result"] for step in plans[-1]["steps"])
    assert all(step["started_at"] and step["finished_at"] for step in plans[-1]["steps"])
    assert plans[-1]["active_step_id"] is None
    assert turn["reply"] == "报告已交付。"
    final_text = next(block for block in blocks if block["type"] == "text")
    assert final_text["plan_id"] is None and final_text["step_id"] is None
    reports = [block for block in blocks if block["type"] == "progress"]
    assert reports[0] == progress and len(reports) == 2
    assert reports[1]["plan_id"] == initial_plan["plan_id"]
    assert reports[1]["step_id"] == initial_plan["steps"][1]["step_id"]
    tools = [block for block in blocks if block["type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool_call_id"] == "report-create"
    assert tools[0]["tool_name"] == "create_artifact" and tools[0]["status"] == "completed"
    assert tools[0]["tool_label"]
    assert tools[0]["plan_id"] == initial_plan["plan_id"]
    assert tools[0]["step_id"] == initial_plan["steps"][0]["step_id"]
    assert tools[0]["arguments"] == {"name": "report.md", "content": "实际报告内容"}
    assert json.loads(tools[0]["output"])["status"] == "attached"
    assert datetime.fromisoformat(tools[0]["finished_at"]) >= datetime.fromisoformat(
        tools[0]["started_at"]
    )
    assert tools[0]["duration_ms"] >= 0
    assert len({block["block_id"] for block in blocks}) == len(blocks)
    assert len(turn["artifacts"]) == 1
    attachment = turn["artifacts"][0]
    assert (
        await client.get(attachment["download_url"], headers=auth)
    ).content == "实际报告内容".encode()
    messages = observed[-1][1]
    notify = next(
        message
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id == "report-progress"
    )
    assert notify.status == "success" and notify.artifact == {
        "type": "task_progress",
        "text": progress["text"],
    }
    assert {"planning", "message_notify_user", "create_artifact"} <= observed[0][2]
    assert "write_todos" not in observed[0][2]
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert json.loads(saved.content_blocks_json) == blocks
    reopened = (await client.get(f"/api/project-tasks/{done['task_id']}", headers=auth)).json()
    assert reopened["turns"][0]["content_blocks"] == blocks
    assert reopened["turns"][0]["plan"] == plans[-1]

    session, ws = await connected(client, auth)
    try:
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "planning-resume",
                "task_id": done["task_id"],
                "cursor": cursor,
            }
        )
        restored = await until(
            ws,
            lambda frame: (
                frame["type"] == "turn" and frame["data"]["turn"]["status"] == "completed"
            ),
        )
        assert restored["data"]["turn"]["content_blocks"] == blocks
        assert restored["data"]["turn"]["plan"] == plans[-1]
        await until(ws, lambda frame: frame.get("id") == "planning-resume")
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "planning-current",
                "task_id": done["task_id"],
                "cursor": restored["data"]["cursor"],
            }
        )
        frames = []
        while True:
            frame = await ws.receive_json(timeout=5)
            if frame.get("id") == "planning-current":
                break
            frames.append(frame)
        assert not any(frame["type"] == "turn" for frame in frames)
    finally:
        await session.__aexit__(None, None, None)

    continued = await follow_up(client, auth, done["task_id"], prompt="解释一下报告。")
    assert continued["status"] == "completed", continued
    first, second = continued["turns"]
    assert first["plan"] == plans[-1] and first["content_blocks"] == blocks
    assert second["plan"] is None and second["plan_revision"] == 0
    assert second["reply"] == "这是普通追问的答案。"
    assert [block["type"] for block in second["content_blocks"]] == ["text"]
    assert len(agents) == 2


@pytest.mark.parametrize("terminal", ["cancelled", "failed"])
async def test_cancelled_and_failed_turns_preserve_unfinished_plan(
    env_with_provider, monkeypatch, terminal
):
    client, server, auth = env_with_provider
    steps = ["生成报告", "验证交付结果"]
    harness_models(
        monkeypatch,
        [
            tool_call("planning", "unfinished-plan", command="create", steps=steps),
            AIMessage(content="未到达的答案"),
        ],
    )
    original = runtime.run_turn
    release = asyncio.Event()

    async def interrupted(services, owner, turn, history):
        stream = original(services, owner, turn, history)
        try:
            async for event in stream:
                yield event
                if event["type"] == "plan":
                    await release.wait()
                    raise OctopError.localized(ErrorCode.TASK_EXECUTION_FAILED)
        finally:
            await stream.aclose()

    monkeypatch.setattr(runtime, "run_turn", interrupted)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "生成并验证报告"},
    )
    assert response.status_code == 202, response.text
    created = response.json()
    try:
        for _ in range(300):
            live = (
                await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)
            ).json()
            if live["turns"][0]["plan"] is not None:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the harness plan was not persisted")
        initial_plan = live["turns"][0]["plan"]
        assert [step["content"] for step in initial_plan["steps"]] == steps
        assert [step["status"] for step in initial_plan["steps"]] == ["in_progress", "pending"]
        assert all(step["step_id"] for step in initial_plan["steps"])
        if terminal == "cancelled":
            cancelled = await client.post(
                f"/api/project-tasks/{created['task_id']}/cancel",
                headers=auth,
                json={"turn_id": created["active_turn_id"]},
            )
            assert cancelled.status_code == 200, cancelled.text
        else:
            release.set()
        done = await settled(client, auth, created["task_id"])
    finally:
        release.set()
    assert done["status"] == terminal, done
    turn = done["turns"][0]
    assert turn["reply"] == "" and turn["artifacts"] == []
    assert turn["plan"] == initial_plan
    assert turn["plan_revision"] == 1
    assert [block["type"] for block in turn["content_blocks"]] == ["plan"]
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert json.loads(saved.content_blocks_json) == turn["content_blocks"]
    reopened = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    assert reopened["turns"][0]["plan"] == turn["plan"]


async def test_failed_notification_after_checkpoint_restore_remains_a_failed_action(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    policy = SecuritySettingsStore(settings_repo=server.services.settings_repo)
    value = policy.load().to_dict()
    value["hitl"] = {"enabled": True, "tools": ["message_notify_user"]}
    policy.save(value)
    observed, agents = harness_models(
        monkeypatch,
        [
            tool_call(
                "planning", "approval-plan", command="create", steps=["报告进展", "完成回答"]
            ),
            tool_call("message_notify_user", "invalid-notification", text="   "),
            AIMessage(content="通知未发送，我会继续回答。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "发送进展通知后回答。"},
    )
    assert response.status_code == 202, response.text
    created = response.json()
    await parked(server, created["active_turn_id"])
    task = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    turn = task["turns"][0]
    assert task["status"] == "waiting_approval" and len(turn["interactions"]) == 1
    assert [block["type"] for block in turn["content_blocks"]] == ["plan"]
    initial_plan = turn["plan"]
    assert initial_plan["revision"] == 1 and initial_plan["active_step_id"]
    item = turn["interactions"][0]
    decision = approval(item)
    await server.project_task_worker.close()
    accepted = await respond(env_with_provider, task, item, decision)
    assert accepted.status_code == 200, accepted.text
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    assert len(done["turns"]) == 1 and done["turns"][0]["turn_id"] == turn["turn_id"]
    final = done["turns"][0]
    blocks = final["content_blocks"]
    assert [block["type"] for block in blocks] == ["plan", "tool", "text"]
    assert final["plan"] == initial_plan and final["plan_revision"] == 1
    failed = blocks[1]
    assert failed["tool_call_id"] == "invalid-notification"
    assert failed["tool_name"] == "message_notify_user" and failed["status"] == "failed"
    assert failed["tool_label"]
    assert failed["plan_id"] == initial_plan["plan_id"]
    assert failed["step_id"] == initial_plan["active_step_id"]
    assert failed["arguments"] == {"text": "   "}
    assert failed["started_at"] and failed["finished_at"]
    assert failed["duration_ms"] >= 0
    assert final["reply"] == "通知未发送，我会继续回答。"
    result = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "invalid-notification"
    )
    assert result.status == "error" and result.artifact is None
    assert failed["output"] == result.content
    assert len(agents) == 2
    assert (await respond(env_with_provider, task, item, decision)).status_code == 200
    reopened = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    assert reopened["turns"][0]["content_blocks"] == blocks
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert json.loads(saved.content_blocks_json) == blocks


async def test_parallel_actions_use_explicit_steps_and_keep_binding_when_results_reverse(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider
    second_finished = asyncio.Event()
    original_create = TaskArtifactPublisher.create
    completion_order = []

    async def reverse_completion(publisher, call_id, arguments):
        if call_id == "first-artifact":
            await second_finished.wait()
        try:
            artifact = await original_create(publisher, call_id, arguments)
            completion_order.append(call_id)
            return artifact
        finally:
            if call_id == "second-artifact":
                second_finished.set()

    monkeypatch.setattr(TaskArtifactPublisher, "create", reverse_completion)

    def parallel_actions(messages):
        plan = latest_plan(messages)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": call_id,
                    "name": "create_artifact",
                    "args": {
                        "name": f"{index}.txt",
                        "content": f"step-{index}",
                        "task_step_id": plan["steps"][index]["step_id"],
                    },
                }
                for index, call_id in enumerate(["first-artifact", "second-artifact"])
            ],
        )

    planning_models(
        monkeypatch,
        [
            tool_call("planning", "parallel-plan", command="create", steps=["第一步", "第二步"]),
            step_command("start_step", "parallel-start", index=1),
            parallel_actions,
            AIMessage(content="两个结果均已交付。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "并行执行两个步骤并交付结果。"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "completed", done
    turn = done["turns"][0]
    plan = turn["plan"]
    assert [step["status"] for step in plan["steps"]] == ["in_progress", "in_progress"]
    assert completion_order == ["second-artifact", "first-artifact"]
    tools = {
        block["tool_call_id"]: block for block in turn["content_blocks"] if block["type"] == "tool"
    }
    assert set(tools) == {"first-artifact", "second-artifact"}
    for index, call_id in enumerate(["first-artifact", "second-artifact"]):
        block = tools[call_id]
        assert block["status"] == "completed"
        assert block["plan_id"] == plan["plan_id"]
        assert block["step_id"] == plan["steps"][index]["step_id"]
        assert block["arguments"] == {"name": f"{index}.txt", "content": f"step-{index}"}
        assert json.loads(block["output"])["name"] == f"{index}.txt"
        assert datetime.fromisoformat(block["finished_at"]) >= datetime.fromisoformat(
            block["started_at"]
        )
        assert block["duration_ms"] >= 0


async def test_planning_mixed_with_action_is_rejected_without_mutation_or_artifact(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider

    def unsafe_batch(messages):
        plan = latest_plan(messages)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "premature-completion",
                    "name": "planning",
                    "args": {
                        "command": "mark_step",
                        "expected_revision": plan["revision"],
                        "step_id": plan["steps"][0]["step_id"],
                        "status": "completed",
                        "result": "Premature result.",
                    },
                },
                {
                    "id": "mixed-artifact",
                    "name": "create_artifact",
                    "args": {"name": "bad.txt", "content": "Must not publish"},
                },
            ],
        )

    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call("planning", "exclusive-plan", command="create", steps=["生成并核对结果"]),
            unsafe_batch,
            AIMessage(content="同批操作已被拒绝。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "核对计划与实际动作。"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "completed", done
    turn = done["turns"][0]
    assert turn["artifacts"] == []
    assert turn["plan"]["revision"] == 1
    assert turn["plan"]["steps"][0]["status"] == "in_progress"
    assert turn["plan"]["steps"][0]["result"] == ""
    rejected = [
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage)
        and message.tool_call_id in {"premature-completion", "mixed-artifact"}
    ]
    assert len(rejected) == 2 and all(message.status == "error" for message in rejected)
