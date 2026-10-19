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
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

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


def ready_execution(*args, **kwargs):
    middleware = TaskExecution(*args, **kwargs)
    middleware.accept_opening()
    return middleware


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
        await ready_execution(Planning(), "zh", "root").awrap_model_call(original, handler)
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
    assert schema["required"] == ["path", "brief"]
    assert schema["properties"]["brief"]["minLength"] == 1
    assert schema["properties"]["brief"]["maxLength"] == 240
    assert forwarded.tools[0] is not tool
    assert forwarded.tools[1] is original.tools[1]
    assert tool.args_schema.model_json_schema() == original_schema
    assert events == [{"type": "task_step_context", "plan_id": "plan", "step_id": "step-a"}]


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
    await ready_execution(Planning(), "en", "root").awrap_model_call(model_request([tool]), handler)
    forwarded = handler.await_args.args[0].tools[0]
    schema = forwarded["function"]["parameters"] if wrapped else forwarded["parameters"]
    assert STEP_METADATA in schema["properties"]
    assert schema["required"] == ["path", "brief"]
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
    middleware = ready_execution(Planning(), "en", "root")
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
    middleware = ready_execution(Planning(), "en", "root")
    model = model_request([note_tool()])
    model_handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model, model_handler)
    assert model_handler.await_args.args[0] is model
    call = request(args={STEP_METADATA: "step-b"})
    tool_handler = AsyncMock(return_value=ToolMessage(content="Peer", tool_call_id="call"))
    await middleware.run(call, tool_handler)
    assert tool_handler.await_args.args[0] is call
    assert events == []


async def test_tool_ownership_is_fixed_at_start_and_only_public_output_is_emitted(captured):
    events, _ = captured
    planning = Planning()
    middleware = ready_execution(planning, "en", "root")
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
    result = await ready_execution(Planning(), "zh", "root").run(
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
    await ready_execution(planning, "en", "root").run(
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
        await ready_execution(Planning(), "en", "root").run(
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
        await ready_execution(Planning(), "en", "root").run(
            request(args={"brief": "Read the note"}), AsyncMock(side_effect=error)
        )
    assert [event["type"] for event in events] == ["task_tool_start", "task_tool_end"]
    assert events[-1]["failed"] is True and events[-1]["output"] is None
    assert "secret" not in str(events)


async def test_graph_interrupt_has_no_false_end_and_resume_keeps_the_started_step(captured):
    events, _ = captured
    planning = Planning()
    middleware = ready_execution(planning, "en", "root")
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
    middleware = ready_execution(planning, "en", "root")
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
    await ready_execution(Planning(), "en", "root").run(call, handler)
    assert handler.await_args.args[0] is call
    assert events == []


@pytest.mark.parametrize("first", ["call-a", "call-b"])
async def test_parallel_tools_keep_bindings_in_either_completion_order(captured, first):
    events, _ = captured
    planning = Planning()
    middleware = ready_execution(planning, "en", "root")
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
    middleware = ready_execution(Planning(), "en", "root")
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
    middleware = ready_execution(planning, "en", "root")
    model_handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([note_tool()]), model_handler)
    assert model_handler.await_args.args[0].tools == []
    events.clear()
    handler = AsyncMock()
    result = await middleware.run(request(), handler)
    assert result.status == "error"
    handler.assert_not_awaited()
    assert events == []


async def test_disabled_action_generation_keeps_actual_execution_records(captured):
    events, _ = captured
    middleware = ready_execution(Planning(), "en", "root", action_timeline_enabled=False)
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
    middleware = ready_execution(Planning(), "en", "root")
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
    middleware = ready_execution(Planning(), "en", "root")
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
async def test_work_without_a_valid_brief_never_dispatches(captured, args):
    handler = AsyncMock()
    result = await ready_execution(Planning(), "zh", "root").run(request(args=args), handler)
    assert result.status == "error"
    assert result.content == tr("projects.task_brief_invalid", "zh")
    handler.assert_not_awaited()
    assert captured[0] == []


async def test_brief_length_counts_unicode_scalars(captured):
    handler = AsyncMock(return_value=ToolMessage(content="Result", tool_call_id="call"))
    await ready_execution(Planning(), "en", "root").run(
        request(args={"brief": "读" * 240}), handler
    )
    handler.assert_awaited_once()
    assert captured[0][0]["brief"] == "读" * 240


@pytest.mark.parametrize("invalid", [{}, {"brief": " "}])
async def test_reused_active_call_id_cannot_bypass_brief_validation(captured, invalid):
    middleware = ready_execution(Planning(), "en", "root")
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def execute(actual):
        dispatched.append(actual.tool_call["args"]["path"])
        entered.set()
        await release.wait()
        return ToolMessage(content="Read", tool_call_id="call")

    first = asyncio.create_task(
        middleware.run(request(args={"path": "first.md", "brief": "Read the first note"}), execute)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = await middleware.run(request(args={"path": "second.md", **invalid}), execute)
        assert second.status == "error"
        assert second.content == tr("projects.task_brief_invalid", "en")
        assert dispatched == ["first.md"]
    finally:
        release.set()
        await first
    assert [event["type"] for event in captured[0]] == ["task_tool_start", "task_tool_end"]


@pytest.mark.parametrize("name", ["planning", "message_notify_user", "ask_user_question"])
async def test_control_tools_do_not_require_host_briefs(captured, name):
    middleware = ready_execution(Planning(), "en", "root")
    tool = note_tool(name)
    handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([tool]), handler)
    forwarded = handler.await_args.args[0].tools[0].tool_call_schema
    schema = forwarded if isinstance(forwarded, dict) else forwarded.model_json_schema()
    assert "brief" not in schema["properties"]
    assert middleware.rejection(request(name).tool_call, tool) is None


async def test_provider_input_schema_requires_brief_without_mutating_original(captured):
    tool = {
        "name": "mcp_read",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
    original = deepcopy(tool)
    middleware = ready_execution(Planning(), "en", "root")
    handler = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([tool]), handler)
    projected = handler.await_args.args[0].tools[0]
    assert projected["input_schema"]["required"] == ["path", "brief"]
    await middleware.awrap_model_call(model_request([projected]), handler)
    assert handler.await_args.args[0].tools[0]["input_schema"]["required"] == ["path", "brief"]
    assert tool == original


@pytest.mark.parametrize(
    "name,args,allowed",
    [
        ("read_note", {}, False),
        ("planning", {"command": "mark_step"}, False),
        ("planning", {"command": ["create"]}, False),
        ("planning", {"command": "create"}, True),
        ("planning", {"command": "get"}, True),
        ("message_notify_user", {"text": "Opening"}, True),
        ("ask_user_question", {}, True),
    ],
)
async def test_work_requires_a_durable_opening_with_explicit_exemptions(
    captured, name, args, allowed
):
    middleware = TaskExecution(Planning(), "en", "root")
    call = request(name, args=args)
    error = middleware.rejection(call.tool_call)
    assert (error is None) is allowed
    if not allowed:
        handler = AsyncMock()
        result = await middleware.run(call, handler)
        assert result.status == "error"
        assert result.content == tr("projects.progress_opening_required", "en")
        handler.assert_not_awaited()
        assert captured[0] == []


async def test_notice_success_cannot_authorize_work_in_the_same_response(captured):
    events, _ = captured
    middleware = TaskExecution(Planning(), "en", "root")
    model = AsyncMock(return_value=ModelResponse(result=[]))
    await middleware.awrap_model_call(model_request([note_tool()]), model)
    middleware.accept_opening()
    handler = AsyncMock(return_value=ToolMessage(content="Actual result", tool_call_id="call"))
    blocked = await middleware.run(request(args={"brief": "Read the note"}), handler)
    assert blocked.status == "error"
    handler.assert_not_awaited()
    await middleware.awrap_model_call(model_request([note_tool()]), model)
    events.clear()
    allowed = await middleware.run(request(args={"brief": "Read the note"}), handler)
    assert allowed.content == "Actual result" and handler.await_count == 1
    assert [event["type"] for event in events] == ["task_tool_start", "task_tool_end"]


@pytest.mark.parametrize(
    "status,introduction,accepted",
    [
        ("completed", True, True),
        ("completed", False, False),
        ("completed", None, False),
        ("streaming", True, False),
        ("failed", True, False),
    ],
)
async def test_restore_accepts_only_explicit_completed_introduction(
    captured, status, introduction, accepted
):
    middleware = TaskExecution(Planning(), "en", "root")
    middleware.restore(
        [
            {
                "type": "progress",
                "status": status,
                "introduction": introduction,
                "tool_call_id": "opening",
            }
        ]
    )
    assert (
        middleware.rejection(request(args={"brief": "Read the note"}).tool_call) is None
    ) is accepted


async def test_restored_opening_does_not_authorize_its_interrupted_sibling_work(captured):
    middleware = TaskExecution(Planning(), "en", "root")
    middleware.restore(
        [
            {
                "type": "progress",
                "status": "completed",
                "introduction": True,
                "tool_call_id": "opening",
            }
        ]
    )
    middleware.restore_model_calls(
        [
            request("message_notify_user", call_id="opening").tool_call,
            request(call_id="work").tool_call,
        ]
    )
    assert middleware.rejection(request().tool_call) is not None
    middleware.restore_model_calls([request(call_id="later-work").tool_call])
    assert middleware.rejection(request(args={"brief": "Read the note"}).tool_call) is None
