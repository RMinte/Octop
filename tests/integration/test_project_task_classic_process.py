"""Disabling public actions preserves ordinary execution and provider reasoning."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.projects import runtime
from octop.infra.projects.settings import ACTION_TIMELINE_ENV
from octop.infra.utils.env_file import save_env_file
from tests.integration.test_project_tasks import settled


@pytest.mark.parametrize("override", [False, True])
async def test_disabled_actions_do_not_register_notify_or_add_briefs_and_keep_execution_facts(
    env_with_provider, monkeypatch, override
):
    client, server, auth = env_with_provider
    save_env_file(server.paths.root / "env", {ACTION_TIMELINE_ENV: "true" if override else "false"})
    schemas, effects = [], []

    async def record_fact(value: str) -> str:
        effects.append(value)
        return "Recorded actual value: " + value

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            schemas.append([convert_to_openai_tool(tool) for tool in tools])
            return self

    model = Model(
        responses=[
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "先记录请求中的事实。"},
                tool_calls=[
                    {"id": "classic-fact", "name": "record_fact", "args": {"value": "proof"}}
                ],
            ),
            AIMessage(
                content="已记录。",
                additional_kwargs={"reasoning_content": "工具返回了真实记录结果。"},
            ),
        ]
    )

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    def create(cfg, **kwargs):
        cfg.tools = [
            *(cfg.tools or []),
            StructuredTool.from_function(
                name="record_fact", description="Record the provided fact.", coroutine=record_fact
            ),
        ]
        return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

    monkeypatch.setattr(runtime, "HarnessAgent", create)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "Record the fact proof",
            "options": {"action_timeline_enabled": False} if override else {},
        },
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    assert task["status"] == "completed", task
    turn = task["turns"][0]
    assert turn["options"]["action_timeline_enabled"] is False
    assert turn["plan"] is None and effects == ["proof"]
    assert schemas
    for advertised in schemas:
        assert "message_notify_user" not in {schema["function"]["name"] for schema in advertised}
        business = next(
            schema["function"]
            for schema in advertised
            if schema["function"]["name"] == "record_fact"
        )
        assert "brief" not in business["parameters"].get("properties", {})
        assert "brief" not in business["parameters"].get("required", [])
    assert turn["reasoning"] == "先记录请求中的事实。工具返回了真实记录结果。"
    assert not any(block["type"] == "progress" for block in turn["content_blocks"])
    tool = next(block for block in turn["content_blocks"] if block["type"] == "tool")
    assert tool["status"] == "completed"
    assert tool["arguments"] == {"value": "proof"}
    assert tool["output"] == "Recorded actual value: proof"
    assert tool["started_at"] and tool["finished_at"] and tool["duration_ms"] is not None
    stored = server.services.project_task_turn_repo.get(turn["turn_id"])
    assert stored.reasoning == turn["reasoning"]
    assert json.loads(stored.configuration_json)["action_timeline_enabled"] is False
    assert json.loads(stored.content_blocks_json) == turn["content_blocks"]
    reopened = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
    assert reopened["turns"][0]["content_blocks"] == turn["content_blocks"]
