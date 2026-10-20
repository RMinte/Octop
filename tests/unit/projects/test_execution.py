"""Root-task execution ownership follows actual model and tool boundaries."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, ValidationError

from octop.i18n import tr
from octop.infra.projects import execution
from octop.infra.projects.execution import STEP_METADATA, TaskExecution, strip_step_metadata


class Planning:
    phase = "executing"

    def __init__(self):
        self.active = "step-a"
        self.running = {"step-a", "step-b"}

    def binding(self, step_id=None):
        chosen = step_id or self.active
        if chosen is not None and chosen not in self.running:
            raise ValueError("step is not in progress")
        return "plan", chosen


@pytest.fixture
def captured(monkeypatch):
    events = []
    config = {"configurable": {"thread_id": "root"}}
    monkeypatch.setattr(execution, "get_config", lambda: config)
    monkeypatch.setattr(execution, "get_stream_writer", lambda: events.append)
    return events, config


def request(name="read_note", *, args=None, tool=None, call_id="call"):
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id, "type": "tool_call"},
        tool=tool,
        state={},
        runtime=None,
    )


def model_request(tools):
    return ModelRequest(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="Answer")]),
        messages=[],
        tools=tools,
    )


class NoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


def note_tool(name="read_note"):
    async def read_note(path):
        return "Read " + path

    return StructuredTool.from_function(
        name=name, description="Read a note.", args_schema=NoteArgs, coroutine=read_note
    )


async def test_model_only_schema_and_context_are_copies(captured):
    events, _ = captured
    tool = note_tool()
    original_schema = deepcopy(tool.args_schema.model_json_schema())
    original = model_request([tool, note_tool("planning")])
    response = ModelResponse(result=[AIMessage(content="Answer")])
    handler = AsyncMock(return_value=response)

    assert (
        await TaskExecution(Planning(), "zh", "root").awrap_model_call(original, handler)
        is response
    )

    forwarded = handler.await_args.args[0]
    schema = forwarded.tools[0].tool_call_schema
    assert schema["properties"]["path"] == original_schema["properties"]["path"]
    assert STEP_METADATA in schema["properties"]
    assert STEP_METADATA not in schema.get("required", [])
    assert schema["properties"][STEP_METADATA]["description"] == tr(
        "projects.task_step_metadata", "zh"
    )
    assert schema["required"] == ["path"]
    assert "brief" not in schema["properties"]
    assert forwarded.tools[0] is not tool
    assert forwarded.tools[1] is original.tools[1]
    assert tool.args_schema.model_json_schema() == original_schema
    assert events == [
        {
            "type": "task_step_context",
            "plan_id": "plan",
            "step_id": "step-a",
            "tools_disabled": False,
        }
    ]


@pytest.mark.parametrize("wrapped", [False, True])
async def test_dictionary_schemas_keep_their_format_and_original_parameters(captured, wrapped):
    function = {
        "name": "external_tool",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
    tool = {"type": "function", "function": function} if wrapped else function
    original = deepcopy(tool)
    handler = AsyncMock(return_value=ModelResponse(result=[]))
    await TaskExecution(Planning(), "en", "root").awrap_model_call(model_request([tool]), handler)
    forwarded = handler.await_args.args[0].tools[0]
    schema = forwarded["function"]["parameters"] if wrapped else forwarded["parameters"]
    assert STEP_METADATA in schema["properties"]
    assert schema["required"] == ["path"]
    assert "brief" not in schema["properties"]
    assert tool == original


async def test_a_business_parameter_with_the_same_name_is_not_host_metadata(captured):
    events, _ = captured

    class BusinessArgs(BaseModel):
        task_step_id: int

    async def business(task_step_id):
        return str(task_step_id)

    tool = StructuredTool.from_function(
        name="business",
        description="Business identifier.",
        args_schema=BusinessArgs,
        coroutine=business,
    )
    middleware = TaskExecution(Planning(), "en", "root")
    handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([tool]), handler)
    forwarded = handler.await_args.args[0].tools[0]
    assert forwarded.tool_call_schema["properties"][STEP_METADATA]["type"] == "integer"
    events.clear()
    original = request(
        "business", args={STEP_METADATA: 42, "brief": "Read the business value"}, tool=tool
    )

    async def execute(actual):
        return await actual.tool.ainvoke(actual.tool_call)

    result = await middleware.run(original, execute)
    assert result.content == "42"
    assert events[0]["arguments"] == {STEP_METADATA: 42}
    assert events[0]["step_id"] == "step-a"
    assert strip_step_metadata(original.tool_call, tool)["args"] == {STEP_METADATA: 42}


@pytest.mark.parametrize(
    "configurable",
    [{"thread_id": "peer"}, {"thread_id": "root", "ls_agent_type": "subagent"}],
)
async def test_subagent_models_and_tools_do_not_emit_root_facts(captured, configurable):
    events, config = captured
    config["configurable"] = configurable
    middleware = TaskExecution(Planning(), "en", "root")
    model = model_request([note_tool()])
    model_handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model, model_handler)
    assert model_handler.await_args.args[0] is model
    call = request(args={STEP_METADATA: "step-b"})
    tool_handler = AsyncMock(return_value=ToolMessage(content="Peer", tool_call_id="call"))
    await middleware.run(call, tool_handler)
    assert tool_handler.await_args.args[0] is call
    assert events == []


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("native", [False, True])
async def test_client_project_hides_server_writes_and_keeps_read_export_and_client_tools(
    captured, nested, native
):
    _, config = captured
    if nested:
        config["configurable"]["ls_agent_type"] = "subagent"
    blocked = {"write_file", "edit_file", "execute"}
    allowed = {
        "read_file",
        "ls",
        "publish_workspace_file",
        "publish_workspace_archive",
        "client__write_file",
        "client__exec_command",
    }
    tools = [note_tool(name) for name in sorted(blocked | allowed)]
    if native:
        tools = [
            {
                "type": "function",
                "function": {"name": tool.name, "parameters": {"type": "object", "properties": {}}},
            }
            for tool in tools
        ]
    original = model_request(tools)
    middleware = TaskExecution(Planning(), "en", "root", client_directory="C:\\selected-project")
    handler = AsyncMock(
        return_value=ModelResponse(result=[AIMessage(content="Waiting for the client.")])
    )
    await middleware.awrap_model_call(original, handler)
    forwarded = handler.await_args.args[0]
    assert {execution._tool_name(tool) for tool in forwarded.tools} == allowed
    assert {execution._tool_name(tool) for tool in original.tools} == blocked | allowed


@pytest.mark.parametrize("name", ["write_file", "edit_file", "execute"])
@pytest.mark.parametrize("nested", [False, True])
async def test_client_project_rejects_unadvertised_server_writes_before_dispatch(
    captured, name, nested
):
    events, config = captured
    if nested:
        config["configurable"]["ls_agent_type"] = "subagent"
    middleware = TaskExecution(Planning(), "en", "root", client_directory="C:\\selected-project")
    actual = request(name, args={"file_path": "/replacement/app.js"})
    assert middleware.rejection(actual.tool_call) == tr("projects.client_workspace_required", "en")
    handler = AsyncMock()
    result = await middleware.run(actual, handler)
    assert result.status == "error" and "client__" in result.content
    handler.assert_not_awaited()
    assert events == []


async def test_client_project_does_not_force_a_server_attachment_from_restored_writes(captured):
    publisher = SimpleNamespace(has_artifacts=AsyncMock(return_value=False))
    planning = SimpleNamespace(phase="unplanned", binding=lambda step_id=None: (None, None))
    middleware = TaskExecution(
        planning,
        "en",
        "root",
        artifact_publisher=publisher,
        client_directory="C:\\selected-project",
    )
    middleware.restore(
        [
            {
                "type": "tool",
                "tool_name": "write_file",
                "tool_call_id": "old-server-write",
                "status": "completed",
                "started_at": datetime.now(UTC).isoformat(),
                "arguments": {"file_path": "/old/app.html"},
            }
        ]
    )
    assert not await middleware.delivery_pending()
    handler = AsyncMock(
        return_value=ModelResponse(result=[AIMessage(content="The local channel is not ready.")])
    )
    await middleware.awrap_model_call(model_request([note_tool("read_file")]), handler)
    assert handler.await_count == 1
    publisher.has_artifacts.assert_not_awaited()


async def test_tool_ownership_is_fixed_at_start_and_only_public_output_is_emitted(captured):
    events, _ = captured
    planning = Planning()
    middleware = TaskExecution(planning, "en", "root")
    original = request(
        args={"path": "note.md", STEP_METADATA: "step-b", "brief": "Read the note"},
        tool=note_tool(),
    )
    raw = deepcopy(original.tool_call)
    output = [{"type": "text", "text": "Complete tool output"}]

    async def execute(actual):
        assert actual.tool is original.tool
        assert actual.tool_call["args"] == {"path": "note.md"}
        planning.active = "step-a"
        planning.running.remove("step-b")
        return ToolMessage(
            content=output,
            tool_call_id="call",
            artifact={"credentials": "private"},
            additional_kwargs={"encrypted_reasoning": "private"},
        )

    await middleware.run(original, execute)

    assert original.tool_call == raw
    start, end = events
    assert start["type"] == "task_tool_start" and start["arguments"] == {"path": "note.md"}
    assert (start["plan_id"], start["step_id"]) == ("plan", "step-b")
    assert (end["plan_id"], end["step_id"]) == ("plan", "step-b")
    assert end["output"] == output and end["output"] is not output
    assert end["failed"] is False
    assert datetime.fromisoformat(start["started_at"]) <= datetime.fromisoformat(end["finished_at"])
    assert end["duration_ms"] >= 0
    assert "private" not in str(events)


@pytest.mark.parametrize("step_id", [42, "", " ", "unknown", "completed-step"])
async def test_invalid_binding_never_dispatches_or_creates_execution_facts(captured, step_id):
    events, _ = captured
    handler = AsyncMock()
    result = await TaskExecution(Planning(), "zh", "root").run(
        request(args={STEP_METADATA: step_id, "brief": "Read the note"}), handler
    )
    assert result.status == "error" and result.tool_call_id == "call"
    assert result.content == tr("projects.task_step_invalid", "zh")
    handler.assert_not_awaited()
    assert events == []


async def test_no_plan_context_has_no_fabricated_step(captured):
    events, _ = captured
    planning = SimpleNamespace(phase="unplanned", binding=lambda step_id=None: (None, None))
    handler = AsyncMock(return_value=ToolMessage(content="Read", tool_call_id="call"))
    await TaskExecution(planning, "en", "root").run(
        request(args={"brief": "Read the note"}), handler
    )
    assert events[0]["plan_id"] is None and events[0]["step_id"] is None


async def test_command_uses_only_the_matching_tool_message_and_keeps_errors(captured):
    events, _ = captured
    result = Command(
        update={
            "messages": [
                ToolMessage(content="Unrelated", tool_call_id="other"),
                ToolMessage(
                    content="Validation failed",
                    tool_call_id="call",
                    status="error",
                    artifact={"private": True},
                ),
            ]
        }
    )
    handler = AsyncMock(return_value=result)
    assert (
        await TaskExecution(Planning(), "en", "root").run(
            request(args={"brief": "Read the note"}), handler
        )
        is result
    )
    assert events[-1]["failed"] is True and events[-1]["output"] == "Validation failed"
    assert "private" not in events[-1]


@pytest.mark.parametrize(
    "error", [RuntimeError("secret exception details"), asyncio.CancelledError()]
)
async def test_exceptions_and_cancellation_finish_without_exposing_exception_text(captured, error):
    events, _ = captured
    with pytest.raises(type(error)):
        await TaskExecution(Planning(), "en", "root").run(
            request(args={"brief": "Read the note"}), AsyncMock(side_effect=error)
        )
    assert [event["type"] for event in events] == ["task_tool_start", "task_tool_end"]
    assert events[-1]["failed"] is True and events[-1]["output"] is None
    assert "secret" not in str(events)


async def test_graph_interrupt_has_no_false_end_and_resume_keeps_the_started_step(captured):
    events, _ = captured
    planning = Planning()
    middleware = TaskExecution(planning, "en", "root")
    call = request(args={STEP_METADATA: "step-b", "brief": "Read the note"})
    with pytest.raises(GraphInterrupt):
        await middleware.run(call, AsyncMock(side_effect=GraphInterrupt()))
    assert len(events) == 1 and events[0]["type"] == "task_tool_start"
    planning.running.remove("step-b")
    result = ToolMessage(content="Resumed", tool_call_id="call")
    assert await middleware.run(call, AsyncMock(return_value=result)) is result
    assert [event["type"] for event in events] == ["task_tool_start", "task_tool_end"]
    assert events[-1]["step_id"] == "step-b"


async def test_restart_restores_real_start_and_ignores_unknown_client_timestamps(captured):
    events, _ = captured
    planning = Planning()
    planning.running.clear()
    middleware = TaskExecution(planning, "en", "root")
    started = datetime.now(UTC).isoformat()
    middleware.restore(
        [
            {
                "type": "tool",
                "status": "streaming",
                "tool_call_id": "call",
                "plan_id": "old-plan",
                "step_id": "old-step",
                "started_at": started,
            },
            {"type": "tool", "status": "streaming", "tool_call_id": "client", "started_at": None},
            {
                "type": "tool",
                "status": "completed",
                "tool_call_id": "finished",
                "started_at": started,
            },
        ]
    )
    await middleware.run(
        request(args={"brief": "Resume the original read"}),
        AsyncMock(return_value=ToolMessage(content="Restored", tool_call_id="call")),
    )
    assert len(events) == 1 and events[0]["type"] == "task_tool_end"
    assert (events[0]["plan_id"], events[0]["step_id"]) == ("old-plan", "old-step")
    assert events[0]["duration_ms"] >= 0


async def test_planning_is_excluded_from_execution_metadata(captured):
    events, _ = captured
    call = request("planning", args={"command": "get"})
    handler = AsyncMock(return_value=ToolMessage(content="Plan", tool_call_id="call"))
    await TaskExecution(Planning(), "en", "root").run(call, handler)
    assert handler.await_args.args[0] is call
    assert events == []


@pytest.mark.parametrize("first", ["call-a", "call-b"])
async def test_parallel_tools_keep_bindings_in_either_completion_order(captured, first):
    events, _ = captured
    planning = Planning()
    middleware = TaskExecution(planning, "en", "root")
    gates = {call_id: asyncio.Event() for call_id in ("call-a", "call-b")}
    both_started = asyncio.Event()
    entered = []

    async def execute(actual):
        call_id = actual.tool_call["id"]
        entered.append(call_id)
        if len(entered) == 2:
            both_started.set()
        await gates[call_id].wait()
        return ToolMessage(content="Result " + call_id, tool_call_id=call_id)

    tasks = {
        call_id: asyncio.create_task(
            middleware.run(
                request(call_id=call_id, args={STEP_METADATA: step_id, "brief": "Read the note"}),
                execute,
            )
        )
        for call_id, step_id in (("call-a", "step-a"), ("call-b", "step-b"))
    }
    try:
        await asyncio.wait_for(both_started.wait(), 1)
        planning.running.clear()
        planning.active = None
        second = next(call_id for call_id in tasks if call_id != first)
        gates[first].set()
        await tasks[first]
        gates[second].set()
        await tasks[second]
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    starts = {event["call_id"]: event for event in events if event["type"] == "task_tool_start"}
    ends = [event for event in events if event["type"] == "task_tool_end"]
    assert [event["call_id"] for event in ends] == [first, second]
    for end in ends:
        start = starts[end["call_id"]]
        assert end["step_id"] == start["step_id"]
        assert end["plan_id"] == start["plan_id"]
        assert end["output"] == "Result " + end["call_id"]


async def test_real_graph_delivers_context_and_public_execution_custom_events(monkeypatch):
    monkeypatch.setattr(execution, "tr", lambda key, locale: "Optional task step ownership.")
    middleware = TaskExecution(Planning(), "en", "root")
    tool = note_tool()

    async def node(state):
        assert middleware.is_root()
        model_handler = AsyncMock(return_value=ModelResponse(result=[]))
        await middleware.awrap_model_call(model_request([tool]), model_handler)

        async def execute(actual):
            return await actual.tool.ainvoke(actual.tool_call)

        result = await middleware.run(
            request(
                args={"path": "note.md", STEP_METADATA: "step-b", "brief": "Read the note"},
                tool=tool,
            ),
            execute,
        )
        return {"messages": [result]}

    builder = StateGraph(MessagesState)
    builder.add_node("work", node)
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    graph = builder.compile()
    events = [
        event
        async for event in graph.astream(
            {"messages": []}, config={"configurable": {"thread_id": "root"}}, stream_mode="custom"
        )
    ]
    assert [event["type"] for event in events] == [
        "task_step_context",
        "task_tool_start",
        "task_tool_end",
    ]
    assert events[0]["step_id"] == "step-a"
    assert events[1]["step_id"] == events[2]["step_id"] == "step-b"
    assert events[1]["arguments"] == {"path": "note.md"}
    assert events[2]["output"] == "Read note.md"


@pytest.mark.parametrize("phase", ["completed", "blocked"])
async def test_finished_plan_withdraws_tools_and_rejects_dispatch(captured, phase):
    events, _ = captured
    planning = Planning()
    planning.phase = phase
    planning.active = None
    middleware = TaskExecution(planning, "en", "root")
    model_handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([note_tool()]), model_handler)
    assert model_handler.await_args.args[0].tools == []
    assert model_handler.await_args.args[0].tool_choice is None
    assert events == [
        {
            "type": "task_step_context",
            "plan_id": "plan",
            "step_id": None,
            "tools_disabled": True,
        }
    ]
    events.clear()
    handler = AsyncMock()
    result = await middleware.run(request(), handler)
    assert result.status == "error"
    handler.assert_not_awaited()
    assert events == []


async def test_delivery_guard_requires_a_successful_write_and_current_turn_attachment(captured):
    publisher = SimpleNamespace(has_artifacts=AsyncMock(return_value=False))
    middleware = TaskExecution(Planning(), "en", "root", artifact_publisher=publisher)
    write = request("write_file", args={"file_path": "/app.html", "content": "proof"})
    await middleware.run(
        write,
        AsyncMock(return_value=ToolMessage(content="failed", tool_call_id="call", status="error")),
    )
    await middleware.require_delivery()
    publisher.has_artifacts.assert_not_awaited()
    await middleware.run(
        write, AsyncMock(return_value=ToolMessage(content="written", tool_call_id="call"))
    )
    with pytest.raises(ToolException, match="publish_workspace_archive"):
        await middleware.require_delivery()
    publisher.has_artifacts.return_value = True
    await middleware.require_delivery()


@pytest.mark.parametrize(
    ("wrote_files", "attached", "allowed"),
    [(False, False, False), (True, True, False), (True, False, True)],
)
async def test_completed_plan_allows_only_verified_missing_delivery(
    captured, wrote_files, attached, allowed
):
    publisher = SimpleNamespace(has_artifacts=AsyncMock(return_value=attached))
    planning = Planning()
    planning.phase, planning.active = "completed", None
    middleware = TaskExecution(planning, "en", "root", artifact_publisher=publisher)
    if wrote_files:
        middleware.restore(
            [
                {
                    "type": "tool",
                    "tool_name": "write_file",
                    "tool_call_id": "saved-write",
                    "status": "completed",
                    "started_at": datetime.now(UTC).isoformat(),
                    "arguments": {"file_path": "/app.html"},
                }
            ]
        )
    call = request("create_artifact", args={"name": "forbidden.txt", "content": "not allowed"})
    assert (
        middleware.rejection(call.tool_call) is not None
    )  # Closed until verified, including restore.
    assert await middleware.delivery_pending() is allowed
    model_handler = AsyncMock(return_value=ModelResponse(result=[]))
    if allowed:
        model_handler.return_value = ModelResponse(
            result=[AIMessage(content="", tool_calls=[call.tool_call])]
        )
    await middleware.awrap_model_call(model_request([note_tool("create_artifact")]), model_handler)
    assert (middleware.rejection(call.tool_call) is None) is allowed
    handler = AsyncMock(return_value=ToolMessage(content="attached", tool_call_id="call"))
    result = await middleware.run(call, handler)
    assert handler.await_count == int(allowed)
    assert (result.status == "success") is allowed
    assert (model_handler.await_args.args[0].tools == []) is not allowed


@pytest.mark.parametrize("status", ["completed", "failed", "streaming"])
async def test_restored_write_receipts_preserve_delivery_requirement(captured, status):
    publisher = SimpleNamespace(has_artifacts=AsyncMock(return_value=False))
    middleware = TaskExecution(Planning(), "en", "root", artifact_publisher=publisher)
    middleware.restore(
        [
            {
                "type": "tool",
                "tool_name": "write_file",
                "tool_call_id": "saved-write",
                "status": status,
                "started_at": datetime.now(UTC).isoformat(),
                "arguments": {"file_path": "/app.html"},
            }
        ]
    )
    assert await middleware.delivery_pending() is (status == "completed")


async def test_disabled_action_generation_keeps_actual_execution_records(captured):
    events, _ = captured
    middleware = TaskExecution(Planning(), "en", "root", action_timeline_enabled=False)
    model_handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([note_tool()]), model_handler)
    assert "brief" not in model_handler.await_args.args[0].tools[0].tool_call_schema["properties"]
    events.clear()
    handler = AsyncMock(return_value=ToolMessage(content="Actual result", tool_call_id="call"))
    await middleware.run(request(args={"path": "note.md"}), handler)
    assert [event["type"] for event in events] == ["task_tool_start", "task_tool_end"]
    assert events[-1]["output"] == "Actual result"
    notify = await middleware.run(request("message_notify_user"), AsyncMock())
    assert notify.status == "error" and len(events) == 2


async def test_brief_is_public_metadata_and_never_a_business_argument(captured):
    events, _ = captured
    middleware = TaskExecution(Planning(), "en", "root")
    handler = AsyncMock(return_value=ToolMessage(content="Result", tool_call_id="call"))
    original = request(args={"path": "note.md", "brief": " Read the note to verify its facts. "})
    await middleware.run(original, handler)
    assert handler.await_args.args[0].tool_call["args"] == {"path": "note.md"}
    assert events[0]["brief"] == "Read the note to verify its facts."
    assert original.tool_call["args"]["brief"].startswith(" ")


async def test_brief_named_business_field_is_retained(captured):
    class BusinessArgs(BaseModel):
        brief: int

    async def business(brief):
        return str(brief)

    tool = StructuredTool.from_function(
        name="business",
        description="Read a business value.",
        args_schema=BusinessArgs,
        coroutine=business,
    )
    middleware = TaskExecution(Planning(), "en", "root")
    model_handler = AsyncMock(return_value=ModelResponse(result=[]))
    original_schema = deepcopy(tool.tool_call_schema.model_json_schema())
    await middleware.awrap_model_call(model_request([tool]), model_handler)
    forwarded = model_handler.await_args.args[0].tools[0].tool_call_schema
    assert forwarded["properties"]["brief"] == original_schema["properties"]["brief"]
    assert forwarded["required"] == original_schema["required"]
    captured[0].clear()
    call = request("business", args={"brief": 42}, tool=tool)

    async def execute(actual):
        return await actual.tool.ainvoke(actual.tool_call)

    result = await middleware.run(call, execute)
    assert result.content == "42"
    assert captured[0][0]["arguments"] == {"brief": 42} and captured[0][0]["brief"] is None


async def test_business_brief_still_uses_the_business_schema_validation(captured):
    class BusinessArgs(BaseModel):
        brief: int

    async def business(brief):
        return str(brief)

    tool = StructuredTool.from_function(
        name="business",
        description="Read a business value.",
        args_schema=BusinessArgs,
        coroutine=business,
    )

    async def execute(actual):
        assert actual.tool_call["args"] == {"brief": "invalid integer"}
        return await actual.tool.ainvoke(actual.tool_call)

    with pytest.raises(ValidationError):
        await TaskExecution(Planning(), "en", "root").run(
            request("business", args={"brief": "invalid integer"}, tool=tool), execute
        )
    assert captured[0][-1]["failed"] is True


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"brief": None},
        {"brief": ""},
        {"brief": "  "},
        {"brief": 42},
        {"brief": True},
        {"brief": "读" * 241},
    ],
)
async def test_missing_or_invalid_display_brief_does_not_block_execution(captured, args):
    handler = AsyncMock(return_value=ToolMessage(content="Actual result", tool_call_id="call"))
    result = await TaskExecution(Planning(), "zh", "root").run(request(args=args), handler)
    assert result.content == "Actual result"
    handler.assert_awaited_once()
    assert handler.await_args.args[0].tool_call["args"] == {}
    assert captured[0][0]["brief"] is None
    assert [event["type"] for event in captured[0]] == ["task_tool_start", "task_tool_end"]


async def test_brief_length_counts_unicode_scalars(captured):
    handler = AsyncMock(return_value=ToolMessage(content="Result", tool_call_id="call"))
    await TaskExecution(Planning(), "en", "root").run(request(args={"brief": "读" * 240}), handler)
    handler.assert_awaited_once()
    assert captured[0][0]["brief"] == "读" * 240


@pytest.mark.parametrize("name", ["planning", "ask_user_question"])
async def test_control_tools_do_not_require_host_briefs(captured, name):
    middleware = TaskExecution(Planning(), "en", "root")
    tool = note_tool(name)
    handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([tool]), handler)
    forwarded = handler.await_args.args[0].tools[0].tool_call_schema
    schema = forwarded if isinstance(forwarded, dict) else forwarded.model_json_schema()
    assert "brief" not in schema["properties"]
    assert middleware.rejection(request(name).tool_call, tool) is None


async def test_provider_input_schema_never_adds_a_display_brief(captured):
    tool = {
        "name": "mcp_read",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
    original = deepcopy(tool)
    middleware = TaskExecution(Planning(), "en", "root")
    handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([tool]), handler)
    projected = handler.await_args.args[0].tools[0]
    assert projected["input_schema"]["required"] == ["path"]
    assert "brief" not in projected["input_schema"]["properties"]
    await middleware.awrap_model_call(model_request([projected]), handler)
    assert handler.await_args.args[0].tools[0]["input_schema"]["required"] == ["path"]
    assert tool == original


@pytest.mark.parametrize("status", ["completed", "streaming", "failed"])
async def test_old_progress_records_do_not_gate_work(captured, status):
    middleware = TaskExecution(Planning(), "en", "root")
    middleware.restore(
        [{"type": "progress", "status": status, "introduction": True, "tool_call_id": "opening"}]
    )
    handler = AsyncMock(return_value=ToolMessage(content="Read", tool_call_id="call"))
    await middleware.run(request(), handler)
    handler.assert_awaited_once()
    assert captured[0][0]["brief"] is None


@pytest.mark.parametrize("representation", ["tool", "function", "wrapped"])
async def test_new_model_requests_hide_notifications_but_existing_calls_can_finish(
    captured, representation
):
    middleware = TaskExecution(Planning(), "en", "root")
    notify = note_tool("message_notify_user")
    if representation != "tool":
        notify = {"name": "message_notify_user", "parameters": {"type": "object"}}
        if representation == "wrapped":
            notify = {"type": "function", "function": notify}
    model = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([notify, note_tool()]), model)
    assert len(model.await_args.args[0].tools) == 1
    assert model.await_args.args[0].tools[0].name == "read_note"
    handler = AsyncMock(return_value=ToolMessage(content="Saved", tool_call_id="call"))
    result = await middleware.run(request("message_notify_user"), handler)
    assert result.content == "Saved"
    handler.assert_awaited_once()
