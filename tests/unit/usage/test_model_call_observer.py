"""Logical invocation receipts preserve provenance without retaining model content."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import httpx
import openai
import pytest
from langchain_core.callbacks import AsyncCallbackManager
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult
from langchain_openai import ChatOpenAI
from octop_harness.config import ModelConfig, ProviderConfig

from octop.infra.agents.providers.box import build_box_chat_model, build_box_provider_config
from octop.infra.usage.model_calls import (
    ModelCallObserver,
    attach_model_observer,
    current_model_call_scope,
    model_call_scope,
    normalize_model_usage,
    override_model_call_scope,
)

FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
}


class MemoryRepo:
    def __init__(self):
        self.starts = []
        self.finishes = []
        self.thread_ids = []

    def start(self, row):
        self.thread_ids.append(threading.get_ident())
        self.starts.append(row)
        return row

    def finish(self, row):
        self.thread_ids.append(threading.get_ident())
        self.finishes.append(row)
        return row


@pytest.fixture
def services():
    return SimpleNamespace(model_call_repo=MemoryRepo())


@pytest.fixture
def provider():
    return ProviderConfig(
        id="configured-provider", base_url="http://appliance.invalid/v1", api_key="fixture"
    )


def model_for(provider, *, response=None, metadata=None):
    return attach_model_observer(
        FakeMessagesListChatModel(
            responses=[response or AIMessage(content="private response")], metadata=metadata
        ),
        provider,
        ModelConfig(id="actual-model"),
    )


def observer_for(model):
    callbacks = model.callbacks
    handlers = callbacks.handlers if isinstance(callbacks, AsyncCallbackManager) else callbacks
    return next(handler for handler in handlers if isinstance(handler, ModelCallObserver))


def test_missing_usage_is_null_and_reported_zero_is_retained():
    missing, source = normalize_model_usage()
    assert source == "missing"
    assert all(missing[field] is None for field in FIELDS)
    assert missing["presence"] == dict.fromkeys(FIELDS, False)
    zero, source = normalize_model_usage(usage_metadata=dict.fromkeys(FIELDS, 0))
    assert source == "usage_metadata"
    assert all(zero[field] == 0 for field in FIELDS)
    assert zero["presence"] == dict.fromkeys(FIELDS, True)


def test_cache_and_reasoning_are_subdivisions_and_aliases_are_not_added_twice():
    result, source = normalize_model_usage(
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 30,
            "input_token_details": {"cache_read": 40, "cached_tokens": 40, "cache_creation": 10},
            "output_token_details": {"reasoning": 12, "reasoning_tokens": 12},
        }
    )
    assert source == "usage_metadata"
    assert result == {
        "input_tokens": 100,
        "output_tokens": 30,
        "total_tokens": 130,
        "uncached_input_tokens": 50,
        "cache_read_tokens": 40,
        "cache_write_tokens": 10,
        "reasoning_tokens": 12,
        "presence": {
            **dict.fromkeys(FIELDS, True),
            "total_tokens": False,
            "uncached_input_tokens": False,
        },
    }


@pytest.mark.parametrize("value", [True, "12", 2.5, -1, None])
def test_invalid_counts_do_not_become_zero_or_estimates(value):
    result, source = normalize_model_usage(usage_metadata=dict.fromkeys(FIELDS, value))
    assert source == "missing"
    assert all(result[field] is None for field in FIELDS)


def test_wire_presence_wins_over_adapter_defaults_and_keeps_unknown_details():
    result, source = normalize_model_usage(
        usage_metadata={"input_tokens": 0},
        response_metadata={
            "token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 9},
            "private_header": "never retain",
        },
    )
    assert source == "response_metadata"
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5
    assert result["total_tokens"] == 15
    assert result["cache_read_tokens"] is None
    assert result["uncached_input_tokens"] is None
    assert "private_header" not in repr(result)


def test_raw_anthropic_input_is_uncached_and_total_requires_known_cache_buckets():
    raw = {"input_tokens": 20, "output_tokens": 7, "cache_read_input_tokens": 30}
    partial, _ = normalize_model_usage(response_metadata={"usage": raw}, protocol="anthropic")
    assert partial["uncached_input_tokens"] == 20
    assert partial["input_tokens"] is None and partial["total_tokens"] is None
    complete, _ = normalize_model_usage(
        response_metadata={"usage": {**raw, "cache_creation_input_tokens": 5}},
        protocol="anthropic",
    )
    assert complete["input_tokens"] == 55 and complete["total_tokens"] == 62
    assert complete["presence"]["input_tokens"] is False
    assert complete["presence"]["uncached_input_tokens"] is True


def test_canonical_only_openai_stream_zero_is_unknown_and_total_is_derived():
    partial, _ = normalize_model_usage(
        usage_metadata={"input_tokens": 7, "output_tokens": 0, "total_tokens": 7},
        openai_adapter=True,
    )
    assert partial["input_tokens"] == 7
    assert partial["output_tokens"] is None and partial["total_tokens"] is None
    known, _ = normalize_model_usage(
        usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
        openai_adapter=True,
    )
    assert known["total_tokens"] == 9 and known["presence"]["total_tokens"] is False
    # Other adapters do not inherit ChatOpenAI's known default-zero behavior.
    other, _ = normalize_model_usage(
        usage_metadata={"input_tokens": 7, "output_tokens": 0, "total_tokens": 7},
        protocol="anthropic",
    )
    assert other["output_tokens"] == 0 and other["presence"]["output_tokens"] is True


async def test_scope_is_required_and_binds_the_original_instance_until_finish(provider, services):
    model = model_for(provider)
    await model.ainvoke("no configured instance")
    assert not services.model_call_repo.starts
    observer = observer_for(model)
    run_id, parent_id = uuid4(), uuid4()
    second = SimpleNamespace(model_call_repo=MemoryRepo())
    with model_call_scope(
        services,
        user_id=3,
        purpose="execution",
        agent_id="agent",
        project_id="project",
        task_id="task",
        turn_id="turn",
        submission_id="submission",
        thread_id="thread",
    ):
        await observer.on_chat_model_start(
            {"secret": "serialized"},
            [[HumanMessage("private request")]],
            run_id=run_id,
            parent_run_id=parent_id,
        )
    with model_call_scope(second, user_id=9, purpose="chat"):
        await observer.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=AIMessage(content="private reply"))]]),
            run_id=run_id,
        )
    assert current_model_call_scope() is None
    assert len(services.model_call_repo.starts) == len(services.model_call_repo.finishes) == 1
    assert not second.model_call_repo.starts and not second.model_call_repo.finishes
    row = services.model_call_repo.finishes[0]
    assert (row.user_id, row.task_id, row.thread_id) == (3, "task", "thread")
    assert row.call_id == str(run_id) and row.parent_call_id == ""
    assert json.loads(row.metadata_json)["parent_run_id"] == str(parent_id)
    assert row.model_ref == "configured-provider/actual-model" and row.status == "succeeded"
    assert "private" not in repr(asdict(row)) and "secret" not in repr(asdict(row))
    assert all(value != threading.get_ident() for value in services.model_call_repo.thread_ids)


async def test_sync_invoke_inside_async_scope_keeps_context_and_offloads_writes(provider, services):
    model = model_for(provider)
    with model_call_scope(services, user_id=4, purpose="knowledge_ocr", agent_id="ocr-agent"):
        assert model.invoke("private image-derived request").content == "private response"
        assert (
            await asyncio.to_thread(model.invoke, "another request")
        ).content == "private response"
    assert len(services.model_call_repo.finishes) == 2
    assert all(
        row.user_id == 4 and row.purpose == "knowledge_ocr"
        for row in services.model_call_repo.finishes
    )
    assert all(row.status == "succeeded" for row in services.model_call_repo.finishes)


def test_nested_override_inherits_services_and_identity_and_resets(services):
    with override_model_call_scope(purpose="subagent", agent_id="peer") as absent:
        assert absent is None
    with model_call_scope(services, user_id=1, purpose="execution", task_id="task") as root_scope:
        with override_model_call_scope(purpose="subagent", agent_id="peer") as child:
            assert child.services is services and child.user_id == 1
            assert (
                child.task_id == "task" and child.agent_id == "peer" and child.purpose == "subagent"
            )
        assert current_model_call_scope() is root_scope
    assert current_model_call_scope() is None


@pytest.mark.parametrize(
    "explicit", [None, "unknown", "box", "third_party", "self_hosted", ["box"]]
)
def test_ordinary_provider_is_third_party_regardless_of_model_metadata(provider, explicit):
    metadata = {"execution_source": explicit} if explicit is not None else None
    model = model_for(provider, metadata=metadata)
    assert observer_for(model).source == "third_party"


def test_explicit_box_marker_wins_over_metadata_and_attachment_is_idempotent():
    provider = build_box_provider_config(
        id="appliance",
        name="Appliance",
        base_url="http://appliance.invalid/v1",
        models=[],
        headers={},
    )
    model = model_for(provider, metadata={"execution_source": "third_party"})
    assert observer_for(model).source == "box"
    again = attach_model_observer(model, provider, ModelConfig(id="actual-model"))
    assert again is model
    assert len([item for item in model.callbacks if isinstance(item, ModelCallObserver)]) == 1


def test_callback_manager_is_copied_and_existing_callbacks_preserved(provider):
    manager = AsyncCallbackManager([])
    model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")], callbacks=manager)
    configured = attach_model_observer(model, provider, ModelConfig(id="actual-model"))
    assert model.callbacks is manager and not manager.handlers
    assert configured.callbacks is not manager
    assert len(configured.callbacks.handlers) == 1
    assert isinstance(configured.callbacks.handlers[0], ModelCallObserver)


@pytest.mark.parametrize(
    ("scope_purpose", "metadata", "expected"),
    [
        ("execution", {"lc_source": "summarization"}, "summarization"),
        ("execution", {"ls_agent_type": "subagent"}, "subagent"),
        ("compaction", {"lc_source": "summarization"}, "compaction"),
        ("chat", {"lc_agent_name": "private arbitrary node"}, "chat"),
    ],
)
async def test_framework_metadata_identifies_summary_and_subagent_without_storing_text(
    provider, services, scope_purpose, metadata, expected
):
    model = model_for(provider)
    with model_call_scope(services, user_id=1, purpose=scope_purpose):
        await model.ainvoke("private request", config={"metadata": metadata})
    row = services.model_call_repo.finishes[0]
    assert row.purpose == expected
    assert "private" not in repr(asdict(row))
    assert "lc_source" not in row.metadata_json and "lc_agent_name" not in row.metadata_json


async def test_error_and_cancellation_keep_partial_usage_without_exception_text(provider, services):
    model = model_for(provider)
    observer = observer_for(model)
    for error in (RuntimeError("private exception credential"), asyncio.CancelledError()):
        run_id = uuid4()
        with model_call_scope(services, user_id=1, purpose="execution"):
            await observer.on_chat_model_start({}, [[HumanMessage("private")]], run_id=run_id)
        await observer.on_llm_error(
            error,
            run_id=run_id,
            response=LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(
                                content="private partial output",
                                usage_metadata={
                                    "input_tokens": 10,
                                    "output_tokens": 0,
                                    "total_tokens": 10,
                                },
                            )
                        )
                    ]
                ]
            ),
        )
    failed, cancelled = services.model_call_repo.finishes
    assert failed.status == "failed" and cancelled.status == "cancelled"
    assert json.loads(cancelled.usage_json)["output_tokens"] == 0
    assert "private" not in repr([asdict(failed), asdict(cancelled)])


async def test_replayed_start_and_end_do_not_create_another_receipt(provider, services):
    observer = observer_for(model_for(provider))
    run_id = uuid4()
    with model_call_scope(services, user_id=1, purpose="execution"):
        await observer.on_chat_model_start({}, [[]], run_id=run_id)
        await observer.on_chat_model_start({}, [[]], run_id=run_id)
        await observer.on_llm_end(LLMResult(generations=[]), run_id=run_id)
        await observer.on_llm_end(LLMResult(generations=[]), run_id=run_id)
    assert len(services.model_call_repo.starts) == len(services.model_call_repo.finishes) == 1


async def test_concurrent_scopes_keep_separate_owners_and_sinks(provider):
    model = model_for(provider)
    first, second = (SimpleNamespace(model_call_repo=MemoryRepo()) for _ in range(2))

    async def invoke(services, user_id):
        with model_call_scope(services, user_id=user_id, purpose="chat"):
            await model.ainvoke("private")

    await asyncio.gather(invoke(first, 1), invoke(second, 2))
    assert [row.user_id for row in first.model_call_repo.finishes] == [1]
    assert [row.user_id for row in second.model_call_repo.finishes] == [2]


async def test_sdk_transparent_retry_is_one_logical_invocation(provider, services, monkeypatch):
    requests = []
    clients = []

    def reply(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(500, json={"error": {"message": "private failure"}})
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 0,
                "model": "actual-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "private"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
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
        model = attach_model_observer(
            build_box_chat_model(provider, ModelConfig(id="actual-model")),
            provider,
            ModelConfig(id="actual-model"),
        )
        with model_call_scope(services, user_id=1, purpose="chat"):
            await model.ainvoke("private request")
        assert len(requests) == 2
        assert len(services.model_call_repo.starts) == len(services.model_call_repo.finishes) == 1
        row = services.model_call_repo.finishes[0]
        assert row.status == "succeeded" and json.loads(row.usage_json)["total_tokens"] == 9
        assert row.source == "third_party"
    finally:
        for client in clients:
            if isinstance(client, httpx.AsyncClient):
                await client.aclose()
            else:
                client.close()


@pytest.mark.parametrize(
    ("wire_usage", "expected"),
    [
        (None, {"input_tokens": None, "output_tokens": None, "total_tokens": None}),
        ({}, {"input_tokens": None, "output_tokens": None, "total_tokens": None}),
        ({"prompt_tokens": 7}, {"input_tokens": 7, "output_tokens": None, "total_tokens": None}),
        (
            {"completion_tokens": 0},
            {"input_tokens": None, "output_tokens": 0, "total_tokens": None},
        ),
        (
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        ),
    ],
)
async def test_real_chatopenai_partial_wire_does_not_report_adapter_default_zero(
    provider, services, wire_usage, expected
):
    def reply(_request):
        body = {
            "id": "offline-partial",
            "object": "chat.completion",
            "created": 0,
            "model": "actual-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "private"},
                    "finish_reason": "stop",
                }
            ],
        }
        if wire_usage is not None:
            body["usage"] = wire_usage
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(reply)) as sync_http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as async_http:
            model = attach_model_observer(
                ChatOpenAI(
                    model="actual-model",
                    api_key="fixture",
                    base_url=provider.base_url,
                    http_client=sync_http,
                    http_async_client=async_http,
                ),
                provider,
                ModelConfig(id="actual-model"),
            )
            with model_call_scope(services, user_id=1, purpose="chat"):
                await model.ainvoke("private request")
    usage = json.loads(services.model_call_repo.finishes[0].usage_json)
    for field, value in expected.items():
        assert usage[field] == value
        assert usage["presence"][field] is (value is not None)


async def test_real_stream_cancellation_closes_the_invocation_without_invented_usage(
    provider, services
):
    entered, finished = asyncio.Event(), threading.Event()
    original_finish = services.model_call_repo.finish

    def finish(row):
        result = original_finish(row)
        finished.set()
        return result

    services.model_call_repo.finish = finish

    class PendingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            event = {
                "id": "offline-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "actual-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "private"},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(event)}\n\n".encode()
            entered.set()
            await asyncio.Event().wait()

    def reply(_request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=PendingStream()
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as sync_http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as async_http:
            model = attach_model_observer(
                ChatOpenAI(
                    model="actual-model",
                    api_key="fixture",
                    base_url=provider.base_url,
                    http_client=sync_http,
                    http_async_client=async_http,
                ),
                provider,
                ModelConfig(id="actual-model"),
            )

            async def consume():
                async for _chunk in model.astream("private request"):
                    pass

            with model_call_scope(services, user_id=1, purpose="chat"):
                task = asyncio.create_task(consume())
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert await asyncio.to_thread(finished.wait, 2)
    assert len(services.model_call_repo.finishes) == 1
    row = services.model_call_repo.finishes[0]
    assert row.status == "cancelled" and row.finished_at >= row.started_at
    usage = json.loads(row.usage_json)
    assert all(usage[field] is None for field in FIELDS)
    assert not observer_for(model)._runs


async def test_cancellation_during_start_settles_both_running_and_cancelled_receipts(
    provider, services
):
    entered, release = threading.Event(), threading.Event()
    original_start = services.model_call_repo.start

    def start(row):
        entered.set()
        assert release.wait(2)
        return original_start(row)

    services.model_call_repo.start = start
    observer = observer_for(model_for(provider))
    with model_call_scope(services, user_id=1, purpose="chat"):
        task = asyncio.create_task(observer.on_chat_model_start({}, [[]], run_id=uuid4()))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(services.model_call_repo.starts) == len(services.model_call_repo.finishes) == 1
    assert services.model_call_repo.finishes[0].status == "cancelled"
    assert not observer._runs


async def test_stream_can_close_from_another_task_without_context_token_leak(provider, services):
    class PendingModel(FakeMessagesListChatModel):
        async def _astream(self, *_args, **_kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content="private partial"))
            await asyncio.Event().wait()

    model = attach_model_observer(
        PendingModel(responses=[AIMessage(content="private")]),
        provider,
        ModelConfig(id="actual-model"),
    )
    with model_call_scope(services, user_id=1, purpose="chat"):
        stream = model.astream("private")
        await asyncio.create_task(anext(stream))
        await asyncio.create_task(stream.aclose())
    assert len(services.model_call_repo.finishes) == 1
    assert services.model_call_repo.finishes[0].status == "cancelled"
    assert not observer_for(model)._runs


async def test_model_copy_bind_tools_and_structured_output_keep_original_behavior_and_observer(
    provider, services
):
    requests = []

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "offline-bind",
                "object": "chat.completion",
                "created": 0,
                "model": "actual-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"value":"ok"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as sync_http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as async_http:
            original = ChatOpenAI(
                model="actual-model",
                api_key="fixture",
                base_url=provider.base_url,
                http_client=sync_http,
                http_async_client=async_http,
            )
            configured = attach_model_observer(original, provider, ModelConfig(id="actual-model"))
            copied = configured.model_copy(update={"temperature": 0.4, "max_tokens": 17})
            schema = {
                "title": "Reply",
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }
            tool = {"type": "function", "function": {"name": "reply", "parameters": schema}}
            with model_call_scope(services, user_id=1, purpose="chat"):
                await copied.ainvoke("private")
                await copied.bind_tools([tool]).ainvoke("private")
                assert await copied.with_structured_output(schema, method="json_mode").ainvoke(
                    "private"
                ) == {"value": "ok"}
    assert original.callbacks is None
    assert len(services.model_call_repo.finishes) == 3
    assert all(row.status == "succeeded" for row in services.model_call_repo.finishes)
    assert all(
        body["temperature"] == 0.4 and body["max_completion_tokens"] == 17 for body in requests
    )
    assert requests[1]["tools"][0]["function"]["name"] == "reply"
    assert requests[2]["response_format"] == {"type": "json_object"}


async def test_abatch_keeps_one_receipt_for_each_logical_invocation(provider, services):
    model = model_for(provider)
    with model_call_scope(services, user_id=1, purpose="chat"):
        replies = await model.abatch(["first", "second"])
    assert len(replies) == len(services.model_call_repo.finishes) == 2
    assert len({row.call_id for row in services.model_call_repo.finishes}) == 2
    assert all(row.status == "succeeded" for row in services.model_call_repo.finishes)
