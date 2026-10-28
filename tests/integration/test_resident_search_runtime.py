"""HTTP resident acceptance reaches the real Task graph with its frozen search policy."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict
from langchain_core.tools import StructuredTool
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory
from pydantic import Field

from octop.infra.projects import resident_tasks
from octop.infra.projects.tasks import now_ms


def _json(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


async def _settled(client, auth, base, request_id):
    async with asyncio.timeout(10):
        while True:
            turn = _json(await client.get(f"{base}/requests/{request_id}", headers=auth))
            if turn["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return turn
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("legacy", [False, True])
async def test_http_resident_task_real_graph_keeps_accepted_search_policy(
    env_with_provider, monkeypatch, legacy
):
    client, server, auth = env_with_provider
    services = server.services
    runtime = server.app_runtime.resident_conversations
    queries, configs = [], []

    async def search(query: str) -> str:
        """Return actual public search evidence without an external request."""
        queries.append(query)
        return "Current public evidence https://example.test/current"

    native_search = StructuredTool.from_function(coroutine=search, name="searchfree_search")
    monkeypatch.setattr("octop_harness.agent.load_web_search_tools", lambda policy: [native_search])

    class SearchModel(FakeMessagesListChatModel):
        visible: list[set[str]] = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            self.visible.append(
                {
                    tool["function"]["name"] if isinstance(tool, dict) else tool.name
                    for tool in tools
                }
            )
            return self

    model = SearchModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "searchfree_search",
                        "args": {"query": "current Octop information"},
                        "id": "native-search",
                    }
                ],
            ),
            AIMessage(content="Checked the returned search receipt."),
        ]
    )

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    def harness(cfg, **kwargs):
        configs.append(cfg)
        return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

    monkeypatch.setattr(resident_tasks, "HarnessAgent", harness)
    binding = _json(await client.put("/api/agents/resident-conversation", headers=auth, json={}))
    base = f"/api/agents/{binding['agent_id']}/threads/{binding['thread_id']}"
    owner = services.agent_repo.get(binding["agent_id"]).user_id
    body = {"request_id": uuid4().hex, "prompt": "Find current public information."}
    await runtime.close()
    try:
        if legacy:
            # Recreate an immutable receipt accepted before built-in search was available.
            configuration = runtime._configuration(owner, binding["agent_id"])
            configuration["resident_input"] = {}
            assert "web_search_enabled" not in configuration
            queued, created = services.resident_request_repo.create(
                owner,
                binding["thread_id"],
                body["request_id"],
                uuid4().hex,
                body["prompt"],
                now_ms(),
                configuration_json=json.dumps(configuration),
            )
            assert created
        accepted = _json(await client.post(f"{base}/messages", headers=auth, json=body), 202)
        frozen = services.resident_request_repo.get(owner, body["request_id"])
        frozen_json = frozen.configuration_json
        assert accepted["status"] == "queued"
        if legacy:
            assert accepted["turn_id"] == queued.turn_id
            assert "web_search_enabled" not in json.loads(frozen_json)
        else:
            assert json.loads(frozen_json)["web_search_enabled"] is True
        await runtime.start()
        completed = await _settled(client, auth, base, body["request_id"])
        assert completed["status"] == "completed", completed
        assert completed["reply"] == "Checked the returned search receipt."
        assert len(configs) == 1 and configs[0].web_search_tools is True
        native_turn = services.project_task_turn_repo.get(accepted["turn_id"])
        raw = json.loads(native_turn.configuration_json)
        assert raw["execution_kind"] == "resident"
        assert raw["resident_configuration"] == json.loads(frozen_json)
        context = services.project_task_turn_repo.context(native_turn.task_id)
        messages = messages_from_dict(json.loads(context[1]))
        receipts = [message for message in messages if isinstance(message, ToolMessage)]
        assert len(receipts) == 1 and receipts[0].tool_call_id == "native-search"
        if legacy:
            assert not queries and not model.visible
            assert receipts[0].status == "error"
            assert json.loads(receipts[0].content)["error_code"] == "FORBIDDEN"
        else:
            assert queries == ["current Octop information"]
            assert model.visible and all("searchfree_search" in names for names in model.visible)
            assert all(
                not names.intersection({"execute", "web_fetch", "browser_use", "read_file"})
                for names in model.visible
            )
            assert receipts[0].status == "success"
            assert receipts[0].content == "Current public evidence https://example.test/current"
        replay = _json(await client.post(f"{base}/messages", headers=auth, json=body), 202)
        assert replay["turn_id"] == accepted["turn_id"]
        assert (
            services.resident_request_repo.get(owner, body["request_id"]).configuration_json
            == frozen_json
        )
        assert len(configs) == 1
    finally:
        await runtime.start()
