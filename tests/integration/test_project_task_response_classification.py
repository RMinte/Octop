"""Classify each model response before publishing prose as a final answer."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
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


def observe_output(monkeypatch):
    original = runtime.run_turn
    emitted = []

    async def observed(services, owner, turn, history):
        async with aclosing(original(services, owner, turn, history)) as events:
            async for event in events:
                if event["type"] in {"token", "reasoning", "reasoning_summary"}:
                    emitted.append((turn.turn_id, event["type"], event["content"]))
                yield event

    monkeypatch.setattr(runtime, "run_turn", observed)
    return emitted


def text_content(turn):
    return "".join(block["text"] for block in turn["content_blocks"] if block["type"] == "text")


async def test_direct_question_preserves_final_text_and_durable_answer(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    answer = "这是普通问答的直接答案。"
    harness_models(monkeypatch, [AIMessage(content=answer)])
    emitted = observe_output(monkeypatch)
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
    assert "".join(text for _, kind, text in emitted if kind == "token") == answer
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert saved.reply == answer
    assert json.loads(saved.content_blocks_json) == turn["content_blocks"]


@pytest.mark.parametrize("mode", ["tool", "planning", "mixed-planning"])
async def test_tool_response_prose_never_enters_answer_even_before_plan_creation(
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
    expected_process = [tool_prose]
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
            expected_process = [mixed_prose, correction_prose, tool_prose, step_prose]
        else:
            expected_process = [plan_prose, tool_prose, step_prose]
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
    emitted = observe_output(monkeypatch)
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
    assert turn["reply"] == final_answer
    assert text_content(turn) == final_answer
    streamed_answer = "".join(text for _, kind, text in emitted if kind == "token")
    assert streamed_answer == final_answer
    process = "".join(
        block["text"]
        for block in turn["content_blocks"]
        if block["type"] in {"reasoning", "reasoning_summary"}
    )
    for prose in expected_process:
        assert process.count(prose) == turn["reasoning"].count(prose) == 1
        assert prose not in streamed_answer
    assert final_answer not in process
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
    assert saved.reply == final_answer
    assert json.loads(saved.content_blocks_json) == turn["content_blocks"]


async def test_same_prompt_followup_and_approval_resume_preserve_answer_classification(
    env_with_provider, monkeypatch
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
    emitted = observe_output(monkeypatch)
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
    assert current["reply"] == text_content(current) == ""
    assert current["reasoning"].count(prelude) == 1
    item = current["interactions"][0]
    await server.project_task_worker.close()
    accepted = await respond(env_with_provider, waiting, item, approval(item))
    assert accepted.status_code == 200, accepted.text
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    final = await settled(client, auth, first["task_id"])
    assert final["status"] == "completed", final
    assert final["turns"][0] == old_turn
    current = final["turns"][-1]
    assert current["reply"] == text_content(current) == second_answer
    assert current["reasoning"].count(prelude) == 1
    assert effects == [command]
    assert (
        "".join(
            text
            for emitted_turn, kind, text in emitted
            if emitted_turn == turn_id and kind == "token"
        )
        == second_answer
    )
    saved = server.services.project_task_turn_repo.get(turn_id)
    assert saved.reply == second_answer
    assert json.loads(saved.content_blocks_json) == current["content_blocks"]
    server.project_task_worker.wake()
    await asyncio.sleep(0.05)
    reopened = (await client.get(base, headers=auth)).json()
    assert reopened["turns"] == final["turns"] and effects == [command]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_interrupted_provider_preserves_received_prose_and_later_summary_as_process(
    env_with_provider, monkeypatch, status
):
    client, server, auth = env_with_provider
    prose, summary = "我先核对当前输入。", "已收到输入，尚未形成结论。"
    received, requests = [], []
    buffered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    append = runtime.TaskResponseText.append

    def observe_buffer(response, event):
        output = append(response, event)
        if event["type"] in {"token", "reasoning_summary"}:
            received.append((event["type"], event["content"]))
            if event["type"] == "reasoning_summary":
                buffered.set()
        return output

    monkeypatch.setattr(runtime.TaskResponseText, "append", observe_buffer)

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
    emitted = observe_output(monkeypatch)
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
        await asyncio.wait_for(buffered.wait(), timeout=3)
        assert received == [("token", prose), ("reasoning_summary", summary)]
        live = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
        assert live["status"] == "running" and live["turns"][0]["reply"] == ""
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
        assert turn["reply"] == text_content(turn) == ""
        assert turn["reasoning"] == prose + summary
        assert [(block["type"], block["text"]) for block in turn["content_blocks"]] == [
            ("reasoning", prose),
            ("reasoning_summary", summary),
        ]
        assert all(block["status"] != "streaming" for block in turn["content_blocks"])
        assert [(kind, text) for _, kind, text in emitted] == [
            ("reasoning", prose),
            ("reasoning_summary", summary),
        ]
        assert len(requests) == 1 and stopped.is_set()
        saved = server.services.project_task_turn_repo.get(turn["turn_id"])
        assert saved.reply == "" and saved.reasoning == prose + summary
        assert json.loads(saved.content_blocks_json) == turn["content_blocks"]
        reopened = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
        assert reopened["turns"][0] == turn
    finally:
        release.set()
