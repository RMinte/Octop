from langchain.agents.middleware import ModelRequest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from octop.infra.agents.middleware import reasoning


def test_task_summary_uses_responses_and_preserves_effort_without_remote_storage(monkeypatch):
    monkeypatch.setattr(
        reasoning,
        "get_config",
        lambda: {
            "configurable": {reasoning.CONFIG_KEY: {"model_fields": {"reasoning_effort": "high"}}}
        },
    )
    model = ChatOpenAI(model="gpt-5", api_key="test", base_url="https://api.openai.com/v1")
    configured = reasoning._configured_request(
        ModelRequest(model=model, messages=[]), summary=True
    ).model
    payload = configured._get_request_payload([])
    assert configured.use_responses_api is True
    assert payload["reasoning"] == {"summary": "auto", "effort": "high"}
    assert "reasoning_effort" not in payload
    assert payload["store"] is False
    assert "reasoning.encrypted_content" in payload["include"]
    assert model.reasoning is None


def test_summary_does_not_switch_compatible_chat_endpoints_to_responses(monkeypatch):
    monkeypatch.setattr(reasoning, "get_config", lambda: {})
    model = ChatOpenAI(model="glm-5", api_key="test", base_url="https://example.invalid/v1")
    assert (
        reasoning._configured_request(ModelRequest(model=model, messages=[]), summary=True).model
        is model
    )
    native = model.model_copy(update={"use_responses_api": True})
    configured = reasoning._configured_request(
        ModelRequest(model=native, messages=[]), summary=True
    ).model
    assert configured.reasoning == {"summary": "auto"}


def test_reasoning_middleware_updates_openai_field_and_extra_body(monkeypatch) -> None:
    monkeypatch.setattr(
        reasoning,
        "get_config",
        lambda: {
            "configurable": {
                reasoning.CONFIG_KEY: {
                    "model_fields": {"reasoning_effort": "high"},
                    "extra_body": {"thinking": {"type": "enabled"}},
                }
            }
        },
    )
    model = ChatOpenAI(model="gpt-5.4", api_key="test")
    request = ModelRequest(model=model, messages=[])

    configured = reasoning._configured_request(request)

    assert configured.model is not model
    assert configured.model.reasoning_effort == "high"
    assert configured.model.extra_body == {"thinking": {"type": "enabled"}}


def test_reasoning_middleware_updates_native_anthropic_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        reasoning,
        "get_config",
        lambda: {
            "configurable": {
                reasoning.CONFIG_KEY: {
                    "model_fields": {
                        "thinking": {"type": "adaptive"},
                        "reasoning_effort": "high",
                    }
                }
            }
        },
    )
    model = ChatAnthropic(model="claude-opus-4-6", api_key="test", max_tokens=1024)
    request = ModelRequest(model=model, messages=[])

    configured = reasoning._configured_request(request)

    assert configured.model.thinking == {"type": "adaptive"}
    assert configured.model.reasoning_effort == "high"
