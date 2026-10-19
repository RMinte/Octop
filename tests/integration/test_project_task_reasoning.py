"""Reasoning settings, real harness output, and durable task subscriptions."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import runtime
from tests.integration.test_project_capabilities import create_task, harness_models
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until


def configure_reasoning(server, *, toggle=True, adapter="thinking", default_mode="enabled"):
    repo = server.services.provider_repo
    provider = repo.get_by_name("openai")
    capability = {
        "adapter": adapter,
        "toggle": toggle,
        "default_mode": default_mode,
        "efforts": ["low", "high"],
        "default_effort": "low",
    }
    repo.update(
        provider.id, models_json=json.dumps([{"id": "gpt-4o", "reasoning_config": capability}])
    )


async def test_real_task_harness_applies_reasoning_settings_and_saves_separate_output(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    configure_reasoning(server, adapter="openai_reasoning_effort")
    observed = []

    class Model(FakeMessagesListChatModel):
        reasoning_effort: str | None = None

        def bind_tools(self, tools: Any, **kwargs: Any):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.append(self.reasoning_effort)
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Model(
        responses=[
            AIMessage(
                content="正式回复", additional_kwargs={"reasoning_content": "模型返回的思考内容"}
            )
        ]
    )

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    monkeypatch.setattr(
        runtime,
        "HarnessAgent",
        lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
    )
    resources = (await client.get("/api/project-tasks/resources", headers=auth)).json()
    capability = resources["models"][0]["reasoning_config"]
    assert capability["efforts"] == ["low", "high"] and capability["default_effort"] == "low"
    task = await create_task(
        client, auth, options={"reasoning_mode": "enabled", "reasoning_effort": "high"}
    )
    assert task["status"] == "completed", task
    turn = task["turns"][0]
    assert turn["reasoning"] == "模型返回的思考内容"
    assert turn["reply"] == "正式回复"
    assert [block["type"] for block in turn["content_blocks"]] == ["reasoning", "text"]
    assert [block["text"] for block in turn["content_blocks"]] == ["模型返回的思考内容", "正式回复"]
    assert turn["options"]["reasoning_mode"] == "enabled"
    assert turn["options"]["reasoning_effort"] == "high"
    assert observed == ["high"]
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert saved.reasoning == turn["reasoning"]
    reopened = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
    assert reopened["turns"][0]["reasoning"] == turn["reasoning"]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_reasoning_only_updates_stream_and_survive_terminal_states_and_reconnect(
    env_with_provider, monkeypatch, status
):
    client, server, auth = env_with_provider
    first, finish = asyncio.Event(), asyncio.Event()

    async def run(services, owner, turn, history):
        yield {"type": "reasoning", "content": "先核对已有信息。"}
        await first.wait()
        yield {"type": "reasoning", "content": "再确认结论。"}
        await finish.wait()
        if status == "failed":
            raise OctopError.localized(ErrorCode.TASK_EXECUTION_FAILED)
        yield {"type": "token", "content": "正式答案"}

    monkeypatch.setattr(runtime, "run_turn", run)
    session, ws = await connected(client, auth)
    try:
        task = (
            await command(
                ws,
                "task.create",
                "reasoning-create",
                data={"request_id": str(uuid4()), "prompt": "Think"},
            )
        )["data"]
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "reasoning-watch",
                "task_id": task["task_id"],
                "cursor": task["event_cursor"],
            }
        )
        live = await until(
            ws,
            lambda f: f["type"] == "turn" and f["data"]["turn"]["reasoning"] == "先核对已有信息。",
        )
        assert live["data"]["turn"]["reply"] == ""
        first_block = live["data"]["turn"]["content_blocks"][0]
        assert first_block["type"] == "reasoning" and first_block["status"] == "streaming"
        first.set()
        longer = await until(
            ws,
            lambda f: (
                f["type"] == "turn"
                and f["data"]["turn"]["reasoning"] == "先核对已有信息。再确认结论。"
            ),
        )
        assert longer["data"]["task"]["list_revision"] == live["data"]["task"]["list_revision"]
        assert longer["data"]["cursor"] != live["data"]["cursor"]
        assert longer["data"]["turn"]["content_blocks"][0]["block_id"] == first_block["block_id"]
        assert longer["data"]["turn"]["content_revision"] > live["data"]["turn"]["content_revision"]
        cursor = longer["data"]["cursor"]
        if status == "cancelled":
            await client.post(
                f"/api/project-tasks/{task['task_id']}/cancel",
                headers=auth,
                json={"turn_id": task["active_turn_id"]},
            )
        else:
            finish.set()
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == status, done
        assert done["turns"][0]["reasoning"] == "先核对已有信息。再确认结论。"
        assert done["turns"][0]["reply"] == ("正式答案" if status == "completed" else "")
        assert all(block["status"] != "streaming" for block in done["turns"][0]["content_blocks"])
    finally:
        first.set()
        finish.set()
        await session.__aexit__(None, None, None)
    session, ws = await connected(client, auth)
    try:
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "reasoning-resume",
                "task_id": task["task_id"],
                "cursor": cursor,
            }
        )
        restored = await until(
            ws, lambda f: f["type"] == "turn" and f["data"]["turn"]["status"] == status
        )
        assert restored["data"]["turn"]["reasoning"] == done["turns"][0]["reasoning"]
        assert restored["data"]["turn"]["content_blocks"] == done["turns"][0]["content_blocks"]
    finally:
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("source", ["responses", "glm"])
async def test_summary_and_tool_rounds_survive_persistence_and_reopen(
    env_with_provider, monkeypatch, source
):
    client, server, auth = env_with_provider

    def summary(text):
        return {
            "type": "reasoning",
            "index": 0,
            "summary": [{"type": "summary_text", "index": 0, "text": text}],
            "encrypted_content": "must-not-be-public",
        }

    def message(reasoning_text, text, **kwargs):
        if source == "glm":
            return AIMessage(
                content=text, additional_kwargs={"reasoning_content": reasoning_text}, **kwargs
            )
        return AIMessage(
            content=[summary(reasoning_text), {"type": "text", "index": 1, "text": text}], **kwargs
        )

    harness_models(
        monkeypatch,
        [
            message(
                "先准备报告。",
                "正在整理。",
                tool_calls=[
                    {
                        "id": "report",
                        "name": "create_artifact",
                        "args": {"name": "report.md", "content": "report"},
                    }
                ],
            ),
            message("报告已生成。", "请查看附件。"),
        ],
    )
    task = await create_task(client, auth, options={"action_timeline_enabled": False})
    assert task["status"] == "completed", task
    turn = task["turns"][0]
    blocks = turn["content_blocks"]
    reasoning_kind = "reasoning_summary" if source == "responses" else "reasoning"
    assert [block["type"] for block in blocks] == [
        reasoning_kind,
        "reasoning",
        "tool",
        reasoning_kind,
        "text",
    ]
    assert [block["text"] for block in blocks] == [
        "先准备报告。",
        "正在整理。",
        "",
        "报告已生成。",
        "请查看附件。",
    ]
    assert blocks[2]["tool_call_id"] == "report" and blocks[2]["tool_name"] == "create_artifact"
    assert all(block["status"] == "completed" for block in blocks)
    assert "must-not-be-public" not in json.dumps(task)
    assert turn["reasoning"] == "先准备报告。正在整理。报告已生成。"
    assert turn["reply"] == "请查看附件。"
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert json.loads(saved.content_blocks_json) == blocks
    reopened = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
    assert reopened["turns"][0]["content_blocks"] == blocks
    assert reopened["turns"][0]["content_revision"] == turn["content_revision"]


async def test_invalid_reasoning_settings_do_not_create_tasks(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    unsupported = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "Work",
            "options": {"reasoning_mode": "enabled"},
        },
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "TASK_REASONING_INVALID"
    configure_reasoning(server, toggle=False)
    before = (await client.get("/api/project-tasks", headers=auth)).json()["items"]
    for options in (
        {"reasoning_mode": "disabled"},
        {"reasoning_effort": "unsupported"},
        {"reasoning_mode": "sometimes"},
        {"reasoning_effort": ""},
    ):
        response = await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": str(uuid4()), "prompt": "Work", "options": options},
        )
        assert response.status_code == 422, response.text
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == before
    configure_reasoning(server, adapter="status_only")
    harness_models(monkeypatch, [AIMessage(content="No exposed reasoning")])
    task = await create_task(client, auth)
    assert task["status"] == "completed" and task["turns"][0]["reasoning"] == ""
    assert task["turns"][0]["options"]["reasoning_mode"] == "enabled"
    assert task["turns"][0]["options"]["reasoning_effort"] is None
