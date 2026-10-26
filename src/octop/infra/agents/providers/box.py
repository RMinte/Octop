"""Explicit appliance adapters for the locked harness's credential-only factory."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from octop_harness import HarnessAgent
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory, build_chat_model
from octop_harness.request import ChatRequest
from openai import omit


class BoxProviderConfig(ProviderConfig):
    """Runtime marker; the internal key satisfies harness 1.0.1 validation only."""


def build_box_provider_config(
    *,
    id: str,
    base_url: str,
    name: str,
    models: list[ModelConfig],
    headers: dict[str, str],
) -> ProviderConfig:
    return BoxProviderConfig(
        id=id,
        base_url=base_url,
        name=name,
        api_key="octop-box-no-auth",
        protocol="openai",
        models=models,
        headers={key: value for key, value in headers.items() if key.lower() != "authorization"},
    )


def _configure_box_model(model: BaseChatModel) -> BaseChatModel:
    # Per-request omission is required: default_headers alone does not satisfy
    # the SDK's explicit no-auth check. The internal marker never reaches HTTP.
    kwargs = dict(getattr(model, "model_kwargs", None) or {})
    headers = {
        key: value
        for key, value in (kwargs.get("extra_headers") or {}).items()
        if key.lower() != "authorization"
    }
    headers["Authorization"] = omit
    kwargs["extra_headers"] = headers
    extra = dict(getattr(model, "extra_body", None) or {})
    extra["chat_template_kwargs"] = {
        **(extra.get("chat_template_kwargs") or {}),
        "enable_thinking": False,
    }
    return model.model_copy(update={"model_kwargs": kwargs, "extra_body": extra})


def build_box_chat_model(provider: ProviderConfig, model: ModelConfig) -> BaseChatModel:
    """Keep harness tool/modality/token handling and adapt explicitly marked boxes."""
    instance = build_chat_model(provider, model)
    return _observe_model(instance, provider, model)


def _observe_model(
    instance: BaseChatModel, provider: ProviderConfig, model: ModelConfig
) -> BaseChatModel:
    from octop.infra.usage.model_calls import attach_model_observer

    if isinstance(provider, BoxProviderConfig):
        instance = _configure_box_model(instance)
    return attach_model_observer(instance, provider, model)


class OctopChatModelFactory(ChatModelFactory):
    def _build(
        self, provider: ProviderConfig, model: ModelConfig, *, max_retries: int | None = None
    ) -> BaseChatModel:
        instance = super()._build(provider, model, max_retries=max_retries)
        return _observe_model(instance, provider, model)


class OctopHarnessAgent(HarnessAgent):
    async def call(
        self,
        request: ChatRequest | str | dict[str, Any],
        *,
        protocol: str | None = None,
    ) -> dict[str, Any]:
        from octop.infra.usage.model_calls import (
            current_model_call_scope,
            override_model_call_scope,
        )

        scope = current_model_call_scope()
        if scope is not None and self._agent_id != scope.agent_id:
            with override_model_call_scope(purpose="subagent", agent_id=self._agent_id):
                return await super().call(request, protocol=protocol)
        return await super().call(request, protocol=protocol)

    def _init_model_factory(self, injected: ChatModelFactory | None) -> ChatModelFactory:
        if injected is None and self._config.providers:
            return OctopChatModelFactory(self._config.providers, agent_config=self._config)
        return super()._init_model_factory(injected)


__all__ = [
    "BoxProviderConfig",
    "OctopChatModelFactory",
    "OctopHarnessAgent",
    "build_box_chat_model",
    "build_box_provider_config",
]
