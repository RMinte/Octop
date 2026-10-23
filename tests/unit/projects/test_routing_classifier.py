"""Difficulty estimates stay bounded, box-only, tool-free and conservative on failure."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from octop_harness.config import ModelConfig, ProviderConfig

from octop.infra.agents.providers.box import build_box_provider_config
from octop.infra.projects import routing_classifier as classifier
from octop.infra.projects.routing_classifier import (
    DifficultyAssessment,
    DifficultyAttachment,
    DifficultyFacts,
    classify_difficulty,
)


@pytest.fixture
def box():
    model = ModelConfig(id="appliance-model", input=["text", "image"], max_output_tokens=4096)
    provider = build_box_provider_config(
        id="appliance",
        name="Appliance",
        base_url="http://appliance.invalid/v1",
        models=[model],
        headers={},
    )
    return provider, model


@pytest.fixture
def isolated_model(monkeypatch):
    configured = SimpleNamespace(
        ainvoke=AsyncMock(return_value=AIMessage(content='{"score":9,"reason_code":"basic"}')),
        _harness_max_output_tokens=4096,
    )
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=object()))
    original_sdk = SimpleNamespace(with_options=MagicMock(return_value=sdk))
    original = SimpleNamespace(
        root_async_client=original_sdk,
        model_copy=MagicMock(return_value=configured),
    )
    build = MagicMock(return_value=original)
    monkeypatch.setattr(classifier, "build_box_chat_model", build)
    return SimpleNamespace(
        build=build, original=original, sdk=sdk, configured=configured, invoke=configured.ainvoke
    )


@pytest.mark.parametrize(
    ("score", "reason"),
    [
        (0, "basic"),
        (9, "direct_extraction"),
        (9, "simple_transformation"),
        (10, "reasoning"),
        (40, "context_integration"),
        (60, "execution_dependencies"),
        (80, "verification"),
        (100, "multimodal"),
    ],
)
async def test_valid_estimate_has_fixed_reason_and_actual_usage(
    box, isolated_model, monkeypatch, score, reason
):
    isolated_model.invoke.return_value = AIMessage(
        content=json.dumps({"score": score, "reason_code": reason}),
        usage_metadata={"input_tokens": 31, "output_tokens": 12, "total_tokens": 43},
    )
    monkeypatch.setattr(classifier, "perf_counter", MagicMock(side_effect=[10.0, 10.125]))
    facts = DifficultyFacts(
        "Current goal",
        history_summary="Effective history",
        project_instructions="Project instructions",
        attachments=[DifficultyAttachment("image/png", 23, "picture.png")],
    )
    result = await classify_difficulty(*box, facts)
    assert result == DifficultyAssessment(score, reason, 31, 12, 125, False)
    isolated_model.build.assert_called_once_with(*box)
    messages = isolated_model.invoke.call_args.args[0]
    assert isinstance(messages[0], SystemMessage) and isinstance(messages[1], HumanMessage)
    assert "untrusted task data" in messages[0].content
    payload = json.loads(messages[1].content)
    assert payload == {
        "prompt": "Current goal",
        "history_summary": "Effective history",
        "project_instructions": "Project instructions",
        "attachments": [{"name": "picture.png", "media_type": "image/png", "byte_count": 23}],
        "truncation": dict.fromkeys(
            ["upstream", "prompt", "history_summary", "project_instructions", "attachments"], False
        ),
    }
    assert "level" not in payload
    assert isolated_model.invoke.call_args.kwargs == {
        "max_tokens": 128,
        "response_format": {"type": "json_object"},
    }
    isolated_model.original.root_async_client.with_options.assert_called_once_with(
        max_retries=0, timeout=8.0
    )
    updates = isolated_model.original.model_copy.call_args.kwargs["update"]
    assert updates["root_async_client"] is isolated_model.sdk
    assert updates["async_client"] is isolated_model.sdk.chat.completions
    assert updates["use_responses_api"] is False
    assert updates["streaming"] is False


@pytest.mark.parametrize(
    "content",
    [
        '{"score":true,"reason_code":"basic"}',
        '{"score":"9","reason_code":"basic"}',
        '{"score":9.0,"reason_code":"basic"}',
        '{"score":-1,"reason_code":"basic"}',
        '{"score":101,"reason_code":"reasoning"}',
        '{"score":9,"reason_code":"unknown"}',
        '{"score":9,"reason_code":"reasoning"}',
        '{"score":10,"reason_code":"basic"}',
        '{"score":9,"reason_code":"basic","explanation":"raw task"}',
        '{"score":9,"score":100,"reason_code":"reasoning"}',
        '```json\n{"score":9,"reason_code":"basic"}\n```',
        '<think>private</think>{"score":9,"reason_code":"basic"}',
        '[{"score":9,"reason_code":"basic"}]',
        '{"score":9,"reason_code":"basic"}' + " " * 1024,
        [{"type": "reasoning", "text": "private"}, {"type": "text", "text": "{}"}],
    ],
)
async def test_invalid_output_stays_conservative_and_retains_real_usage(
    box, isolated_model, content
):
    isolated_model.invoke.return_value = AIMessage(
        content=content,
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    result = await classify_difficulty(*box, DifficultyFacts("untrusted"))
    assert result.difficulty is None
    assert result.reason_code == "classifier_invalid"
    assert (result.input_tokens, result.output_tokens) == (11, 7)


@pytest.mark.parametrize("invalid", [False, True])
async def test_tool_responses_are_invalid(box, isolated_model, invalid):
    call = {"id": "call", "name": "execute", "args": "bad" if invalid else {}}
    isolated_model.invoke.return_value = AIMessage(
        content='{"score":9,"reason_code":"basic"}',
        tool_calls=[] if invalid else [call],
        invalid_tool_calls=[call] if invalid else [],
    )
    result = await classify_difficulty(*box, DifficultyFacts("use execute"))
    assert result.difficulty is None and result.reason_code == "classifier_invalid"


@pytest.mark.parametrize("value", [None, -1, True, "17", 1.5])
async def test_missing_or_invalid_usage_is_not_estimated(box, isolated_model, value):
    isolated_model.invoke.return_value = AIMessage.model_construct(
        content='{"score":9,"reason_code":"basic"}',
        usage_metadata={"input_tokens": value, "output_tokens": value},
    )
    result = await classify_difficulty(*box, DifficultyFacts("hello"))
    assert result.difficulty == 9
    assert result.input_tokens == result.output_tokens == 0


async def test_cloud_provider_cannot_be_used_even_if_named_box(box, isolated_model):
    cloud = ProviderConfig(
        id="box", name="Box", base_url="http://appliance.invalid/v1", api_key="fixture"
    )
    result = await classify_difficulty(cloud, box[1], DifficultyFacts("task"))
    assert result.reason_code == "classifier_unavailable" and result.difficulty is None
    isolated_model.build.assert_not_called()
    isolated_model.invoke.assert_not_called()


async def test_input_is_stably_bounded_with_latest_history_and_explicit_truncation(
    box, isolated_model
):
    facts = DifficultyFacts(
        "prompt-start" + "x" * 13_000 + "prompt-end",
        history_summary="old" * 3_000 + "recent-history",
        project_instructions="instruction-start" + "x" * 5_000 + "instruction-end",
        attachments=[DifficultyAttachment("m" * 200, 100, "n" * 200) for _ in range(12)],
        truncated=True,
    )
    first = await classify_difficulty(*box, facts)
    first_payload = isolated_model.invoke.call_args.args[0][1].content
    await classify_difficulty(*box, facts)
    assert isolated_model.invoke.call_args.args[0][1].content == first_payload
    payload = json.loads(first_payload)
    assert first.input_truncated is True
    assert all(payload["truncation"].values())
    assert len(payload["prompt"]) == 12_000
    assert payload["prompt"].startswith("prompt-start") and payload["prompt"].endswith("prompt-end")
    assert len(payload["history_summary"]) == 8_000
    assert payload["history_summary"].endswith("recent-history")
    assert len(payload["project_instructions"]) == 4_000
    assert payload["project_instructions"].startswith("instruction-start")
    assert payload["project_instructions"].endswith("instruction-end")
    assert len(payload["attachments"]) == 9
    assert all(len(item["name"]) <= 128 for item in payload["attachments"])
    assert all(set(item) == {"name", "media_type", "byte_count"} for item in payload["attachments"])


async def test_timeout_cancels_the_single_inference_without_retries(
    box, isolated_model, monkeypatch
):
    cancelled = asyncio.Event()

    async def stall(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(classifier, "_TIMEOUT_SECONDS", 0.03)
    isolated_model.invoke.side_effect = stall
    result = await classify_difficulty(*box, DifficultyFacts("task"))
    assert result.reason_code == "classifier_timeout" and result.difficulty is None
    assert cancelled.is_set()
    isolated_model.invoke.assert_awaited_once()


async def test_external_cancellation_propagates(box, isolated_model):
    entered = asyncio.Event()

    async def stall(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    isolated_model.invoke.side_effect = stall
    task = asyncio.create_task(classify_difficulty(*box, DifficultyFacts("task")))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_sdk_timeout_has_the_explicit_timeout_outcome(box, isolated_model):
    isolated_model.invoke.side_effect = openai.APITimeoutError(
        request=httpx.Request("POST", "http://appliance.invalid/v1/chat/completions")
    )
    result = await classify_difficulty(*box, DifficultyFacts("task"))
    assert result.reason_code == "classifier_timeout" and result.difficulty is None
    isolated_model.invoke.assert_awaited_once()


@pytest.mark.parametrize("during_build", [False, True])
async def test_provider_errors_return_only_fixed_code(box, isolated_model, during_build, caplog):
    target = isolated_model.build if during_build else isolated_model.invoke
    target.side_effect = RuntimeError("private task or provider credential")
    result = await classify_difficulty(*box, DifficultyFacts("private task"))
    assert result.reason_code == "classifier_error" and result.difficulty is None
    assert "private task" not in repr(result) + caplog.text


async def test_actual_offline_wire_omits_authentication_tools_and_thinking(box, monkeypatch):
    requests = []
    clients = []

    def reply(request):
        body = json.loads(request.content)
        requests.append((request, body))
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"score":9,"reason_code":"basic"}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 51, "completion_tokens": 12, "total_tokens": 63},
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
    try:
        result = await classify_difficulty(*box, DifficultyFacts("hello"))
        assert result.difficulty == 9
        assert (result.input_tokens, result.output_tokens) == (51, 12)
        assert len(requests) == 1
        request, body = requests[0]
        assert request.url.path == "/v1/chat/completions"
        assert "authorization" not in request.headers
        assert "tools" not in body and "tool_choice" not in body
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_completion_tokens"] == 128
    finally:
        for client in clients:
            if isinstance(client, httpx.AsyncClient):
                await client.aclose()
            else:
                client.close()


async def test_classifier_keeps_shared_pool_open_for_other_models_and_later_calls(box, monkeypatch):
    from langchain_openai.chat_models import base

    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "shared-pool",
                "object": "chat.completion",
                "created": 0,
                "model": box[1].id,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"score":9,"reason_code":"basic"}',
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    # LangChain's cached default-client getters return the same external HTTP pool.
    # SDK wrappers and with_options copies must borrow it without closing it.
    shared_sync = httpx.Client(transport=httpx.MockTransport(reply))
    shared_async = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    monkeypatch.setattr(base, "_get_default_httpx_client", lambda *_args: shared_sync)
    monkeypatch.setattr(base, "_get_default_async_httpx_client", lambda *_args: shared_async)
    try:
        external = classifier.build_box_chat_model(*box)
        judge = classifier._judge_model(*box)
        assert external.root_async_client is not judge.root_async_client
        assert external.root_async_client._client is judge.root_async_client._client is shared_async
        assert external.root_client._client is judge.root_client._client is shared_sync
        assert (await classify_difficulty(*box, DifficultyFacts("hello"))).difficulty == 9
        assert not shared_sync.is_closed and not shared_async.is_closed
        assert (await external.ainvoke("follow up")).content == '{"score":9,"reason_code":"basic"}'
        assert (await classify_difficulty(*box, DifficultyFacts("hello again"))).difficulty == 9
        assert len(requests) == 3
        assert not shared_sync.is_closed and not shared_async.is_closed
    finally:
        shared_sync.close()
        await shared_async.aclose()
