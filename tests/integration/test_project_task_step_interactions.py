"""Native waits validate step ownership without changing approved business facts."""

from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory
from pydantic import BaseModel, ConfigDict

from octop.i18n import tr
from octop.infra.projects import runtime
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_client_tools import CLIENT, DEFINITION, claim, result
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected


@pytest.fixture
def step_harness(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider

    def install(
        kind,
        *,
        invalid=False,
        mixed=False,
        business_field=False,
        invalid_brief=None,
        business_brief=False,
    ):
        observed, effects, arguments = [], [], []

        async def guarded(value: str) -> str:
            effects.append(value)
            return "executed:" + value

        class GuardedArgs(BaseModel):
            model_config = ConfigDict(extra="forbid")
            value: str

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                observed.append(messages)
                last = messages[-1]
                if isinstance(last, HumanMessage):
                    calls = [
                        {
                            "id": "create-plan",
                            "name": "planning",
                            "args": {
                                "command": "create",
                                "title": "Inspect the requested note",
                                "steps": ["Inspect the requested note"],
                            },
                        }
                    ]
                elif isinstance(last, ToolMessage) and last.tool_call_id == "create-plan":
                    calls = [
                        {
                            "id": "opening-notice",
                            "name": "message_notify_user",
                            "args": {
                                "text": "I will inspect the note and verify its actual result."
                            },
                        }
                    ]
                elif isinstance(last, ToolMessage) and last.tool_call_id == "opening-notice":
                    plan = next(
                        json.loads(message.content)
                        for message in reversed(messages)
                        if isinstance(message, ToolMessage)
                        and message.name == "planning"
                        and message.status != "error"
                    )
                    step = "missing-step" if invalid else plan["steps"][0]["step_id"]
                    if kind == "client":
                        name, args = "client__read_note", {"path": "note.md"}
                    elif kind == "question":
                        name, args = (
                            "ask_user_question",
                            {"questions": [{"question": "Which format?"}]},
                        )
                    else:
                        name, args = "guarded_action", {"value": "proof"}
                    args["task_step_id"] = 42 if business_field else step
                    if kind != "question":
                        args["brief"] = "Read the note" if kind == "client" else "Record the fact"
                    if invalid_brief is not None:
                        args["brief"] = invalid_brief
                    arguments.append(deepcopy(args))
                    calls = [{"id": "step-action", "name": name, "args": args}]
                    if mixed:
                        calls.insert(
                            0, {"id": "mixed-plan", "name": "planning", "args": {"command": "get"}}
                        )
                elif isinstance(last, ToolMessage) and last.tool_call_id != "complete-step":
                    plan = next(
                        json.loads(message.content)
                        for message in reversed(messages)
                        if isinstance(message, ToolMessage)
                        and message.name == "planning"
                        and message.status != "error"
                    )
                    action = next(
                        message
                        for message in reversed(messages)
                        if isinstance(message, ToolMessage)
                        and message.tool_call_id == "step-action"
                    )
                    calls = [
                        {
                            "id": "complete-step",
                            "name": "planning",
                            "args": {
                                "command": "mark_step",
                                "expected_revision": plan["revision"],
                                "step_id": plan["active_step_id"],
                                "status": "skipped" if action.status == "error" else "completed",
                                "result": (
                                    "Action was not executed: "
                                    if action.status == "error"
                                    else "Action returned: "
                                )
                                + str(action.content),
                            },
                        }
                    ]
                else:
                    return ChatResult(
                        generations=[ChatGeneration(message=AIMessage(content="Finished."))]
                    )
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))]
                )

        model = Model(responses=[])

        class Factory(ChatModelFactory):
            def get_chat_model(self, ref):
                return model

        def create(cfg, **kwargs):
            cfg.tools = [
                *(cfg.tools or []),
                StructuredTool.from_function(
                    name="guarded_action",
                    description="A guarded business action.",
                    args_schema=GuardedArgs,
                    coroutine=guarded,
                ),
            ]
            cfg.interrupt_on = {
                **(cfg.interrupt_on or {}),
                "guarded_action": {"allowed_decisions": ["approve", "reject"]},
            }
            return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

        monkeypatch.setattr(runtime, "HarnessAgent", create)
        definition = deepcopy(DEFINITION)
        if business_field:
            definition["input_schema"]["properties"]["task_step_id"] = {"type": "integer"}
            definition["input_schema"]["required"].append("task_step_id")
        if business_brief:
            definition["input_schema"]["properties"]["brief"] = {"type": "integer"}
            definition["input_schema"]["required"].append("brief")
        options = {"client_id": CLIENT, "client_tools": [definition]} if kind == "client" else {}
        return observed, effects, arguments, options

    return client, server, auth, install


async def start(env, options):
    client, server, auth, _ = env
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "Inspect my note", "options": options},
    )
    assert created.status_code == 202, created.text
    task = created.json()
    turn = await parked(server, task["active_turn_id"])
    detail = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
    return detail, turn


@pytest.mark.parametrize("business_field", [False, True])
async def test_client_dispatch_strips_only_host_metadata_and_resumes_real_graph(
    step_harness, business_field
):
    client, server, auth, install = step_harness
    observed, _, raw, options = install("client", business_field=business_field)
    task, turn = await start(step_harness, options)
    assert task["status"] == "waiting_client_tool"
    with server.services.db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM project_task_tool_calls WHERE turn_id=?", (turn.turn_id,)
        ).fetchone()
    assert row is not None
    expected = {"path": "note.md", **({"task_step_id": 42} if business_field else {})}
    assert json.loads(row["arguments_json"]) == expected
    assert raw[0]["task_step_id"] == (
        42 if business_field else task["turns"][0]["plan"]["active_step_id"]
    )
    session, ws = await connected(client, auth)
    try:
        await command(ws, "client.attach", "step-attach", client_id=CLIENT)
        call = {"task_id": task["task_id"], "turn_id": turn.turn_id, "call_id": row["call_id"]}
        token = (await claim(ws, call))["data"]["claim_token"]
        assert (await result(ws, call, token, {"proof": "from-client"}))["type"] == "result"
        done = await settled(client, auth, task["task_id"])
    finally:
        await session.__aexit__(None, None, None)
    assert done["status"] == "completed", done
    message = next(
        m for m in observed[-1] if isinstance(m, ToolMessage) and m.tool_call_id == "step-action"
    )
    assert json.loads(message.content)["output"] == {"proof": "from-client"}


async def test_question_metadata_is_not_part_of_question_payload(step_harness):
    client, _, auth, install = step_harness
    observed, _, raw, options = install("question")
    task, _ = await start(step_harness, options)
    assert task["status"] == "waiting_answer", task
    question = task["turns"][0]["interactions"][0]
    assert question["payload"]["question"] == "Which format?"
    assert "task_step_id" not in question["payload"]
    assert "task_step_id" in raw[0]
    response = await respond(
        (client, None, auth),
        task,
        question,
        {
            "request_id": str(uuid4()),
            "expected_revision": question["revision"],
            "response": {"kind": "question", "action": "answer", "answer": "Markdown"},
        },
    )
    assert response.status_code == 200, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    message = next(
        m for m in observed[-1] if isinstance(m, ToolMessage) and m.tool_call_id == "step-action"
    )
    assert json.loads(message.content)["answers"][0]["result"]["answer"] == "Markdown"


async def test_server_approval_preserves_original_step_metadata_but_tool_gets_business_args(
    step_harness,
):
    client, _, auth, install = step_harness
    _, effects, raw, options = install("server")
    task, _ = await start(step_harness, options)
    assert task["status"] == "waiting_approval", task
    item = task["turns"][0]["interactions"][0]
    assert item["payload"]["arguments"] == raw[0]
    assert effects == []
    response = await respond((client, None, auth), task, item, approval(item))
    assert response.status_code == 200, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    assert effects == ["proof"]
    assert done["turns"][0]["interactions"][0]["payload"]["arguments"] == raw[0]


@pytest.mark.parametrize("kind", ["client", "question", "server"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_invalid_ownership_and_mixed_planning_never_dispatch_native_wait_actions(
    step_harness, kind, mixed
):
    client, server, auth, install = step_harness
    observed, effects, _, options = install(kind, invalid=not mixed, mixed=mixed)
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "Reject unsafe wait admission",
            "options": options,
        },
    )
    assert created.status_code == 202, created.text
    done = await settled(client, auth, created.json()["task_id"])
    assert done["status"] == "completed", done
    turn = done["turns"][0]
    assert turn["interactions"] == [] and effects == []
    with server.services.db.connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM project_task_tool_calls WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()[0]
    assert count == 0
    message = next(
        m for m in observed[-1] if isinstance(m, ToolMessage) and m.tool_call_id == "step-action"
    )
    expected = tr("projects.planning_mixed_batch" if mixed else "projects.task_step_invalid", "zh")
    assert message.content == expected
    assert message.status == "error"
    assert "User rejected" not in message.content
    block = next(
        block
        for block in turn["content_blocks"]
        if block["type"] == "tool" and block["tool_call_id"] == "step-action"
    )
    assert block["status"] == "failed"
    assert block["started_at"] is None
    assert block["output"] == expected
    assert turn["plan"]["revision"] == 2
    assert turn["plan"]["steps"][0]["status"] == "skipped"
    assert "Action was not executed:" in turn["plan"]["steps"][0]["result"]


@pytest.mark.parametrize("kind", ["client", "question", "server"])
@pytest.mark.parametrize("brief", [42, "   ", "x" * 241])
async def test_invalid_display_brief_does_not_block_native_wait_actions(step_harness, kind, brief):
    client, server, auth, install = step_harness
    observed, effects, raw, options = install(kind, invalid_brief=brief)
    task, turn = await start(step_harness, options)
    assert raw[0]["brief"] == brief and effects == []
    assert (
        task["status"]
        == {
            "client": "waiting_client_tool",
            "question": "waiting_answer",
            "server": "waiting_approval",
        }[kind]
    )
    original = deepcopy(raw[0])
    business = {
        key: value for key, value in original.items() if key not in {"task_step_id", "brief"}
    }
    session = None
    try:
        if kind == "client":
            with server.services.db.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM project_task_tool_calls WHERE turn_id=?", (turn.turn_id,)
                ).fetchall()
            assert len(rows) == 1 and json.loads(rows[0]["arguments_json"]) == business
            session, ws = await connected(client, auth)
            await command(ws, "client.attach", "display-brief-attach", client_id=CLIENT)
            call = {
                "task_id": task["task_id"],
                "turn_id": turn.turn_id,
                "call_id": rows[0]["call_id"],
            }
            token = (await claim(ws, call))["data"]["claim_token"]
            receipt = await result(ws, call, token, {"proof": "from-client"})
            assert receipt["type"] == "result"
        else:
            item = task["turns"][0]["interactions"][0]
            if kind == "server":
                assert item["payload"]["arguments"] == original
                decision = approval(item)
            else:
                assert item["payload"]["question"] == "Which format?"
                assert not {"task_step_id", "brief"} & item["payload"].keys()
                decision = {
                    "request_id": str(uuid4()),
                    "expected_revision": item["revision"],
                    "response": {"kind": "question", "action": "answer", "answer": "Markdown"},
                }
            response = await respond((client, None, auth), task, item, decision)
            assert response.status_code == 200, response.text
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "completed", done
        final = done["turns"][0]
        assert effects == (["proof"] if kind == "server" else [])
        messages = [
            message
            for message in observed[-1]
            if isinstance(message, ToolMessage) and message.tool_call_id == "step-action"
        ]
        assert len(messages) == 1 and messages[0].status == "success"
        block = next(b for b in final["content_blocks"] if b["tool_call_id"] == "step-action")
        assert block["status"] == "completed" and block["arguments"] == business
        assert raw[0] == original
        assert final["plan"]["steps"][0]["status"] == "completed"
        assert "Action returned:" in final["plan"]["steps"][0]["result"]
        if kind == "client":
            assert json.loads(messages[0].content)["output"] == {"proof": "from-client"}
            assert await result(ws, call, token, {"proof": "from-client"}) == receipt
            with server.services.db.connect() as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM project_task_tool_calls WHERE turn_id=?",
                        (turn.turn_id,),
                    ).fetchone()[0]
                    == 1
                )
        else:
            assert (await respond((client, None, auth), task, item, decision)).status_code == 200
            if kind == "server":
                assert effects == ["proof"]
                assert final["interactions"][0]["payload"]["arguments"] == original
            else:
                assert (
                    json.loads(messages[0].content)["answers"][0]["result"]["answer"] == "Markdown"
                )
        assert (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()[
            "turns"
        ][0] == final
    finally:
        if session is not None:
            await session.__aexit__(None, None, None)


async def test_client_business_brief_remains_a_required_typed_argument(step_harness):
    client, server, auth, install = step_harness
    observed, _, raw, options = install("client", business_brief=True, invalid_brief=42)
    task, turn = await start(step_harness, options)
    assert task["status"] == "waiting_client_tool"
    with server.services.db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM project_task_tool_calls WHERE turn_id=?", (turn.turn_id,)
        ).fetchone()
    assert json.loads(row["arguments_json"]) == {"path": "note.md", "brief": 42}
    assert options["client_tools"][0]["input_schema"]["properties"]["brief"] == {"type": "integer"}
    assert "brief" in options["client_tools"][0]["input_schema"]["required"]
    assert raw[0]["brief"] == 42
    session, ws = await connected(client, auth)
    try:
        await command(ws, "client.attach", "business-brief-attach", client_id=CLIENT)
        call = {"task_id": task["task_id"], "turn_id": turn.turn_id, "call_id": row["call_id"]}
        token = (await claim(ws, call))["data"]["claim_token"]
        receipt = await result(ws, call, token, {"proof": "business-field-retained"})
        assert receipt["type"] == "result"
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "completed", done
        block = next(
            b for b in done["turns"][0]["content_blocks"] if b["tool_call_id"] == "step-action"
        )
        assert block["arguments"] == {"path": "note.md", "brief": 42}
        message = next(
            m
            for m in observed[-1]
            if isinstance(m, ToolMessage) and m.tool_call_id == "step-action"
        )
        assert message.status == "success"
        assert await result(ws, call, token, {"proof": "business-field-retained"}) == receipt
    finally:
        await session.__aexit__(None, None, None)


async def test_invalid_business_brief_still_fails_its_original_schema(step_harness):
    client, server, auth, install = step_harness
    _, effects, _, options = install("client", business_brief=True, invalid_brief="invalid integer")
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "Validate the original business brief",
            "options": options,
        },
    )
    assert created.status_code == 202, created.text
    done = await settled(client, auth, created.json()["task_id"])
    assert done["status"] == "failed", done
    final = done["turns"][0]
    assert final["error_code"] == "TASK_EXECUTION_FAILED"
    assert final["recovery"]["cause_type"] == "ValidationError"
    assert effects == [] and final["interactions"] == []
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM project_task_tool_calls WHERE turn_id=?", (final["turn_id"],)
            ).fetchone()[0]
            == 0
        )
    assert final["plan"]["steps"][0]["status"] == "in_progress"
