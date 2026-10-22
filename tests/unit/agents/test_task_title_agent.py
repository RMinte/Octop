"""Semantic naming uses a bounded, isolated request rather than a tool runtime."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from octop.infra.db.repos.project_task_titles import TaskTitleJob
from octop.infra.projects import title
from octop.infra.projects.title_policy import normalized_title, provisional_title


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\n  标题： “修复登录问题。” \n解释", "修复登录问题"),
        ("Title: Fix login!", "Fix login"),
        (" \n ", None),
        ("x" * 80, "x" * 60),
    ],
)
def test_generated_title_normalization(raw, expected):
    assert normalized_title(raw) == expected


def test_provisional_title_uses_first_nonempty_line():
    assert provisional_title("\n   修复   登录问题  \n不要用于标题的附加上下文") == "修复 登录问题"


@pytest.mark.parametrize("tool_call", [False, True])
async def test_title_request_has_no_history_tools_or_project_context(monkeypatch, tool_call):
    user_repo = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(locale="zh", disabled=0))
    )
    services = SimpleNamespace(
        user_policy_repo=object(),
        usage_repo=object(),
        user_repo=user_repo,
        provider_repo=object(),
        project_task_repo=SimpleNamespace(get=MagicMock(return_value=None)),
    )
    store = MagicMock()
    store.is_model_ref_usable.return_value = True
    store.build_harness_configs.return_value = []
    monkeypatch.setattr(title, "ProviderStore", MagicMock(return_value=store))
    monkeypatch.setattr(title, "assert_token_quota_available", MagicMock())
    response = AIMessage(
        content="<think>hidden reasoning</think>标题：修复登录问题。",
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        tool_calls=[{"id": "unsafe", "name": "shell", "args": {}}] if tool_call else [],
    )
    model = SimpleNamespace(
        ainvoke=AsyncMock(return_value=response), _harness_max_output_tokens=2_048
    )
    factory = SimpleNamespace(get_chat_model=MagicMock(return_value=model), aclose=AsyncMock())
    monkeypatch.setattr(title, "ChatModelFactory", MagicMock(return_value=factory))
    job = TaskTitleJob("task", 7, 1, "用户需求" * 2_000, "chosen/model", "worker", 1)
    result = await title.TaskTitleAgent(services).generate(job)
    assert result == title.TitleResult(None if tool_call else "修复登录问题", 11, 7)
    messages = model.ainvoke.call_args.args[0]
    assert (
        len(messages) == 2
        and isinstance(messages[0], SystemMessage)
        and isinstance(messages[1], HumanMessage)
    )
    assert messages[1].content == job.prompt[:4_000]
    assert model.ainvoke.call_args.kwargs == {"max_tokens": 2_048}
    factory.get_chat_model.assert_called_once_with("chosen/model")
    factory.aclose.assert_awaited_once()
