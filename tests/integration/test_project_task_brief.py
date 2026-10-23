"""Optional action descriptions never gate work, approval, or client handoff."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from octop.i18n import tr
from octop.infra.projects import runtime
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_client_tools import CLIENT, OPTIONS, claim, result
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_task_opening import assert_not_dispatched, tool_block
from tests.integration.test_project_task_opening import opening_harness as opening_harness
from tests.integration.test_project_task_planning import tool_call
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected


@pytest.mark.parametrize("kind", ["server", "approval", "client"])
@pytest.mark.parametrize(
    "metadata", [{}, {"brief": "   "}, {"brief": 42}, {"brief": "记录真实事实"}]
)
async def test_display_brief_does_not_gate_work_or_cold_handoff(opening_harness, kind, metadata):
    client, server, auth, install = opening_harness
    name = "client__read_note" if kind == "client" else "record_fact"
    business = {"path": "note.md"} if kind == "client" else {"value": "proof"}
    message = tool_call(name, "work", **business, **metadata)
    message.content = "先核对真实返回。"
    effects, observed, agents = install(
        [message, AIMessage(content="真实结果已核对。")], approval_required=kind == "approval"
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "核对真实工具返回，并报告结果。",
            "options": {**(OPTIONS if kind == "client" else {}), "action_timeline_enabled": True},
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    session = None
    try:
        if kind != "server":
            await parked(server, created.json()["active_turn_id"])
            waiting = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
            turn = waiting["turns"][0]
            assert effects == [] and turn["reply"] == "先核对真实返回。"
            assert turn["reasoning"] == ""
            before = next(block for block in turn["content_blocks"] if block["type"] == "text")
            assert before["text"] == "先核对真实返回。"
            assert tool_block(turn, "work")["started_at"] is None
            await server.project_task_worker.close()
            if kind == "approval":
                assert waiting["status"] == "waiting_approval"
                assert len(turn["interactions"]) == 1
                item = turn["interactions"][0]
                # Approval remains bound to the exact original model arguments.
                assert item["payload"]["arguments"] == {**business, **metadata}
                decision = approval(item)
                accepted = await respond(opening_harness, waiting, item, decision)
                assert accepted.status_code == 200, accepted.text
            else:
                assert waiting["status"] == "waiting_client_tool" and turn["interactions"] == []
                with server.services.db.connect() as conn:
                    calls = conn.execute(
                        "SELECT * FROM project_task_tool_calls WHERE turn_id=?", (turn["turn_id"],)
                    ).fetchall()
                assert len(calls) == 1 and json.loads(calls[0]["arguments_json"]) == business
                call = {key: calls[0][key] for key in ("task_id", "turn_id", "call_id")}
                session, ws = await connected(client, auth)
                await command(ws, "client.attach", "brief-attach", client_id=CLIENT)
                token = (await claim(ws, call))["data"]["claim_token"]
                receipt = await result(ws, call, token, {"proof": "from-client"})
                assert receipt["type"] == "result"
            server.project_task_worker = runtime.ProjectTaskWorker(server.services)
            await server.project_task_worker.start()
        done = await settled(client, auth, task_id)
        assert done["status"] == "completed", done
        turn = done["turns"][0]
        assert effects == ([] if kind == "client" else ["proof"])
        assert len(agents) == (1 if kind == "server" else 2)
        assert turn["reply"] == "先核对真实返回。真实结果已核对。"
        assert turn["reasoning"] == ""
        text = [block for block in turn["content_blocks"] if block["type"] == "text"]
        assert [block["text"] for block in text] == ["先核对真实返回。", "真实结果已核对。"]
        if kind != "server":
            assert text[0]["block_id"] == before["block_id"]
        block = tool_block(turn, "work")
        assert block["status"] == "completed" and block["arguments"] == business
        assert block["brief"] == (
            metadata["brief"]
            if isinstance(metadata.get("brief"), str) and metadata["brief"].strip()
            else None
        )
        messages = [message for message in observed[-1][1] if isinstance(message, ToolMessage)]
        assert [message.tool_call_id for message in messages] == ["work"]
        assert messages[0].status == "success"
        assert len(observed) == 2
        if kind == "approval":
            assert (await respond(opening_harness, waiting, item, decision)).status_code == 200
            assert effects == ["proof"]
        elif kind == "client":
            assert await result(ws, call, token, {"proof": "from-client"}) == receipt
            with server.services.db.connect() as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM project_task_tool_calls WHERE turn_id=?",
                        (turn["turn_id"],),
                    ).fetchone()[0]
                    == 1
                )
        stored = server.services.project_task_turn_repo.get(turn["turn_id"])
        assert stored.reply == turn["reply"]
        assert json.loads(stored.content_blocks_json) == turn["content_blocks"]
    finally:
        if session is not None:
            await session.__aexit__(None, None, None)


async def test_disabled_actions_keep_actual_work_without_display_metadata(opening_harness):
    client, _, auth, install = opening_harness
    effects, _, _ = install(
        [tool_call("record_fact", "classic-work", value="proof"), AIMessage(content="已记录。")]
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "记录事实。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    done = await settled(client, auth, created.json()["task_id"])
    assert done["status"] == "completed" and effects == ["proof"]
    block = tool_block(done["turns"][0], "classic-work")
    assert block["brief"] is None and block["arguments"] == {"value": "proof"}
    assert not any(b["type"] == "progress" for b in done["turns"][0]["content_blocks"])


async def test_user_denial_without_display_brief_remains_a_real_denial(opening_harness):
    client, server, auth, install = opening_harness
    effects, observed, agents = install(
        [
            tool_call("record_fact", "denied-work", value="proof"),
            AIMessage(content="该动作未执行。"),
        ],
        approval_required=True,
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "记录并核对事实。",
            "options": {"action_timeline_enabled": True},
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    await parked(server, created.json()["active_turn_id"])
    waiting = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
    assert waiting["status"] == "waiting_approval" and effects == []
    item = waiting["turns"][0]["interactions"][0]
    await server.project_task_worker.close()
    decision = approval(item, "deny")
    assert (await respond(opening_harness, waiting, item, decision)).status_code == 200
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    done = await settled(client, auth, task_id)
    assert done["status"] == "completed", done
    assert effects == [] and len(agents) == 2
    result_message = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "denied-work"
    )
    assert (
        result_message.status == "error" and "User rejected the tool call" in result_message.content
    )
    final = done["turns"][0]
    assert_not_dispatched(tool_block(final, "denied-work"))
    assert tool_block(final, "denied-work")["output"] == result_message.content
    assert final["interactions"][0]["resolution"]["outcome"] == "deny"
    assert final["interactions"][0]["dispatch_started"] is False
    assert (await respond(opening_harness, waiting, item, decision)).status_code == 200
    assert effects == []


@pytest.mark.parametrize(
    ("failure", "error_key"), [("step", "task_step_invalid"), ("mixed", "planning_mixed_batch")]
)
async def test_plan_admission_errors_still_precede_approval_and_dispatch(
    opening_harness, failure, error_key
):
    client, _, auth, install = opening_harness
    work = tool_call(
        "record_fact",
        "system-rejected-work",
        value="proof",
        **({"task_step_id": "unknown-step"} if failure == "step" else {}),
    )
    if failure == "mixed":
        work.tool_calls.insert(
            0, {"id": "mixed-plan", "name": "planning", "args": {"command": "get"}}
        )
    effects, observed, _ = install(
        [work, AIMessage(content="系统校验未通过，动作未执行。")], approval_required=True
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "记录并核对事实。",
            "options": {"action_timeline_enabled": True},
        },
    )
    assert created.status_code == 202, created.text
    done = await settled(client, auth, created.json()["task_id"])
    assert done["status"] == "completed", done
    final = done["turns"][0]
    assert effects == [] and final["interactions"] == []
    message = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "system-rejected-work"
    )
    assert message.status == "error" and message.content == tr("projects." + error_key, "zh")
    assert "User rejected" not in message.content
    block = tool_block(final, "system-rejected-work")
    assert_not_dispatched(block)
    assert block["output"] == message.content
