from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphBubbleUp

from octop.infra.agents.middleware import model_failover
from octop.infra.agents.middleware.model_failover import (
    AutoRouteCandidate,
    ModelExecutionSafety,
    SafeModelFailoverMiddleware,
)
from octop.infra.agents.providers.health import FailureScope, ModelHealthRegistry


def _model(ref: str, cap: int) -> SimpleNamespace:
    provider, _, model = ref.partition("/")
    return SimpleNamespace(
        model=ref,
        provider=provider,
        model_name=model,
        profile={"max_input_tokens": cap},
    )


@pytest.mark.asyncio
async def test_success_does_not_construct_unused_fallback_models(monkeypatch) -> None:
    factory = MagicMock()
    middleware = SafeModelFailoverMiddleware(
        health=ModelHealthRegistry(),
        candidates=lambda _current, _vision: [AutoRouteCandidate("p/b", 100_000)],
        model_for_ref=factory,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": True},
    )

    async def handler(request: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content="ok")])

    await middleware.awrap_model_call(
        ModelRequest(model=_model("p/a", 100_000), messages=[HumanMessage(content="hello")]),
        handler,
    )
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_auto_route_switches_after_retry_exhaustion(monkeypatch) -> None:
    models = {"p/a": _model("p/a", 100_000), "p/b": _model("p/b", 100_000)}
    configurable = {"model": "p/a", "octop_auto_route": True}
    health = ModelHealthRegistry(clock=lambda: 100.0)
    middleware = SafeModelFailoverMiddleware(
        health=health,
        candidates=lambda _current, _vision: [AutoRouteCandidate("p/b", 100_000)],
        model_for_ref=models.__getitem__,
        max_retries=1,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: configurable,
    )
    attempts: list[tuple[str, str]] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        attempts.append((request.model.model, configurable["model"]))
        if request.model.model == "p/a":
            raise RuntimeError("HTTP 503 service unavailable")
        return ModelResponse(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(
        ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hello")]),
        handler,
    )

    assert response.result[0].content == "ok"
    assert attempts == [("p/a", "p/a"), ("p/a", "p/a"), ("p/b", "p/b")]
    assert configurable["model"] == "p/b"
    assert not health.is_routable("p/a")


@pytest.mark.asyncio
async def test_explicit_route_retries_but_does_not_switch(monkeypatch) -> None:
    models = {"p/a": _model("p/a", 100_000), "p/b": _model("p/b", 100_000)}
    health = ModelHealthRegistry(clock=lambda: 100.0)
    middleware = SafeModelFailoverMiddleware(
        health=health,
        candidates=lambda _current, _vision: [AutoRouteCandidate("p/b", 100_000)],
        model_for_ref=models.__getitem__,
        max_retries=1,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": False},
    )
    handler = MagicMock(side_effect=RuntimeError("HTTP 503 service unavailable"))

    response = middleware.wrap_model_call(
        ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hello")]),
        handler,
    )

    assert "[model_call_failed]" in str(response.result[0].content)
    assert handler.call_count == 2
    assert not health.is_routable("p/a")


def test_terminal_failure_uses_observability_callback(monkeypatch) -> None:
    model = _model("p/a", 100_000)
    on_failure = MagicMock(return_value="tracked failure")
    middleware = SafeModelFailoverMiddleware(
        health=ModelHealthRegistry(clock=lambda: 100.0),
        candidates=lambda _current, _vision: [],
        model_for_ref=lambda _ref: model,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
        on_failure=on_failure,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": False},
    )
    error = RuntimeError("HTTP 503 service unavailable")

    response = middleware.wrap_model_call(
        ModelRequest(model=model, messages=[HumanMessage(content="hello")]),
        MagicMock(side_effect=error),
    )

    assert response.result[0].content == "tracked failure"
    on_failure.assert_called_once_with(error)


@pytest.mark.asyncio
async def test_failover_skips_model_with_smaller_context(monkeypatch) -> None:
    models = {
        "p/a": _model("p/a", 100_000),
        "p/small": _model("p/small", 1_000),
        "p/large": _model("p/large", 100_000),
    }
    middleware = SafeModelFailoverMiddleware(
        health=ModelHealthRegistry(clock=lambda: 100.0),
        candidates=lambda _current, _vision: [
            AutoRouteCandidate("p/small", 1_000),
            AutoRouteCandidate("p/large", 100_000),
        ],
        model_for_ref=models.__getitem__,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": True},
    )
    monkeypatch.setattr(model_failover, "_estimated_input_tokens", lambda _request: 2_000)
    attempts: list[str] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        attempts.append(request.model.model)
        if request.model.model == "p/a":
            raise RuntimeError("context_length_exceeded")
        return ModelResponse(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(
        ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hello")]),
        handler,
    )

    assert response.result[0].content == "ok"
    assert attempts == ["p/a", "p/large"]


@pytest.mark.asyncio
async def test_failover_respects_agent_context_cap(monkeypatch) -> None:
    models = {"p/a": _model("p/a", 100_000), "p/b": _model("p/b", 100_000)}
    middleware = SafeModelFailoverMiddleware(
        health=ModelHealthRegistry(clock=lambda: 100.0),
        candidates=lambda _current, _vision: [AutoRouteCandidate("p/b", 100_000)],
        model_for_ref=models.__getitem__,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {
            "model": "p/a",
            "octop_auto_route": True,
            "max_input_tokens": 1_000,
        },
    )
    monkeypatch.setattr(model_failover, "_estimated_input_tokens", lambda _request: 2_000)
    attempts: list[str] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        attempts.append(request.model.model)
        raise RuntimeError("context_length_exceeded")

    response = await middleware.awrap_model_call(
        ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hello")]),
        handler,
    )

    assert "[model_call_failed]" in str(response.result[0].content)
    assert attempts == ["p/a"]


@pytest.mark.asyncio
async def test_failover_does_not_retry_unknown_invalid_request(monkeypatch) -> None:
    model = _model("p/a", 100_000)
    middleware = SafeModelFailoverMiddleware(
        health=ModelHealthRegistry(clock=lambda: 100.0),
        candidates=lambda _current, _vision: [AutoRouteCandidate("p/b", 100_000)],
        model_for_ref=lambda _ref: _model("p/b", 100_000),
        max_retries=0,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": True},
    )
    handler = MagicMock(side_effect=RuntimeError("HTTP 400 invalid tool schema"))

    response = await middleware.awrap_model_call(
        ModelRequest(model=model, messages=[HumanMessage(content="hello")]),
        handler,
    )

    assert "[model_call_failed]" in str(response.result[0].content)
    assert handler.call_count == 1


@pytest.mark.asyncio
async def test_provider_failure_skips_sibling_model(monkeypatch) -> None:
    models = {
        "p/a": _model("p/a", 100_000),
        "p/b": _model("p/b", 100_000),
        "q/c": _model("q/c", 100_000),
    }
    configurable = {"model": "p/a", "octop_auto_route": True}
    middleware = SafeModelFailoverMiddleware(
        health=ModelHealthRegistry(clock=lambda: 100.0),
        candidates=lambda _current, _vision: [
            AutoRouteCandidate("p/b", 100_000),
            AutoRouteCandidate("q/c", 100_000),
        ],
        model_for_ref=models.__getitem__,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(model_failover, "_configurable", lambda _request: configurable)
    attempts: list[str] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        attempts.append(request.model.model)
        if request.model.model == "p/a":
            raise RuntimeError("HTTP 429 too many requests")
        return ModelResponse(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(
        ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hello")]),
        handler,
    )

    assert response.result[0].content == "ok"
    assert attempts == ["p/a", "q/c"]
    assert configurable["model"] == "q/c"


@pytest.mark.parametrize("scope", [FailureScope.MODEL, FailureScope.PROVIDER])
@pytest.mark.parametrize("interruption", [GraphBubbleUp, asyncio.CancelledError])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_interrupted_half_open_probe_can_be_attempted_again(
    monkeypatch, scope, interruption, asynchronous
) -> None:
    now = 100.0
    health = ModelHealthRegistry(clock=lambda: now)
    health.record_failure("p/a", kind="timeout", scope=scope)
    now = 161.0
    middleware = SafeModelFailoverMiddleware(
        health=health,
        candidates=lambda _ref, _vision: [],
        model_for_ref=lambda _ref: None,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": True},
    )
    request = ModelRequest(model=_model("p/a", 100_000), messages=[HumanMessage(content="hello")])

    with pytest.raises(interruption):
        if asynchronous:
            await middleware.awrap_model_call(request, AsyncMock(side_effect=interruption("stop")))
        else:
            middleware.wrap_model_call(request, MagicMock(side_effect=interruption("stop")))

    assert health.begin_attempt("p/a")
    health.record_failure("p/a", kind="timeout", scope=scope)
    assert health.snapshot("p/a")["failures"] == 2


@pytest.mark.asyncio
async def test_selection_callback_failure_releases_probe_without_changing_health(monkeypatch):
    now = 100.0
    health = ModelHealthRegistry(clock=lambda: now)
    health.record_failure("q/b", kind="timeout", scope=FailureScope.MODEL)
    now = 161.0
    selected = AsyncMock(side_effect=RuntimeError("audit unavailable"))
    middleware = SafeModelFailoverMiddleware(
        health=health,
        candidates=lambda _ref, _vision: [AutoRouteCandidate("q/b", 100_000)],
        model_for_ref=lambda _ref: _model("q/b", 100_000),
        max_retries=0,
        initial_delay=0,
        max_delay=0,
        on_model_selected=selected,
    )
    configurable = {"model": "p/a", "octop_auto_route": True}
    monkeypatch.setattr(model_failover, "_configurable", lambda _request: configurable)
    handler = AsyncMock(side_effect=ConnectionError("offline"))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await middleware.awrap_model_call(
            ModelRequest(model=_model("p/a", 100_000), messages=[HumanMessage(content="hello")]),
            handler,
        )
    assert handler.await_count == 1
    assert health.begin_attempt("q/b")
    health.record_failure("q/b", kind="timeout", scope=FailureScope.MODEL)
    assert health.snapshot("q/b")["failures"] == 2


@pytest.mark.asyncio
async def test_cancelled_shutdown_drains_factory_once():
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()

    closer = AsyncMock(side_effect=close)
    middleware = SafeModelFailoverMiddleware(
        health=ModelHealthRegistry(),
        candidates=lambda _ref, _vision: [],
        model_for_ref=lambda _ref: None,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
        close_models=closer,
    )
    pending = asyncio.create_task(middleware.aclose())
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await middleware.aclose()
    closer.assert_awaited_once()


@pytest.mark.parametrize("boundary", ["chunk", "tool"])
@pytest.mark.asyncio
async def test_partial_execution_raises_without_replaying_or_quarantining_new_turns(
    monkeypatch, boundary
):
    safety = ModelExecutionSafety()
    health = ModelHealthRegistry()
    factory = MagicMock()
    middleware = SafeModelFailoverMiddleware(
        health=health,
        candidates=lambda _ref, _vision: [AutoRouteCandidate("q/b", 100_000)],
        model_for_ref=factory,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
        execution_safety=safety,
        on_failure="error",
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": True},
    )
    attempts = []

    async def handler(request):
        attempts.append(request.model.model)
        if boundary == "chunk":
            safety.on_llm_new_token("Partial answer")
        else:
            safety.on_tool_start(None, "side effect")
        raise ConnectionError("stream interrupted")

    with pytest.raises(ConnectionError, match="stream interrupted"):
        await middleware.awrap_model_call(
            ModelRequest(model=_model("p/a", 100_000), messages=[HumanMessage(content="hello")]),
            handler,
        )
    assert attempts == ["p/a"]
    factory.assert_not_called()
    assert health.is_routable("p/a")


@pytest.mark.asyncio
async def test_new_task_can_attempt_its_only_model_after_an_earlier_turn_failed(monkeypatch):
    health = ModelHealthRegistry(clock=lambda: 100.0)
    health.record_failure("p/a", kind="timeout", scope=FailureScope.MODEL)
    middleware = SafeModelFailoverMiddleware(
        health=health,
        candidates=lambda _ref, _vision: [],
        model_for_ref=MagicMock(),
        max_retries=0,
        initial_delay=0,
        max_delay=0,
        execution_safety=ModelExecutionSafety(),
        on_failure="error",
    )
    monkeypatch.setattr(
        model_failover,
        "_configurable",
        lambda _request: {"model": "p/a", "octop_auto_route": True},
    )
    handler = AsyncMock(return_value=ModelResponse(result=[AIMessage(content="new turn done")]))
    response = await middleware.awrap_model_call(
        ModelRequest(model=_model("p/a", 100_000), messages=[HumanMessage(content="new turn")]),
        handler,
    )
    assert response.result[0].content == "new turn done"
    handler.assert_awaited_once()
    assert health.is_routable("p/a")
