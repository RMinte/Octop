"""Client tools cross the actual WebSocket, database and harness tool loop."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from octop.infra.projects import client_tools, runtime
from octop.infra.projects.client_tool_models import ClientToolDefinition
from octop.infra.projects.tasks import TaskExecutionOptions
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until
from tests.support.auth import create_user

CLIENT = "mac-install-001"
DEFINITION = {
    "name": "read_note",
    "description": "Read a note on the user computer.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
}
OPTIONS = {"client_id": CLIENT, "client_tools": [DEFINITION]}


@pytest.fixture
async def tool_env(env_with_provider, monkeypatch):
    from octop.api.routers import project_tasks_ws

    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    monkeypatch.setattr(client_tools, "_POLL_SECONDS", 0.01)
    calls = []

    async def run(services, owner, turn, history):
        options = json.loads(turn.configuration_json)["execution_options"]
        definition = ClientToolDefinition.model_validate(options["client_tools"][0])
        broker = client_tools.ClientToolBroker(
            services, owner, turn, options["client_id"], [definition]
        )
        yield {"type": "token", "content": "正在读取"}
        output = await broker.invoke(definition, {"path": "note.md"})
        calls.append(json.loads(output))
        yield {"type": "token", "content": "，已读取"}

    monkeypatch.setattr(runtime, "run_turn", run)
    yield *env_with_provider, calls


async def start_call(ws, *, request="tool-message-001", web_search_enabled=True):
    await command(ws, "client.attach", "attach-client-001", client_id=CLIENT)
    created = await command(
        ws,
        "task.create",
        "create-tool-task",
        data={
            "request_id": request,
            "prompt": "Read my note",
            "web_search_enabled": web_search_enabled,
            "options": {**OPTIONS, "action_timeline_enabled": False},
        },
    )
    assert created["type"] == "result", created
    call = (await until(ws, lambda f: f["type"] == "tool.call"))["data"]
    assert call["status"] == "pending" and call["arguments"] == {"path": "note.md"}
    return created["data"], call


def identity(call):
    return {key: call[key] for key in ("task_id", "turn_id", "call_id")}


async def claim(ws, call, request="claim-request-001"):
    return await command(
        ws, "tool.claim", "claim-command-001", **identity(call), request_id=request
    )


async def result(ws, call, token, output=None, *, is_error=False):
    return await command(
        ws,
        "tool.result",
        "result-command-001",
        **identity(call),
        data={"claim_token": token, "output": output, "is_error": is_error},
    )


async def task_with_status(client, auth, task_id, status):
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
            assert response.status_code == 200, response.text
            task = response.json()
            if task["status"] == status:
                return task
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", ["success", "tool_error", "timeout", "pending_timeout"])
async def test_real_harness_waits_for_client_and_receives_actual_result(
    env_with_provider, monkeypatch, mode
):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from octop_harness import HarnessAgent
    from octop_harness.llm.factory import ChatModelFactory

    from octop.api.routers import project_tasks_ws

    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    monkeypatch.setattr(client_tools, "_POLL_SECONDS", 0.01)
    if mode in {"timeout", "pending_timeout"}:
        monkeypatch.setattr(client_tools, "CALL_TIMEOUT_MS", 500)
    observed, names = [], set()

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any):
            names.update(t.name if hasattr(t, "name") else t.get("name") for t in tools)
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.append(messages)
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "model-tool-001",
                        "name": "client__read_note",
                        "args": {"path": "note.md"},
                    }
                ],
            ),
            AIMessage(content="已根据客户端笔记完成回答"),
        ]
    )

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    monkeypatch.setattr(
        runtime,
        "HarnessAgent",
        lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
    )
    experts = server.services.agent_repo.list_all()
    session, ws = await connected(client, auth)
    try:
        task, call = await start_call(ws)
        await command(ws, "task.subscribe", "subscribe-tool-001", task_id=task["task_id"])
        live = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
        assert live["status"] in {"running", "waiting_client_tool"}
        assert len(observed) == 1  # No second model request before a tool result.
        if mode == "pending_timeout":
            interrupted = await task_with_status(client, auth, task["task_id"], "interrupted")
            assert interrupted["active_turn_id"] == call["turn_id"]
            assert not interrupted["turns"][0]["recovery"]["can_resume"]
            state = await command(ws, "tool.get", "get-expired-call", **identity(call))
            assert state["data"]["status"] == "expired" and not state["data"]["dispatch_allowed"]
            assert (await claim(ws, call))["error"]["code"] == "TASK_CLIENT_TOOL_CLOSED"
            resumed = await command(
                ws,
                "task.resume",
                "resume-expired-call",
                task_id=task["task_id"],
                data={"turn_id": call["turn_id"], "request_id": "resume-expired-intent"},
            )
            assert resumed["error"]["code"] == "TASK_CONTROL_INVALID"
            assert len(observed) == 1
            return
        receipt = await claim(ws, call)
        token = receipt["data"]["claim_token"]
        if mode == "timeout":
            interrupted = await task_with_status(client, auth, task["task_id"], "interrupted")
            recovery = interrupted["turns"][0]["recovery"]
            assert not recovery["can_resume"] and recovery["needs_reconciliation"]
            state = await command(ws, "tool.get", "get-claimed-timeout", **identity(call))
            assert state["data"]["status"] == "claimed" and not state["data"]["dispatch_allowed"]
            assert state["data"]["error_code"] == "TASK_CLIENT_TOOL_TIMEOUT"
            assert len(observed) == 1
        accepted = await result(
            ws, call, token, {"text": "client-note-proof"}, is_error=mode == "tool_error"
        )
        assert accepted["data"]["status"] == "completed"
        if mode == "timeout":
            reconciled = await task_with_status(client, auth, task["task_id"], "interrupted")
            recovery = reconciled["turns"][0]["recovery"]
            assert recovery["can_resume"] and not recovery["needs_reconciliation"]
            assert len(observed) == 1
            resumed = await command(
                ws,
                "task.resume",
                "resume-claimed-call",
                task_id=task["task_id"],
                data={"turn_id": call["turn_id"], "request_id": "resume-claimed-intent"},
            )
            assert resumed["type"] == "result"
            assert len(resumed["data"]["turns"]) == 1
            assert resumed["data"]["turns"][0]["turn_id"] == call["turn_id"]
        final = await until(
            ws,
            lambda f: (
                f["type"] == "turn" and f["data"]["turn"]["status"] in {"completed", "failed"}
            ),
        )
        assert final["data"]["turn"]["reply"] == "已根据客户端笔记完成回答", final
        assert final["data"]["turn"]["status"] == "completed"
        assert "client__read_note" in names
        system_content = observed[0][0].content
        system = (
            system_content
            if isinstance(system_content, str)
            else "\n".join(str(block.get("text", "")) for block in system_content)
        )
        assert '"client_tools": ["client__read_note"]' in system
        replies = [m for m in observed[-1] if isinstance(m, ToolMessage)]
        assert any(
            m.tool_call_id == "model-tool-001" and "client-note-proof" in str(m.content)
            for m in replies
        )
        assert json.loads(replies[0].content)["is_error"] == (mode == "tool_error")
        assert server.services.agent_repo.list_all() == experts
        assert "claim_token" not in json.dumps(final)
    finally:
        await session.__aexit__(None, None, None)


async def test_claim_and_result_are_durable_and_idempotent_across_reconnect(tool_env):
    client, _, auth, outputs = tool_env
    session, ws = await connected(client, auth)
    task, call = await start_call(ws)
    receipt = await claim(ws, call)
    token = receipt["data"]["claim_token"]
    await session.__aexit__(None, None, None)
    session, ws = await connected(client, auth)
    try:
        await command(ws, "client.attach", "attach-client-002", client_id=CLIENT)
        replay = (await until(ws, lambda f: f["type"] == "tool.call"))["data"]
        assert replay["call_id"] == call["call_id"] and replay["status"] == "claimed"
        assert "claim_token" not in replay
        conflict = await claim(ws, call, "different-claim-001")
        assert conflict["error"]["code"] == "TASK_CLIENT_TOOL_CLAIMED"
        assert (await claim(ws, call))["data"]["claim_token"] == token
        bad = await result(ws, call, "bad-token-value-001", {})
        assert bad["status"] == 403
        output = {"b": 2, "a": "客户端结果"}
        assert (await result(ws, call, token, output, is_error=True))["type"] == "result"
        closed = await until(ws, lambda f: f["type"] == "tool.closed")
        assert closed["data"]["status"] == "completed"
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "completed"
        assert outputs == [{"output": output, "is_error": True}]
        # Repeat after model/turn completion is a receipt recovery, never another model run.
        assert (await result(ws, call, token, {"a": "客户端结果", "b": 2}, is_error=True))[
            "type"
        ] == "result"
        conflicting = await result(ws, call, token, {"text": "different"})
        assert conflicting["error"]["code"] == "TASK_CLIENT_TOOL_RESULT_CONFLICT"
        assert (await command(ws, "tool.get", "lookup-closed-001", **identity(call)))["data"][
            "status"
        ] == "completed"
        assert len(outputs) == 1
    finally:
        await session.__aexit__(None, None, None)


async def test_pending_call_survives_disconnect_and_does_not_block_text_or_queue_control(tool_env):
    client, _, auth, _ = tool_env
    session, ws = await connected(client, auth)
    task, call = await start_call(ws)
    await session.__aexit__(None, None, None)
    session, ws = await connected(client, auth)
    try:
        queued = (
            await command(
                ws,
                "task.create",
                "queue-second-001",
                data={
                    "request_id": "queue-message-001",
                    "prompt": "Second",
                    "options": OPTIONS,
                },
            )
        )["data"]
        assert queued["status"] == "queued"
        for _ in range(100):
            live = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
            if live["turns"][0]["reply"]:
                break
            await asyncio.sleep(0.01)
        assert (
            live["status"] in {"running", "waiting_client_tool"}
            and live["turns"][0]["reply"] == "正在读取"
        )
        await command(
            ws,
            "task.cancel",
            "stop-queued-001",
            task_id=queued["task_id"],
            data={"turn_id": queued["active_turn_id"]},
        )
        await command(ws, "client.attach", "reattach-client-001", client_id=CLIENT)
        replay = (await until(ws, lambda f: f["type"] == "tool.call"))["data"]
        assert replay["call_id"] == call["call_id"] and replay["status"] == "pending"
        receipt = await claim(ws, call)
        await result(ws, call, receipt["data"]["claim_token"], "read")
        assert (await settled(client, auth, task["task_id"]))["status"] == "completed"
    finally:
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("claimed", [False, True])
async def test_cancel_closes_call_and_rejects_late_results(tool_env, claimed):
    client, _, auth, outputs = tool_env
    session, ws = await connected(client, auth)
    try:
        task, call = await start_call(ws)
        token = (await claim(ws, call))["data"]["claim_token"] if claimed else None
        await command(
            ws,
            "task.cancel",
            "cancel-tool-001",
            task_id=task["task_id"],
            data={"turn_id": call["turn_id"]},
        )
        closed = await until(ws, lambda f: f["type"] == "tool.closed")
        assert closed["data"]["status"] == "cancelled"
        assert (await claim(ws, call))["error"]["code"] == "TASK_CLIENT_TOOL_CLOSED"
        if token:
            assert (await result(ws, call, token, "late"))["error"][
                "code"
            ] == "TASK_CLIENT_TOOL_CLOSED"
        assert (await settled(client, auth, task["task_id"]))["status"] == "cancelled"
        assert outputs == []
    finally:
        await session.__aexit__(None, None, None)


async def test_uncheckpointed_broker_timeout_does_not_replay_call(tool_env, monkeypatch):
    client, _, auth, outputs = tool_env
    monkeypatch.setattr(client_tools, "CALL_TIMEOUT_MS", 150)
    session, ws = await connected(client, auth)
    try:
        task, call = await start_call(ws)
        closed = await until(ws, lambda f: f["type"] == "tool.closed")
        assert closed["data"]["status"] == "expired"
        done = await settled(client, auth, task["task_id"])
        assert done["turns"][0]["error_code"] == "TASK_CLIENT_TOOL_TIMEOUT"
        assert done["status"] == "failed" and outputs == []
        assert (await claim(ws, call))["error"]["code"] == "TASK_CLIENT_TOOL_CLOSED"
    finally:
        await session.__aexit__(None, None, None)


async def test_wrong_user_client_and_identifiers_cannot_claim_or_inject(tool_env):
    client, _, auth, _ = tool_env
    alice = await create_user(client, auth, username="tool-alice")
    session, ws = await connected(client, auth)
    task, call = await start_call(ws)
    other, wrong = await connected(client, alice)
    try:
        await command(wrong, "client.attach", "attach-other-001", client_id=CLIENT)
        assert (await claim(wrong, call))["status"] == 404
        await other.__aexit__(None, None, None)
        other, wrong = await connected(client, auth)
        assert (await claim(wrong, call))["error"]["code"] == "TASK_FRAME_INVALID"
        await command(wrong, "client.attach", "wrong-client-001", client_id="another-mac-001")
        assert (await claim(wrong, call))["status"] == 403
        bad_call = {**call, "turn_id": "turn_unknown"}
        assert (await claim(ws, bad_call))["status"] == 404
        changed = await command(
            ws, "client.attach", "switch-client-001", client_id="another-mac-001"
        )
        assert changed["error"]["code"] == "TASK_FRAME_INVALID"
        await command(
            ws,
            "task.cancel",
            "cancel-final-001",
            task_id=task["task_id"],
            data={"turn_id": call["turn_id"]},
        )
    finally:
        await other.__aexit__(None, None, None)
        await session.__aexit__(None, None, None)


async def test_worker_shutdown_closes_claim_without_replaying(tool_env):
    client, server, auth, outputs = tool_env
    session, ws = await connected(client, auth)
    try:
        task, call = await start_call(ws)
        token = (await claim(ws, call))["data"]["claim_token"]
        await server.project_task_worker.close()
        done = await settled(client, auth, task["task_id"])
        assert done["turns"][0]["error_code"] == "TASK_INTERRUPTED"
        state = await command(ws, "tool.get", "get-interrupted-001", **identity(call))
        assert state["data"]["status"] == "interrupted"
        assert (await result(ws, call, token, "late"))["error"]["code"] == "TASK_CLIENT_TOOL_CLOSED"
        assert outputs == []
    finally:
        await session.__aexit__(None, None, None)


async def test_two_connections_cannot_claim_with_different_request_ids(tool_env):
    client, _, auth, outputs = tool_env
    session, ws = await connected(client, auth)
    other, second = await connected(client, auth)
    try:
        task, call = await start_call(ws)
        await command(second, "client.attach", "attach-second-001", client_id=CLIENT)
        replay = await until(second, lambda f: f["type"] == "tool.call")
        assert replay["data"]["call_id"] == call["call_id"]
        receipts = await asyncio.gather(claim(ws, call), claim(second, call, "claim-competing-001"))
        assert sorted(r["type"] for r in receipts) == ["error", "result"]
        winner = 0 if receipts[0]["type"] == "result" else 1
        loser = receipts[1 - winner]
        assert loser["error"]["code"] == "TASK_CLIENT_TOOL_CLAIMED"
        await result((ws, second)[winner], call, receipts[winner]["data"]["claim_token"], "once")
        assert (await settled(client, auth, task["task_id"]))["status"] == "completed"
        assert outputs == [{"output": "once", "is_error": False}]
    finally:
        await other.__aexit__(None, None, None)
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("claimed", [False, True])
async def test_stale_worker_preserves_calls_without_replaying_until_explicit_cancel(
    tool_env, claimed
):
    from octop.infra.projects.tasks import now_ms

    client, server, auth, outputs = tool_env
    session, ws = await connected(client, auth)
    try:
        task, call = await start_call(ws)
        token = (await claim(ws, call))["data"]["claim_token"] if claimed else None
        with server.services.db.connect() as conn:
            conn.execute(
                "UPDATE project_task_turns SET updated_at=? WHERE turn_id=?",
                (now_ms() - 100_000, call["turn_id"]),
            )
        # Simulate another worker recovering the crashed process's lease before any replay.
        runtime.ProjectTaskWorker(server.services)._recover()
        state = await command(ws, "tool.get", "get-stale-call-001", **identity(call))
        assert state["data"]["status"] == ("claimed" if claimed else "pending")
        assert not state["data"]["dispatch_allowed"]
        interrupted = await task_with_status(client, auth, task["task_id"], "interrupted")
        assert interrupted["active_turn_id"] == call["turn_id"]
        assert outputs == []
        await command(
            ws,
            "task.cancel",
            "cancel-stale-call",
            task_id=task["task_id"],
            data={"turn_id": call["turn_id"]},
        )
        assert (await settled(client, auth, task["task_id"]))["status"] == "cancelled"
        state = await command(ws, "tool.get", "get-cancelled-stale-call", **identity(call))
        assert state["data"]["status"] == "cancelled" and not state["data"]["dispatch_allowed"]
        assert (await claim(ws, call))["error"]["code"] == "TASK_CLIENT_TOOL_CLOSED"
        if token:
            assert (await result(ws, call, token, "late"))["error"][
                "code"
            ] == "TASK_CLIENT_TOOL_CLOSED"
        assert outputs == []
    finally:
        await session.__aexit__(None, None, None)


async def test_invalid_model_arguments_never_create_client_call(tool_env):
    client, server, auth, _ = tool_env
    definition = ClientToolDefinition.model_validate(DEFINITION)
    broker = client_tools.ClientToolBroker(server.services, 1, None, CLIENT, [definition])
    output = json.loads(await broker.invoke(definition, {"path": 123}))
    assert output == {"is_error": True, "error_code": "TASK_CLIENT_TOOL_ARGUMENTS_INVALID"}
    # Schema-local references work, while validation still prevents side effects.
    referenced = ClientToolDefinition.model_validate(
        {
            **DEFINITION,
            "input_schema": {
                "type": "object",
                "$defs": {"name": {"type": "string"}},
                "properties": {"path": {"$ref": "#/$defs/name"}},
                "required": ["path"],
            },
        }
    )
    assert json.loads(await broker.invoke(referenced, {"path": False}))["is_error"]
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_task_tool_calls").fetchone()[0] == 0


async def test_declarations_are_per_turn_and_bound_to_message_idempotency(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider

    async def run(*args):
        yield {"type": "token", "content": "done"}

    monkeypatch.setattr(runtime, "run_turn", run)
    body = {"request_id": "declared-message-001", "prompt": "hello", "options": OPTIONS}
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 202
    task = await settled(client, auth, response.json()["task_id"])
    assert task["turns"][0]["options"]["client_tools"] == [DEFINITION]
    changed = {**body, "options": {**OPTIONS, "client_id": "another-mac-001"}}
    assert (await client.post("/api/project-tasks", headers=auth, json=changed)).status_code == 409
    replay = await client.post("/api/project-tasks", headers=auth, json=body)
    assert replay.json()["task_id"] == task["task_id"]
    assert (
        await client.post(
            f"/api/project-tasks/{task['task_id']}/messages",
            headers=auth,
            json={"request_id": "undeclared-message-002", "prompt": "next"},
        )
    ).status_code == 202
    next_turn = (await settled(client, auth, task["task_id"]))["turns"][-1]
    assert (
        next_turn["options"]["client_tools"] is None and next_turn["options"]["client_id"] is None
    )


@pytest.mark.parametrize(
    "options",
    [
        {"client_tools": [DEFINITION]},
        {"client_id": "short", "client_tools": [DEFINITION]},
        {"client_id": CLIENT, "client_tools": [DEFINITION, DEFINITION]},
        {"client_id": CLIENT, "client_tools": [{**DEFINITION, "name": "bad-name"}]},
        {"client_id": CLIENT, "client_tools": [{**DEFINITION, "input_schema": {"type": "string"}}]},
        {
            "client_id": CLIENT,
            "client_tools": [
                {
                    **DEFINITION,
                    "input_schema": {
                        "type": "object",
                        "properties": {"x": {"$ref": "https://example.invalid/schema"}},
                    },
                }
            ],
        },
        {
            "client_id": CLIENT,
            "client_tools": [
                {
                    **DEFINITION,
                    "input_schema": {
                        "type": "object",
                        "$id": "https://example.invalid/schema",
                    },
                }
            ],
        },
    ],
)
def test_invalid_tool_declarations_are_rejected(options):
    with pytest.raises(ValidationError):
        TaskExecutionOptions.model_validate(options)
