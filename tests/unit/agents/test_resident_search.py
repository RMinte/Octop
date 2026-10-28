"""Private searches use native tools without broadening the resident execution scope."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from octop_harness.middleware.conversation_mode import ConversationModeMiddleware
from pydantic import Field
from tests.unit.agents.test_resident_profiles import profile_env as profile_env
from tests.unit.agents.test_resident_tools import _scope
from tests.unit.agents.test_resident_tools import tool_env as tool_env

from octop.infra.agents.resident_identity import (
    ResidentTextOnlyMiddleware,
    resident_execution_scope,
)
from octop.infra.agents.resident_tools import RESIDENT_WEB_SEARCH_TOOLS, resident_tools_scope
from octop.infra.projects.tasks import now_ms


def _tool(name):
    return StructuredTool.from_function(
        func=lambda query: f"Actual evidence for {query}", name=name, description="Read evidence."
    )


def _name(tool):
    return tool["function"]["name"] if isinstance(tool, dict) else tool.name


def _request(tool, **updates):
    call = {"name": tool.name, "args": {"query": "Octop"}, "id": "search-call", **updates}
    return SimpleNamespace(tool=tool, tool_call=call)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_model_sees_only_registered_searches_from_accepted_turn(tool_env, enabled):
    _, profile, _ = tool_env
    searches = [_tool(name) for name in RESIDENT_WEB_SEARCH_TOOLS]
    forbidden = [
        _tool(name)
        for name in (
            "web_fetch",
            "browser_use",
            "read_file",
            "execute",
            "mcp/search",
            "other_search",
        )
    ]
    declaration = {"type": "function", "function": {"name": "tavily_search"}}
    request = SimpleNamespace(
        tools=[*searches, *forbidden, declaration], override=lambda **kwargs: kwargs
    )
    guard = ResidentTextOnlyMiddleware("en", 1, profile.thread_id)
    with _scope(tool_env, web_search_enabled=enabled):
        tools = guard.wrap_model_call(request, lambda update: update)["tools"]
    assert set(map(_name, tools)) == {
        "resident_read_memory",
        "resident_read_tasks",
        *(RESIDENT_WEB_SEARCH_TOOLS if enabled else []),
    }
    assert declaration not in tools
    # A search declaration in a model request cannot grant a scope of its own.
    with resident_execution_scope(1, profile.thread_id):
        assert guard.wrap_model_call(request, lambda update: update)["tools"] == []


@pytest.mark.asyncio
async def test_sync_and_async_searches_use_native_handler_after_live_authorization(tool_env):
    _, profile, _ = tool_env
    guard = ResidentTextOnlyMiddleware("en", 1, profile.thread_id)
    request = _request(_tool("tavily_search"))
    receipt = ToolMessage(content="Real search receipt", tool_call_id="search-call")
    sync_handler, async_handler = Mock(return_value=receipt), AsyncMock(return_value=receipt)
    with _scope(tool_env, web_search_enabled=True):
        assert guard.wrap_tool_call(request, sync_handler) is receipt
        assert await guard.awrap_tool_call(request, async_handler) is receipt
    sync_handler.assert_called_once_with(request)
    async_handler.assert_awaited_once_with(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "disabled",
        "missing_scope",
        "wrong_owner",
        "wrong_thread",
        "wrong_turn",
        "cancelled",
        "expired",
        "completed",
        "disabled_user",
        "unregistered",
        "mismatched_name",
        "invalid_id",
    ],
)
async def test_search_rechecks_identity_request_and_registered_tool_before_dispatch(
    tool_env, invalid
):
    services, profile, source = tool_env
    guard = ResidentTextOnlyMiddleware("en", 1, profile.thread_id)
    request = _request(_tool("searchfree_search"))
    if invalid == "cancelled":
        services.resident_request_repo.cancel(
            1, profile.thread_id, "cancel-search", source.turn_id, now_ms()
        )
    elif invalid == "expired":
        assert services.resident_request_repo.renew(
            source.turn_id, "worker", now_ms(), now_ms() - 1
        )
    elif invalid == "completed":
        assert services.resident_request_repo.finish(
            source.turn_id, "worker", "completed", "done", "", now_ms()
        )
    elif invalid == "disabled_user":
        services.user_repo.set_disabled(1, True)
    elif invalid == "unregistered":
        request.tool = None
    elif invalid == "mismatched_name":
        request.tool_call["name"] = "brave_search"
    elif invalid == "invalid_id":
        request.tool_call["id"] = " "
    handler = AsyncMock()
    with ExitStack() as stack:
        if invalid == "missing_scope":
            stack.enter_context(resident_execution_scope(1, profile.thread_id))
        elif invalid == "wrong_turn":
            stack.enter_context(resident_execution_scope(1, profile.thread_id))
            stack.enter_context(
                resident_tools_scope(
                    services=services,
                    owner_id=1,
                    agent_id=profile.agent_id,
                    thread_id=profile.thread_id,
                    turn_id="another-turn",
                    web_search_enabled=True,
                )
            )
        else:
            stack.enter_context(_scope(tool_env, web_search_enabled=invalid != "disabled"))
            if invalid in {"wrong_owner", "wrong_thread"}:
                stack.enter_context(
                    resident_execution_scope(
                        2 if invalid == "wrong_owner" else 1,
                        "another-thread" if invalid == "wrong_thread" else profile.thread_id,
                    )
                )
        result = await guard.awrap_tool_call(request, handler)
    assert result.status == "error"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_real_graph_search_returns_evidence_and_denies_old_scope(tool_env, enabled):
    class SearchModel(FakeMessagesListChatModel):
        exposed: list[list[str]] = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            self.exposed.append([_name(tool) for tool in tools])
            return self

    searches = []

    async def search(query: str) -> str:
        """Return actual public evidence."""
        searches.append(query)
        return "Actual cited evidence https://example.test/octop"

    native_search = StructuredTool.from_function(coroutine=search, name="searchfree_search")
    _, profile, _ = tool_env
    model = SearchModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "searchfree_search", "args": {"query": "Octop"}, "id": "search-call"}
                ],
            ),
            AIMessage(content="The actual search evidence has been checked."),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[native_search, _tool("web_fetch"), _tool("execute")],
        middleware=[
            ResidentTextOnlyMiddleware("en", 1, profile.thread_id),
            ConversationModeMiddleware(),
        ],
    )
    with _scope(tool_env, read_tools=False, web_search_enabled=enabled):
        result = await graph.ainvoke(
            {"messages": [HumanMessage("Find current public evidence")]},
            config={"configurable": {"conversation_mode": "ask"}},
        )
    receipts = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(receipts) == 1 and receipts[0].tool_call_id == "search-call"
    assert receipts[0].status == ("success" if enabled else "error")
    assert searches == (["Octop"] if enabled else [])
    if enabled:
        assert receipts[0].content == "Actual cited evidence https://example.test/octop"
    assert result["messages"][-1].content == "The actual search evidence has been checked."
    if enabled:
        assert model.exposed and all(set(names) == {"searchfree_search"} for names in model.exposed)
    else:
        assert not model.exposed
