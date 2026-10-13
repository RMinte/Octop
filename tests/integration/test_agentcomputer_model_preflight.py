"""Offline ProviderStore-to-harness wire smoke, without opening production execution."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.tools import tool
from octop_harness.agent import HarnessAgent
from octop_harness.config import HarnessAgentConfig
from octop_harness.llm import factory as harness_factory
from openai import AuthenticationError

from octop.infra.agents.providers.store import ProviderStore
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.providers import ProviderRepo
from octop.infra.utils.paths import PathLayout


def _completion(message: dict[str, Any], finish_reason: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "qwen-27b-fixture",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def test_configured_provider_real_harness_offline_wire_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise tool, streaming, and error paths over a synthetic HTTP wire."""
    paths = PathLayout(tmp_path / "octop")
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    providers = ProviderRepo(pool)
    providers.create(
        name="local",
        kind="openai",
        base_url="https://wire.invalid/v1",
        api_key="fixture-local-key",
        models_json=json.dumps([{"id": "qwen-27b-fixture", "enabled": True}]),
    )
    providers.create(
        name="empty-key",
        kind="openai",
        base_url="https://wire.invalid/v1",
        api_key="",
        models_json=json.dumps([{"id": "qwen-27b-fixture", "enabled": True}]),
    )
    store = ProviderStore(providers)
    assert store.is_model_ref_usable("local/qwen-27b-fixture")
    assert not store.is_model_ref_usable("empty-key/qwen-27b-fixture")
    configs = store.build_harness_configs()
    assert len(configs) == 1 and configs[0].models[0].id == "qwen-27b-fixture"

    wire: list[dict[str, Any]] = []
    streamed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://wire.invalid/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fixture-local-key"
        body = json.loads(request.content)
        assert body["model"] == "qwen-27b-fixture"
        assert "store" not in body and "previous_response_id" not in body
        if body.get("stream"):
            streamed.append(body)
            chunks = [
                {
                    "id": "chatcmpl-stream-fixture",
                    "object": "chat.completion.chunk",
                    "created": 2,
                    "model": "qwen-27b-fixture",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "流式"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-stream-fixture",
                    "object": "chat.completion.chunk",
                    "created": 2,
                    "model": "qwen-27b-fixture",
                    "choices": [{"index": 0, "delta": {"content": "完成"}, "finish_reason": None}],
                },
                {
                    "id": "chatcmpl-stream-fixture",
                    "object": "chat.completion.chunk",
                    "created": 2,
                    "model": "qwen-27b-fixture",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            content = "".join(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(content + "data: [DONE]\n\n").encode(),
            )
        if any(
            message["role"] == "user" and "模拟认证失败" in str(message["content"])
            for message in body["messages"]
        ):
            rejected.append(body)
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": "synthetic invalid key",
                        "type": "invalid_api_key",
                        "code": "invalid_api_key",
                    }
                },
            )
        wire.append(body)
        if len(wire) == 1:
            assert any(item["function"]["name"] == "synthetic_echo" for item in body["tools"])
            assert not any(message["role"] == "tool" for message in body["messages"])
            payload = _completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-fixture-1",
                            "type": "function",
                            "function": {
                                "name": "synthetic_echo",
                                "arguments": '{"value":"ping"}',
                            },
                        }
                    ],
                },
                "tool_calls",
            )
        else:
            assert len(wire) == 2
            result = next(message for message in body["messages"] if message["role"] == "tool")
            assert result["tool_call_id"] == "call-fixture-1"
            assert result["content"] == "pong"
            payload = _completion({"role": "assistant", "content": "已收到 pong。"}, "stop")
            payload.pop("usage")
        return httpx.Response(200, json=payload)

    sync_http = httpx.Client(transport=httpx.MockTransport(answer))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    original_get_class = harness_factory._get_reasoning_aware_chat_openai

    def transport_class(base_class):
        actual_class = original_get_class(base_class)

        def construct(**kwargs):
            return actual_class(http_client=sync_http, http_async_client=async_http, **kwargs)

        return construct

    monkeypatch.setattr(harness_factory, "_get_reasoning_aware_chat_openai", transport_class)
    tool_inputs: list[str] = []

    @tool
    def synthetic_echo(value: str) -> str:
        """Return one harmless synthetic preflight value."""
        tool_inputs.append(value)
        return "pong"

    config = HarnessAgentConfig(
        workspace_dir=tmp_path / "workspace",
        backend="filesystem",
        providers=configs,
        default_model="local/qwen-27b-fixture",
        tools=[synthetic_echo],
        checkpointer=False,
        session_log_enabled=False,
        todos_enabled=False,
        ask_user_enabled=False,
        team_enabled=False,
        web_search_tools=False,
        pii_enabled=False,
        media_offload_enabled=False,
        model_retry_enabled=False,
    )
    try:
        agent = await asyncio.to_thread(HarnessAgent, config)
        try:
            answer_state = await agent.call(
                "请调用 synthetic_echo，参数 value=ping，然后用中文确认结果。"
            )
            stream_chunks = [chunk async for chunk in agent.stream("只输出流式完成。")]
            with pytest.raises(AuthenticationError, match="synthetic invalid key"):
                await agent.call("模拟认证失败。")
        finally:
            await agent.aclose()
    finally:
        await async_http.aclose()
        sync_http.close()
        pool.close()
    assert tool_inputs == ["ping"]
    assert len(wire) == 2
    assert answer_state["messages"][-1].content == "已收到 pong。"
    assert answer_state["messages"][-1].usage_metadata is None
    assert len(streamed) == 1
    assert len(rejected) == 1
    assert any(chunk.get("type") == "state_snapshot" for chunk in stream_chunks)
    assert (
        "".join(
            str(chunk.get("content", "")) for chunk in stream_chunks if chunk.get("type") == "token"
        )
        == "流式完成"
    )


async def test_harness_stream_cancel_preempts_blocked_model_without_running_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread-scoped harness cancel closes a blocked local stream, not a remote Run."""
    paths = PathLayout(tmp_path / "octop")
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    providers = ProviderRepo(pool)
    providers.create(
        name="local",
        kind="openai",
        base_url="https://wire.invalid/v1",
        api_key="fixture-local-key",
        models_json=json.dumps([{"id": "fixture-model", "enabled": True}]),
    )
    configs = ProviderStore(providers).build_harness_configs()
    blocked = asyncio.Event()
    released = asyncio.Event()
    closed = asyncio.Event()
    tool_inputs: list[str] = []
    requests: list[httpx.Request] = []

    class BlockedCompletion(httpx.AsyncByteStream):
        async def __aiter__(self):
            partial_tool_call = {
                "id": "chatcmpl-cancel-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-cancel-fixture",
                                    "type": "function",
                                    "function": {
                                        "name": "synthetic_echo",
                                        "arguments": '{"value":"ping"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(partial_tool_call)}\n\n".encode()
            blocked.set()
            await released.wait()
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            closed.set()

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://wire.invalid/v1/chat/completions"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert any(item["function"]["name"] == "synthetic_echo" for item in body["tools"])
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BlockedCompletion(),
        )

    sync_http = httpx.Client(transport=httpx.MockTransport(answer))
    async_http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    original_get_class = harness_factory._get_reasoning_aware_chat_openai

    def transport_class(base_class):
        actual_class = original_get_class(base_class)

        def construct(**kwargs):
            return actual_class(http_client=sync_http, http_async_client=async_http, **kwargs)

        return construct

    monkeypatch.setattr(harness_factory, "_get_reasoning_aware_chat_openai", transport_class)

    @tool
    def synthetic_echo(value: str) -> str:
        """Record whether a cancelled stream executes the proposed tool call."""
        tool_inputs.append(value)
        return "pong"

    config = HarnessAgentConfig(
        workspace_dir=tmp_path / "workspace",
        backend="filesystem",
        providers=configs,
        default_model="local/fixture-model",
        tools=[synthetic_echo],
        checkpointer=False,
        session_log_enabled=False,
        todos_enabled=False,
        ask_user_enabled=False,
        team_enabled=False,
        web_search_tools=False,
        pii_enabled=False,
        media_offload_enabled=False,
        model_retry_enabled=False,
    )
    agent = await asyncio.to_thread(HarnessAgent, config)
    thread_id = "cancel-fixture-thread"

    async def consume() -> None:
        async for _ in agent.stream(
            {"messages": "请调用 synthetic_echo，参数 value=ping。", "thread_id": thread_id}
        ):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=10)
        agent.cancel(thread_id)
        await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(closed.wait(), timeout=5)
    finally:
        released.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await agent.aclose()
        await async_http.aclose()
        sync_http.close()
        pool.close()

    assert len(requests) == 1
    assert tool_inputs == []
