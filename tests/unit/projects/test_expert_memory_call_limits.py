"""Memory limits reach real SDK requests, retry loops and cancellation boundaries."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from dataclasses import replace

import httpx
import pytest
from langchain_openai import ChatOpenAI
from openai import InternalServerError
from tests.unit.projects.test_expert_execution import services  # noqa: F401
from tests.unit.projects.test_expert_memory_extraction import capture_job, response

from octop.infra.projects import expert_memory_extraction as extraction


@pytest.fixture
async def memory_client(services):  # noqa: F811
    _, original = await capture_job(services)
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    models = []

    def build(handler, options=None):
        payload = json.loads(original.payload_json)
        payload["profile"]["memory"]["extract_config"] = options or {}
        payload["resolved_model_ref"] = "local/model"
        selected = replace(job, payload_json=json.dumps(payload))
        model = ChatOpenAI(
            model="memory-test",
            api_key="fake",
            base_url="http://memory.invalid/v1",
            max_retries=2,
            http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        models.append(model)
        client = extraction._ModelClient(worker, selected, model, asyncio.get_running_loop())
        return client, worker, selected, model

    yield build
    for model in models:
        await model.root_async_client.close()
        model.root_client.close()


def completion(content="ok"):
    return httpx.Response(
        200,
        json={
            "id": "test",
            "object": "chat.completion",
            "model": "memory-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


@pytest.mark.parametrize(
    ("tier", "options", "expected"),
    [
        ("light", {"extract_light_timeout_s": 75, "extract_heavy_timeout_s": 240}, 75),
        ("heavy", {"extract_light_timeout_s": 75, "extract_heavy_timeout_s": 240}, 240),
        ("light", {"extract_light_timeout_s": 0}, 120),
        ("heavy", {"extract_heavy_timeout_s": None}, 300),
    ],
)
async def test_tier_timeout_reaches_sdk_request_and_outer_deadline(
    memory_client, monkeypatch, tier, options, expected
):
    requests, deadlines = [], []
    original_timeout = asyncio.timeout

    def deadline(delay):
        deadlines.append(delay)
        return original_timeout(delay)

    def handler(request):
        requests.append(request)
        return completion()

    monkeypatch.setattr(extraction.asyncio, "timeout", deadline)
    client, _, _, model = memory_client(handler, {**options, "extract_max_retries": 0})
    assert await client._invoke("input", {"tier": tier, "max_tokens": 512}) == "ok"
    assert requests[0].extensions["timeout"]["read"] == expected
    payload = json.loads(requests[0].content)
    assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 512
    assert payload["temperature"] == 0
    assert deadlines == [expected, expected]
    assert model.root_async_client.max_retries == 2  # Shared model stays untouched.


@pytest.mark.parametrize(
    ("options", "attempts", "expected_delays"),
    [
        ({}, 3, [1, 2]),
        ({"extract_max_retries": 0}, 1, []),
        ({"extract_max_retries": 10, "extract_retry_backoff_s": 0}, 11, [0] * 10),
        ({"extract_max_retries": 4, "extract_retry_backoff_s": 40}, 5, [40, 60, 60, 60]),
    ],
)
async def test_transient_failure_count_and_actual_backoffs(
    memory_client, monkeypatch, options, attempts, expected_delays
):
    requests, delays = [], []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    def handler(request):
        requests.append(request)
        return httpx.Response(
            503, json={"error": {"message": "temporarily unavailable", "type": "server_error"}}
        )

    monkeypatch.setattr(extraction.asyncio, "sleep", sleep)
    client, _, _, _ = memory_client(handler, options)
    with pytest.raises(InternalServerError):
        await client._invoke("input", {"tier": "light"})
    assert len(requests) == attempts  # SDK's own retries must remain disabled.
    assert delays == expected_delays


async def test_sync_wait_budget_matches_capped_retry_schedule(memory_client, monkeypatch):
    requests, deadlines, waits, delays = [], [], [], []
    original_sleep, original_timeout = asyncio.sleep, asyncio.timeout
    original_result = concurrent.futures.Future.result

    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    def deadline(delay):
        deadlines.append(delay)
        return original_timeout(delay)

    def result(future, timeout=None):
        if timeout is not None:
            waits.append(timeout)
        return original_result(future, timeout)

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(503, json={"error": {"message": "retry", "type": "server_error"}})
        return completion()

    monkeypatch.setattr(extraction.asyncio, "sleep", sleep)
    monkeypatch.setattr(extraction.asyncio, "timeout", deadline)
    monkeypatch.setattr(concurrent.futures.Future, "result", result)
    client, _, _, _ = memory_client(
        handler,
        {"extract_heavy_timeout_s": 300, "extract_max_retries": 2, "extract_retry_backoff_s": 40},
    )
    assert await asyncio.to_thread(client.call_llm, "input", tier="heavy") == "ok"
    assert delays == [40, 60]
    assert deadlines == [1000, 300, 300, 300]
    assert waits == [1010]  # Same total budget, plus bounded thread delivery margin.
    assert all(request.extensions["timeout"]["read"] == 300 for request in requests)


@pytest.mark.parametrize(
    ("configured", "output_limit", "expected"),
    [
        (None, None, 2048),
        (0, None, 2048),
        (8192, None, 8192),
        (8192, 1024, 1024),
    ],
)
async def test_extractor_budget_reaches_request(memory_client, configured, output_limit, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return completion(response())

    client, worker, job, model = memory_client(handler, {"extract_max_tokens": configured})
    if output_limit is not None:
        object.__setattr__(model, "_harness_max_output_tokens", output_limit)
    sources = await asyncio.to_thread(worker._sources, job)
    proposal = await asyncio.to_thread(worker._prepare, job, sources, client)
    assert json.loads(proposal)["candidates"]
    payload = json.loads(requests[0].content)
    assert payload.get("max_completion_tokens", payload.get("max_tokens")) == expected


async def test_attempt_deadline_cancels_pending_sdk_request(memory_client, monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original_timeout = asyncio.timeout
    deadlines = []

    async def handler(request):
        entered.set()
        try:
            assert len(deadlines) == 2
            deadlines[1].reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def deadline(delay):
        context = original_timeout(delay)
        deadlines.append(context)
        return context

    monkeypatch.setattr(extraction.asyncio, "timeout", deadline)
    client, _, _, _ = memory_client(
        handler, {"extract_light_timeout_s": 75, "extract_max_retries": 0}
    )
    with pytest.raises(TimeoutError):
        await client._invoke("input", {"tier": "light"})
    assert entered.is_set() and cancelled.is_set()
    assert deadlines[1].expired() and not deadlines[0].expired()


@pytest.mark.parametrize("stop_worker", [False, True])
async def test_cancellation_or_shutdown_does_not_retry_pending_request(memory_client, stop_worker):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client, worker, _, _ = memory_client(handler)
    pending = asyncio.create_task(client._invoke("input", {"tier": "light"}))
    await asyncio.wait_for(entered.wait(), timeout=2)
    if stop_worker:
        worker._stopping.set()
        with pytest.raises(extraction._Stopped):
            await asyncio.wait_for(pending, timeout=2)
    else:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert cancelled.is_set() and len(requests) == 1


async def test_revoked_owner_stops_before_next_retry(memory_client, services, monkeypatch):  # noqa: F811
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "retry", "type": "server_error"}})

    async def revoke(_delay):
        with services.db.transaction() as conn:
            conn.execute("UPDATE users SET disabled=1 WHERE id=1")

    monkeypatch.setattr(extraction.asyncio, "sleep", revoke)
    client, _, _, _ = memory_client(handler)
    with pytest.raises(extraction._Stopped):
        await client._invoke("input", {"tier": "light"})
    assert len(requests) == 1
