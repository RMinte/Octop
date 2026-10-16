"""Apply per-turn provider-specific reasoning request parameters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langgraph.config import get_config

CONFIG_KEY = "octop_reasoning_overrides"


def _configured_request(request: ModelRequest[Any], *, summary: bool = False) -> ModelRequest[Any]:
    configurable = dict(get_config().get("configurable") or {})
    overrides = configurable.get(CONFIG_KEY)
    overrides = overrides if isinstance(overrides, dict) else {}
    model = request.model
    if not hasattr(model, "model_copy"):
        return request
    updates = dict(overrides.get("model_fields") or {})
    if (
        summary
        and hasattr(model, "reasoning")
        and (
            getattr(model, "use_responses_api", None) is True
            or getattr(model, "output_version", None) == "responses/v1"
            or urlparse(
                str(getattr(model, "openai_api_base", None) or "https://api.openai.com/v1")
            ).hostname
            == "api.openai.com"
        )
    ):
        reasoning = dict(getattr(model, "reasoning", None) or {})
        reasoning["summary"] = "auto"
        effort = updates.pop("reasoning_effort", getattr(model, "reasoning_effort", None))
        if effort:
            reasoning["effort"] = effort
        updates.update(
            reasoning=reasoning,
            reasoning_effort=None,
            use_responses_api=True,
            output_version="responses/v1",
            store=False,
            include=list(
                dict.fromkeys(
                    [*(getattr(model, "include", None) or []), "reasoning.encrypted_content"]
                )
            ),
        )
    extra = overrides.get("extra_body")
    if isinstance(extra, dict) and extra and hasattr(model, "extra_body"):
        current = dict(getattr(model, "extra_body", None) or {})
        current.update(extra)
        updates["extra_body"] = current
    if not updates:
        return request
    configured = model.model_copy(update=updates)
    return request.override(model=configured)


class ReasoningRequestMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, *, summary: bool = False) -> None:
        self.summary = summary

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(_configured_request(request, summary=self.summary))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(_configured_request(request, summary=self.summary))


__all__ = ["CONFIG_KEY", "ReasoningRequestMiddleware"]
