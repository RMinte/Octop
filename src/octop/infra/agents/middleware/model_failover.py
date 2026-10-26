"""Health-aware and context-safe model failover for AUTO-routed turns."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.model_retry import ModelRetryMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult
from langgraph.errors import GraphBubbleUp
from octop_harness.context_usage import breakdown_from_model_request
from octop_harness.middleware.runtime import runtime_config
from octop_harness.middleware.turn_model import latest_human_needs_multimodal

from octop.i18n.domains.stream import model_retry_failure_prompt
from octop.infra.agents.providers.health import (
    ModelHealthRegistry,
    classify_model_failure,
)
from octop.infra.agents.providers.store import AutoRouteCandidate
from octop.infra.agents.settings.runtime_limits import CONFIGURABLE_MAX_INPUT_TOKENS

logger = logging.getLogger(__name__)

AUTO_ROUTE_CONFIG_KEY = "octop_auto_route"
_CONTEXT_SAFETY_RATIO = 0.9


class ModelExecutionSafety(BaseCallbackHandler):
    """Stop automatic replay once a turn exposes output or starts a tool."""

    run_inline = True

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed

    def on_llm_new_token(self, token: str | list[str | dict[str, Any]], **kwargs: Any) -> None:
        self.allowed = False

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.allowed = False

    def on_tool_start(
        self, serialized: dict[str, Any] | None, input_str: str, **kwargs: Any
    ) -> None:
        self.allowed = False


def _configurable(request: ModelRequest[Any]) -> dict[str, Any]:
    config = runtime_config(request)
    configurable = config.get("configurable") if isinstance(config, dict) else None
    return configurable if isinstance(configurable, dict) else {}


def _estimated_input_tokens(request: ModelRequest[Any]) -> int:
    return sum(breakdown_from_model_request(request).values())


def _fits_context(
    candidate: AutoRouteCandidate,
    estimated_tokens: int,
    route_cap: int | None = None,
) -> bool:
    cap = candidate.max_input_tokens
    if route_cap is not None and route_cap > 0:
        cap = min(cap, route_cap) if cap > 0 else route_cap
    if cap <= 0:
        return True
    return estimated_tokens <= int(cap * _CONTEXT_SAFETY_RATIO)


class SafeModelFailoverMiddleware(AgentMiddleware[Any, Any]):
    """Retry one model, then switch only for AUTO and safe failure classes."""

    def __init__(
        self,
        *,
        health: ModelHealthRegistry,
        candidates: Callable[[str, bool], Sequence[AutoRouteCandidate]],
        model_for_ref: Callable[[str], Any],
        max_retries: int,
        initial_delay: float,
        max_delay: float,
        on_failure: Callable[[Exception], str] | Literal["error"] | None = None,
        on_model_selected: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        close_models: Callable[[], Awaitable[None]] | None = None,
        execution_safety: ModelExecutionSafety | None = None,
    ) -> None:
        super().__init__()
        self._health = health
        self._candidates = candidates
        self._model_for_ref = model_for_ref
        self._on_failure = on_failure
        self._on_model_selected = on_model_selected
        self._close_models = close_models
        self._close_task: asyncio.Future[None] | None = None
        self._execution_safety = execution_safety
        self._retry = ModelRetryMiddleware(
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
            on_failure="error",
        )

    async def aclose(self) -> None:
        if self._close_models is not None:
            if self._close_task is None:
                self._close_task = asyncio.ensure_future(self._close_models())
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                await self._close_task
                raise

    @property
    def callbacks(self) -> list[BaseCallbackHandler]:
        return [self._execution_safety] if self._execution_safety is not None else []

    def _may_failover(self) -> bool:
        return self._execution_safety is None or self._execution_safety.allowed

    def _failure_response(self, exc: Exception) -> ModelResponse[Any]:
        if self._on_failure == "error":
            raise exc
        content = (
            self._on_failure(exc)
            if self._on_failure is not None
            else model_retry_failure_prompt(exc, "en")
        )
        return ModelResponse(result=[AIMessage(content=content)])

    def _route(self, request: ModelRequest[Any]) -> tuple[str, bool]:
        configurable = _configurable(request)
        ref = configurable.get("model")
        return (
            ref.strip() if isinstance(ref, str) else "",
            configurable.get(AUTO_ROUTE_CONFIG_KEY) is True and self._may_failover(),
        )

    def _fallbacks(self, request: ModelRequest[Any], current_ref: str) -> list[AutoRouteCandidate]:
        needs_multimodal = latest_human_needs_multimodal(list(request.messages or []))
        estimated = _estimated_input_tokens(request)
        raw_route_cap = _configurable(request).get(CONFIGURABLE_MAX_INPUT_TOKENS)
        route_cap = raw_route_cap if isinstance(raw_route_cap, int) else None
        return [
            candidate
            for candidate in self._candidates(current_ref, needs_multimodal)
            if candidate.ref != current_ref
            and self._health.is_routable(candidate.ref)
            and _fits_context(candidate, estimated, route_cap)
        ]

    def _record_failure(self, ref: str, exc: Exception) -> bool:
        decision = classify_model_failure(exc)
        self._health.record_failure(ref, kind=decision.kind, scope=decision.scope)
        return decision.failover

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        if self._on_model_selected is not None:
            raise RuntimeError("Audited Task routes require async model execution")
        current_ref, automatic = self._route(request)
        last_error: Exception | None = None
        attempts: list[tuple[str, Any]] = [(current_ref, request.model)]
        if automatic and current_ref:
            attempts.extend(
                (candidate.ref, None) for candidate in self._fallbacks(request, current_ref)
            )
        if self._execution_safety is not None and len(attempts) == 1:
            # Keep an admitted Task executable when its catalog has no safe
            # alternative. One fresh attempt still propagates failure normally.
            automatic = False

        for ref, model in attempts:
            reserved = automatic and bool(ref)
            if reserved and not self._health.begin_attempt(ref):
                continue
            try:
                if automatic and ref and ref != current_ref:
                    _configurable(request)["model"] = ref
                model = self._model_for_ref(ref) if model is None else model
                routed = request if model is request.model else request.override(model=model)
                try:
                    response = self._retry.wrap_model_call(routed, handler)
                except GraphBubbleUp:
                    raise
                except Exception as exc:
                    last_error = exc
                    if not self._may_failover():
                        return self._failure_response(exc)
                    may_failover = self._record_failure(ref, exc)
                    if not automatic or not may_failover or not self._may_failover():
                        return self._failure_response(exc)
                    logger.warning("AUTO model route failed; trying next candidate: model=%s", ref)
                    continue
                self._health.record_success(ref)
                return cast(ModelResponse[Any], response)
            finally:
                if reserved:
                    self._health.release_attempt(ref)

        return self._failure_response(
            last_error or RuntimeError("no healthy AUTO model is available")
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        current_ref, automatic = self._route(request)
        last_error: Exception | None = None
        attempts: list[tuple[str, Any]] = [(current_ref, request.model)]
        if automatic and current_ref:
            attempts.extend(
                (candidate.ref, None) for candidate in self._fallbacks(request, current_ref)
            )
        if self._execution_safety is not None and len(attempts) == 1:
            automatic = False

        for ref, model in attempts:
            reserved = automatic and bool(ref)
            if reserved and not self._health.begin_attempt(ref):
                continue
            try:
                if automatic and ref and ref != current_ref:
                    _configurable(request)["model"] = ref
                    if self._on_model_selected is not None:
                        await self._on_model_selected(ref, _configurable(request))
                model = self._model_for_ref(ref) if model is None else model
                routed = request if model is request.model else request.override(model=model)
                try:
                    response = await self._retry.awrap_model_call(routed, handler)
                except GraphBubbleUp:
                    raise
                except Exception as exc:
                    last_error = exc
                    if not self._may_failover():
                        return self._failure_response(exc)
                    may_failover = self._record_failure(ref, exc)
                    if not automatic or not may_failover or not self._may_failover():
                        return self._failure_response(exc)
                    logger.warning("AUTO model route failed; trying next candidate: model=%s", ref)
                    continue
                self._health.record_success(ref)
                return cast(ModelResponse[Any], response)
            finally:
                if reserved:
                    self._health.release_attempt(ref)

        return self._failure_response(
            last_error or RuntimeError("no healthy AUTO model is available")
        )


__all__ = [
    "AUTO_ROUTE_CONFIG_KEY",
    "AutoRouteCandidate",
    "SafeModelFailoverMiddleware",
]
