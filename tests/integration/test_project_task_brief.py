"""Require public action briefs before real work, approval, and client handoff."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config

from octop.i18n import tr
from octop.infra.projects import runtime
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_client_tools import CLIENT, OPTIONS, claim, result
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_task_opening import (
    assert_not_dispatched,
    notification,
    tool_block,
)
from tests.integration.test_project_task_opening import (
    opening_harness as opening_harness,
)
from tests.integration.test_project_task_planning import tool_call
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected


@pytest.mark.parametrize("kind", ["server", "approval", "client"])
@pytest.mark.parametrize("invalid", ["missing", "empty"])
async def test_brief_rejection_precedes_side_effects_and_corrected_work_runs_once(
    opening_harness, kind, invalid
):
    client, server, auth, install = opening_harness
    prose, correction, answer = (
        "我先尝试读取真实结果。",
        "补齐动作说明后重新执行。",
        "真实结果已核对。",
    )
    brief = "读取笔记并核对真实返回" if kind == "client" else "记录并核对请求中的事实"
    name = "client__read_note" if kind == "client" else "record_fact"
    business = {"path": "note.md"} if kind == "client" else {"value": "proof"}
    checked = []

    def work(call_id, text, **metadata):
        message = tool_call(name, call_id, **business, **metadata)
        message.content = text
        return message

    def corrected(messages):
        rejected = next(
            message
            for message in messages
            if isinstance(message, ToolMessage) and message.tool_call_id == "invalid-brief"
        )
        assert rejected.status == "error"
        assert rejected.content == tr("projects.task_brief_invalid", "zh")
        assert "User rejected" not in rejected.content
        assert effects == []
        turn_id = get_config()["configurable"]["thread_id"]
        saved = server.services.project_task_turn_repo.get(turn_id)
        assert json.loads(saved.interactions_json) == []
        with server.services.db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM project_task_tool_calls WHERE turn_id=?", (turn_id,)
                ).fetchone()[0]
                == 0
            )
        checked.append(True)
        return work("corrected-work", correction, brief=brief)

    effects, observed, agents = install(
        [
            notification(),
            work("invalid-brief", prose, **({"brief": "   "} if invalid == "empty" else {})),
            corrected,
            AIMessage(content=answer),
        ],
        approval_required=kind == "approval",
    )
    options = {**OPTIONS} if kind == "client" else {}
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "核对真实工具返回，并报告结果。",
            "options": {**options, "action_timeline_enabled": True},
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    session = None
    receipt = None
    agents_before_restart = len(agents)
    try:
        if kind != "server":
            await parked(server, created.json()["active_turn_id"])
            task = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
            turn = task["turns"][0]
            assert checked == [True] and effects == []
            assert_not_dispatched(tool_block(turn, "invalid-brief"))
            assert turn["reply"] == ""
            assert tool_block(turn, "corrected-work")["brief"] == brief
            agents_before_restart = len(agents)
            await server.project_task_worker.close()
            if kind == "approval":
                assert task["status"] == "waiting_approval"
                assert len(turn["interactions"]) == 1
                item = turn["interactions"][0]
                assert item["payload"]["arguments"] == {**business, "brief": brief}
                decision = approval(item)
                accepted = await respond(opening_harness, task, item, decision)
                assert accepted.status_code == 200, accepted.text
            else:
                assert task["status"] == "waiting_client_tool" and turn["interactions"] == []
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
        assert checked == [True]
        turn = done["turns"][0]
        assert effects == ([] if kind == "client" else ["proof"])
        assert len(agents) == (1 if kind == "server" else agents_before_restart + 1)
        assert turn["reply"] == answer
        assert turn["reasoning"] == prose + correction
        assert [b["text"] for b in turn["content_blocks"] if b["type"] == "text"] == [answer]
        assert_not_dispatched(tool_block(turn, "invalid-brief"))
        block = tool_block(turn, "corrected-work")
        assert block["status"] == "completed" and block["brief"] == brief
        assert block["arguments"] == business
        tools = [message for message in observed[-1][1] if isinstance(message, ToolMessage)]
        assert [message.tool_call_id for message in tools] == [
            "opening",
            "invalid-brief",
            "corrected-work",
        ]
        rejected = tools[1]
        assert rejected.status == "error"
        assert rejected.content == tr("projects.task_brief_invalid", "zh")
        assert "User rejected" not in rejected.content
        assert tool_block(turn, "invalid-brief")["output"] == rejected.content
        if kind == "approval":
            assert (await respond(opening_harness, task, item, decision)).status_code == 200
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
        assert stored.reply == answer
        assert json.loads(stored.content_blocks_json) == turn["content_blocks"]
    finally:
        if session is not None:
            await session.__aexit__(None, None, None)


async def test_disabled_actions_allow_work_without_host_brief_or_opening(opening_harness):
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


@pytest.mark.parametrize("legacy_checkpoint", [False, True], ids=["user-deny", "cold-admission"])
async def test_user_denial_and_cold_admission_failure_keep_distinct_results(
    opening_harness, monkeypatch, legacy_checkpoint
):
    client, server, auth, install = opening_harness
    original = runtime.TaskExecution
    call_id = "legacy-unbriefed-work" if legacy_checkpoint else "user-denied-work"
    metadata = {} if legacy_checkpoint else {"brief": "记录并核对请求中的事实"}
    if legacy_checkpoint:

        class LegacyExecution(original):
            def brief_for_call(self, call, tool=None):
                # Model an approval checkpoint written before briefs were mandatory.
                if call["id"] == call_id and "brief" not in call["args"]:
                    return None
                return super().brief_for_call(call, tool)

        monkeypatch.setattr(runtime, "TaskExecution", LegacyExecution)
    effects, observed, agents = install(
        [
            notification(),
            tool_call("record_fact", call_id, value="proof", **metadata),
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
    turn = waiting["turns"][0]
    assert waiting["status"] == "waiting_approval" and effects == []
    assert len(turn["interactions"]) == 1
    item = turn["interactions"][0]
    assert item["tool_call_id"] == call_id and item["dispatch_started"] is False
    assert tool_block(turn, call_id)["started_at"] is None
    await server.project_task_worker.close()
    monkeypatch.setattr(runtime, "TaskExecution", original)
    decision = approval(item, "allow_once" if legacy_checkpoint else "deny")
    accepted = await respond(opening_harness, waiting, item, decision)
    assert accepted.status_code == 200, accepted.text
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    done = await settled(client, auth, task_id)
    assert done["status"] == "completed", done
    assert effects == [] and len(agents) == 2
    result_message = next(
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == call_id
    )
    assert result_message.status == "error"
    if legacy_checkpoint:
        assert result_message.content == tr("projects.task_brief_invalid", "zh")
        assert "User rejected" not in result_message.content
    else:
        assert "User rejected the tool call" in result_message.content
        assert tr("projects.task_brief_invalid", "zh") not in result_message.content
    final = done["turns"][0]
    assert final["reply"] == "该动作未执行。"
    assert_not_dispatched(tool_block(final, call_id))
    assert tool_block(final, call_id)["output"] == result_message.content
    resolved = final["interactions"][0]
    assert resolved["resolution"] == {
        "outcome": "allow_once" if legacy_checkpoint else "deny",
        "source": "user",
        "answer": None,
    }
    assert resolved["dispatch_started"] is False
    assert (await respond(opening_harness, waiting, item, decision)).status_code == 200
    reopened = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
    assert reopened["turns"][0] == final and effects == []


@pytest.mark.parametrize(
    ("failure", "error_key"),
    [
        ("opening", "progress_opening_required"),
        ("step", "task_step_invalid"),
        ("mixed", "planning_mixed_batch"),
    ],
)
async def test_other_system_admission_errors_are_not_attributed_to_user_denial(
    opening_harness, failure, error_key
):
    client, _, auth, install = opening_harness
    work = tool_call(
        "record_fact",
        "system-rejected-work",
        value="proof",
        brief="记录并核对请求中的事实",
        **({"task_step_id": "unknown-step"} if failure == "step" else {}),
    )
    if failure == "mixed":
        work.tool_calls.insert(
            0, {"id": "mixed-plan", "name": "planning", "args": {"command": "get"}}
        )
    effects, observed, _ = install(
        [
            *([] if failure == "opening" else [notification()]),
            work,
            AIMessage(content="系统校验未通过，动作未执行。"),
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
    done = await settled(client, auth, created.json()["task_id"])
    assert done["status"] == "completed", done
    final = done["turns"][0]
    assert effects == [] and final["interactions"] == []
    messages = [
        message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "system-rejected-work"
    ]
    assert len(messages) == 1
    assert messages[0].status == "error"
    assert messages[0].content == tr("projects." + error_key, "zh")
    assert "User rejected" not in messages[0].content
    block = tool_block(final, "system-rejected-work")
    assert_not_dispatched(block)
    assert block["output"] == messages[0].content
