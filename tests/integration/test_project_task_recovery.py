"""Interrupted turns keep their plans, completed effects and prepared skill bytes."""

from __future__ import annotations

import asyncio
from collections import Counter
from uuid import uuid4

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from octop_harness.plugins import LoadedPlugin, PluginManifest, PluginRegistry
from octop_harness.plugins.registry import ToolRegistration

from octop.infra.agents.security.policy_store import SecuritySettingsStore
from octop.infra.projects import receipts, runtime
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_task_planning import (
    latest_plan,
    planning_models,
    step_command,
    tool_call,
)
from tests.integration.test_project_tasks_ws import command, connected


@pytest.fixture
def recovery_plugin(env_with_provider):
    _, server, _ = env_with_provider
    path = server.paths.plugins_dir / "recovery-proof"
    path.mkdir(parents=True)
    (path / "plugin.yaml").write_text(
        "id: recovery-proof\nversion: 1.0.0\nname: Recovery proof\nkind: tool\nentry: main.py\n",
        encoding="utf8",
    )
    effects = []

    def recovery_fact(value: str) -> str:
        """Record a fact once, optionally losing the connection after the effect."""
        effects.append(value)
        if value == "uncertain":
            raise ConnectionError("connection lost after recording the fact")
        return "Recorded actual value: " + value

    plugin = LoadedPlugin(
        manifest=PluginManifest.load(path / "plugin.yaml"),
        source_path=path,
        tools=[ToolRegistration("recovery-proof", "recovery_fact", recovery_fact, "Record a fact")],
    )
    PluginRegistry().register(plugin)
    yield effects
    PluginRegistry().unregister("recovery-proof")


def fail_model_once_then(response):
    failed = False

    def generate(messages):
        nonlocal failed
        if not failed:
            failed = True
            raise ConnectionError("model connection temporarily unavailable")
        return response(messages) if callable(response) else response

    return generate


def step_action(call_id, value, index):
    def generate(messages):
        plan = latest_plan(messages)
        return tool_call(
            "recovery_fact",
            call_id,
            value=value,
            task_step_id=plan["steps"][index]["step_id"],
        )

    return generate


async def create_task(client, auth, **options):
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "保留前两步成果，从第三步测试继续。",
            "options": {"action_timeline_enabled": False, "memory_enabled": False, **options},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


async def wait_status(client, auth, task_id, status):
    for _ in range(600):
        response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
        assert response.status_code == 200, response.text
        task = response.json()
        if task["turns"][0]["status"] == status:
            return task
        if task["turns"][0]["status"] in {"failed", "cancelled", "completed"}:
            pytest.fail(f"Expected {status}, received {task}")
        await asyncio.sleep(0.01)
    pytest.fail(f"Task did not reach {status}")


async def resume_turn(ws, task_id, turn_id, request_id=None):
    return await command(
        ws,
        "task.resume",
        str(uuid4()),
        task_id=task_id,
        data={"turn_id": turn_id, "request_id": request_id or str(uuid4())},
    )


@pytest.mark.parametrize("callbacks", [False, True])
async def test_model_interruption_resumes_third_step_in_original_turn_without_replaying_effects(
    env_with_provider, recovery_plugin, monkeypatch, callbacks
):
    client, _, auth = env_with_provider
    downloads = []

    def download(slug):
        downloads.append(slug)
        assert downloads == ["recovery-notes"], "Resuming must reuse the prepared skill snapshot"
        return [("SKILL.md", b"---\nname: recovery-notes\ndescription: Notes\n---\nOriginal skill")]

    monkeypatch.setattr(runtime, "download_skillhub_package_files", download)
    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "create-original-plan",
                command="create",
                title="完成笔记并测试",
                steps=["整理笔记", "保存笔记", "测试笔记"],
            ),
            step_action("first-fact", "first", 0),
            step_command("mark_step", "first-complete", status="completed", result="已整理笔记。"),
            step_action("second-fact", "second", 1),
            step_command(
                "mark_step", "second-complete", index=1, status="completed", result="已保存笔记。"
            ),
            fail_model_once_then(step_action("third-test", "third", 2)),
            step_command(
                "mark_step", "third-complete", index=2, status="completed", result="测试通过。"
            ),
            AIMessage(content="已沿用前两步成果完成测试。"),
        ],
    )
    if callbacks:
        original_create = runtime.HarnessAgent

        def create_with_callbacks(*args, **kwargs):
            agent = original_create(*args, **kwargs)
            agent.set_langfuse_callbacks([BaseCallbackHandler()])
            assert agent.graph is not agent._base_graph
            return agent

        monkeypatch.setattr(runtime, "HarnessAgent", create_with_callbacks)
    created = await create_task(
        client,
        auth,
        plugins=["recovery-proof"],
        skills=[{"source": "skillhub", "slug": "recovery-notes", "name": "Notes"}],
    )
    interrupted = await wait_status(client, auth, created["task_id"], "interrupted")
    original = interrupted["turns"][0]
    assert original["turn_id"] == created["active_turn_id"]
    assert interrupted["active_turn_id"] == original["turn_id"]
    assert original["recovery"]["can_resume"] is True
    assert original["recovery"]["needs_reconciliation"] is False
    assert original["recovery"]["cause_type"] == "ConnectionError"
    assert original["recovery"]["phase"] and original["recovery"]["diagnostic_id"]
    original_plan = original["plan"]
    assert [step["status"] for step in original_plan["steps"]] == [
        "completed",
        "completed",
        "in_progress",
    ]
    assert Counter(recovery_plugin) == {"first": 1, "second": 1}
    completed_tools = [
        block
        for block in original["content_blocks"]
        if block["type"] == "tool" and block["tool_call_id"] in {"first-fact", "second-fact"}
    ]
    assert len(completed_tools) == 2 and all(b["status"] == "completed" for b in completed_tools)

    session, ws = await connected(client, auth)
    request_id = str(uuid4())
    try:
        assert (await resume_turn(ws, created["task_id"], original["turn_id"], request_id))[
            "type"
        ] == "result"
        final = await wait_status(client, auth, created["task_id"], "completed")
        repeated = await resume_turn(ws, created["task_id"], original["turn_id"], request_id)
        assert repeated["type"] == "result", repeated
        assert repeated["data"]["status"] == "completed"
    finally:
        await session.__aexit__(None, None, None)

    assert len(final["turns"]) == 1 and final["active_turn_id"] is None
    resumed = final["turns"][0]
    assert resumed["turn_id"] == original["turn_id"]
    assert resumed["plan"]["plan_id"] == original_plan["plan_id"]
    assert [step["step_id"] for step in resumed["plan"]["steps"]] == [
        step["step_id"] for step in original_plan["steps"]
    ]
    assert [step["status"] for step in resumed["plan"]["steps"]] == ["completed"] * 3
    assert Counter(recovery_plugin) == {"first": 1, "second": 1, "third": 1}
    assert downloads == ["recovery-notes"]
    assert all(block in resumed["content_blocks"] for block in completed_tools)
    assert sum(isinstance(message, HumanMessage) for message in observed[-1][1]) == 1


async def test_skill_download_timeout_can_resume_preparation_in_original_turn(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider
    attempts = []

    def download(slug):
        attempts.append(slug)
        if len(attempts) == 1:
            raise TimeoutError("skill download temporarily unavailable")
        return [("SKILL.md", b"---\nname: recovery-notes\ndescription: Notes\n---\nPrepared skill")]

    monkeypatch.setattr(runtime, "download_skillhub_package_files", download)
    observed, _ = planning_models(monkeypatch, [AIMessage(content="技能恢复后完成。")])
    created = await create_task(
        client,
        auth,
        skills=[{"source": "skillhub", "slug": "recovery-notes", "name": "Notes"}],
    )
    interrupted = await wait_status(client, auth, created["task_id"], "interrupted")
    original = interrupted["turns"][0]
    assert original["recovery"]["can_resume"] is True
    assert original["recovery"]["needs_reconciliation"] is False
    assert original["recovery"]["cause_type"] == "TimeoutError"
    assert original["recovery"]["resource_id"] == "recovery-notes"
    assert not observed
    session, ws = await connected(client, auth)
    try:
        assert (await resume_turn(ws, created["task_id"], original["turn_id"]))["type"] == "result"
        final = await wait_status(client, auth, created["task_id"], "completed")
    finally:
        await session.__aexit__(None, None, None)
    assert len(final["turns"]) == 1 and final["turns"][0]["turn_id"] == original["turn_id"]
    assert attempts == ["recovery-notes", "recovery-notes"]
    assert sum(isinstance(message, HumanMessage) for message in observed[-1][1]) == 1


async def test_corrupt_prepared_skill_snapshot_stops_without_downloading_replacement(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    downloads = []

    def download(slug):
        downloads.append(slug)
        return [("SKILL.md", b"---\nname: recovery-notes\ndescription: Notes\n---\nOriginal skill")]

    monkeypatch.setattr(runtime, "download_skillhub_package_files", download)
    observed, _ = planning_models(
        monkeypatch, [fail_model_once_then(AIMessage(content="Original skill completed."))]
    )
    created = await create_task(
        client,
        auth,
        skills=[{"source": "skillhub", "slug": "recovery-notes", "name": "Notes"}],
    )
    interrupted = await wait_status(client, auth, created["task_id"], "interrupted")
    turn_id = interrupted["turns"][0]["turn_id"]
    snapshot = server.paths.root / "project-tasks" / created["task_id"] / turn_id / "resources"
    (snapshot / "skills.json").write_bytes(b'{"skills":')
    session, ws = await connected(client, auth)
    try:
        receipt = await resume_turn(ws, created["task_id"], turn_id)
        assert receipt["type"] == "result", receipt
        # Wait for the next interruption, rather than observing the original one
        # while the resumed worker is still queued.
        for _ in range(600):
            final = (
                await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)
            ).json()
            recovery = final["turns"][0]["recovery"]
            if recovery and recovery["cause_type"] == "JSONDecodeError":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(f"Corrupted snapshot was not reported: {final}")
    finally:
        await session.__aexit__(None, None, None)
    assert final["turns"][0]["status"] == "interrupted"
    assert final["turns"][0]["turn_id"] == turn_id and len(final["turns"]) == 1
    assert recovery["can_resume"] is False
    assert recovery["reason_code"] == "TASK_RESOURCE_UNAVAILABLE"
    assert downloads == ["recovery-notes"] and len(observed) == 1


async def test_uncertain_tool_effect_requires_reconciliation_before_resume(
    env_with_provider, recovery_plugin, monkeypatch
):
    client, _, auth = env_with_provider
    planning_models(
        monkeypatch,
        [
            tool_call("recovery_fact", "uncertain-fact", value="uncertain"),
            AIMessage(content="Must not be reached without reconciling the tool effect."),
        ],
    )
    created = await create_task(client, auth, plugins=["recovery-proof"])
    interrupted = await wait_status(client, auth, created["task_id"], "interrupted")
    turn = interrupted["turns"][0]
    assert turn["recovery"]["needs_reconciliation"] is True
    assert turn["recovery"]["can_resume"] is False
    assert recovery_plugin == ["uncertain"]
    session, ws = await connected(client, auth)
    try:
        receipt = await resume_turn(ws, created["task_id"], turn["turn_id"])
        assert receipt["type"] == "error", receipt
    finally:
        await session.__aexit__(None, None, None)
    final = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    assert final["turns"][0]["status"] == "interrupted" and recovery_plugin == ["uncertain"]


async def test_resumed_graph_can_reach_fresh_approval_and_finish_original_turn(
    env_with_provider, recovery_plugin, monkeypatch
):
    client, server, auth = env_with_provider
    policy = SecuritySettingsStore(settings_repo=server.services.settings_repo)
    value = policy.load().to_dict()
    value["hitl"] = {"enabled": True, "tools": ["recovery_fact"]}
    policy.save(value)
    observed, _ = planning_models(
        monkeypatch,
        [
            fail_model_once_then(
                tool_call("recovery_fact", "approve-after-recovery", value="approved")
            ),
            AIMessage(content="已在原轮恢复并完成审批动作。"),
        ],
    )
    created = await create_task(client, auth, plugins=["recovery-proof"])
    interrupted = await wait_status(client, auth, created["task_id"], "interrupted")
    turn_id = interrupted["turns"][0]["turn_id"]
    assert recovery_plugin == []
    session, ws = await connected(client, auth)
    try:
        assert (await resume_turn(ws, created["task_id"], turn_id))["type"] == "result"
        waiting = await wait_status(client, auth, created["task_id"], "waiting_approval")
        assert len(waiting["turns"]) == 1 and waiting["turns"][0]["turn_id"] == turn_id
        item = waiting["turns"][0]["interactions"][0]
        assert item["tool_call_id"] == "approve-after-recovery" and not item["dispatch_started"]
        assert recovery_plugin == []
        result = await respond(env_with_provider, waiting, item, approval(item))
        assert result.status_code == 200, result.text
        final = await wait_status(client, auth, created["task_id"], "completed")
    finally:
        await session.__aexit__(None, None, None)
    assert len(final["turns"]) == 1 and final["turns"][0]["turn_id"] == turn_id
    assert recovery_plugin == ["approved"]
    assert final["turns"][0]["interactions"][0]["dispatch_started"] is True
    assert sum(isinstance(message, HumanMessage) for message in observed[-1][1]) == 1


async def test_completed_receipt_backfills_graph_result_without_dispatching_tool_twice(
    env_with_provider, recovery_plugin, monkeypatch
):
    client, server, auth = env_with_provider
    original_run = receipts.TaskReceipts.run
    interrupted = False
    recovered_results = []

    async def lose_result_after_durable_receipt(self, request, handler):
        nonlocal interrupted
        result = await original_run(self, request, handler)
        if request.tool_call["id"] == "receipt-before-graph" and not interrupted:
            interrupted = True
            with self.services.db.transaction() as conn:
                saved = self.services.project_task_execution_receipt_repo.get(
                    conn, self.turn.turn_id, "receipt-before-graph"
                )
                assert saved is not None and saved.state == "completed"
            # The real side effect and receipt are complete, but the graph has
            # not yet received the ToolMessage returned by this middleware.
            raise ConnectionError("connection lost after receipt persistence")
        return result

    def answer(messages):
        result = next(
            message
            for message in reversed(messages)
            if isinstance(message, ToolMessage) and message.tool_call_id == "receipt-before-graph"
        )
        recovered_results.append(result)
        return AIMessage(content="原执行收据结果：" + str(result.content))

    monkeypatch.setattr(receipts.TaskReceipts, "run", lose_result_after_durable_receipt)
    observed, _ = planning_models(
        monkeypatch,
        [tool_call("recovery_fact", "receipt-before-graph", value="receipt-proof"), answer],
    )
    created = await create_task(client, auth, plugins=["recovery-proof"])
    paused = await wait_status(client, auth, created["task_id"], "interrupted")
    original = paused["turns"][0]
    assert original["recovery"]["can_resume"] is True
    assert original["recovery"]["needs_reconciliation"] is False
    assert recovery_plugin == ["receipt-proof"] and not recovered_results
    with server.services.db.transaction() as conn:
        receipt = server.services.project_task_execution_receipt_repo.get(
            conn, original["turn_id"], "receipt-before-graph"
        )
        assert receipt is not None and receipt.state == "completed"
        original_receipt_id = receipt.receipt_id
    session, ws = await connected(client, auth)
    try:
        assert (await resume_turn(ws, created["task_id"], original["turn_id"]))["type"] == "result"
        final = await wait_status(client, auth, created["task_id"], "completed")
    finally:
        await session.__aexit__(None, None, None)
    assert len(final["turns"]) == 1 and final["turns"][0]["turn_id"] == original["turn_id"]
    assert recovery_plugin == ["receipt-proof"]
    assert len(recovered_results) == 1
    assert recovered_results[0].content == "Recorded actual value: receipt-proof"
    tool = next(
        block
        for block in final["turns"][0]["content_blocks"]
        if block["type"] == "tool" and block["tool_call_id"] == "receipt-before-graph"
    )
    assert tool["status"] == "completed"
    assert tool["output"] == "Recorded actual value: receipt-proof"
    with server.services.db.transaction() as conn:
        receipt = server.services.project_task_execution_receipt_repo.get(
            conn, original["turn_id"], "receipt-before-graph"
        )
        assert receipt.receipt_id == original_receipt_id and receipt.state == "completed"
    assert sum(isinstance(message, HumanMessage) for message in observed[-1][1]) == 1


async def test_cancelled_interruption_does_not_allow_old_resume_to_change_new_followup(
    env_with_provider, monkeypatch
):
    client, _, auth = env_with_provider
    entered, release = asyncio.Event(), asyncio.Event()
    original_run = runtime.run_turn

    async def gated(services, owner, turn, history):
        if turn.prompt == "新的追问":
            entered.set()
            await release.wait()
        async for event in original_run(services, owner, turn, history):
            yield event

    monkeypatch.setattr(runtime, "run_turn", gated)
    planning_models(monkeypatch, [fail_model_once_then(AIMessage(content="新的追问已完成。"))])
    created = await create_task(client, auth)
    interrupted = await wait_status(client, auth, created["task_id"], "interrupted")
    original_turn_id = interrupted["turns"][0]["turn_id"]
    session, ws = await connected(client, auth)
    try:
        cancelled = await command(
            ws,
            "task.cancel",
            str(uuid4()),
            task_id=created["task_id"],
            data={"turn_id": original_turn_id},
        )
        assert cancelled["type"] == "result", cancelled
        assert cancelled["data"]["status"] == "cancelled"
        response = await client.post(
            f"/api/project-tasks/{created['task_id']}/messages",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "prompt": "新的追问",
                "options": {"action_timeline_enabled": False, "memory_enabled": False},
            },
        )
        assert response.status_code == 202, response.text
        new_turn_id = response.json()["active_turn_id"]
        assert new_turn_id != original_turn_id
        await asyncio.wait_for(entered.wait(), 5)
        late = await resume_turn(ws, created["task_id"], original_turn_id)
        assert late["type"] == "error", late
        active = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
        assert active["active_turn_id"] == new_turn_id
        assert active["turns"][0]["status"] == "cancelled"
        release.set()
        for _ in range(600):
            final = (
                await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)
            ).json()
            if final["active_turn_id"] is None:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(f"New follow-up did not finish: {final}")
        assert len(final["turns"]) == 2
        assert final["turns"][0]["turn_id"] == original_turn_id
        assert final["turns"][0]["status"] == "cancelled"
        assert final["turns"][1]["turn_id"] == new_turn_id
        assert final["turns"][1]["status"] == "completed"
    finally:
        release.set()
        await session.__aexit__(None, None, None)
