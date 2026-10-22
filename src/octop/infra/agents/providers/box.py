"""Explicit appliance adapters for the locked harness's credential-only factory."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from octop_harness import HarnessAgent
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory, build_chat_model
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
    return _configure_box_model(instance) if isinstance(provider, BoxProviderConfig) else instance


class OctopChatModelFactory(ChatModelFactory):
    def _build(self, provider: ProviderConfig, model: ModelConfig) -> BaseChatModel:
        instance = super()._build(provider, model)
        return (
            _configure_box_model(instance) if isinstance(provider, BoxProviderConfig) else instance
        )


class OctopHarnessAgent(HarnessAgent):
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
