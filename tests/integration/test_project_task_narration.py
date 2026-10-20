"""Independent progress generation never gates the real task or rewrites its facts."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from octop.infra.projects import runtime
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_task_planning import planning_models, step_command, tool_call
from tests.integration.test_project_tasks import settled


def hold_models(monkeypatch, responses, *indices):
    observed, _ = planning_models(monkeypatch, responses)
    gates = {index: (asyncio.Event(), asyncio.Event()) for index in indices}
    original = FakeMessagesListChatModel._agenerate

    async def generate(model, *args, **kwargs):
        if getattr(model, "role", "") == "project-task" and model.i in gates:
            entered, release = gates[model.i]
            entered.set()
            await release.wait()
        return await original(model, *args, **kwargs)

    monkeypatch.setattr(FakeMessagesListChatModel, "_agenerate", generate)
    return observed, gates


async def create(env, *, enabled=True):
    client, _, auth = env
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "核对输入并交付实际结果。",
            "options": {"action_timeline_enabled": enabled},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


async def visible(env, task_id, predicate):
    client, _, auth = env
    async with asyncio.timeout(4):
        while True:
            response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
            assert response.status_code == 200, response.text
            task = response.json()
            if predicate(task["turns"][0]):
                return task
            await asyncio.sleep(0.01)


def progress(turn):
    return [block for block in turn["content_blocks"] if block["type"] == "progress"]


def action(turn):
    return next(block for block in turn["content_blocks"] if block["type"] == "tool")


@pytest.mark.parametrize("narrator_mode", ["blocked", "failed"])
async def test_narrator_does_not_gate_tools_or_final_answer(
    env_with_provider, monkeypatch, narrator_mode
):
    client, _, auth = env_with_provider
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    late = "这条迟到旁路文字不能写入。"

    async def narrate(self, snapshot):
        entered.set()
        if narrator_mode == "failed":
            raise RuntimeError("simulated independent narrator failure")
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
        return late

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    observed, gates = hold_models(
        monkeypatch,
        [
            tool_call("create_artifact", "proof", name="proof.txt", content="actual-proof"),
            AIMessage(content="已交付实际验证结果。"),
        ],
        1,
    )
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(gates[1][0].wait(), timeout=3)
        await asyncio.wait_for(entered.wait(), timeout=3)
        live = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: any(
                block["type"] == "tool" and block["status"] == "completed"
                for block in turn["content_blocks"]
            ),
        )
        assert live["status"] == "running"
        assert action(live["turns"][0])["arguments"] == {
            "name": "proof.txt",
            "content": "actual-proof",
        }
        gates[1][1].set()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed", final
        turn = final["turns"][0]
        assert turn["reply"] == "已交付实际验证结果。"
        assert [artifact["name"] for artifact in turn["artifacts"]] == ["proof.txt"]
        assert len(observed) == 2
        assert all(late != block["text"] for block in turn["content_blocks"])
        assert len([block for block in progress(turn) if block["introduction"] is True]) == 1
        if narrator_mode == "blocked":
            assert cancelled.is_set() and not release.is_set()
        release.set()
        assert (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()[
            "turns"
        ] == final["turns"]
    finally:
        release.set()
        gates[1][1].set()


async def test_opening_narration_is_visible_while_main_model_is_still_waiting(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider
    entered, release = asyncio.Event(), asyncio.Event()
    explanation = "我正在核对输入，随后会给出可直接使用的说明。"

    async def narrate(self, snapshot):
        assert snapshot.key == "opening" and snapshot.tool_call_id is None
        assert snapshot.facts["stage"] == "received"
        entered.set()
        await release.wait()
        return explanation

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    observed, gates = hold_models(monkeypatch, [AIMessage(content="核对完成。")], 0)
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(gates[0][0].wait(), timeout=3)
        await asyncio.wait_for(entered.wait(), timeout=3)
        initial = await visible(
            env_with_provider, task["task_id"], lambda turn: bool(progress(turn))
        )
        opening = progress(initial["turns"][0])
        assert len(opening) == 1 and opening[0]["text"] and opening[0]["introduction"] is True
        release.set()
        narrated = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: any(block["text"] == explanation for block in progress(turn)),
        )
        assert narrated["status"] == "running" and narrated["turns"][0]["reply"] == ""
        assert observed == [] and not gates[0][1].is_set()
        updated = progress(narrated["turns"][0])
        assert len(updated) == 1 and updated[0]["block_id"] == opening[0]["block_id"]
        gates[0][1].set()
        assert (await settled(client, auth, task["task_id"]))["status"] == "completed"
    finally:
        release.set()
        gates[0][1].set()


async def test_plan_progress_precedes_work_and_tool_narration_updates_only_its_brief(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    plan_entered, plan_release = asyncio.Event(), asyncio.Event()
    tool_entered, tool_release = asyncio.Event(), asyncio.Event()
    plan_text, brief = "先核对当前步骤，再生成验证附件。", "生成验证附件，保留实际返回结果供核对。"

    async def narrate(self, snapshot):
        if snapshot.facts["stage"] == "plan_update":
            saved = await asyncio.to_thread(
                server.services.project_task_turn_repo.get, task["active_turn_id"]
            )
            saved_plans = [
                block["plan"]
                for block in json.loads(saved.content_blocks_json)
                if block["type"] == "plan"
            ]
            assert saved_plans[-1]["plan_id"] == snapshot.plan_id
            assert saved_plans[-1]["revision"] == snapshot.facts["revision"]
            plan_entered.set()
            await plan_release.wait()
            return plan_text
        if snapshot.tool_call_id == "proof":
            tool_entered.set()
            await tool_release.wait()
            return brief
        return None

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    observed, gates = hold_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "plan-create",
                command="create",
                title="生成验证附件",
                steps=["核对实际结果"],
            ),
            tool_call("create_artifact", "proof", name="proof.txt", content="actual-proof"),
            step_command(
                "mark_step", "step-finished", status="completed", result="已核对附件实际返回结果。"
            ),
            AIMessage(content="验证附件已交付。"),
        ],
        1,
        2,
    )
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(gates[1][0].wait(), timeout=3)
        await asyncio.wait_for(plan_entered.wait(), timeout=3)
        prepared = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: (
                turn["plan"] is not None
                and any(block["step_id"] is not None for block in progress(turn))
            ),
        )
        turn = prepared["turns"][0]
        step_id, plan_id = turn["plan"]["active_step_id"], turn["plan"]["plan_id"]
        host_plan = next(block for block in progress(turn) if block["step_id"] == step_id)
        assert host_plan["text"] and "核对实际结果" in host_plan["text"]
        assert host_plan["plan_id"] == plan_id and host_plan["introduction"] is False
        assert not any(block["type"] == "tool" for block in turn["content_blocks"])
        plan_release.set()
        narrated = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: any(block["text"] == plan_text for block in progress(turn)),
        )
        narrated_plan = next(
            block for block in progress(narrated["turns"][0]) if block["text"] == plan_text
        )
        assert narrated_plan["block_id"] == host_plan["block_id"]
        gates[1][1].set()
        await asyncio.wait_for(gates[2][0].wait(), timeout=3)
        await asyncio.wait_for(tool_entered.wait(), timeout=3)
        executed = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: any(
                block["type"] == "tool" and block["status"] == "completed"
                for block in turn["content_blocks"]
            ),
        )
        original = deepcopy(action(executed["turns"][0]))
        assert original["tool_call_id"] == "proof" and original["tool_label"]
        assert original["plan_id"] == plan_id and original["step_id"] == step_id
        assert original["arguments"] == {"name": "proof.txt", "content": "actual-proof"}
        assert original["output"] is not None and original["started_at"] and original["finished_at"]
        tool_release.set()
        updated = await visible(
            env_with_provider, task["task_id"], lambda turn: action(turn)["brief"] == brief
        )
        actual = action(updated["turns"][0])
        assert {key: value for key, value in actual.items() if key != "brief"} == {
            key: value for key, value in original.items() if key != "brief"
        }
        assert (
            len(
                [
                    block
                    for block in updated["turns"][0]["content_blocks"]
                    if block["type"] == "tool"
                ]
            )
            == 1
        )
        gates[2][1].set()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed" and final["turns"][0]["reply"] == "验证附件已交付。"
        assert len(observed) == 4
    finally:
        plan_release.set()
        tool_release.set()
        for _, release in gates.values():
            release.set()


async def test_test_file_narration_receives_completed_ui_phase_and_prior_actual_writes(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider
    snapshots, received = [], asyncio.Event()
    ui_result = "已实际写入界面文件 index.html。"
    explanation = "测试脚本已写入；界面实现步骤此前已有结果，本次写入不证明测试运行。"

    async def narrate(self, snapshot):
        if snapshot.facts["stage"] == "plan_update":
            assert snapshot.facts["context"]["plan"]["phase"] == "executing"
        if snapshot.tool_call_id == "write-tests" and snapshot.facts["stage"] == "tool_end":
            snapshots.append(snapshot)
            received.set()
            return explanation
        return None

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    _, gates = hold_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "plan",
                command="create",
                title="实现界面并编写测试",
                steps=["实现界面", "编写测试文件"],
            ),
            tool_call(
                "write_file", "write-ui", file_path="index.html", content="<main>ready</main>"
            ),
            step_command("mark_step", "ui-done", status="completed", result=ui_result),
            tool_call(
                "write_file",
                "write-tests",
                file_path="unit.test.js",
                content="// Test script only; not executed.",
            ),
            tool_call("publish_workspace_file", "publish-tests", path="unit.test.js"),
            step_command(
                "mark_step",
                "tests-written",
                index=1,
                status="completed",
                result="测试文件已写入，未执行测试。",
            ),
            AIMessage(content="界面与测试文件已保存，尚未执行测试。"),
        ],
        4,
    )
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(gates[4][0].wait(), 3)
        await asyncio.wait_for(received.wait(), 3)
        actual = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: any(
                block["brief"] == explanation
                for block in turn["content_blocks"]
                if block["type"] == "tool"
            ),
        )
        facts = snapshots[-1].facts
        assert facts["context"]["plan"]["steps"][0]["result"] == ui_result
        assert facts["context"]["plan"]["steps"][0]["status"] == "completed"
        assert facts["context"]["plan"]["steps"][1]["content"] == "编写测试文件"
        assert "test_counts" not in facts
        writes = facts["context"]["recent_actions"]
        assert [write["target"] for write in writes] == ["index.html", "unit.test.js"]
        assert all(write["status"] == "completed" and write["result"] for write in writes)
        assert (
            len(
                [block for block in actual["turns"][0]["content_blocks"] if block["type"] == "tool"]
            )
            == 2
        )
        gates[4][1].set()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed", final["turns"][0]["error_code"]
    finally:
        gates[4][1].set()


@pytest.mark.parametrize(
    "footer",
    [
        "ℹ tests 36\nℹ pass 29\nℹ fail 7",
        "# tests 36\n# pass 29\n# fail 7",
        "======= 7 failed, 29 passed in 0.12s =======",
    ],
)
async def test_full_runner_counts_are_host_rendered_before_long_output_is_excerpted(
    env_with_provider, monkeypatch, footer
):
    client, _, auth = env_with_provider
    waiting, release = asyncio.Event(), asyncio.Event()
    output = "actual captured output\n" * 200 + footer + "\n[Command succeeded with exit code 0]"

    async def execute(services, owner, turn, history):
        yield {
            "type": "tool_start",
            "call_id": "tests",
            "name": "execute",
            "label": "运行测试",
            "arguments": {"command": "test fixture"},
            "started_at": datetime.now(UTC).isoformat(),
        }
        yield {
            "type": "tool_end",
            "call_id": "tests",
            "failed": False,
            "output": output,
            "finished_at": datetime.now(UTC).isoformat(),
            "duration_ms": 1,
        }
        waiting.set()
        await release.wait()
        yield {"type": "token", "content": "已保留真实测试输出。"}

    monkeypatch.setattr(runtime, "run_turn", execute)
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        observed = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: action(turn)["brief"] == "测试输出汇总：总计 36, 通过 29, 失败 7。",
        )
        tool = action(observed["turns"][0])
        assert tool["output"] == output and tool["status"] == "completed"
        assert tool["arguments"] == {"command": "test fixture"}
        release.set()
        assert (await settled(client, auth, task["task_id"]))["status"] == "completed"
    finally:
        release.set()


async def test_cancel_discards_narration_even_when_generator_returns_after_cancellation(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    late = "取消之后生成的旁路文字。"

    async def narrate(self, snapshot):
        entered.set()
        with suppress(asyncio.CancelledError):
            await release.wait()
        returned.set()
        return late

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    _, gates = hold_models(monkeypatch, [AIMessage(content="不应出现的最终回答。")], 0)
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(gates[0][0].wait(), timeout=3)
        await asyncio.wait_for(entered.wait(), timeout=3)
        cancelled = await client.post(
            f"/api/project-tasks/{task['task_id']}/cancel",
            headers=auth,
            json={"turn_id": task["active_turn_id"]},
        )
        assert cancelled.status_code == 200, cancelled.text
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "cancelled" and returned.is_set()
        assert final["turns"][0]["reply"] == ""
        assert all(block["text"] != late for block in final["turns"][0]["content_blocks"])
        release.set()
        assert (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()[
            "turns"
        ] == final["turns"]
    finally:
        release.set()
        gates[0][1].set()


@pytest.mark.parametrize("update_kind", ["tool_receipt", "tool_end"])
async def test_completed_tool_output_change_discards_old_narration(
    env_with_provider, monkeypatch, update_kind
):
    client, server, auth = env_with_provider
    old_entered, old_release, new_entered, new_release, final_release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    old_output, new_output = {"result": "earlier receipt"}, {"result": "latest actual result"}
    old_brief, new_brief = "这条说明依据旧输出。", "这条说明依据最新实际输出。"
    snapshots, saved_briefs = [], []
    repo = server.services.project_task_turn_repo
    original_progress = repo.progress

    def observe_progress(*args, **kwargs):
        for block in json.loads(kwargs.get("content_blocks_json") or "[]"):
            if block["type"] == "tool":
                saved_briefs.append(block["brief"])
        return original_progress(*args, **kwargs)

    monkeypatch.setattr(repo, "progress", observe_progress)

    async def narrate(self, snapshot):
        if snapshot.facts["stage"] != "tool_end":
            return None
        snapshots.append(snapshot)
        result = json.loads(snapshot.facts["result"])
        if result == old_output:
            old_entered.set()
            await old_release.wait()
            return old_brief
        assert result == new_output
        new_entered.set()
        await new_release.wait()
        return new_brief

    async def run(services, owner, turn, history):
        timestamp = datetime.now(UTC)
        yield {
            "type": "tool_start",
            "call_id": "same-call",
            "name": "read_note",
            "label": "读取记录",
            "arguments": {"path": "selected.txt"},
            "started_at": timestamp,
        }
        yield {
            "type": "tool_end",
            "call_id": "same-call",
            "failed": False,
            "output": old_output,
            "finished_at": timestamp,
            "duration_ms": 1,
        }
        await old_entered.wait()
        yield {
            "type": update_kind,
            "call_id": "same-call",
            "failed": False,
            "output": new_output,
        }
        await final_release.wait()
        yield {"type": "token", "content": "已核对最新结果。"}

    monkeypatch.setattr(runtime, "run_turn", run)
    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(old_entered.wait(), timeout=3)
        updated = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: action(turn)["output"] == new_output,
        )
        before = action(updated["turns"][0])
        assert before["status"] == "completed" and before["brief"] is None
        old_release.set()
        await asyncio.wait_for(new_entered.wait(), timeout=3)
        assert len(snapshots) == 2
        assert snapshots[0].facts["status"] == snapshots[1].facts["status"] == "completed"
        assert snapshots[0].source_digest != snapshots[1].source_digest
        assert snapshots[0].key != snapshots[1].key
        assert old_brief not in saved_briefs
        new_release.set()
        narrated = await visible(
            env_with_provider, task["task_id"], lambda turn: action(turn)["brief"] == new_brief
        )
        actual = action(narrated["turns"][0])
        assert {key: value for key, value in actual.items() if key != "brief"} == {
            key: value for key, value in before.items() if key != "brief"
        }
        final_release.set()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed" and old_brief not in saved_briefs
        assert action(final["turns"][0])["brief"] == new_brief
        assert action(final["turns"][0])["output"] == new_output
    finally:
        old_release.set()
        new_release.set()
        final_release.set()


async def test_cancel_between_publish_check_and_write_rejects_and_rolls_back_narration(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    late = "取消边界之后不能保存这条旁路说明。"
    main_release, intercepted = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    repo = server.services.project_task_turn_repo
    original_progress = repo.progress
    writes = []

    def cancel_before_progress(*args, **kwargs):
        if kwargs.get("require_active") and not writes:
            turn_id = args[0]
            before = repo.get(turn_id)
            assert before.status == "running" and not before.cancel_requested
            with server.services.db.transaction() as conn:
                conn.execute(
                    "UPDATE project_task_turns SET cancel_requested=1 WHERE turn_id=?", (turn_id,)
                )
            result = original_progress(*args, **kwargs)
            after = repo.get(turn_id)
            writes.append((before, after, result))
            loop.call_soon_threadsafe(intercepted.set)
            return result
        return original_progress(*args, **kwargs)

    async def narrate(self, snapshot):
        return late

    async def run(services, owner, turn, history):
        await main_release.wait()
        yield {"type": "token", "content": "取消后不应执行到最终回答。"}

    monkeypatch.setattr(repo, "progress", cancel_before_progress)
    monkeypatch.setattr(runtime, "run_turn", run)
    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(intercepted.wait(), timeout=3)
        assert len(writes) == 1
        before, after, rejected = writes[0]
        assert rejected is True and after.cancel_requested
        assert after.content_blocks_json == before.content_blocks_json
        assert after.content_revision == before.content_revision
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "cancelled" and final["turns"][0]["reply"] == ""
        assert final["turns"][0]["content_blocks"] == json.loads(before.content_blocks_json)
        assert all(block["text"] != late for block in progress(final["turns"][0]))
        saved = repo.get(final["turns"][0]["turn_id"])
        assert saved.content_revision == before.content_revision
        assert saved.content_blocks_json == before.content_blocks_json
    finally:
        main_release.set()


async def test_disabled_action_timeline_never_starts_narration(env_with_provider, monkeypatch):
    client, _, auth = env_with_provider
    snapshots = []

    async def narrate(self, snapshot):
        snapshots.append(snapshot)
        return "不应产生旁路进展。"

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    planning_models(monkeypatch, [AIMessage(content="直接回答。")])
    task = await create(env_with_provider, enabled=False)
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed" and snapshots == []
    assert progress(final["turns"][0]) == []


async def test_main_and_narrator_usage_accumulate_even_when_late_summary_is_unusable(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider
    recorded, release, late_release, late_recorded = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def narrate(self, snapshot):
        assert self.record_usage is not None
        await self.record_usage(3, 2)
        recorded.set()
        with suppress(asyncio.CancelledError):
            await release.wait()
        await late_release.wait()
        await self.record_usage(5, 4)
        late_recorded.set()
        return None

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    _, gates = hold_models(
        monkeypatch,
        [
            AIMessage(
                content="主回答已完成。",
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            )
        ],
        0,
    )
    task = await create(env_with_provider)
    try:
        await asyncio.wait_for(recorded.wait(), timeout=3)
        await asyncio.wait_for(gates[0][0].wait(), timeout=3)
        early = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: (turn["input_tokens"], turn["output_tokens"]) == (3, 2),
        )
        assert early["status"] == "running"
        gates[0][1].set()
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed", final
        assert (final["turns"][0]["input_tokens"], final["turns"][0]["output_tokens"]) == (14, 9)
        assert not late_recorded.is_set()
        late_release.set()
        await asyncio.wait_for(late_recorded.wait(), timeout=3)
        billed = await visible(
            env_with_provider,
            task["task_id"],
            lambda turn: (turn["input_tokens"], turn["output_tokens"]) == (19, 13),
        )
        assert billed["status"] == "completed"
        assert billed["turns"][0]["content_blocks"] == final["turns"][0]["content_blocks"]
        assert billed["turns"][0]["reply"] == "主回答已完成。"
    finally:
        release.set()
        late_release.set()
        gates[0][1].set()


async def test_approval_resume_does_not_redispatch_opening_or_repeat_real_work(
    env_with_provider, monkeypatch
):
    from deepagents.backends.protocol import ExecuteResponse
    from octop_harness.backends.bwrap_shell import BubbledLocalShellBackend
    from octop_harness.backends.local_shell import HarnessLocalShellBackend

    client, server, auth = env_with_provider
    keys, effects = [], []

    async def narrate(self, snapshot):
        keys.append(snapshot.key)
        return "正在核对本轮的实际执行进展。"

    def execute(self, command, *, timeout=None):
        effects.append(command)
        return ExecuteResponse(output="actual-proof", exit_code=0, truncated=False)

    monkeypatch.setattr(runtime.TaskProgressAgent, "generate", narrate)
    monkeypatch.setattr(HarnessLocalShellBackend, "execute", execute)
    monkeypatch.setattr(BubbledLocalShellBackend, "execute", execute)
    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call("execute", "approved-work", command="echo actual-proof"),
            AIMessage(content="已核对审批后的实际结果。"),
        ],
    )
    task = await create(env_with_provider)
    await parked(server, task["active_turn_id"])
    waiting = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
    assert waiting["status"] == "waiting_approval" and effects == []
    opening = next(
        block for block in progress(waiting["turns"][0]) if block["introduction"] is True
    )
    assert keys.count("opening") == 1
    await server.project_task_worker.close()
    item = waiting["turns"][0]["interactions"][0]
    accepted = await respond(env_with_provider, waiting, item, approval(item))
    assert accepted.status_code == 200, accepted.text
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    await server.project_task_worker.start()
    final = await settled(client, auth, task["task_id"])
    assert final["status"] == "completed", final
    assert final["turns"][0]["reply"] == "已核对审批后的实际结果。"
    assert effects == ["echo actual-proof"] and len(observed) == 2
    assert keys.count("opening") == 1
    assert len(keys) == len(set(keys))
    introductions = [
        block for block in progress(final["turns"][0]) if block["introduction"] is True
    ]
    assert len(introductions) == 1 and introductions[0]["block_id"] == opening["block_id"]
