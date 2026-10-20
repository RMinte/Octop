"""Independent narration stays bounded, factual and outside the execution path."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from octop.infra.projects import narration
from octop.infra.projects.content import TaskContent
from octop.infra.projects.progress import TaskPlan, TaskPlanStep


def snapshot(key="opening", **facts):
    return narration.ProgressSnapshot(key, facts or {"goal": "Read the selected file"})


@pytest.fixture
def isolated_model(monkeypatch):
    services = SimpleNamespace(
        user_policy_repo=object(),
        usage_repo=object(),
        user_repo=SimpleNamespace(get=MagicMock(return_value=SimpleNamespace(disabled=False))),
        provider_repo=object(),
    )
    store = MagicMock()
    store.is_model_ref_usable.return_value = True
    store.get_model_reasoning_capability.return_value = None
    store.build_harness_configs.return_value = []
    monkeypatch.setattr(narration, "ProviderStore", MagicMock(return_value=store))
    monkeypatch.setattr(narration, "assert_token_quota_available", MagicMock())
    configured = SimpleNamespace(
        ainvoke=AsyncMock(return_value=AIMessage(content="I will read it."))
    )
    model = SimpleNamespace(
        model_copy=MagicMock(return_value=configured),
        _harness_max_output_tokens=2_048,
        extra_body={"provider_option": "preserved"},
    )
    factory = SimpleNamespace(get_chat_model=MagicMock(return_value=model), aclose=AsyncMock())
    monkeypatch.setattr(narration, "ChatModelFactory", MagicMock(return_value=factory))
    return services, store, model, configured, factory


async def test_agent_uses_only_supplied_facts_and_independent_request_parameters(isolated_model):
    services, store, model, configured, factory = isolated_model
    store.get_model_reasoning_capability.return_value = {
        "toggle": True,
        "adapter": "thinking",
        "default_effort": "high",
        "efforts": ["low", "high"],
    }
    usage = AsyncMock()
    configured.ainvoke.return_value = AIMessage(
        content="I will inspect the selected file.",
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    agent = narration.TaskProgressAgent(services, 1, "chosen/model", "en", record_usage=usage)
    facts = snapshot("tool-one", tool="read_file", state="started")
    assert await agent.generate(facts) == "I will inspect the selected file."
    messages = configured.ainvoke.call_args.args[0]
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage) and isinstance(messages[1], HumanMessage)
    assert json.loads(messages[1].content) == facts.facts
    assert configured.ainvoke.call_args.kwargs == {"max_tokens": 512}
    assert model.model_copy.call_args.kwargs == {
        "update": {
            "max_retries": 0,
            "extra_body": {"provider_option": "preserved", "thinking": {"type": "disabled"}},
        }
    }
    assert model.extra_body == {"provider_option": "preserved"}
    factory.get_chat_model.assert_called_once_with("chosen/model")
    usage.assert_awaited_once_with(11, 7)
    factory.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        (None, {}),
        (
            {"toggle": False, "adapter": "openai_reasoning_effort", "efforts": ["high", "low"]},
            {"model_fields": {"reasoning_effort": "low"}},
        ),
        (
            {
                "toggle": False,
                "adapter": "anthropic_budget",
                "effort_type": "token_budget",
                "efforts": ["4096", "1024"],
            },
            {"model_fields": {"thinking": {"type": "enabled", "budget_tokens": 1024}}},
        ),
        ({"toggle": False, "adapter": "status_only", "efforts": []}, {}),
    ],
)
def test_reasoning_parameters_only_use_supported_minimum(capability, expected):
    assert narration._reasoning_parameters(capability) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("  Reading the file.  ", "Reading the file."),
        ("<think>private</think>Reading the file.", "Reading the file."),
        (
            [{"type": "reasoning", "text": "private"}, {"type": "text", "text": "Reading."}],
            "Reading.",
        ),
        ([{"type": "thinking", "thinking": "private"}], None),
        ([{"type": "reasoning", "text": "private"}], None),
        ("<think>private</think>", None),
        ("private prefix</think>", None),
        ("<thinking>unfinished private thoughts", None),
        (" ", None),
        ("界" * 241, None),
        ("```text\nReading.\n```", None),
        ([{"type": "unexpected", "text": "private"}], None),
        ([{"type": "text", "text": 123}], None),
    ],
)
async def test_agent_validates_public_text_without_extracting_hidden_reasoning(
    isolated_model, content, expected
):
    services, _store, _model, configured, factory = isolated_model
    configured.ainvoke.return_value = AIMessage(
        content=content, additional_kwargs={"reasoning_content": "never publish this"}
    )
    assert (
        await narration.TaskProgressAgent(services, 1, "chosen/model", "zh").generate(snapshot())
        == expected
    )
    factory.aclose.assert_awaited_once()


@pytest.mark.parametrize("invalid", [False, True])
async def test_tool_responses_are_rejected_but_known_usage_is_recorded(isolated_model, invalid):
    services, _store, _model, configured, _factory = isolated_model
    call = {"id": "bad", "name": "execute", "args": "invalid" if invalid else {}}
    configured.ainvoke.return_value = AIMessage(
        content="Do not publish this.",
        invalid_tool_calls=[call] if invalid else [],
        tool_calls=[] if invalid else [call],
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    usage = AsyncMock()
    assert (
        await narration.TaskProgressAgent(
            services, 1, "chosen/model", "en", record_usage=usage
        ).generate(snapshot())
        is None
    )
    usage.assert_awaited_once_with(3, 2)


async def test_provider_failure_still_closes_factory(isolated_model):
    services, _store, _model, configured, factory = isolated_model
    configured.ainvoke.side_effect = RuntimeError("private provider details")
    with pytest.raises(RuntimeError):
        await narration.TaskProgressAgent(services, 1, "chosen/model", "en").generate(snapshot())
    factory.aclose.assert_awaited_once()


async def test_model_budget_is_respected_when_less_than_512(isolated_model):
    services, _store, model, configured, _factory = isolated_model
    model._harness_max_output_tokens = 128
    await narration.TaskProgressAgent(services, 1, "chosen/model", "en").generate(snapshot())
    assert configured.ainvoke.call_args.kwargs["max_tokens"] == 128


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("ℹ tests 36\nℹ pass 29\nℹ fail 7\n", {"tests": 36, "passed": 29, "failed": 7}),
        ("ℹ tests 36\nℹ pass 30\nℹ fail 6\n", {"tests": 36, "passed": 30, "failed": 6}),
        (
            "# tests 16\n# pass 10\n# fail 6\n# cancelled 0\n# skipped 0\n# todo 0\n",
            {"tests": 16, "passed": 10, "failed": 6, "cancelled": 0, "skipped": 0, "todo": 0},
        ),
        (
            "\x1b[32mℹ tests 36\x1b[0m\nℹ pass 36\nℹ fail 0\n",
            {"tests": 36, "passed": 36, "failed": 0},
        ),
        (
            {"output": "ℹ tests 1\nℹ pass 0\nℹ fail 1\n", "exit_code": 0},
            {"tests": 1, "passed": 0, "failed": 1},
        ),
        (
            "======= 7 failed, 29 passed, 3 warnings in 0.12s =======",
            {"tests": 36, "passed": 29, "failed": 7},
        ),
        ("2 passed, 1 skipped, 5 deselected in 1.01s", {"tests": 3, "passed": 2, "skipped": 1}),
        ("1 error in 0.03s", {"errors": 1}),
        (
            "TAP version 13\nok 1 - works\nnot ok 2 - broken\nok 3 - deferred # SKIP\n1..3\n",
            {"tests": 3, "passed": 1, "failed": 1, "skipped": 1, "todo": 0},
        ),
        ("1..0\n", {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "todo": 0}),
    ],
)
def test_test_statistics_come_only_from_explicit_consistent_runner_summaries(output, expected):
    assert narration.extract_test_counts(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "ℹ tests 36\nℹ pass 29\n",  # A missing failure count is unknown.
        "ℹ tests 36\nℹ pass 36\nℹ fail 7\n",  # Contradictory totals.
        "ℹ tests 1\nℹ pass 1\nℹ fail 0\n" * 2,  # Multiple runs must not be combined.
        "ℹ tests 1\nℹ pass 1\nℹ fail 0\n[Output was truncated due to size limits]",
        {"output": "ℹ tests 1\nℹ pass 1\nℹ fail 0\n", "truncated": True},
        "1 passed in 0.01s\n2 passed in 0.02s",
        "ok 1 - first\n1..2\n",  # Incomplete TAP.
        "ok 1 - first\nBail out!\n1..1\n",
        "Updated file /tests/unit.test.js",  # Writing tests proves no execution.
        "Node v26.7.0\n[Command succeeded with exit code 0]",
        '{"tests":36,"pass":29,"fail":7}',  # Arbitrary JSON is not a runner footer.
    ],
)
def test_ambiguous_incomplete_or_truncated_test_output_produces_no_counts(output):
    assert narration.extract_test_counts(output) is None


async def test_long_actual_node_output_is_rendered_without_a_model_rewriting_counts(isolated_model):
    services, _store, _model, configured, factory = isolated_model
    output = (
        "unrelated output\n" * 300
        + "ℹ tests 36\nℹ pass 29\nℹ fail 7\n[Command succeeded with exit code 0]"
    )
    content = TaskContent("turn-test")
    content.start_tool("run", "execute", started_at=datetime.now(UTC))
    content.end_tool("run", output=output)
    facts = narration.tool_progress_facts(content, content.blocks[0], finished=True)
    assert facts["result_truncated"] is True
    assert facts["test_counts"] == {"tests": 36, "passed": 29, "failed": 7}
    configured.ainvoke.return_value = AIMessage(content="测试完成，28 项，22 通过、6 失败。")
    agent = narration.TaskProgressAgent(services, 1, "chosen/model", "zh")
    assert (
        await agent.generate(snapshot("counts", **facts))
        == "测试输出汇总：总计 36, 通过 29, 失败 7。"
    )
    configured.ainvoke.assert_not_awaited()
    factory.get_chat_model.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "测试完成，28 项，22 通过、6 失败。",
        "测试完成，28项，22通过、6失败。",
        "36/36 全部通过。",
        "29 passing/7 failing.",
        "29 passed, 7 failed.",
        "共二十八个测试用例。",
        "测试全部通过。",
        "All tests passed.",
        "测试完成，36 项，通过29项，失败7项。",
        "36 tests have passed; 0 tests have failed.",
        "全部 36 个用例均已通过。",
    ],
)
async def test_generated_test_statistics_are_rejected_when_not_host_verified(isolated_model, text):
    services, _store, _model, configured, _factory = isolated_model
    configured.ainvoke.return_value = AIMessage(content=text)
    assert (
        await narration.TaskProgressAgent(services, 1, "chosen/model", "zh").generate(snapshot())
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "已写入 v2 的测试文件 tests/v2.test.js。",
        "已保存 2 个测试文件，尚未取得运行结果。",
        "Node v26.7.0 is available.",
        "版本2通过校验，测试脚本已保存。",
    ],
)
async def test_non_statistical_numbers_remain_available_to_narration(isolated_model, text):
    services, _store, _model, configured, _factory = isolated_model
    configured.ainvoke.return_value = AIMessage(content=text)
    assert (
        await narration.TaskProgressAgent(services, 1, "chosen/model", "zh").generate(snapshot())
        == text
    )


def test_progress_facts_preserve_completed_ui_step_and_actual_writes_without_reusing_briefs():
    content = TaskContent("turn-context")
    content.update_plan(
        TaskPlan(
            source="octop_planning",
            plan_id="plan",
            revision=2,
            active_step_id="tests",
            steps=[
                TaskPlanStep(
                    step_id="ui",
                    content="实现界面",
                    status="completed",
                    result="已实际写入 HTML、CSS 和应用逻辑。",
                ),
                TaskPlanStep(step_id="tests", content="编写测试文件", status="in_progress"),
            ],
        )
    )
    for call_id, path in [("ui", "index.html"), ("tests", "tests/unit.test.js")]:
        content.start_tool(
            call_id,
            "write_file",
            started_at=datetime.now(UTC),
            arguments={"file_path": path, "content": "private source"},
            brief="界面尚未开始的旧错误旁白",
        )
        content.end_tool(call_id, output="Updated file /" + path)
    facts = narration.tool_progress_facts(content, content.blocks[-1], finished=True)
    plan = facts["context"]["plan"]
    assert plan["phase"] == "executing" and plan["active_step_id"] == "tests"
    assert plan["steps"][0]["status"] == "completed"
    assert plan["steps"][0]["result"] == "已实际写入 HTML、CSS 和应用逻辑。"
    assert facts["context"]["recent_actions"][0]["target"] == "index.html"
    encoded = json.dumps(facts, ensure_ascii=False)
    assert "private source" not in encoded and "旧错误旁白" not in encoded
    assert "test_counts" not in facts
    assert "omitted or unknown is not evidence of not started" in facts["context"]["scope"]


def test_cumulative_progress_context_is_explicitly_bounded():
    content = TaskContent("turn-bounded")
    for index in range(15):
        content.start_tool(
            str(index),
            "write_file",
            started_at=datetime.now(UTC),
            arguments={"path": f"file-{index}"},
        )
        content.end_tool(str(index), output="long actual receipt" * 100)
    facts = narration.progress_context(content)
    assert facts["executed_action_count"] == 15 and facts["actions_omitted"] == 3
    assert len(facts["recent_actions"]) == 12
    assert all(action["result_truncated"] is True for action in facts["recent_actions"])


def test_long_legacy_plan_context_keeps_current_and_recent_completed_steps():
    content = TaskContent("turn-long-plan")
    content.update_plan(
        TaskPlan(
            source="harness_todos",
            revision=1,
            active_step_id="step-14",
            steps=[
                TaskPlanStep(
                    step_id=f"step-{index}",
                    content=f"阶段 {index}",
                    status="in_progress" if index == 14 else "completed",
                    result="" if index == 14 else f"已记录真实结果 {index}",
                )
                for index in range(15)
            ],
        )
    )
    plan = narration.progress_context(content)["plan"]
    assert plan["steps_omitted"] == 3
    assert [step["step_id"] for step in plan["steps"]] == [
        f"step-{index}" for index in range(3, 15)
    ]
    assert plan["steps"][-1]["status"] == "in_progress"
    assert plan["steps"][-2]["result"] == "已记录真实结果 13"


def test_client_terminal_poll_test_counts_use_the_same_actual_output_parser():
    content = TaskContent("turn-terminal-poll")
    content.start_tool("poll", "client__write_stdin", started_at=datetime.now(UTC))
    content.end_tool(
        "poll",
        output={
            "output": {"stdout": "ℹ tests 36\nℹ pass 29\nℹ fail 7\n", "exit_code": 0},
            "is_error": False,
        },
    )
    facts = narration.tool_progress_facts(content, content.blocks[0], finished=True)
    assert facts["test_counts"] == {"tests": 36, "passed": 29, "failed": 7}


@pytest.mark.parametrize(
    ("tool_name", "receipt", "expected"),
    [
        (
            "write_file",
            "Updated file /notes-app/tests/unit.test.js",
            "已保存 /notes-app/tests/unit.test.js。",
        ),
        (
            "edit_file",
            "Successfully replaced 1 instance(s) of the string in '/notes-app/index.html'",
            "已更新 /notes-app/index.html。",
        ),
    ],
)
async def test_successful_file_receipt_has_no_model_generated_global_phase_claim(
    isolated_model, tool_name, receipt, expected
):
    services, _store, _model, configured, factory = isolated_model
    configured.ainvoke.return_value = AIMessage(
        content="测试文件已成功写入，单元测试部分完成，界面开发尚未开始。"
    )
    content = TaskContent("turn-written")
    content.start_tool(
        "write", tool_name, started_at=datetime.now(UTC), arguments={"file_path": "selected"}
    )
    content.end_tool("write", output=receipt)
    facts = narration.tool_progress_facts(content, content.blocks[0], finished=True)
    assert (
        await narration.TaskProgressAgent(services, 1, "chosen/model", "zh").generate(
            snapshot("write", **facts)
        )
        == expected
    )
    configured.ainvoke.assert_not_awaited()
    factory.get_chat_model.assert_not_called()


def test_failed_or_unrecognized_file_output_cannot_become_a_success_template():
    content = TaskContent("turn-write-error")
    content.start_tool("write", "write_file", started_at=datetime.now(UTC))
    content.end_tool("write", output="Error: write denied", failed=True)
    assert "file_result" not in narration.tool_progress_facts(
        content, content.blocks[0], finished=True
    )
    content.blocks[0].status = "completed"
    assert "file_result" not in narration.tool_progress_facts(
        content, content.blocks[0], finished=True
    )


@pytest.mark.parametrize(
    "text",
    [
        "界面开发尚未开始。",
        "仍在整理阶段，尚未进行任何编码。",
        "当前还没有开展编码和测试。",
        "UI implementation has not started.",
        "No coding has started.",
    ],
)
async def test_partial_context_cannot_support_an_absolute_unstarted_phase_claim(
    isolated_model, text
):
    services, _store, _model, configured, _factory = isolated_model
    configured.ainvoke.return_value = AIMessage(content=text)
    assert (
        await narration.TaskProgressAgent(services, 1, "chosen/model", "zh").generate(snapshot())
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "已收到客户端本机文件接线测试任务，但目前没有任何工具回执，尚未开始创建或读取文件，无法确认任何结果。",
        "目前没有任何工具回执。",
        "尚未开始创建或读取文件。",
        "无法确认任何结果。",
        "No tool receipts are available.",
        "I have not yet started creating or reading files.",
        "I cannot confirm any results.",
    ],
)
async def test_received_opening_rejects_absent_receipt_or_global_operation_state(
    isolated_model, text
):
    services, _store, _model, configured, factory = isolated_model
    configured.ainvoke.return_value = AIMessage(content=text)
    agent = narration.TaskProgressAgent(services, 1, "chosen/model", "zh")
    assert await agent.generate(snapshot(stage="received", goal="核对客户端本机文件接线")) is None
    factory.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "text",
    [
        "正在核对客户端本机文件接线需求，接下来会按授权检查文件创建和读取流程。",
        "我会核对客户端工具回执字段与预期输入。",
        "I am reviewing the requested local file creation and reading workflow.",
    ],
)
async def test_received_opening_keeps_concrete_goal_and_input_narration(isolated_model, text):
    services, _store, _model, configured, _factory = isolated_model
    configured.ainvoke.return_value = AIMessage(content=text)
    assert (
        await narration.TaskProgressAgent(services, 1, "chosen/model", "zh").generate(
            snapshot(stage="received", goal="核对客户端本机文件接线")
        )
        == text
    )


async def test_submit_does_not_wait_and_only_latest_pending_snapshot_is_generated():
    started, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
    generated, output = [], []

    async def generate(item):
        generated.append(item)
        if item.key == "first":
            started.set()
            await release.wait()
        return item.key

    async def publish(item, text):
        output.append((item.key, text))
        published.set()

    worker = narration.TaskProgressNarrator(generate, publish)
    worker.start()
    worker.submit(snapshot("first"))
    await asyncio.wait_for(started.wait(), 1)
    worker.submit(snapshot("middle"))
    latest = snapshot("latest", state="current")
    worker.submit(latest)
    latest.facts["state"] = "mutated after submission"
    worker.submit(snapshot("latest", state="duplicate"))
    release.set()
    await asyncio.wait_for(published.wait(), 1)
    await worker.close()
    assert [item.key for item in generated] == ["first", "latest"]
    assert generated[-1].facts == {"state": "current"}
    assert output == [("latest", "latest")]


async def test_timeout_drops_narration_and_continues_to_latest_snapshot():
    started, cancelled, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
    output = []

    async def generate(item):
        if item.key == "slow":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return item.key

    async def publish(item, text):
        output.append(text)
        published.set()

    worker = narration.TaskProgressNarrator(generate, publish, timeout=0.01)
    worker.start()
    worker.submit(snapshot("slow"))
    await asyncio.wait_for(started.wait(), 1)
    worker.submit(snapshot("latest"))
    await asyncio.wait_for(published.wait(), 1)
    await worker.close()
    assert cancelled.is_set()
    assert output == ["latest"]


@pytest.mark.parametrize("failure", ["generate", "publish"])
async def test_failures_do_not_stop_the_worker(failure):
    failed, published = asyncio.Event(), asyncio.Event()

    async def generate(item):
        if failure == "generate" and item.key == "bad":
            failed.set()
            raise RuntimeError("private details")
        return item.key

    async def publish(item, text):
        if failure == "publish" and item.key == "bad":
            failed.set()
            raise RuntimeError("private details")
        published.set()

    worker = narration.TaskProgressNarrator(generate, publish)
    worker.start()
    worker.submit(snapshot("bad"))
    await asyncio.wait_for(failed.wait(), 1)
    worker.submit(snapshot("good"))
    await asyncio.wait_for(published.wait(), 1)
    await worker.close()


async def test_close_does_not_wait_for_a_generator_that_swallows_cancellation():
    started, cancelled, release, finished = (asyncio.Event() for _ in range(4))
    publish = AsyncMock()

    async def generate(item):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finished.set()
        return "late result"

    worker = narration.TaskProgressNarrator(generate, publish)
    worker.start()
    worker.submit(snapshot("slow"))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(worker.close(), 0.1)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert not finished.is_set()
    assert worker._task is not None
    worker.submit(snapshot("never start"))
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    for _ in range(10):
        if worker._task is None:
            break
        await asyncio.sleep(0)
    assert worker._task is None
    publish.assert_not_awaited()
