"""Native ACP task execution uses real protocol shapes without launching a Runner."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.schema import (
    AgentCapabilities,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
)

from octop.infra.projects.engines import acp as module
from octop.infra.projects.engines.acp import (
    ACPFeatureError,
    ACPTaskError,
    ACPTaskSession,
    _TaskClient,
)


def initial(session_id="native-session", **extra):
    return NewSessionResponse.model_validate({"sessionId": session_id, **extra})


@pytest.fixture
def native(monkeypatch, tmp_path):
    connection = SimpleNamespace(
        initialize=AsyncMock(
            return_value=InitializeResponse(
                protocol_version=PROTOCOL_VERSION,
                agent_capabilities=AgentCapabilities(load_session=True),
            )
        ),
        new_session=AsyncMock(return_value=initial()),
        load_session=AsyncMock(return_value=LoadSessionResponse()),
        prompt=AsyncMock(return_value=PromptResponse(stop_reason="end_turn")),
        cancel=AsyncMock(),
        close=AsyncMock(),
        close_session=AsyncMock(),
        set_session_mode=AsyncMock(),
        set_config_option=AsyncMock(return_value=None),
    )
    process = SimpleNamespace(stdin=object(), stdout=object())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    stop = AsyncMock()
    monkeypatch.setattr(module, "_stop_process", stop)
    client = []

    def connect(callback, *args, **kwargs):
        client.append(callback)
        return connection

    monkeypatch.setattr("acp.connect_to_agent", connect)
    session = ACPTaskSession(
        ["trusted-runner", "acp"],
        {"API_KEY": "private-key"},
        str(tmp_path),
        [{"name": "allowed", "command": "allowed-mcp", "args": [], "env": []}],
    )
    return session, connection, client, stop


async def collect(session, **kwargs):
    return [
        event async for event in session.stream([{"type": "text", "text": "do this"}], **kwargs)
    ]


@pytest.mark.asyncio
async def test_open_forwards_workspace_mcp_and_reads_advertised_capabilities(native):
    session, connection, _, stop = native
    info = await session.open()
    assert info.session_id == "native-session" and not info.resumed and info.supports_load
    assert not info.prompt_images and info.model_ids == []
    assert connection.new_session.await_args.kwargs["cwd"] == session.cwd
    assert connection.new_session.await_args.kwargs["mcp_servers"] == session.mcp_servers
    capability = connection.initialize.await_args.kwargs["client_capabilities"]
    assert not capability.terminal and not capability.fs.write_text_file
    await session.close()
    stop.assert_awaited_once()
    connection.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_suppresses_history_and_failure_falls_back_only_before_prompt(native):
    session, connection, clients, _ = native

    async def load(**kwargs):
        await clients[0].session_update(
            session_id="saved",
            update={
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "old"},
            },
        )
        return LoadSessionResponse()

    connection.load_session.side_effect = load
    info = await session.open("saved")
    assert info.resumed and info.session_id == "saved"
    connection.new_session.assert_not_awaited()
    assert await collect(session) == [{"type": "message_end"}]
    await session.close()


@pytest.mark.asyncio
async def test_load_error_creates_fresh_session_without_hidden_prompt_replay(native):
    session, connection, _, _ = native
    connection.load_session.side_effect = RequestError(-32002, "private-key")
    info = await session.open("missing")
    assert not info.resumed and info.session_id == "native-session"
    connection.new_session.assert_awaited_once()
    connection.prompt.assert_not_awaited()
    connection.prompt.side_effect = RuntimeError("request contains private-key")
    with pytest.raises(ACPTaskError) as exc:
        await collect(session)
    assert "private-key" not in str(exc.value)
    connection.prompt.assert_awaited_once()
    await session.close()


@pytest.mark.asyncio
async def test_live_tokens_thought_and_partial_tool_updates_are_normalized(native):
    session, connection, clients, _ = native
    await session.open()

    async def prompt(**kwargs):
        updates = [
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "first"}},
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "reason"}},
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool",
                "title": "Read",
                "kind": "read",
                "rawInput": {"path": "file"},
                "status": "pending",
            },
            {"sessionUpdate": "tool_call_update", "toolCallId": "tool", "title": "Reading"},
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tool",
                "status": "completed",
                "rawOutput": "result",
            },
            {"sessionUpdate": "tool_call_update", "toolCallId": "tool", "status": "completed"},
        ]
        for update in updates:
            await clients[0].session_update(session_id="native-session", update=update)
        return PromptResponse(stop_reason="end_turn")

    connection.prompt.side_effect = prompt
    events = await collect(session)
    assert [event["type"] for event in events] == [
        "token",
        "reasoning",
        "tool_start",
        "progress",
        "tool_end",
        "message_end",
    ]
    assert events[0]["content"] == "first" and events[1]["content"] == "reason"
    assert events[2]["arguments"] == {"path": "file"}
    assert events[4]["type"] == "tool_end" and events[4]["call_id"] == "tool"
    assert events[4]["failed"] is False and events[4]["output"] == "result"
    assert events[4]["finished_at"] and "duration_ms" not in events[4]
    assert not any(event["type"] == "usage" for event in events)
    await session.close()


@pytest.mark.asyncio
async def test_native_running_update_refines_same_tool_parameters_and_execution_facts(native):
    from octop.infra.projects.content import TaskContent

    session, connection, clients, _ = native
    await session.open()

    async def prompt(**kwargs):
        for update in [
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "read-one",
                "kind": "read",
                "title": "read",
                "status": "pending",
                "rawInput": {},
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "read-one",
                "status": "in_progress",
                "rawInput": {"filePath": "seed.txt"},
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "read-one",
                "status": "completed",
                "rawOutput": "seed content",
            },
        ]:
            await clients[0].session_update(session_id="native-session", update=update)
        return PromptResponse(stop_reason="end_turn")

    connection.prompt.side_effect = prompt
    events = await collect(session)
    assert [event["type"] for event in events] == [
        "tool_start",
        "tool_start",
        "tool_end",
        "message_end",
    ]
    assert events[0]["arguments"] == {} and "started_at" not in events[0]
    assert events[1]["arguments"] == {"filePath": "seed.txt"} and events[1]["started_at"]
    assert events[2]["finished_at"] and events[2]["duration_ms"] >= 0
    content = TaskContent("native-turn")
    for event in events:
        if event["type"] == "tool_start":
            content.start_tool(
                event["call_id"],
                event["name"],
                arguments=event["arguments"],
                started_at=event.get("started_at"),
            )
        elif event["type"] == "tool_end":
            content.end_tool(
                event["call_id"],
                output=event["output"],
                finished_at=event["finished_at"],
                duration_ms=event.get("duration_ms"),
            )
    assert len(content.blocks) == 1
    block = content.blocks[0]
    assert block.arguments == {"filePath": "seed.txt"} and block.status == "completed"
    assert block.started_at is not None and block.finished_at >= block.started_at
    await session.close()


@pytest.mark.asyncio
async def test_pending_tool_without_running_fact_does_not_gain_execution_start(native):
    session, connection, clients, _ = native
    await session.open()

    async def prompt(**kwargs):
        await clients[0].session_update(
            session_id="native-session",
            update={
                "sessionUpdate": "tool_call",
                "toolCallId": "pending-only",
                "status": "pending",
                "rawInput": {},
            },
        )
        return PromptResponse(stop_reason="end_turn")

    connection.prompt.side_effect = prompt
    events = await collect(session)
    assert events[0]["type"] == "tool_start" and "started_at" not in events[0]
    assert not any(event["type"] == "tool_end" for event in events)
    await session.close()


@pytest.mark.asyncio
async def test_permission_uses_previously_reported_tool_details_without_inventing_arguments(native):
    session, _, _, _ = native
    await session.open()
    session._accept_updates = True
    session._tools["read-one"] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "read-one",
        "title": "Read seed file",
        "kind": "read",
        "rawInput": {"filePath": "seed.txt"},
        "locations": [{"path": "seed.txt"}],
    }
    session.on_permission = AsyncMock(return_value=None)
    await _TaskClient(session).request_permission(
        "native-session", {"toolCallId": "read-one", "rawInput": {}, "title": ""}, []
    )
    proposal = session.on_permission.await_args.args[1]
    assert proposal["rawInput"] == {"filePath": "seed.txt"}
    assert proposal["title"] == "Read seed file" and proposal["locations"] == [{"path": "seed.txt"}]
    assert "sessionUpdate" not in proposal
    await session.close()


@pytest.mark.asyncio
async def test_stream_yields_before_prompt_finishes(native):
    session, connection, clients, _ = native
    await session.open()
    finish = asyncio.Event()

    async def prompt(**kwargs):
        await clients[0].session_update(
            session_id="native-session",
            update={
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "streaming"},
            },
        )
        await finish.wait()
        return PromptResponse(stop_reason="end_turn")

    connection.prompt.side_effect = prompt
    stream = session.stream([{"type": "text", "text": "request"}])
    assert (await anext(stream))["content"] == "streaming"
    assert not finish.is_set()
    finish.set()
    assert [item async for item in stream] == [{"type": "message_end"}]
    await session.close()


@pytest.mark.asyncio
async def test_permission_callback_returns_only_an_advertised_option(native):
    session, _, _, _ = native
    await session.open()
    session._accept_updates = True
    handler = AsyncMock(return_value="allow")
    session.on_permission = handler
    client = _TaskClient(session)
    options = [{"optionId": "allow", "name": "Once", "kind": "allow_once"}]
    response = await client.request_permission("native-session", {"title": "Write"}, options)
    assert response.outcome.outcome == "selected" and response.outcome.option_id == "allow"
    handler.assert_awaited_once_with("native-session", {"title": "Write"}, options)
    handler.return_value = "unadvertised"
    assert (
        await client.request_permission("native-session", {}, options)
    ).outcome.outcome == "cancelled"
    session.on_permission = None
    assert (
        await client.request_permission("native-session", {}, options)
    ).outcome.outcome == "cancelled"
    await session.close()


@pytest.mark.asyncio
async def test_models_and_modes_are_validated_before_prompt_dispatch(native):
    session, connection, _, _ = native
    connection.new_session.return_value = initial(
        modes={
            "currentModeId": "ask",
            "availableModes": [{"id": "ask", "name": "Ask"}, {"id": "plan", "name": "Plan"}],
        },
        configOptions=[
            {
                "id": "model",
                "name": "Model",
                "type": "select",
                "category": "model",
                "currentValue": "native-a",
                "options": [
                    {
                        "group": "provider",
                        "name": "Provider",
                        "options": [
                            {"value": "native-a", "name": "A"},
                            {"value": "native-b", "name": "B"},
                        ],
                    }
                ],
            }
        ],
    )
    info = await session.open()
    assert info.model_ids == ["native-a", "native-b"]
    with pytest.raises(ACPFeatureError):
        await collect(session, model_id="octop-provider/model")
    connection.prompt.assert_not_awaited()
    with pytest.raises(ACPFeatureError):
        await collect(session, mode_id="unadvertised")
    connection.prompt.assert_not_awaited()
    await collect(session, model_id="native-b", mode_id="plan")
    connection.set_config_option.assert_awaited_once_with(
        config_id="model", session_id="native-session", value="native-b"
    )
    connection.set_session_mode.assert_awaited_once_with(
        session_id="native-session", mode_id="plan"
    )
    await session.close()


@pytest.mark.asyncio
async def test_unsupported_images_are_rejected_without_prompt(native):
    session, connection, _, _ = native
    await session.open()
    with pytest.raises(ACPFeatureError):
        _ = [
            event
            async for event in session.stream(
                [{"type": "image", "data": "abc", "mimeType": "image/png"}]
            )
        ]
    connection.prompt.assert_not_awaited()
    await session.close()


@pytest.mark.asyncio
async def test_cancellation_notifies_native_and_never_replays(native):
    session, connection, _, stop = native
    await session.open()
    started, finish = asyncio.Event(), asyncio.Event()

    async def prompt(**kwargs):
        started.set()
        await finish.wait()
        return PromptResponse(stop_reason="cancelled")

    async def cancel(**kwargs):
        finish.set()

    connection.prompt.side_effect = prompt
    connection.cancel.side_effect = cancel
    consumer = asyncio.create_task(collect(session))
    await started.wait()
    await session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    connection.cancel.assert_awaited_once_with(session_id="native-session")
    connection.prompt.assert_awaited_once()
    await session.close()
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_failure_is_sanitized_and_cleans_up(native):
    session, connection, _, stop = native
    connection.initialize.side_effect = RuntimeError("private-key")
    with pytest.raises(ACPTaskError) as exc:
        await session.open()
    assert "private-key" not in str(exc.value)
    connection.close.assert_awaited_once()
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_native_close_still_reaps_the_process(native):
    session, connection, _, stop = native
    info = await session.open()
    info.capabilities["sessionCapabilities"] = {"close": {}}
    connection.close_session.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await session.close()
    stop.assert_awaited_once()
    connection.close.assert_awaited_once()
    await session.close()
    stop.assert_awaited_once()
