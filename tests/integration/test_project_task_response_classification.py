"""Public model text keeps its source through tools, plans and recovery."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.projects import runtime
from tests.integration.test_project_capabilities import harness_models
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_task_planning import planning_models, step_command
from tests.integration.test_project_tasks import settled


def action_response(text, tool_name, call_id, **arguments):
    return AIMessage(
        content=text,
        tool_calls=[{"id": call_id, "name": tool_name, "args": arguments}],
    )


def text_content(turn):
    return "".join(block["text"] for block in turn["content_blocks"] if block["type"] == "text")


async def visible_output(client, auth, task_id, reply, *, reasoning=None):
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
            assert response.status_code == 200, response.text
            detail = response.json()
            turn = detail["turns"][-1]
            if turn["reply"] == reply and (reasoning is None or turn["reasoning"] == reasoning):
                return detail
            await asyncio.sleep(0.01)


async def test_direct_question_preserves_final_text_and_durable_answer(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    answer = "这是普通问答的直接答案。"
    harness_models(monkeypatch, [AIMessage(content=answer)])
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "请直接解释这句话。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    assert task["status"] == "completed", task
    turn = task["turns"][0]
    assert turn["reply"] == text_content(turn) == answer
    assert turn["reasoning"] == "" and turn["plan"] is None
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert saved.reply == answer
    assert json.loads(saved.content_blocks_json) == turn["content_blocks"]


@pytest.mark.parametrize("mode", ["tool", "planning", "mixed-planning"])
async def test_tool_and_plan_response_prose_stays_public_even_before_plan_creation(
    env_with_provider, monkeypatch, mode
):
    client, server, auth = env_with_provider
    final_answer = "验证结果已交付，请查看附件。"
    tool_prose = "我会先生成验证文件。"
    plan_prose = "我会先建立正式计划。"
    mixed_prose = "本轮错误地混合了规划和工作。"
    correction_prose = "接下来单独创建计划再执行。"
    step_prose = "验证文件已经返回，记录本步骤的结果。"
    work = action_response(
        tool_prose, "create_artifact", "publish-proof", name="proof.txt", content="actual-proof"
    )
    expected_public = [tool_prose]
    responses = []
    if mode != "tool":
        if mode == "mixed-planning":
            responses.append(
                AIMessage(
                    content=mixed_prose,
                    tool_calls=[
                        {
                            "id": "mixed-plan",
                            "name": "planning",
                            "args": {
                                "command": "create",
                                "title": "未接受的计划",
                                "steps": ["不能执行"],
                            },
                        },
                        {
                            "id": "mixed-work",
                            "name": "create_artifact",
                            "args": {"name": "must-not-exist.txt", "content": "must-not-execute"},
                        },
                    ],
                )
            )
            expected_public = [mixed_prose, correction_prose, tool_prose, step_prose]
        else:
            expected_public = [plan_prose, tool_prose, step_prose]
        responses.append(
            action_response(
                correction_prose if mode == "mixed-planning" else plan_prose,
                "planning",
                "create-plan",
                command="create",
                title="生成并核对验证文件",
                steps=["生成验证文件并记录返回结果"],
            )
        )
    responses.append(work)
    if mode != "tool":
        finish_step = step_command(
            "mark_step",
            "finish-plan",
            status="completed",
            result="验证附件已发布，工具实际返回了附件记录。",
        )

        def finish_response(messages):
            message = finish_step(messages)
            message.content = step_prose
            return message

        responses.append(finish_response)
    responses.append(AIMessage(content=final_answer))
    planning_models(monkeypatch, responses)
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "生成文件并核对实际返回结果。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    task = await settled(client, auth, created.json()["task_id"])
    assert task["status"] == "completed", task
    turn = task["turns"][0]
    expected_answer = "".join([*expected_public, final_answer])
    assert turn["reply"] == text_content(turn) == expected_answer
    process = "".join(
        block["text"]
        for block in turn["content_blocks"]
        if block["type"] in {"reasoning", "reasoning_summary"}
    )
    assert process == turn["reasoning"] == ""
    for prose in [*expected_public, final_answer]:
        assert turn["reply"].count(prose) == 1
    assert [artifact["name"] for artifact in turn["artifacts"]] == ["proof.txt"]
    if mode == "tool":
        assert turn["plan"] is None
    else:
        assert turn["plan"]["steps"][0]["status"] == "completed"
        if mode == "mixed-planning":
            failed = {
                block["tool_call_id"]
                for block in turn["content_blocks"]
                if block["type"] == "tool" and block["status"] == "failed"
            }
            assert {"mixed-plan", "mixed-work"} <= failed
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert saved.reply == expected_answer
    assert json.loads(saved.content_blocks_json) == turn["content_blocks"]


@pytest.mark.parametrize("plan_status", ["completed", "blocked"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_terminal_plan_streams_answer_before_model_finishes(
    env_with_provider, monkeypatch, plan_status, outcome
):
    client, server, auth = env_with_provider
    first, last = "已核对计划结果，", "这是完整交付说明。"
    plan_prose, step_prose = "我会先建立计划。", "记录本步骤的实际结果。"
    paused, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []

    class Model(FakeMessagesListChatModel):
        tool_names: set[str] = set()

        def bind_tools(self, tools, **kwargs):
            self.tool_names = {tool.name for tool in tools}
            return self

        def bind(self, **kwargs):
            self.tool_names = set()
            return super().bind(**kwargs)

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            requests.append(set(self.tool_names))
            index = self.i
            self.i += 1
            if index < 2:
                response = (
                    action_response(
                        plan_prose,
                        "planning",
                        "create-plan",
                        command="create",
                        title="核对并交付结果",
                        steps=["核对实际结果"],
                    )
                    if index == 0
                    else step_command(
                        "mark_step",
                        "finish-plan",
                        status=plan_status,
                        result="已核对实际结果。",
                    )(messages)
                )
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        id=f"planning-{index}", content=plan_prose if index == 0 else step_prose
                    )
                )
                call = response.tool_calls[0]
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        id=f"planning-{index}",
                        content="",
                        tool_call_chunks=[
                            {
                                "name": call["name"],
                                "args": json.dumps(call["args"]),
                                "id": call["id"],
                                "index": 0,
                            }
                        ],
                    )
                )
                return
            assert index == 2
            try:
                yield ChatGenerationChunk(message=AIMessageChunk(id="answer", content=first))
                paused.set()
                await release.wait()
                if outcome == "failed":
                    raise RuntimeError("simulated final-answer interruption")
                yield ChatGenerationChunk(message=AIMessageChunk(id="answer", content=last))
            finally:
                stopped.set()

    model = Model(responses=[])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    monkeypatch.setattr(
        runtime,
        "HarnessAgent",
        lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "核对结果并记录计划，再交付说明。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    base = f"/api/project-tasks/{task_id}"

    try:
        await asyncio.wait_for(paused.wait(), timeout=3)
        public_prefix = plan_prose + step_prose
        live = await visible_output(client, auth, task_id, public_prefix + first)
        turn = live["turns"][0]
        assert live["status"] == "running" and not stopped.is_set()
        assert text_content(turn) == public_prefix + first
        assert turn["plan"]["steps"][0]["status"] == plan_status
        assert turn["reasoning"] == ""
        assert requests[:2] and all("planning" in tools for tools in requests[:2])
        assert requests[-1] == set()
        if outcome == "cancelled":
            cancelled = await client.post(
                base + "/cancel", headers=auth, json={"turn_id": turn["turn_id"]}
            )
            assert cancelled.status_code == 200, cancelled.text
        else:
            release.set()
        task = await settled(client, auth, task_id)
        expected_status = "failed" if plan_status == "blocked" else outcome
        if outcome == "cancelled":
            expected_status = "cancelled"
        assert task["status"] == expected_status, task
        turn = task["turns"][0]
        expected_answer = public_prefix + first + (last if outcome == "completed" else "")
        assert turn["reply"] == text_content(turn) == expected_answer
        assert turn["reasoning"] == ""
        assert all(block["status"] != "streaming" for block in turn["content_blocks"])
        assert len(requests) == 3 and stopped.is_set()
        saved = server.services.project_task_turn_repo.get(turn["turn_id"])
        assert saved.reply == expected_answer
        assert json.loads(saved.content_blocks_json) == turn["content_blocks"]
    finally:
        release.set()


@pytest.mark.parametrize("pause_before_restart", [False, True])
async def test_same_prompt_followup_and_approval_resume_preserve_public_text(
    env_with_provider, monkeypatch, pause_before_restart
):
    from deepagents.backends.protocol import ExecuteResponse
    from octop_harness.backends.bwrap_shell import BubbledLocalShellBackend
    from octop_harness.backends.local_shell import HarnessLocalShellBackend

    client, server, auth = env_with_provider
    first_answer, second_answer = "第一轮直接答复。", "第二轮已核对工具返回结果。"
    prelude = "我会执行本轮验证，再报告真实结果。"
    prompt = "核对这个结果。"
    command = "echo classification-proof"
    effects = []

    def execute(self, actual, *, timeout=None):
        effects.append(actual)
        return ExecuteResponse(output="classification-proof", exit_code=0, truncated=False)

    monkeypatch.setattr(HarnessLocalShellBackend, "execute", execute)
    monkeypatch.setattr(BubbledLocalShellBackend, "execute", execute)
    harness_models(
        monkeypatch,
        [
            AIMessage(content=first_answer),
            action_response(prelude, "execute", "approved-work", command=command),
            AIMessage(content=second_answer),
        ],
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": prompt,
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    first = await settled(client, auth, created.json()["task_id"])
    assert first["status"] == "completed", first
    old_turn = deepcopy(first["turns"][0])
    assert old_turn["reply"] == text_content(old_turn) == first_answer
    base = f"/api/project-tasks/{first['task_id']}"
    followed = await client.post(
        base + "/messages",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": prompt,
            "options": {"action_timeline_enabled": False},
        },
    )
    assert followed.status_code == 202, followed.text
    turn_id = followed.json()["turns"][-1]["turn_id"]
    await parked(server, turn_id)
    waiting = (await client.get(base, headers=auth)).json()
    assert waiting["status"] == "waiting_approval" and effects == []
    assert waiting["turns"][0] == old_turn
    current = waiting["turns"][-1]
    assert current["reply"] == text_content(current) == prelude
    assert current["reasoning"] == ""
    item = current["interactions"][0]
    if pause_before_restart:
        paused = await client.post(
            base + "/pause",
            headers=auth,
            json={"turn_id": turn_id, "request_id": "pause-public-prelude"},
        )
        assert paused.status_code == 200, paused.text
        snapshot = (await client.get(base, headers=auth)).json()
        assert snapshot["status"] == "paused" and effects == []
        assert snapshot["turns"][0] == old_turn
        assert snapshot["turns"][-1]["reply"] == prelude
    await server.project_task_worker.close()
    accepted = await respond(env_with_provider, waiting, item, approval(item))
    assert accepted.status_code == 200, accepted.text
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    if pause_before_restart:
        snapshot = (await client.get(base, headers=auth)).json()
        assert snapshot["status"] == "paused" and effects == []
        assert snapshot["turns"][-1]["reply"] == prelude
        resumed = await client.post(
            base + "/resume",
            headers=auth,
            json={"turn_id": turn_id, "request_id": "resume-public-prelude"},
        )
        assert resumed.status_code == 200, resumed.text
    final = await settled(client, auth, first["task_id"])
    assert final["status"] == "completed", final
    assert final["turns"][0] == old_turn
    current = final["turns"][-1]
    assert current["reply"] == text_content(current) == prelude + second_answer
    assert current["reasoning"] == ""
    assert effects == [command]
    assert current["reply"].count(prelude) == current["reply"].count(second_answer) == 1
    saved = server.services.project_task_turn_repo.get(turn_id)
    assert saved.reply == prelude + second_answer
    assert json.loads(saved.content_blocks_json) == current["content_blocks"]
    server.project_task_worker.wake()
    await asyncio.sleep(0.05)
    reopened = (await client.get(base, headers=auth)).json()
    assert reopened["turns"] == final["turns"] and effects == [command]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_interrupted_provider_preserves_public_prose_and_separate_summary(
    env_with_provider, monkeypatch, status
):
    client, server, auth = env_with_provider
    prose, summary = "我先核对当前输入。", "已收到输入，尚未形成结论。"
    requests = []
    paused, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            requests.append(messages)
            try:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(id="unfinished-response", content=prose)
                )
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        id="unfinished-response",
                        content=[
                            {
                                "type": "reasoning",
                                "index": 0,
                                "summary": [{"type": "summary_text", "index": 0, "text": summary}],
                            }
                        ],
                    )
                )
                paused.set()
                await release.wait()
                raise RuntimeError("simulated provider interruption")
            finally:
                stopped.set()

    model = Model(responses=[])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    monkeypatch.setattr(
        runtime,
        "HarnessAgent",
        lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "核对输入后再给结论。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    try:
        await asyncio.wait_for(paused.wait(), timeout=3)
        live = await visible_output(client, auth, task_id, prose, reasoning=summary)
        assert live["status"] == "running" and not stopped.is_set()
        if status == "failed":
            release.set()
        else:
            cancelled = await client.post(
                f"/api/project-tasks/{task_id}/cancel",
                headers=auth,
                json={"turn_id": live["turns"][0]["turn_id"]},
            )
            assert cancelled.status_code == 200, cancelled.text
        task = await settled(client, auth, task_id)
        assert task["status"] == status, task
        turn = task["turns"][0]
        assert turn["reply"] == text_content(turn) == prose
        assert turn["reasoning"] == summary
        assert [(block["type"], block["text"]) for block in turn["content_blocks"]] == [
            ("text", prose),
            ("reasoning_summary", summary),
        ]
        assert all(block["status"] != "streaming" for block in turn["content_blocks"])
        assert len(requests) == 1 and stopped.is_set()
        saved = server.services.project_task_turn_repo.get(turn["turn_id"])
        assert saved.reply == prose and saved.reasoning == summary
        assert json.loads(saved.content_blocks_json) == turn["content_blocks"]
        reopened = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
        assert reopened["turns"][0] == turn
    finally:
        release.set()
