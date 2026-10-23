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
from tests.integration.test_project_capabilities import follow_up, harness_models
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until


def tool_call(tool_name, call_id, **args):
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": tool_name, "args": args}])


def planning_models(monkeypatch, responses):
    original = FakeMessagesListChatModel._generate
    original_bind = FakeMessagesListChatModel.bind

    def bind(model, **kwargs):
        # LangChain binds no tools through bind(), not bind_tools([]). The fake
        # records that boundary explicitly so terminal rounds cannot retain its
        # last non-empty advertised tool set in our observations.
        if getattr(model, "role", "") == "project-task" and hasattr(model, "tool_names"):
            model.tool_names = set()
        return original_bind(model, **kwargs)

    def generate(model, messages, stop=None, run_manager=None, **kwargs):
        if getattr(model, "role", "") == "project-task":
            response = responses[model.i]
            if callable(response):
                model.responses[model.i] = response(messages)
        return original(model, messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(FakeMessagesListChatModel, "_generate", generate)
    monkeypatch.setattr(FakeMessagesListChatModel, "bind", bind)
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
            tool_call(
                "planning", "plan-create", command="create", title="生成并交付报告", steps=initial
            ),
            tool_call(
                "message_notify_user", "report-progress", text="我会先生成报告，再整理交付说明。"
            ),
            tool_call(
                "create_artifact",
                "report-create",
                name="report.md",
                content="实际报告内容",
                brief="生成报告附件",
            ),
            step_command(
                "mark_step",
                "report-complete",
                status="completed",
                result="报告已生成并附加到任务。",
            ),
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
                and frame["data"]["turn"]["plan"] is not None
                and any(
                    block["type"] == "progress" and block["tool_call_id"] == "report-progress"
                    for block in frame["data"]["turn"]["content_blocks"]
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
            block
            for block in live_turn["content_blocks"]
            if block["type"] == "progress" and block["tool_call_id"] == "report-progress"
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
    assert [plan["revision"] for plan in plans] == [1, 2, 3]
    assert plans[0] == initial_plan
    assert {plan["plan_id"] for plan in plans} == {initial_plan["plan_id"]}
    assert all(
        [step["step_id"] for step in plan["steps"]]
        == [step["step_id"] for step in initial_plan["steps"]]
        for plan in plans
    )
    assert turn["plan"] == plans[-1] and turn["plan_revision"] == 3
    assert all(step["status"] == "completed" and step["result"] for step in plans[-1]["steps"])
    assert all(step["started_at"] and step["finished_at"] for step in plans[-1]["steps"])
    assert plans[-1]["active_step_id"] is None
    assert turn["reply"] == "报告已交付。"
    final_text = next(block for block in blocks if block["type"] == "text")
    assert final_text["plan_id"] is None and final_text["step_id"] is None
    reports = [
        block
        for block in blocks
        if block["type"] == "progress"
        and block["tool_call_id"] in {"report-progress", "delivery-progress"}
    ]
    assert reports[0] == progress and len(reports) == 2
    assert reports[1]["plan_id"] == initial_plan["plan_id"]
    assert reports[1]["step_id"] == initial_plan["steps"][1]["step_id"]
    host_reports = [
        block
        for block in blocks
        if block["type"] == "progress" and block["tool_call_id"].startswith("narration:")
    ]
    assert [block["tool_call_id"] for block in host_reports] == [
        "narration:opening",
        f"narration:plan:{initial_plan['plan_id']}:1",
        f"narration:plan:{initial_plan['plan_id']}:2",
    ]
    assert [block["introduction"] for block in host_reports] == [True, False, False]
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
        "introduction": False,
    }
    assert progress["introduction"] is False
    assert {"planning", "create_artifact"} <= observed[0][2]
    assert "message_notify_user" not in observed[0][2]
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
    assert [block["type"] for block in second["content_blocks"]] == ["progress", "text"]
    assert second["content_blocks"][0]["tool_call_id"] == "narration:opening"
    assert second["content_blocks"][0]["introduction"] is True
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
            tool_call(
                "planning", "unfinished-plan", command="create", title="生成并验证报告", steps=steps
            ),
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
    assert [block["type"] for block in turn["content_blocks"]] == ["progress", "plan", "progress"]
    assert turn["content_blocks"][1]["plan"] == initial_plan
    reports = [block for block in turn["content_blocks"] if block["type"] == "progress"]
    assert [block["tool_call_id"] for block in reports] == [
        "narration:opening",
        f"narration:plan:{initial_plan['plan_id']}:1",
    ]
    assert [block["introduction"] for block in reports] == [True, False]
    assert all(block["status"] == "completed" and block["text"] for block in reports)
    assert reports[1]["plan_id"] == initial_plan["plan_id"]
    assert reports[1]["step_id"] == initial_plan["active_step_id"]
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
    observed, agents = planning_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "approval-plan",
                command="create",
                title="报告进展并回答",
                steps=["报告进展", "完成回答"],
            ),
            tool_call("message_notify_user", "accepted-opening", text="我会报告通知结果。"),
            tool_call("message_notify_user", "invalid-notification", text="   "),
            step_command(
                "mark_step",
                "notification-blocked",
                status="blocked",
                result="进展通知参数无效，通知未发送。",
            ),
            AIMessage(content="通知未发送，请先检查通知内容。"),
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
    opening_task = (
        await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)
    ).json()
    opening_item = opening_task["turns"][0]["interactions"][0]
    assert (
        await respond(env_with_provider, opening_task, opening_item, approval(opening_item))
    ).status_code == 200
    for _ in range(300):
        waiting = (
            await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)
        ).json()
        if (
            waiting["status"] == "waiting_approval"
            and len(waiting["turns"][0]["interactions"]) == 2
        ):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("invalid notification did not reach its approval checkpoint")
    task = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    turn = task["turns"][0]
    assert task["status"] == "waiting_approval" and len(turn["interactions"]) == 2
    original_blocks = [
        block
        for block in turn["content_blocks"]
        if not (block["tool_call_id"] or "").startswith("narration:")
    ]
    assert [block["type"] for block in original_blocks] == ["plan", "progress"]
    assert original_blocks[1]["tool_call_id"] == "accepted-opening"
    initial_plan = turn["plan"]
    assert initial_plan["revision"] == 1 and initial_plan["active_step_id"]
    item = turn["interactions"][-1]
    decision = approval(item)
    await server.project_task_worker.close()
    accepted = await respond(env_with_provider, task, item, decision)
    assert accepted.status_code == 200, accepted.text
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "failed", done
    assert done["turns"][0]["error_code"] == "TASK_PLAN_BLOCKED"
    assert len(done["turns"]) == 1 and done["turns"][0]["turn_id"] == turn["turn_id"]
    final = done["turns"][0]
    blocks = final["content_blocks"]
    original_blocks = [
        block for block in blocks if not (block["tool_call_id"] or "").startswith("narration:")
    ]
    assert [block["type"] for block in original_blocks] == [
        "plan",
        "progress",
        "tool",
        "plan",
        "text",
    ]
    assert [
        block["tool_call_id"]
        for block in blocks
        if (block["tool_call_id"] or "").startswith("narration:")
    ] == ["narration:opening", f"narration:plan:{initial_plan['plan_id']}:1"]
    assert final["plan_revision"] == 2
    assert final["plan"]["plan_id"] == initial_plan["plan_id"]
    assert [step["status"] for step in final["plan"]["steps"]] == ["blocked", "pending"]
    failed = next(block for block in blocks if block["tool_call_id"] == "invalid-notification")
    assert failed["tool_call_id"] == "invalid-notification"
    assert failed["tool_name"] == "message_notify_user" and failed["status"] == "failed"
    assert failed["tool_label"]
    assert failed["plan_id"] == initial_plan["plan_id"]
    assert failed["step_id"] == initial_plan["active_step_id"]
    assert failed["arguments"] == {"text": "   "}
    assert failed["started_at"] and failed["finished_at"]
    assert failed["duration_ms"] >= 0
    assert final["reply"] == "通知未发送，请先检查通知内容。"
    result = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "invalid-notification"
    )
    assert result.status == "error" and result.artifact is None
    assert failed["output"] == result.content
    assert len(agents) == 3
    assert (await respond(env_with_provider, task, item, decision)).status_code == 200
    reopened = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    assert reopened["turns"][0]["content_blocks"] == blocks
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert json.loads(saved.content_blocks_json) == blocks


async def test_work_cannot_be_assigned_to_a_future_step(env_with_provider, monkeypatch):
    client, _, auth = env_with_provider

    def premature_action(messages):
        plan = latest_plan(messages)
        return tool_call(
            "create_artifact",
            "future-artifact",
            name="future.txt",
            content="Must not publish",
            task_step_id=plan["steps"][1]["step_id"],
            brief="生成未来步骤的验证文件",
        )

    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "ordered-plan",
                command="create",
                title="顺序执行任务",
                steps=["第一步", "第二步"],
            ),
            tool_call(
                "message_notify_user", "ordered-opening", text="我会先完成第一步，再处理第二步。"
            ),
            premature_action,
            step_command(
                "mark_step", "first-done", status="completed", result="已确认不能提前执行第二步。"
            ),
            step_command(
                "mark_step",
                "second-skipped",
                index=1,
                status="skipped",
                result="用户要求只验证步骤门控，不生成文件。",
            ),
            AIMessage(content="未来步骤的动作已被拒绝，没有生成文件。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "验证动作不能越过当前步骤。"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "completed", done
    turn = done["turns"][0]
    assert turn["artifacts"] == []
    assert [step["status"] for step in turn["plan"]["steps"]] == ["completed", "skipped"]
    result = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "future-artifact"
    )
    assert result.status == "error"
    rejected = next(
        block for block in turn["content_blocks"] if block["tool_call_id"] == "future-artifact"
    )
    assert rejected["status"] == "failed" and rejected["started_at"] is None
    assert rejected["finished_at"] is None and rejected["duration_ms"] is None


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
                    "args": {
                        "name": "bad.txt",
                        "content": "Must not publish",
                        "brief": "生成验证文件",
                    },
                },
            ],
        )

    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "exclusive-plan",
                command="create",
                title="核对结果",
                steps=["生成并核对结果"],
            ),
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
    assert done["status"] == "failed", done
    turn = done["turns"][0]
    assert turn["error_code"] == "TASK_PLAN_UNFINISHED"
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


async def test_final_answer_cannot_complete_an_unfinished_plan(env_with_provider, monkeypatch):
    client, _, auth = env_with_provider
    planning_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "unfinished-create",
                command="create",
                title="必须逐步确认",
                steps=["验证结果"],
            ),
            AIMessage(content="这句话不能代替步骤结果。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "创建计划后验证结果。"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "failed", done
    turn = done["turns"][0]
    assert turn["error_code"] == "TASK_PLAN_UNFINISHED"
    assert turn["reply"] == "这句话不能代替步骤结果。"
    assert turn["reasoning"] == ""
    assert (
        "".join(block["text"] for block in turn["content_blocks"] if block["type"] == "text")
        == "这句话不能代替步骤结果。"
    )
    assert turn["plan"]["steps"][0]["status"] == "in_progress"
    assert turn["plan"]["steps"][0]["result"] == ""


@pytest.mark.parametrize("status", ["completed", "blocked"])
async def test_terminal_plan_does_not_dispatch_further_work(env_with_provider, monkeypatch, status):
    client, _, auth = env_with_provider
    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "terminal-create",
                command="create",
                title="完成后停止工作",
                steps=["确认门控"],
            ),
            step_command("mark_step", "terminal-mark", status=status, result="已记录终态结果。"),
            tool_call(
                "create_artifact", "after-plan", name="forbidden.txt", content="Must not publish"
            ),
            AIMessage(content="终态后的动作没有执行。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "验证计划终态后不再执行工作。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == ("completed" if status == "completed" else "failed"), done
    turn = done["turns"][0]
    assert turn["artifacts"] == []
    assert turn["plan"]["steps"][0]["status"] == status
    assert turn["reply"] == "终态后的动作没有执行。"
    assert "create_artifact" not in observed[-1][2]
    rejected = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "after-plan"
    )
    assert rejected.status == "error"
    block = next(block for block in turn["content_blocks"] if block["tool_call_id"] == "after-plan")
    assert block["status"] == "failed" and block["started_at"] is None
    assert block["finished_at"] is None and block["duration_ms"] is None


async def test_plan_write_failure_does_not_dispatch_next_work(env_with_provider, monkeypatch):
    client, _, auth = env_with_provider
    original = runtime.TaskPlanning

    class FailingPlanPersistence(original):
        def __init__(self, restored, locale, persist=None):
            async def fail(plan, call_id, arguments):
                raise RuntimeError("Plan checkpoint could not confirm persistence")

            super().__init__(restored, locale, fail)

    monkeypatch.setattr(runtime, "TaskPlanning", FailingPlanPersistence)
    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call(
                "planning", "unwritten", command="create", title="不能跳过保存", steps=["生成文件"]
            ),
            tool_call(
                "create_artifact",
                "never-dispatched",
                name="bad.txt",
                content="Must not publish",
                brief="生成验证文件",
            ),
            AIMessage(content="未到达"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "计划保存失败时不得继续工作。"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "failed", done
    turn = done["turns"][0]
    assert turn["plan"] is None and turn["plan_revision"] == 0
    assert turn["artifacts"] == [] and len(observed) == 1
    assert not any(block["tool_call_id"] == "never-dispatched" for block in turn["content_blocks"])


@pytest.mark.parametrize(
    "new_plan", [False, True], ids=["unplanned-followup", "new-plan-revision-one"]
)
@pytest.mark.parametrize("action_timeline_enabled", [False, True], ids=["classic", "actions"])
async def test_approval_restore_does_not_import_a_previous_turn_plan(
    env_with_provider, monkeypatch, new_plan, action_timeline_enabled
):
    """A checkpoint carries history, but only this turn owns planning state."""
    from copy import deepcopy

    from deepagents.backends.protocol import ExecuteResponse
    from octop_harness.backends.bwrap_shell import BubbledLocalShellBackend
    from octop_harness.backends.local_shell import HarnessLocalShellBackend

    client, server, auth = env_with_provider
    effects = []
    command_text = "echo approval-resume-proof"
    action_brief = "执行测试工具以核对审批恢复"

    def execute(self, command, *, timeout=None):
        effects.append(command)
        return ExecuteResponse(output="approval-resume-proof", exit_code=0, truncated=False)

    monkeypatch.setattr(HarnessLocalShellBackend, "execute", execute)
    monkeypatch.setattr(BubbledLocalShellBackend, "execute", execute)
    responses = [
        tool_call(
            "planning",
            "shared-plan-create",
            command="create",
            title="前一轮固定计划",
            steps=["记录第一项说明", "记录第二项说明", "记录第三项说明"],
        ),
        *(
            [
                tool_call(
                    "message_notify_user",
                    "previous-turn-opening",
                    text="我会按顺序记录这三项说明。",
                )
            ]
            if action_timeline_enabled
            else []
        ),
        *[
            step_command(
                "mark_step",
                f"previous-turn-finish-{index}",
                index=index,
                status="completed",
                result=f"已记录旧轮第 {index + 1} 项说明；没有外部工具操作。",
            )
            for index in range(3)
        ],
        AIMessage(content="前一轮计划已结束。"),
    ]
    if new_plan:
        responses.append(
            tool_call(
                "planning",
                "shared-plan-create",
                command="create",
                title="本轮独立执行计划",
                steps=["取得许可后执行无副作用测试工具"],
            )
        )
    if action_timeline_enabled:
        responses.append(
            tool_call(
                "message_notify_user",
                "current-turn-opening",
                text="我会请求本次执行许可，再核对工具返回结果。",
            )
        )
    execute_arguments = {
        "command": command_text,
        **({"brief": action_brief} if action_timeline_enabled else {}),
    }
    responses.append(tool_call("execute", "current-turn-execute", **execute_arguments))
    if new_plan:
        responses.append(
            step_command(
                "mark_step",
                "current-turn-finish",
                status="completed",
                result="工具实际返回 approval-resume-proof，退出码为 0。",
            )
        )
    responses.append(AIMessage(content="本轮已取得工具返回结果。"))
    observed, _ = planning_models(monkeypatch, responses)
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "先保存并完成旧轮三步计划。",
            "options": {"action_timeline_enabled": action_timeline_enabled},
        },
    )
    assert created.status_code == 202, created.text
    first = await settled(client, auth, created.json()["task_id"])
    assert first["status"] == "completed", first
    old_turn = deepcopy(first["turns"][0])
    assert old_turn["plan_revision"] == 4
    assert all(step["status"] == "completed" for step in old_turn["plan"]["steps"])
    followed = await client.post(
        f"/api/project-tasks/{first['task_id']}/messages",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "执行本轮测试工具，审批后继续。",
            "options": {"action_timeline_enabled": action_timeline_enabled},
        },
    )
    assert followed.status_code == 202, followed.text
    current_turn_id = followed.json()["turns"][-1]["turn_id"]
    await parked(server, current_turn_id)
    waiting = (await client.get(f"/api/project-tasks/{first['task_id']}", headers=auth)).json()
    current = waiting["turns"][-1]
    assert waiting["status"] == "waiting_approval" and effects == []
    assert current["turn_id"] != old_turn["turn_id"]
    expected_plan = deepcopy(current["plan"])
    if new_plan:
        assert current["plan_revision"] == 1
        assert expected_plan["plan_id"] != old_turn["plan"]["plan_id"]
        assert expected_plan["active_step_id"] == expected_plan["steps"][0]["step_id"]
        assert next(
            block["tool_call_id"] for block in current["content_blocks"] if block["type"] == "plan"
        ) == next(
            block["tool_call_id"] for block in old_turn["content_blocks"] if block["type"] == "plan"
        )
    else:
        assert expected_plan is None and current["plan_revision"] == 0
    item = current["interactions"][0]
    assert item["payload"]["tool_name"] == "execute"
    assert item["payload"]["arguments"] == execute_arguments
    decision = approval(item)
    await server.project_task_worker.close()
    approved = await respond(env_with_provider, waiting, item, decision)
    assert approved.status_code == 200, approved.text
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    final = await settled(client, auth, first["task_id"])
    assert final["status"] == "completed", final
    assert final["turns"][0] == old_turn
    restored = final["turns"][-1]
    assert restored["turn_id"] == current_turn_id
    if new_plan:
        assert restored["plan"]["plan_id"] == expected_plan["plan_id"]
        assert restored["plan_revision"] == 2
        assert [step["step_id"] for step in restored["plan"]["steps"]] == [
            step["step_id"] for step in expected_plan["steps"]
        ]
        assert restored["plan"]["steps"][0]["status"] == "completed"
    else:
        assert restored["plan"] is None and restored["plan_revision"] == 0
        assert not any(block["type"] == "plan" for block in restored["content_blocks"])
    assert effects == [command_text]
    tool = next(
        block
        for block in restored["content_blocks"]
        if block["type"] == "tool" and block["tool_call_id"] == "current-turn-execute"
    )
    assert tool["status"] == "completed"
    assert tool["arguments"] == {"command": command_text}
    assert tool["brief"] == (action_brief if action_timeline_enabled else None)
    assert "approval-resume-proof" in tool["output"]
    assert tool["plan_id"] == (expected_plan["plan_id"] if new_plan else None)
    assert tool["step_id"] == (expected_plan["active_step_id"] if new_plan else None)
    notifications = [
        block
        for block in restored["content_blocks"]
        if block["type"] == "progress" and block["tool_call_id"] == "current-turn-opening"
    ]
    assert len(notifications) == int(action_timeline_enabled)
    if notifications:
        assert notifications[0]["tool_call_id"] == "current-turn-opening"
        assert notifications[0]["plan_id"] == (expected_plan["plan_id"] if new_plan else None)
        assert notifications[0]["step_id"] == (
            expected_plan["active_step_id"] if new_plan else None
        )
        assert notifications[0]["introduction"] is False
    host_reports = [
        block
        for block in restored["content_blocks"]
        if block["type"] == "progress" and block["tool_call_id"].startswith("narration:")
    ]
    expected_reports = []
    if action_timeline_enabled:
        expected_reports.append("narration:opening")
        if new_plan:
            expected_reports.append(f"narration:plan:{expected_plan['plan_id']}:1")
    assert [block["tool_call_id"] for block in host_reports] == expected_reports
    assert all(block["status"] == "completed" and block["text"] for block in host_reports)
    messages = observed[-1][1]
    assert (
        sum(
            isinstance(message, ToolMessage) and message.tool_call_id == "current-turn-execute"
            for message in messages
        )
        == 1
    )
    generation_count = len(observed)
    assert (await respond(env_with_provider, waiting, item, decision)).status_code == 200
    server.project_task_worker.wake()
    await asyncio.sleep(0.05)
    reopened = (await client.get(f"/api/project-tasks/{first['task_id']}", headers=auth)).json()
    assert effects == [command_text] and len(observed) == generation_count
    assert reopened["turns"] == final["turns"]
