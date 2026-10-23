"""Real Harness checkpoints survive task-worker replacement without replaying tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.config import get_config
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory
from octop_harness.llm.session_header import current_session_id

from octop.infra.errors import OctopError
from octop.infra.projects import runtime
from octop.infra.projects.checkpoints import resume_decisions
from tests.integration.test_project_task_client_tools import (
    claim,
    result,
    start_call,
)
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until


@pytest.fixture
async def native_wait_env(env_with_provider, monkeypatch):
    from octop.api.routers import project_tasks_ws

    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    generations = []
    agents = []

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            configurable = get_config()["configurable"]
            assert configurable["source"] == "project-task"
            assert current_session_id() == configurable["thread_id"]
            if not isinstance(messages[-1], HumanMessage):
                assert configurable["mcp_servers"] == ["client"]
            generations.append(messages)
            if isinstance(messages[-1], HumanMessage):
                if messages[-1].content == "ordinary":
                    message = AIMessage(content="ordinary complete")
                else:
                    message = AIMessage(
                        content="Before waiting. ",
                        tool_calls=[
                            {
                                "id": f"native-{i}",
                                "name": "client__read_note",
                                "args": {"path": "note.md" if i == 0 else f"note-{i}.md"},
                            }
                            for i in range(2)
                        ],
                    )
            else:
                assert isinstance(messages[-1], ToolMessage)
                results = [m for m in messages if isinstance(m, ToolMessage)]
                if len(results) == 2 and any(
                    json.loads(m.content)["output"].get("again") for m in results
                ):
                    message = AIMessage(
                        content="Between waits. ",
                        tool_calls=[
                            {
                                "id": "native-2",
                                "name": "client__read_note",
                                "args": {"path": "next.md"},
                            }
                        ],
                    )
                else:
                    message = AIMessage(content="After waiting.")
            message.usage_metadata = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
            return ChatResult(generations=[ChatGeneration(message=message)])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return Model(responses=[])

    def agent(cfg, **kwargs):
        instance = HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)
        agents.append(instance)
        return instance

    monkeypatch.setattr(runtime, "HarnessAgent", agent)
    yield *env_with_provider, generations, agents


async def parked(server, turn_id):
    for _ in range(300):
        turn = await asyncio.to_thread(server.services.project_task_turn_repo.get, turn_id)
        if turn.is_waiting:
            return turn
        await asyncio.sleep(0.01)
    pytest.fail("native wait was not persisted")


async def start_native(client, server, auth, *, web_search_enabled=True):
    session, ws = await connected(client, auth)
    task, first = await start_call(ws, web_search_enabled=web_search_enabled)
    second = (
        await until(
            ws,
            lambda frame: (
                frame["type"] == "tool.call" and frame["data"]["call_id"] != first["call_id"]
            ),
        )
    )["data"]
    turn = await parked(server, first["turn_id"])
    return session, ws, task, [first, second], turn


async def test_wait_releases_execution_slot_and_resumes_after_worker_restart(native_wait_env):
    client, server, auth, generations, agents = native_wait_env
    session, ws, task, calls, turn = await start_native(
        client, server, auth, web_search_enabled=False
    )
    try:
        assert not turn.worker_id and turn.checkpoint_id
        assert len(agents) == 1
        assert json.loads(turn.pause_json)["calls"] == [call["call_id"] for call in calls]
        current = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
        changed = await client.patch(
            f"/api/project-tasks/{task['task_id']}",
            headers=auth,
            json={
                "expected_revision": current["list_revision"],
                "web_search_enabled": True,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["web_search_enabled"] is True
        assert changed.json()["turns"][0]["web_search_enabled"] is False
        # An old wait is not a crashed worker and keeps the original deadline.
        with server.services.db.transaction() as conn:
            conn.execute(
                "UPDATE project_task_turns SET updated_at=0 WHERE turn_id=?", (turn.turn_id,)
            )
        ordinary = (
            await client.post(
                "/api/project-tasks",
                headers=auth,
                json={"request_id": "ordinary-request", "prompt": "ordinary"},
            )
        ).json()
        assert (await settled(client, auth, ordinary["task_id"]))["status"] == "completed"
        assert (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()[
            "status"
        ] == "waiting_client_tool"
        assert len(agents) == 2
        await server.project_task_worker.close()
        for i, call in enumerate(calls):
            token = (await claim(ws, call, f"claim-native-{i}"))["data"]["claim_token"]
            assert (await result(ws, call, token, {"proof": i}))["type"] == "result"
        server.project_task_worker = runtime.ProjectTaskWorker(server.services)
        await server.project_task_worker.start()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed", final
        assert len(final["turns"]) == 1 and final["turns"][0]["turn_id"] == turn.turn_id
        assert final["turns"][0]["reply"] == "Before waiting. After waiting."
        assert final["turns"][0]["reasoning"] == ""
        assert final["turns"][0]["input_tokens"] == 14
        assert final["turns"][0]["output_tokens"] == 6
        assert len(agents) == 3
        assert final["web_search_enabled"] is True
        assert final["turns"][0]["web_search_enabled"] is False
        assert agents[-1].config.web_search_tools is False
        assert (
            len(
                [
                    messages
                    for messages in generations
                    if isinstance(messages[-1], HumanMessage) and messages[-1].content != "ordinary"
                ]
            )
            == 1
        )
        tool_results = [message for message in generations[-1] if isinstance(message, ToolMessage)]
        assert {message.tool_call_id for message in tool_results} == {"native-0", "native-1"}
        assert [json.loads(message.content)["output"]["proof"] for message in tool_results] == [
            0,
            1,
        ]
        assert all(block["status"] == "completed" for block in final["turns"][0]["content_blocks"])
        with server.services.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_task_tool_calls WHERE turn_id=?", (turn.turn_id,)
            ).fetchall()
        assert len(rows) == 2 and all(row["status"] == "completed" for row in rows)
        assert (await result(ws, calls[-1], token, {"proof": 1}))["type"] == "result"
        assert len(generations) == 3
    finally:
        await session.__aexit__(None, None, None)


async def test_stop_parked_turn_closes_calls_and_rejects_late_result(native_wait_env):
    client, server, auth, generations, _ = native_wait_env
    session, ws, task, calls, turn = await start_native(client, server, auth)
    try:
        token = (await claim(ws, calls[0]))["data"]["claim_token"]
        stopped = await command(
            ws,
            "task.cancel",
            "cancel-native-001",
            task_id=task["task_id"],
            data={"turn_id": turn.turn_id},
        )
        assert stopped["data"]["active_turn_id"] is None
        assert stopped["data"]["status"] == "cancelled"
        late = await result(ws, calls[0], token, {"proof": "late"})
        assert late["error"]["code"] == "TASK_CLIENT_TOOL_CLOSED"
        assert len(generations) == 1
        final = await settled(client, auth, task["task_id"])
        assert final["turns"][0]["reply"] == "Before waiting. "
        assert final["turns"][0]["reasoning"] == ""
    finally:
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("damage", ["checkpoint", "journal"])
async def test_missing_checkpoint_fails_without_resending_prompt(native_wait_env, damage):
    client, server, auth, generations, _ = native_wait_env
    session, ws, task, calls, turn = await start_native(client, server, auth)
    try:
        await server.project_task_worker.close()
        wait = json.loads(turn.pause_json)
        if damage == "checkpoint":
            wait["checkpoint_id"] = "missing-checkpoint"
        with server.services.db.transaction() as conn:
            conn.execute(
                "UPDATE project_task_turns SET checkpoint_id='missing-checkpoint',pause_json=? WHERE turn_id=?",
                (json.dumps(wait), turn.turn_id),
            )
        for i, call in enumerate(calls):
            token = (await claim(ws, call, f"claim-native-{i}"))["data"]["claim_token"]
            assert (await result(ws, call, token, {}))["type"] == "result"
        server.project_task_worker = runtime.ProjectTaskWorker(server.services)
        await server.project_task_worker.start()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "failed"
        assert final["turns"][0]["error_code"] == "TASK_INTERRUPTED"
        assert len(generations) == 1
    finally:
        await session.__aexit__(None, None, None)


async def test_same_turn_can_wait_again_without_duplicating_prior_results(native_wait_env):
    client, server, auth, generations, _ = native_wait_env
    session, ws, task, calls, turn = await start_native(client, server, auth)
    try:
        for i, call in enumerate(calls):
            token = (await claim(ws, call, f"claim-native-{i}"))["data"]["claim_token"]
            await result(ws, call, token, {"again": True})
        third = (
            await until(
                ws,
                lambda f: (
                    f["type"] == "tool.call"
                    and f["data"]["call_id"] not in {c["call_id"] for c in calls}
                ),
            )
        )["data"]
        second_wait = await parked(server, turn.turn_id)
        assert second_wait.checkpoint_id != turn.checkpoint_id
        token = (await claim(ws, third, "claim-native-third"))["data"]["claim_token"]
        await result(ws, third, token, {})
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed", final
        assert len(final["turns"]) == 1
        assert final["turns"][0]["reply"] == "Before waiting. Between waits. After waiting."
        assert final["turns"][0]["reasoning"] == ""
        assert final["turns"][0]["input_tokens"] == 21
        assert len(generations) == 3
        results = [m for m in generations[-1] if isinstance(m, ToolMessage)]
        assert [m.tool_call_id for m in results] == ["native-0", "native-1", "native-2"]
    finally:
        await session.__aexit__(None, None, None)


async def test_stale_worker_cannot_consume_wait_after_lease_changes(native_wait_env):
    client, server, auth, generations, _ = native_wait_env
    session, ws, _, calls, turn = await start_native(client, server, auth)
    try:
        await server.project_task_worker.close()
        for i, call in enumerate(calls):
            token = (await claim(ws, call, f"claim-fenced-{i}"))["data"]["claim_token"]
            await result(ws, call, token, {})
        with server.services.db.transaction() as conn:
            conn.execute(
                "UPDATE project_task_turns SET worker_id='replacement-worker' WHERE turn_id=?",
                (turn.turn_id,),
            )
        owner = server.services.project_task_repo.runtime_owner(turn.task_id)
        with pytest.raises(OctopError) as error:
            await asyncio.to_thread(resume_decisions, server.services, owner, turn)
        assert str(error.value.code) == "TASK_INTERRUPTED"
        assert len(generations) == 1
    finally:
        await session.__aexit__(None, None, None)
