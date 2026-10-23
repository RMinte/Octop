"""Model chunks reach durable public text before the real Harness response ends."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.api.routers import project_tasks_ws
from octop.infra.projects import runtime
from tests.integration.test_project_task_planning import step_command
from tests.integration.test_project_task_response_classification import (
    action_response,
    text_content,
    visible_output,
)
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until


@pytest.fixture
async def public_stream_env(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    chunks = asyncio.Queue()
    started, stopped = asyncio.Event(), asyncio.Event()
    requests = []

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            requests.append(messages)
            started.set()
            try:
                while (chunk := await chunks.get()) is not None:
                    yield ChatGenerationChunk(message=chunk)
            finally:
                stopped.set()

    model = Model(responses=[])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    async def no_narration(self, snapshot):
        return None

    monkeypatch.setattr(
        runtime,
        "HarnessAgent",
        lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
    )
    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", no_narration)
    # A model chunk must traverse _run_turn and commit before its notice wakes WS.
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 3600)
    yield client, server, auth, chunks, started, stopped, requests
    chunks.put_nowait(None)


async def start_stream(env, action_timeline_enabled):
    client, _, auth, _, started, *_ = env
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "请逐段说明已经核对的结果。",
            "options": {"action_timeline_enabled": action_timeline_enabled},
        },
    )
    assert created.status_code == 202, created.text
    await asyncio.wait_for(started.wait(), 5)
    session, ws = await connected(client, auth)
    await command(ws, "task.subscribe", "watch-public-output", task_id=created.json()["task_id"])
    return created.json()["task_id"], session, ws


async def assert_live_output(env, ws, task_id, reply, reasoning=""):
    client, server, auth, _, _, stopped, requests = env
    detail = await visible_output(client, auth, task_id, reply, reasoning=reasoning)
    turn = detail["turns"][0]
    frame = await until(
        ws,
        lambda frame: (
            frame["type"] == "turn"
            and frame["data"]["turn"]["reply"] == reply
            and frame["data"]["turn"]["reasoning"] == reasoning
        ),
    )
    assert detail["status"] == turn["status"] == frame["data"]["turn"]["status"] == "running"
    assert not stopped.is_set() and len(requests) == 1
    assert text_content(turn) == reply
    saved = await asyncio.to_thread(server.services.project_task_turn_repo.get, turn["turn_id"])
    assert saved.status == "running" and saved.reply == reply and saved.reasoning == reasoning
    assert json.loads(saved.content_blocks_json) == turn["content_blocks"]
    assert frame["data"]["turn"]["content_blocks"] == turn["content_blocks"]
    return turn


@pytest.mark.parametrize("action_timeline_enabled", [True, False])
async def test_unplanned_question_streams_each_public_segment_before_model_completion(
    public_stream_env, action_timeline_enabled
):
    env = public_stream_env
    client, _, auth, chunks, _, stopped, requests = env
    task_id, session, ws = await start_stream(env, action_timeline_enabled)
    first, second = "首段公开正文。", "第二段仍在持续增长。"
    try:
        chunks.put_nowait(AIMessageChunk(id="direct-answer", content=first))
        initial = await assert_live_output(env, ws, task_id, first)
        first_block = next(block for block in initial["content_blocks"] if block["type"] == "text")
        chunks.put_nowait(AIMessageChunk(id="direct-answer", content=second))
        growing = await assert_live_output(env, ws, task_id, first + second)
        text_blocks = [block for block in growing["content_blocks"] if block["type"] == "text"]
        assert len(text_blocks) == 1 and text_blocks[0]["block_id"] == first_block["block_id"]
        assert text_blocks[0]["status"] == "streaming"
        chunks.put_nowait(None)
        final = await settled(client, auth, task_id)
        turn = final["turns"][0]
        assert final["status"] == "completed" and stopped.is_set() and len(requests) == 1
        assert turn["reply"] == text_content(turn) == first + second
        assert turn["reasoning"] == "" and turn["plan"] is None
        assert all(block["status"] == "completed" for block in turn["content_blocks"])
        assert turn["reply"].count(first) == turn["reply"].count(second) == 1
    finally:
        chunks.put_nowait(None)
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("stage", ["tool", "plan", "step"])
async def test_public_prelude_is_visible_before_tool_intent_in_every_plan_phase(
    env_with_provider, monkeypatch, stage
):
    client, server, auth = env_with_provider
    prelude, final_answer = "先公开说明本轮实际操作。", "已核对附件的真实发布结果。"
    subscribed, paused, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    responses = []
    if stage != "tool":
        responses.append(
            action_response(
                prelude if stage == "plan" else "",
                "planning",
                "public-plan",
                command="create",
                title="核对并发布附件",
                steps=["发布验证附件并核对收据"],
            )
        )
    responses.append(
        action_response(
            prelude if stage != "plan" else "",
            "create_artifact",
            "public-work",
            name="public-proof.txt",
            content="proof",
        )
    )
    if stage != "tool":
        responses.append(
            step_command(
                "mark_step", "public-complete", status="completed", result="验证附件已发布。"
            )
        )
    responses.append(AIMessage(content=final_answer))
    gated_index = 1 if stage == "step" else 0

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            await subscribed.wait()
            index = self.i
            self.i += 1
            response = responses[index]
            if callable(response):
                response = response(messages)
            if response.content:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(id=f"public-{index}", content=response.content)
                )
            if index == gated_index:
                paused.set()
                await release.wait()
            for call in response.tool_calls:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        id=f"public-{index}",
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

    model = Model(responses=[])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    monkeypatch.setattr(
        runtime,
        "HarnessAgent",
        lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
    )
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 3600)
    session, ws = await connected(client, auth)
    try:
        created = await client.post(
            "/api/project-tasks",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "prompt": "发布验证附件并核对工具结果。",
                "options": {"action_timeline_enabled": False},
            },
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        await command(ws, "task.subscribe", "watch-public-prelude", task_id=task_id)
        subscribed.set()
        await asyncio.wait_for(paused.wait(), 5)
        live = await visible_output(client, auth, task_id, prelude, reasoning="")
        turn = live["turns"][0]
        frame = await until(
            ws,
            lambda frame: frame["type"] == "turn" and frame["data"]["turn"]["reply"] == prelude,
        )
        assert frame["data"]["turn"]["status"] == "running"
        assert live["status"] == "running" and model.i == gated_index + 1
        saved = await asyncio.to_thread(server.services.project_task_turn_repo.get, turn["turn_id"])
        assert saved.reply == prelude and saved.reasoning == ""
        public = [block for block in turn["content_blocks"] if block["type"] == "text"]
        assert len(public) == 1 and public[0]["text"] == prelude
        if stage == "step":
            assert public[0]["plan_id"] == turn["plan"]["plan_id"]
            assert public[0]["step_id"] == turn["plan"]["steps"][0]["step_id"]
        else:
            assert turn["plan"] is None and public[0]["plan_id"] is None
        release.set()
        final = await settled(client, auth, task_id)
        current = final["turns"][0]
        assert final["status"] == "completed", final
        assert current["reply"] == text_content(current) == prelude + final_answer
        assert current["reasoning"] == ""
        assert [artifact["name"] for artifact in current["artifacts"]] == ["public-proof.txt"]
        assert all(block["status"] != "streaming" for block in current["content_blocks"])
    finally:
        subscribed.set()
        release.set()
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("action_timeline_enabled", [True, False])
async def test_interleaved_model_text_and_reasoning_keep_order_type_and_block_identity(
    public_stream_env, action_timeline_enabled
):
    env = public_stream_env
    client, _, auth, chunks, *_ = env
    task_id, session, ws = await start_stream(env, action_timeline_enabled)
    thought, summary = "真实思考。", "供应商思考摘要。"
    first, second, last = "首先公开说明。", "接着给出证据。", "正文继续增长。"
    try:
        chunks.put_nowait(
            AIMessageChunk(
                id="interleaved", content="", additional_kwargs={"reasoning_content": thought}
            )
        )
        await assert_live_output(env, ws, task_id, "", thought)
        chunks.put_nowait(AIMessageChunk(id="interleaved", content=first))
        await assert_live_output(env, ws, task_id, first, thought)
        chunks.put_nowait(
            AIMessageChunk(
                id="interleaved",
                content=[
                    {
                        "type": "reasoning",
                        "index": 0,
                        "summary": [{"type": "summary_text", "index": 0, "text": summary}],
                    }
                ],
            )
        )
        await assert_live_output(env, ws, task_id, first, thought + summary)
        chunks.put_nowait(AIMessageChunk(id="interleaved", content=second))
        partial = await assert_live_output(env, ws, task_id, first + second, thought + summary)
        blocks = [block for block in partial["content_blocks"] if block["type"] != "progress"]
        assert [(block["type"], block["text"]) for block in blocks] == [
            ("reasoning", thought),
            ("text", first),
            ("reasoning_summary", summary),
            ("text", second),
        ]
        identities = [block["block_id"] for block in blocks]
        assert len(set(identities)) == len(identities)
        chunks.put_nowait(AIMessageChunk(id="interleaved", content=last))
        growing = await assert_live_output(
            env, ws, task_id, first + second + last, thought + summary
        )
        grown = [block for block in growing["content_blocks"] if block["type"] != "progress"]
        assert [block["block_id"] for block in grown] == identities
        assert grown[-1]["text"] == second + last
        assert all(block["status"] == "completed" for block in grown[:-1])
        chunks.put_nowait(None)
        final = (await settled(client, auth, task_id))["turns"][0]
        assert final["reply"] == text_content(final) == first + second + last
        assert final["reasoning"] == thought + summary
        restored = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
        assert restored["turns"][0] == final
    finally:
        chunks.put_nowait(None)
        await session.__aexit__(None, None, None)
