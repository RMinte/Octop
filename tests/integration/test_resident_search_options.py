"""Conversation search settings affect new turns while accepted execution stays frozen."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict
from langchain_core.tools import StructuredTool
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.projects import resident_tasks
from octop.infra.projects.tasks import now_ms
from tests.integration.test_project_tasks_ws import connected, until
from tests.integration.test_resident_conversations import _base, _binding
from tests.integration.test_resident_conversations import resident_docs_env as resident_docs_env
from tests.integration.test_resident_search_runtime import _json, _settled
from tests.integration.test_resident_stream_delivery import subscribe
from tests.support.auth import create_user


def search_graph(monkeypatch, labels, gate=None):
    queries, visible, configs = [], [], []
    paused = False

    async def search(query: str) -> str:
        """Return controlled evidence without accessing the public network."""
        queries.append(query)
        return "Evidence for " + query

    native = StructuredTool.from_function(coroutine=search, name="searchfree_search")
    monkeypatch.setattr("octop_harness.agent.load_web_search_tools", lambda _policy: [native])

    class SearchModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            visible.append(
                {
                    tool["function"]["name"] if isinstance(tool, dict) else tool.name
                    for tool in tools
                }
            )
            return self

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            nonlocal paused
            if gate is not None and not paused:
                paused = True
                gate[0].set()
                await gate[1].wait()
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    responses = []
    for label in labels:
        responses.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "searchfree_search",
                            "args": {"query": label},
                            "id": "search-" + label,
                        }
                    ],
                ),
                AIMessage(content="Answered " + label),
            ]
        )
    model = SearchModel(responses=responses)

    class Factory(ChatModelFactory):
        def get_chat_model(self, _ref):
            return model

    def harness(cfg, **kwargs):
        configs.append(cfg)
        return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

    monkeypatch.setattr(resident_tasks, "HarnessAgent", harness)
    return SimpleNamespace(queries=queries, visible=visible, configs=configs)


def search_receipt(server, turn, label):
    native = server.services.project_task_turn_repo.get(turn["turn_id"])
    context = server.services.project_task_turn_repo.context(native.task_id)
    return next(
        message
        for message in messages_from_dict(json.loads(context[1]))
        if isinstance(message, ToolMessage) and message.tool_call_id == "search-" + label
    )


async def get_settings(client, auth, base):
    return _json(await client.get(base + "/conversation/settings", headers=auth))


async def change_settings(client, auth, base, previous, enabled):
    return _json(
        await client.patch(
            base + "/conversation/settings",
            headers=auth,
            json={"expected_revision": previous["revision"], "web_search_enabled": enabled},
        )
    )


async def assert_views(client, auth, base, binding, turns, settings):
    for turn in turns:
        assert (
            _json(await client.get(base + "/requests/" + turn["request_id"], headers=auth)) == turn
        )
    snapshot = _json(await client.get(base + "/conversation", headers=auth))
    assert snapshot["turns"] == turns and snapshot["settings"] == settings
    history = _json(await client.get(base + "/conversation/history", headers=auth))
    assert [item["turn"] for item in history["items"]] == turns
    for item, turn in zip(history["items"], turns, strict=True):
        located = _json(
            await client.get(
                base + "/conversation/history/items/" + item["item_id"],
                params={"before": 0, "after": 0},
                headers=auth,
            )
        )
        assert located["items"][0]["turn"] == turn
    session, ws = await connected(client, auth)
    try:
        live = await subscribe(ws, binding, "search-settings-" + uuid4().hex)
        assert live["turns"] == turns and live["settings"] == settings
    finally:
        await session.__aexit__(None, None, None)


async def test_default_search_runs_and_disabled_setting_is_inherited_until_changed(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    graph = search_graph(monkeypatch, ["default", "off-first", "off-second", "on"])
    binding = await _binding(client, auth)
    base = _base(binding)
    settings = await get_settings(client, auth, base)
    assert settings == {"web_search_enabled": True, "revision": 1}
    turns = []
    for label, expected in (
        ("default", True),
        ("off-first", False),
        ("off-second", False),
        ("on", True),
    ):
        if label == "off-first":
            settings = await change_settings(client, auth, base, settings, False)
        elif label == "on":
            settings = await change_settings(client, auth, base, settings, True)
        visible_start = len(graph.visible)
        body = {"request_id": uuid4().hex, "prompt": "Find public information: " + label}
        accepted = _json(await client.post(base + "/messages", headers=auth, json=body), 202)
        assert accepted["web_search_enabled"] is expected
        completed = await _settled(client, auth, base, body["request_id"])
        assert completed["status"] == "completed", completed
        assert completed["web_search_enabled"] is expected
        assert completed["reply"] == "Answered " + label
        assert graph.visible[visible_start:]
        assert all(
            ("searchfree_search" in tools) is expected for tools in graph.visible[visible_start:]
        )
        receipt = search_receipt(server, completed, label)
        if expected:
            assert receipt.status == "success" and receipt.content == "Evidence for " + label
        else:
            assert receipt.status == "error"
            assert json.loads(receipt.content)["error_code"] == "FORBIDDEN"
        turns.append(completed)
    assert graph.queries == ["default", "on"] and len(graph.configs) == 4
    assert await get_settings(client, auth, base) == {"web_search_enabled": True, "revision": 3}
    await assert_views(client, auth, base, binding, turns, settings)


async def test_running_turn_and_retries_keep_accepted_policy_after_settings_change(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    entered, release = asyncio.Event(), asyncio.Event()
    graph = search_graph(monkeypatch, ["accepted-on", "next-off"], (entered, release))
    binding = await _binding(client, auth)
    base = _base(binding)
    settings = await get_settings(client, auth, base)
    first_body = {"request_id": uuid4().hex, "prompt": "Find public information."}
    accepted = _json(await client.post(base + "/messages", headers=auth, json=first_body), 202)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        running = _json(
            await client.get(base + "/requests/" + accepted["request_id"], headers=auth)
        )
        assert running["status"] == "running" and running["web_search_enabled"] is True
        settings = await change_settings(client, auth, base, settings, False)
        replay = _json(await client.post(base + "/messages", headers=auth, json=first_body), 202)
        assert replay["turn_id"] == accepted["turn_id"] and replay["web_search_enabled"] is True
    finally:
        release.set()
    first = await _settled(client, auth, base, accepted["request_id"])
    assert first["status"] == "completed" and first["web_search_enabled"] is True
    assert graph.queries == ["accepted-on"]
    second_body = {"request_id": uuid4().hex, "prompt": "Try a new lookup."}
    second = _json(await client.post(base + "/messages", headers=auth, json=second_body), 202)
    second = await _settled(client, auth, base, second["request_id"])
    assert second["status"] == "completed" and second["web_search_enabled"] is False
    assert (
        json.loads(search_receipt(server, second, "next-off").content)["error_code"] == "FORBIDDEN"
    )
    settings = await change_settings(client, auth, base, settings, True)
    for body, frozen in ((first_body, first), (second_body, second)):
        assert _json(await client.post(base + "/messages", headers=auth, json=body), 202) == frozen
    assert graph.queries == ["accepted-on"] and len(graph.configs) == 2
    await assert_views(client, auth, base, binding, [first, second], settings)


async def test_settings_cas_noop_and_subscribed_snapshot_push(env_with_provider):
    client, _server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    initial = await get_settings(client, auth, base)
    before = _json(await client.get(base + "/conversation", headers=auth))
    assert await change_settings(client, auth, base, initial, True) == initial
    assert (
        _json(await client.get(base + "/conversation", headers=auth))["revision"]
        == before["revision"]
    )
    session, ws = await connected(client, auth)
    try:
        subscribed = await subscribe(ws, binding, "watch-settings")
        assert subscribed["settings"] == initial
        changed = await change_settings(client, auth, base, initial, False)
        assert changed == {"web_search_enabled": False, "revision": initial["revision"] + 1}
        frame = await until(
            ws,
            lambda incoming: (
                incoming.get("type") == "resident.snapshot"
                and incoming["data"]["thread_id"] == binding["thread_id"]
                and incoming["data"]["settings"] == changed
            ),
        )
        assert frame["data"]["revision"] > subscribed["revision"]
        assert frame["data"]["turns"] == subscribed["turns"] == []
        for enabled in (True, False):
            conflict = await client.patch(
                base + "/conversation/settings",
                headers=auth,
                json={"expected_revision": initial["revision"], "web_search_enabled": enabled},
            )
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["error"]["code"] == "RESIDENT_SETTINGS_CONFLICT"
        assert await get_settings(client, auth, base) == changed
        current = _json(await client.get(base + "/conversation", headers=auth))
        assert await change_settings(client, auth, base, changed, False) == changed
        assert (
            _json(await client.get(base + "/conversation", headers=auth))["revision"]
            == current["revision"]
        )
    finally:
        await session.__aexit__(None, None, None)


async def test_settings_strict_types_and_removed_message_override_do_not_mutate_state(
    env_with_provider,
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    original = await get_settings(client, auth, base)
    for invalid in (0, 1, "false", "true", None, [], {}):
        rejected = await client.patch(
            base + "/conversation/settings",
            headers=auth,
            json={"expected_revision": original["revision"], "web_search_enabled": invalid},
        )
        assert rejected.status_code == 422, (invalid, rejected.text)
    for invalid in (True, False, 0, -1, 1.0, "1", None):
        rejected = await client.patch(
            base + "/conversation/settings",
            headers=auth,
            json={"expected_revision": invalid, "web_search_enabled": False},
        )
        assert rejected.status_code == 422, (invalid, rejected.text)
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    for override in (True, False, None):
        body = {
            "request_id": uuid4().hex,
            "prompt": "Find public information.",
            "web_search_enabled": override,
        }
        rejected = await client.post(base + "/messages", headers=auth, json=body)
        assert rejected.status_code == 422, rejected.text
        assert server.services.resident_request_repo.get(owner, body["request_id"]) is None
    assert await get_settings(client, auth, base) == original
    assert _json(await client.get(base + "/conversation", headers=auth))["turns"] == []


async def test_conversation_settings_are_private_even_from_another_administrator(env_with_provider):
    client, _server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    other_auth = await create_user(client, auth, username="search-settings-owner")
    other = await _binding(client, other_auth)
    other_base = _base(other)
    for target, actor in ((base, other_auth), (other_base, auth), (base, {})):
        read = await client.get(target + "/conversation/settings", headers=actor)
        assert read.status_code in {401, 403, 404}, read.text
        write = await client.patch(
            target + "/conversation/settings",
            headers=actor,
            json={"expected_revision": 1, "web_search_enabled": False},
        )
        assert write.status_code in {401, 403, 404}, write.text
    original = await get_settings(client, auth, base)
    await change_settings(client, auth, base, original, False)
    assert await get_settings(client, other_auth, other_base) == {
        "web_search_enabled": True,
        "revision": 1,
    }


async def test_old_receipt_without_search_snapshot_is_not_upgraded_by_current_settings(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    graph = search_graph(monkeypatch, ["old", "new"])
    binding = await _binding(client, auth)
    base = _base(binding)
    resident = server.app_runtime.resident_conversations
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    body = {"request_id": uuid4().hex, "prompt": "Previously accepted question."}
    await resident.close()
    try:
        configuration = resident._configuration(owner, binding["agent_id"])
        configuration["resident_input"] = {}
        configuration.pop("web_search_enabled", None)
        old, created = server.services.resident_request_repo.create(
            owner,
            binding["thread_id"],
            body["request_id"],
            uuid4().hex,
            body["prompt"],
            now_ms(),
            configuration_json=json.dumps(configuration),
        )
        assert created
        frozen = old.configuration_json
        settings = await get_settings(client, auth, base)
        assert settings["web_search_enabled"] is True
        replay = _json(await client.post(base + "/messages", headers=auth, json=body), 202)
        assert replay["turn_id"] == old.turn_id and replay["web_search_enabled"] is False
        settings = await change_settings(client, auth, base, settings, False)
        settings = await change_settings(client, auth, base, settings, True)
        assert _json(await client.post(base + "/messages", headers=auth, json=body), 202) == replay
        await resident.start()
        first = await _settled(client, auth, base, body["request_id"])
        assert first["status"] == "completed" and first["web_search_enabled"] is False
        assert graph.queries == [] and search_receipt(server, first, "old").status == "error"
        next_body = {"request_id": uuid4().hex, "prompt": "A new question."}
        second = _json(await client.post(base + "/messages", headers=auth, json=next_body), 202)
        second = await _settled(client, auth, base, second["request_id"])
        assert second["status"] == "completed" and second["web_search_enabled"] is True
        assert graph.queries == ["new"]
        assert (
            server.services.resident_request_repo.get(owner, body["request_id"]).configuration_json
            == frozen
        )
        await assert_views(client, auth, base, binding, [first, second], settings)
    finally:
        await resident.start()


async def test_api_declares_conversation_settings_without_per_message_override(resident_docs_env):
    client, _server, auth = resident_docs_env
    capabilities = _json(await client.get("/api/agents/resident-capabilities", headers=auth))
    assert capabilities["conversation_settings"] is True and capabilities["web_search"] is True
    assert "turn_web_search" not in capabilities
    schema = _json(await client.get("/api/openapi.json", headers=auth))
    paths = schema["paths"]
    base = "/api/agents/{agent_id}/threads/{thread_id}"
    operation = paths[base + "/messages"]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    models = schema["components"]["schemas"]
    message = models[body_schema["$ref"].rsplit("/", 1)[-1]]
    assert (
        "web_search_enabled" not in message["properties"]
        and message["additionalProperties"] is False
    )
    get = paths[base + "/conversation/settings"]["get"]
    patch = paths[base + "/conversation/settings"]["patch"]
    response_ref = get["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    settings = models[response_ref.rsplit("/", 1)[-1]]
    assert settings["properties"]["web_search_enabled"]["type"] == "boolean"
    assert settings["properties"]["revision"]["minimum"] == 1
    patch_ref = patch["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    change = models[patch_ref.rsplit("/", 1)[-1]]
    assert set(change["required"]) == {"web_search_enabled", "expected_revision"}
    assert change["properties"]["web_search_enabled"]["type"] == "boolean"
    assert change["properties"]["expected_revision"]["type"] == "integer"
    assert change["properties"]["expected_revision"]["minimum"] == 1
    turn = models[
        operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].rsplit(
            "/", 1
        )[-1]
    ]
    assert turn["properties"]["web_search_enabled"]["type"] == "boolean"
    assert turn["properties"]["web_search_enabled"]["default"] is False
    snapshot_ref = paths[base + "/conversation"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert models[snapshot_ref.rsplit("/", 1)[-1]]["properties"]["settings"]["$ref"] == response_ref
    assert get["summary"] and patch["summary"] and patch["description"]
