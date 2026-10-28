"""Model tools cannot choose identity or expand the user's task authorization."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from octop_harness.middleware.conversation_mode import ConversationModeMiddleware
from pydantic import Field, ValidationError
from tests.unit.agents.test_resident_profiles import profile_env as profile_env

from octop.infra.agents import resident_tools
from octop.infra.agents.resident_identity import (
    ResidentTextOnlyMiddleware,
    resident_execution_scope,
)
from octop.infra.agents.resident_memory import (
    ResidentMemoryDeprecate,
    ResidentMemoryWrite,
    create_resident_memory,
    deprecate_resident_memory,
)
from octop.infra.agents.resident_profiles import ResidentProfileCreate, create_profile
from octop.infra.agents.resident_tools import ResidentTaskContext, resident_tools_scope
from octop.infra.projects.tasks import TaskMessage, now_ms


def test_host_knowledge_selection_is_normalized_and_never_enables_defaults():
    assert resident_tools.ResidentToolInput().knowledge_base_ids == []
    normalized = resident_tools.ResidentToolInput(
        knowledge_base_ids=[" selected ", "selected", "next"]
    )
    assert normalized.knowledge_base_ids == ["selected", "next"]
    with pytest.raises(ValidationError):
        resident_tools.ResidentToolInput(knowledge_base_ids=[" "])


@pytest.fixture
async def tool_env(profile_env):
    services, catalog, _ = profile_env
    services.secret_repo.get_or_create("jwt", lambda: b"test-resident-tool-cursor")
    profile = await create_profile(
        services,
        catalog,
        1,
        ResidentProfileCreate(
            request_id="tools-profile",
            name="Tool writer",
            template_kind="builtin",
            template_id="writer",
        ),
    )
    services.provider_repo.create(
        name="tools-model",
        kind="openai",
        base_url="https://model.example.test/v1",
        api_key="test-only",
        models_json='[{"id":"model","name":"model"}]',
    )
    source, _ = services.resident_request_repo.create(
        1, profile.thread_id, "tool-source", "tool-turn", "Prepare the draft", now_ms()
    )
    assert services.resident_request_repo.mark_running(
        source.turn_id, "worker", now_ms(), now_ms() + 30000
    )
    yield services, profile, source


@contextmanager
def _scope(
    tool_env,
    *,
    read_tools=True,
    task_context=None,
    knowledge_base_ids=None,
    web_search_enabled=False,
):
    services, profile, source = tool_env
    with (
        resident_execution_scope(1, profile.thread_id),
        resident_tools_scope(
            services=services,
            owner_id=1,
            agent_id=profile.agent_id,
            thread_id=profile.thread_id,
            turn_id=source.turn_id,
            read_tools=read_tools,
            task_context=task_context,
            knowledge_base_ids=knowledge_base_ids,
            web_search_enabled=web_search_enabled,
        ),
    ):
        yield


def _call(tool_env, name, args=None, call_id="model-call"):
    _, profile, _ = tool_env
    return resident_tools.execute_resident_tool(
        owner_id=1,
        thread_id=profile.thread_id,
        locale="en",
        tool_call={"name": name, "args": args or {}, "id": call_id},
    )


@pytest.mark.asyncio
async def test_only_host_selected_tools_are_exposed_and_generic_handlers_never_run(tool_env):
    _, profile, _ = tool_env
    guard = ResidentTextOnlyMiddleware("en", 1, profile.thread_id)
    request = SimpleNamespace(override=lambda **kwargs: kwargs)
    with resident_execution_scope(1, profile.thread_id):
        assert guard.wrap_model_call(request, lambda update: update)["tools"] == []
    handler = Mock()
    with _scope(tool_env):
        exposed = guard.wrap_model_call(request, lambda update: update)["tools"]
        assert {item["function"]["name"] for item in exposed} == {
            "resident_read_memory",
            "resident_read_tasks",
        }
        denied = guard.wrap_tool_call(
            SimpleNamespace(
                tool_call={"name": "execute", "args": {"command": "shell"}, "id": "unsafe"}
            ),
            handler,
        )
        assert denied.status == "error"
    handler.assert_not_called()
    with (
        resident_execution_scope(2, profile.thread_id),
        resident_tools_scope(
            services=tool_env[0],
            owner_id=1,
            agent_id=profile.agent_id,
            thread_id=profile.thread_id,
            turn_id="tool-turn",
            read_tools=True,
        ),
    ):
        assert resident_tools.resident_tool_definitions(1, profile.thread_id, "en") == []


@pytest.mark.asyncio
async def test_memory_search_reads_active_facts_of_this_instance_only(tool_env, profile_env):
    services, profile, _ = tool_env
    catalog = profile_env[1]
    first = create_resident_memory(
        services,
        1,
        profile.agent_id,
        profile.thread_id,
        ResidentMemoryWrite(assertion="My writing preference is concise."),
    )
    old = create_resident_memory(
        services,
        1,
        profile.agent_id,
        profile.thread_id,
        ResidentMemoryWrite(assertion="Outdated preference."),
    )
    deprecate_resident_memory(
        services,
        1,
        profile.agent_id,
        profile.thread_id,
        old.atom_id,
        ResidentMemoryDeprecate(reason="Old"),
    )
    other = await create_profile(
        services,
        catalog,
        1,
        ResidentProfileCreate(
            request_id="other", name="Other", template_kind="builtin", template_id="writer"
        ),
    )
    create_resident_memory(
        services,
        1,
        other.agent_id,
        other.thread_id,
        ResidentMemoryWrite(assertion="Other instance secret."),
    )
    with _scope(tool_env):
        result = _call(tool_env, "resident_read_memory", {"query": "writing", "limit": 10})
        assert result.status == "success"
        assert json.loads(result.content)["items"][0]["atom_id"] == first.atom_id
        assert "Other instance secret" not in result.content and "Outdated" not in result.content
        forged = _call(tool_env, "resident_read_memory", {"owner_id": 2})
        assert json.loads(forged.content)["error_code"] == "RESIDENT_TOOL_INPUT_INVALID"


@pytest.mark.asyncio
async def test_task_authorization_is_one_durable_slot_and_preserves_user_options(tool_env):
    services, profile, _ = tool_env
    authorization = ResidentTaskContext(
        project_id=None,
        message=TaskMessage(
            request_id="explicit-task",
            prompt="Only prepare a local draft. Do not send it.",
            options={
                "model_ref": "tools-model/model",
                "skills": [],
                "mcp_servers": [],
                "memory_enabled": False,
            },
        ),
    )
    with _scope(tool_env, task_context=authorization):
        created = _call(
            tool_env, "resident_create_task", {"brief": "Draft the proposal"}, "first-call"
        )
        assert created.status == "success", created.content
        replay = _call(
            tool_env, "resident_create_task", {"brief": "Draft the proposal"}, "different-call"
        )
        assert json.loads(created.content)["task_id"] == json.loads(replay.content)["task_id"]
        conflict = _call(
            tool_env, "resident_create_task", {"brief": "Send a different proposal"}, "third-call"
        )
        assert json.loads(conflict.content)["error_code"] == "RESIDENT_REQUEST_CONFLICT"
        forged = _call(
            tool_env, "resident_create_task", {"brief": "Draft", "project_id": "other-project"}
        )
        assert json.loads(forged.content)["error_code"] == "RESIDENT_TOOL_INPUT_INVALID"
        listed = _call(tool_env, "resident_read_tasks")
        assert len(json.loads(listed.content)["items"]) == 1
    task = json.loads(created.content)
    row = services.project_task_turn_repo.get(task["initial_turn_id"])
    assert "Do not send it" in row.prompt and "Draft the proposal" in row.prompt
    assert row.model_ref == "tools-model/model"
    assert json.loads(row.configuration_json)["mcp_servers"] == []


@pytest.mark.asyncio
async def test_cancelled_source_cannot_start_task_even_with_old_host_scope(tool_env):
    services, profile, source = tool_env
    authorization = ResidentTaskContext(
        project_id=None,
        message=TaskMessage(
            request_id="task-request", prompt="Draft", options={"model_ref": "tools-model/model"}
        ),
    )
    with _scope(tool_env, task_context=authorization):
        services.resident_request_repo.cancel(
            1, profile.thread_id, "cancel-source", source.turn_id, now_ms()
        )
        result = _call(tool_env, "resident_create_task", {"brief": "Draft"})
        assert result.status == "error" and json.loads(result.content)["error_code"] == "FORBIDDEN"
    assert services.resident_delegation_repo.get(1, "none") is None
    assert services.project_task_turn_repo.list("none") == []


@pytest.mark.asyncio
async def test_knowledge_search_uses_only_selected_currently_visible_bases(tool_env, monkeypatch):
    services, profile, _ = tool_env
    base = services.knowledge_repo.create_base(owner_user_id=1, name="Selected")
    other = services.knowledge_repo.create_base(owner_user_id=2, name="Other private")
    retrieve = AsyncMock(return_value="Actual cited context [doc-1]")
    monkeypatch.setattr("octop.infra.knowledge.retrieve.retrieve_context", retrieve)
    with _scope(tool_env, read_tools=False, knowledge_base_ids=[base.id]):
        result = await resident_tools.aexecute_resident_tool(
            owner_id=1,
            thread_id=profile.thread_id,
            locale="en",
            tool_call={
                "name": "resident_search_knowledge",
                "id": "knowledge",
                "args": {"query": "Writing"},
            },
        )
        assert json.loads(result.content)["context"] == "Actual cited context [doc-1]"
    assert retrieve.await_args.kwargs["knowledge_base_ids"] == (base.id,)
    assert retrieve.await_args.kwargs["is_admin"] is False
    retrieve.reset_mock()
    with _scope(tool_env, knowledge_base_ids=[other.id]):
        result = await resident_tools.aexecute_resident_tool(
            owner_id=1,
            thread_id=profile.thread_id,
            locale="en",
            tool_call={
                "name": "resident_search_knowledge",
                "id": "forged",
                "args": {"query": "secret"},
            },
        )
        assert json.loads(result.content)["error_code"] == "FORBIDDEN"
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_admission_drains_to_completion_before_model_cancellation_releases_scope(
    tool_env, monkeypatch
):
    _, profile, _ = tool_env
    entered, release = threading.Event(), threading.Event()
    original = resident_tools._read_tasks

    def slow_read(context, arguments):
        entered.set()
        release.wait(3)
        return original(context, arguments)

    monkeypatch.setattr(resident_tools, "_read_tasks", slow_read)
    with _scope(tool_env):
        call = asyncio.create_task(
            resident_tools.aexecute_resident_tool(
                owner_id=1,
                thread_id=profile.thread_id,
                locale="en",
                tool_call={"name": "resident_read_tasks", "id": "read", "args": {}},
            )
        )
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            call.cancel()
            await asyncio.sleep(0)
            call.cancel()
            await asyncio.sleep(0)
            assert not call.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await call


@pytest.mark.asyncio
async def test_real_langchain_graph_executes_dynamic_host_tool_and_returns_to_model(tool_env):
    class ToolModel(FakeMessagesListChatModel):
        exposed: list[list[str]] = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            self.exposed.append([item["function"]["name"] for item in tools])
            return self

    _, profile, _ = tool_env
    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "resident_read_memory", "args": {}, "id": "read-memory"}],
            ),
            AIMessage(content="The confirmed facts have been checked."),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[],
        middleware=[
            ResidentTextOnlyMiddleware("en", 1, profile.thread_id),
            ConversationModeMiddleware(),
        ],
    )
    with _scope(tool_env):
        result = await graph.ainvoke(
            {"messages": [HumanMessage("Check my memory")]},
            config={"configurable": {"conversation_mode": "craft"}},
        )
    tools = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tools) == 1 and tools[0].tool_call_id == "read-memory"
    assert tools[0].status == "success" and json.loads(tools[0].content)["items"] == []
    assert result["messages"][-1].content == "The confirmed facts have been checked."
    assert model.exposed and all(
        set(names) == {"resident_read_memory", "resident_read_tasks"} for names in model.exposed
    )
