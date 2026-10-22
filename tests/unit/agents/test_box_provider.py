"""Appliance requests keep harness behavior without sending an authentication header."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from octop_harness import HarnessAgentConfig
from octop_harness.config import ModelConfig, ProviderConfig

from octop.infra.agents.providers.box import (
    BoxProviderConfig,
    OctopChatModelFactory,
    OctopHarnessAgent,
    build_box_chat_model,
    build_box_provider_config,
)
from octop.infra.agents.providers.harness_factory import OctopHarnessAgentManager


def box_provider() -> ProviderConfig:
    return build_box_provider_config(
        id="appliance",
        name="Appliance",
        base_url="http://appliance.invalid/v1",
        models=[ModelConfig(id="RM-01 VLM", input=["text", "image"], max_output_tokens=4096)],
        headers={"authorization": "must-not-be-forwarded", "X-Appliance": "fixture"},
    )


@pytest.fixture
async def wire(monkeypatch):
    requests: list[tuple[httpx.Request, dict[str, Any]]] = []
    clients = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request, body))
        tool_round = bool(body.get("tools")) and body["messages"][-1]["role"] != "tool"
        if body.get("stream"):
            chunks = [
                {"role": "assistant", "content": "box"},
                {"content": " ok"},
            ]
            events = [
                json.dumps(
                    {
                        "id": "offline-stream",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": body["model"],
                        "choices": [{"index": 0, "delta": chunk, "finish_reason": None}],
                    }
                )
                for chunk in chunks
            ]
            events.append(
                json.dumps(
                    {
                        "id": "offline-stream",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": body["model"],
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(f"data: {event}\n\n" for event in [*events, "[DONE]"]),
            )
        message: dict[str, Any] = {"role": "assistant", "content": "box ok"}
        if tool_round:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "box-call",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"value":"ping"}'},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": "offline-completion",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if tool_round else "stop",
                    }
                ],
            },
        )

    sync_sdk, async_sdk = openai.OpenAI, openai.AsyncOpenAI

    def sync_client(**kwargs):
        client = httpx.Client(transport=httpx.MockTransport(reply))
        clients.append(client)
        return sync_sdk(**{**kwargs, "http_client": client})

    def async_client(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        clients.append(client)
        return async_sdk(**{**kwargs, "http_client": client})

    monkeypatch.setattr(openai, "OpenAI", sync_client)
    monkeypatch.setattr(openai, "AsyncOpenAI", async_client)
    yield requests
    for client in clients:
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()
        else:
            client.close()


def assert_box_wire(requests):
    assert requests
    for request, body in requests:
        assert request.url.path == "/v1/chat/completions"
        assert "authorization" not in request.headers
        assert request.headers["X-Appliance"] == "fixture"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_sync_box_and_cloud_keep_distinct_authentication(wire):
    provider = box_provider()
    assert isinstance(provider, BoxProviderConfig)
    model = build_box_chat_model(provider, provider.models[0])
    assert model.invoke("synthetic").content == "box ok"
    assert model._harness_supported_input == {"text", "image"}
    assert model._harness_max_output_tokens == 4096
    assert_box_wire(wire)
    cloud = ProviderConfig(id="cloud", base_url="https://cloud.invalid/v1", api_key="cloud-fixture")
    build_box_chat_model(cloud, ModelConfig(id="cloud-model")).invoke("synthetic")
    request, body = wire[-1]
    assert request.headers["Authorization"] == "Bearer cloud-fixture"
    assert "chat_template_kwargs" not in body


async def test_async_stream_and_tool_result_rounds_omit_authentication(wire):
    factory = OctopChatModelFactory([box_provider()])
    model = factory.get_chat_model("appliance/RM-01 VLM")
    try:
        assert (await model.ainvoke("synthetic")).content == "box ok"
        chunks = [chunk.content async for chunk in model.astream("synthetic")]
        assert "".join(chunks) == "box ok"
        tool = {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Synthetic echo",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
            },
        }
        first = await model.ainvoke([HumanMessage("call echo")], tools=[tool])
        assert first.tool_calls[0]["args"] == {"value": "ping"}
        result = await model.ainvoke(
            [HumanMessage("call echo"), first, ToolMessage("pong", tool_call_id="box-call")],
            tools=[tool],
        )
        assert result.content == "box ok"
        assert wire[-1][1]["messages"][-1]["tool_call_id"] == "box-call"
        assert_box_wire(wire)
    finally:
        await factory.aclose()


async def test_shared_factory_boot_and_hot_updates_keep_box_adapter(tmp_path, wire):
    manager = OctopHarnessAgentManager(log_dir=tmp_path / "logs")
    manager.add_provider(box_provider())
    factory = manager.shared_factory
    assert isinstance(factory, OctopChatModelFactory)
    first = factory.get_chat_model("appliance/RM-01 VLM")
    await first.ainvoke("synthetic")
    manager.add_provider(box_provider())
    second = factory.get_chat_model("appliance/RM-01 VLM")
    assert second is not first
    await second.ainvoke("synthetic")
    assert_box_wire(wire)
    await manager.aclose()
    manager = OctopHarnessAgentManager(providers=[box_provider()], log_dir=tmp_path / "boot")
    assert isinstance(manager.shared_factory, OctopChatModelFactory)
    await manager.aclose()


@pytest.mark.parametrize("injected", [False, True])
async def test_factory_lifetime_preserves_standalone_and_injected_ownership(
    tmp_path, monkeypatch, injected
):
    shared = OctopChatModelFactory([box_provider()]) if injected else None
    agent = OctopHarnessAgent(
        HarnessAgentConfig(
            name="box-offline",
            workspace_dir=str(tmp_path),
            providers=[box_provider()],
            default_model="appliance/RM-01 VLM",
            memory_enabled=False,
        ),
        model_factory=shared,
    )
    assert isinstance(agent.model_factory, OctopChatModelFactory)
    close = AsyncMock()
    monkeypatch.setattr(agent.model_factory, "aclose", close)
    await agent.aclose()
    if injected:
        close.assert_not_awaited()
    else:
        close.assert_awaited_once()
