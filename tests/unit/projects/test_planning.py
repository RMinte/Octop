"""Ordered planning advances only through durable, host-owned step transitions."""

import asyncio
import json
from copy import deepcopy

import pytest
from langchain_core.messages import ToolMessage

from octop.i18n import tr
from octop.infra.projects.planning import (
    ORDERED_PLAN_STEP_LIMIT,
    PLAN_TITLE_LIMIT,
    PLANNING,
    TaskPlanning,
    plan_report,
)
from octop.infra.projects.progress import (
    PLAN_CONTENT_LIMIT,
    PROGRESS_TEXT_LIMIT,
    TaskPlan,
    TaskPlanStep,
)
from octop.infra.utils.ulid import new_ulid


async def invoke(manager, command, *, call_id=None, **arguments):
    return await manager.tool().ainvoke(
        {
            "type": "tool_call",
            "id": call_id or new_ulid(),
            "name": PLANNING,
            "args": {"command": command, **arguments},
        }
    )


async def created(persist=None):
    manager = TaskPlanning(None, "en", persist)
    result = await invoke(manager, "create", title="  Ship progress  ", steps=["Inspect", "Verify"])
    assert result.status == "success"
    return manager


async def mark(manager, index=0, status="completed", **arguments):
    plan = manager.plan
    return await invoke(
        manager,
        "mark_step",
        expected_revision=plan.revision,
        step_id=plan.steps[index].step_id,
        status=status,
        result="Verified result or recovery needed.",
        **arguments,
    )


@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_create_allocates_stable_ids_and_starts_first_step(locale):
    manager = TaskPlanning(None, locale)
    assert manager.phase == "unplanned" and manager.binding() == (None, None)
    response = await invoke(manager, "create", title="  Task plan  ", steps=[" Inspect ", "Verify"])
    plan = plan_report(response)
    assert plan is not None and plan == manager.plan
    assert plan.source == "octop_planning" and plan.revision == 1
    assert plan.title == "Task plan"
    assert plan.plan_id and len(plan.plan_id) == 26
    assert len({plan.plan_id, *(step.step_id for step in plan.steps)}) == 3
    assert plan.steps[0].content == "Inspect"
    assert plan.steps[0].status == "in_progress" and plan.steps[0].started_at is not None
    assert plan.steps[0].started_at.tzinfo is not None
    assert plan.steps[1].status == "pending" and plan.steps[1].started_at is None
    assert plan.steps[0].result == "" and plan.steps[0].finished_at is None
    assert manager.phase == "executing"
    assert manager.binding() == (plan.plan_id, plan.steps[0].step_id)
    assert json.loads(response.content) == response.artifact["plan"]
    assert manager.tools()[0].description == tr("projects.planning_tool_description", locale)


@pytest.mark.parametrize("status", ["completed", "skipped"])
async def test_host_starts_next_step_after_explicit_outcome(status):
    manager = await created()
    original = manager.plan
    result = plan_report(await mark(manager, status=status))
    assert result.revision == 2 and result.plan_id == original.plan_id
    assert [step.step_id for step in result.steps] == [step.step_id for step in original.steps]
    first, second = result.steps
    assert first.status == status and first.result == "Verified result or recovery needed."
    assert first.started_at == original.steps[0].started_at
    assert first.finished_at >= first.started_at
    assert second.status == "in_progress" and second.started_at == first.finished_at
    assert second.finished_at is None and second.result == ""
    assert result.active_step_id == second.step_id
    assert manager.binding() == (result.plan_id, second.step_id)
    assert manager.phase == "executing"
    finished = plan_report(await mark(manager, index=1))
    assert finished.revision == 3 and finished.active_step_id is None
    assert manager.phase == "completed" and manager.binding() == (None, None)


async def test_blocked_step_stops_without_starting_next_step():
    manager = await created()
    original = manager.plan
    blocked = plan_report(await mark(manager, status="blocked"))
    assert blocked.steps[0].status == "blocked" and blocked.steps[0].finished_at is not None
    assert blocked.steps[1] == original.steps[1]
    assert blocked.active_step_id is None
    assert manager.phase == "blocked" and manager.binding() == (None, None)
    for index in (0, 1):
        rejected = await mark(manager, index=index)
        assert rejected.status == "error" and manager.plan == blocked


async def test_only_current_step_can_be_marked_or_receive_action_binding():
    manager = await created()
    original = manager.plan
    with pytest.raises(ValueError, match=tr("projects.planning_step_not_running", "en")):
        manager.binding(original.steps[1].step_id)
    assert (await mark(manager, index=1)).status == "error" and manager.plan == original
    completed_first = plan_report(await mark(manager))
    assert (await mark(manager)).status == "error" and manager.plan == completed_first
    with pytest.raises(ValueError, match=tr("projects.planning_step_not_running", "en")):
        manager.binding(original.steps[0].step_id)
    unknown = await invoke(
        manager,
        "mark_step",
        expected_revision=manager.plan.revision,
        step_id="unknown",
        status="completed",
        result="Cannot skip to an unknown step.",
    )
    assert unknown.status == "error" and manager.plan == completed_first


@pytest.mark.parametrize("command", ["update", "start_step", "select_step"])
async def test_dynamic_commands_are_not_advertised_or_executable(command):
    manager = await created()
    schema = manager.tool().tool_call_schema.model_json_schema()
    assert schema["properties"]["command"]["enum"] == ["create", "get", "mark_step"]
    original = manager.plan
    rejected = await invoke(manager, command, expected_revision=1)
    assert rejected.status == "error" and manager.plan == original


async def test_plan_creation_is_once_per_turn_even_after_completion():
    manager = await created()
    for after_completion in (False, True):
        if after_completion:
            await mark(manager)
            await mark(manager, index=1)
        original = manager.plan
        rejected = await invoke(manager, "create", title="Replacement", steps=["New step"])
        assert rejected.status == "error"
        assert rejected.content == tr("projects.planning_already_created", "en")
        assert manager.plan == original


async def test_historical_plan_remains_read_only_without_migration_or_replacement():
    historical = TaskPlan(
        revision=7, steps=[TaskPlanStep(content="Legacy step", status="in_progress")]
    )
    manager = TaskPlanning(historical, "en")
    with pytest.raises(ValueError, match=tr("projects.planning_step_not_current", "en")):
        manager.binding()
    assert plan_report(await invoke(manager, "get")) == historical
    rejected = await invoke(
        manager,
        "mark_step",
        expected_revision=7,
        step_id="legacy",
        status="completed",
        result="Do not invent missing identities.",
    )
    assert rejected.status == "error" and rejected.content == tr("projects.planning_legacy", "en")
    replacement = await invoke(manager, "create", title="New plan", steps=["New step"])
    assert replacement.status == "error"
    assert manager.plan == historical and manager.plan.steps[0].step_id is None


async def test_restore_and_get_return_isolated_snapshots():
    manager = await created()
    restored = manager.plan
    resumed = TaskPlanning(restored, "en")
    restored.steps[0].content = "External mutation"
    snapshot = resumed.get()
    snapshot.steps[0].content = "Another external mutation"
    assert plan_report(await invoke(resumed, "get")) == manager.plan
    assert resumed.binding() == manager.binding()


async def test_checkpoint_restore_only_accepts_newer_revision():
    manager = await created()
    initial = manager.plan
    resumed = TaskPlanning(initial, "en")
    latest = plan_report(await mark(manager))
    resumed.restore(latest)
    resumed.restore(initial)
    assert resumed.plan == latest and resumed.binding() == manager.binding()
    latest.steps[0].content = "External mutation"
    assert resumed.plan == manager.plan


async def test_restore_call_replays_create_without_rolling_back_newer_step():
    manager = TaskPlanning(None, "en")
    arguments = {"title": "  Original plan  ", "steps": [" Inspect ", "Verify"]}
    response = await invoke(manager, "create", call_id="create-once", **arguments)
    created_plan = plan_report(response)
    await mark(manager)
    latest = manager.plan
    resumed = TaskPlanning(latest, "en")
    resumed.restore_call("create-once", {"command": "create", **arguments}, created_plan)
    created_plan.steps[0].content = "Mutation outside the manager"
    replayed = await invoke(
        resumed,
        "create",
        call_id="create-once",
        title="Original plan",
        steps=["Inspect", "Verify"],
    )
    assert replayed.status == "success" and replayed.artifact == response.artifact
    assert resumed.plan == latest and resumed.binding() == manager.binding()
    replayed.artifact["plan"]["steps"][0]["content"] = "Mutation in returned artifact"
    assert (
        await invoke(resumed, "create", call_id="create-once", **arguments)
    ).artifact == response.artifact


async def test_repeated_call_is_idempotent_and_changed_payload_cannot_reuse_id():
    manager = TaskPlanning(None, "en")
    first, duplicate = await asyncio.gather(
        invoke(manager, "create", call_id="same-call", title="Plan", steps=["Inspect", "Verify"]),
        invoke(manager, "create", call_id="same-call", title="Plan", steps=["Inspect", "Verify"]),
    )
    assert first.artifact == duplicate.artifact and manager.plan.revision == 1
    reused = await invoke(manager, "create", call_id="same-call", title="Plan", steps=["Different"])
    assert reused.status == "error" and manager.plan.revision == 1
    assert reused.content == tr("projects.planning_call_reused", "en")


async def test_parallel_step_reports_cannot_overwrite_newer_revision():
    manager = await created()
    first = manager.plan.steps[0]
    responses = await asyncio.gather(
        *(
            invoke(
                manager,
                "mark_step",
                expected_revision=1,
                step_id=first.step_id,
                status="completed",
                result="Checks passed.",
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status for response in responses) == ["error", "success"]
    assert manager.plan.revision == 2
    assert next(response for response in responses if response.status == "error").content == tr(
        "projects.planning_conflict", "en"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"steps": ["Inspect"]},
        {"title": " ", "steps": ["Inspect"]},
        {"title": "x" * (PLAN_TITLE_LIMIT + 1), "steps": ["Inspect"]},
        {"title": "Plan", "steps": []},
        {"title": "Plan", "steps": [" "]},
        {"title": "Plan", "steps": ["x" * (PLAN_CONTENT_LIMIT + 1)]},
        {"title": "Plan", "steps": ["Step"] * (ORDERED_PLAN_STEP_LIMIT + 1)},
        {"title": "Plan", "steps": [{"content": "Unknown shape"}]},
        {"title": "Plan", "steps": ["Inspect"], "step_id": "future"},
        {"title": "Plan", "steps": ["Inspect"], "expected_revision": 1},
        {"title": "Plan", "steps": ["Inspect"], "status": "completed"},
        {"title": "Plan", "steps": ["Inspect"], "result": "Premature"},
    ],
)
async def test_invalid_create_does_not_publish_a_plan(arguments):
    manager = TaskPlanning(None, "en")
    rejected = await invoke(manager, "create", **arguments)
    assert rejected.status == "error" and manager.plan is None and manager.phase == "unplanned"


@pytest.mark.parametrize(
    "arguments",
    [
        {"result": " "},
        {"result": "x" * (PROGRESS_TEXT_LIMIT + 1)},
        {"expected_revision": None},
        {"expected_revision": True},
        {"step_id": None},
        {"status": None},
        {"status": "in_progress"},
        {"title": "Cannot rename"},
        {"steps": ["Cannot replan"]},
    ],
)
async def test_invalid_step_reports_are_atomic(arguments):
    manager = await created()
    original = manager.plan
    rejected = await invoke(
        manager,
        "mark_step",
        **{
            "expected_revision": 1,
            "step_id": original.active_step_id,
            "status": "completed",
            "result": "Verified.",
            **arguments,
        },
    )
    assert rejected.status == "error" and manager.plan == original


async def test_get_requires_existing_plan_and_no_mutation_arguments():
    assert (await invoke(TaskPlanning(None, "en"), "get")).status == "error"
    manager = await created()
    original = manager.plan
    for arguments in ({"title": "New"}, {"expected_revision": 1}, {"status": "completed"}):
        assert (await invoke(manager, "get", **arguments)).status == "error"
        assert manager.plan == original


async def test_persistence_is_awaited_before_plan_or_next_step_is_published():
    entered, release = asyncio.Event(), asyncio.Event()
    records = []

    async def persist(plan, call_id, arguments):
        records.append((plan, call_id, arguments))
        entered.set()
        await release.wait()

    manager = TaskPlanning(None, "en", persist)
    creation = asyncio.create_task(
        invoke(
            manager, "create", call_id="durable-create", title="Plan", steps=["Inspect", "Verify"]
        )
    )
    await entered.wait()
    assert manager.plan is None and not creation.done()
    assert records[0][1:] == (
        "durable-create",
        {"command": "create", "title": "Plan", "steps": ["Inspect", "Verify"]},
    )
    release.set()
    await creation
    original = manager.plan
    entered.clear()
    release.clear()
    transition = asyncio.create_task(mark(manager, call_id="durable-mark"))
    await entered.wait()
    assert manager.plan == original and not transition.done()
    assert records[-1][0].steps[1].status == "in_progress"
    release.set()
    await transition
    records[-1][0].steps[1].content = "Mutation in persistence adapter"
    assert manager.plan.steps[1].content == "Verify"
    await invoke(manager, "get")
    assert len(records) == 2


@pytest.mark.parametrize("command", ["create", "mark_step"])
async def test_uncertain_persistence_failure_escapes_without_advancing_memory(command):
    async def fail(plan, call_id, arguments):
        raise RuntimeError("Plan write did not confirm success")

    if command == "create":
        manager = TaskPlanning(None, "en", fail)
        original = None
        operation = invoke(manager, "create", title="Plan", steps=["Inspect", "Verify"])
    else:
        baseline = await created()
        manager = TaskPlanning(baseline.plan, "en", fail)
        original = manager.plan
        operation = mark(manager)
    with pytest.raises(RuntimeError, match="did not confirm success"):
        await operation
    assert manager.plan == original


async def test_persisted_call_replay_does_not_write_again_or_advance_again():
    records = []

    async def persist(plan, call_id, arguments):
        records.append((plan, call_id, arguments))

    manager = await created(persist)
    original = manager.plan
    first = await mark(manager, call_id="mark-once")
    replay = await invoke(
        manager,
        "mark_step",
        call_id="mark-once",
        expected_revision=original.revision,
        step_id=original.active_step_id,
        status="completed",
        result="Verified result or recovery needed.",
    )
    assert first.artifact == replay.artifact and manager.plan.revision == 2
    assert len(records) == 2


async def test_plan_report_requires_successful_planning_artifact():
    manager = await created()
    response = await invoke(manager, "get")
    assert plan_report(response) == manager.plan
    invalid_artifact = deepcopy(response.artifact)
    invalid_artifact["plan"]["active_step_id"] = "missing"
    for message in (
        ToolMessage(content="Raw plan text", tool_call_id="raw", name=PLANNING),
        ToolMessage(
            content="Other", tool_call_id="other", name="execute", artifact=response.artifact
        ),
        ToolMessage(
            content="Failed",
            tool_call_id="failed",
            name=PLANNING,
            artifact=response.artifact,
            status="error",
        ),
        ToolMessage(
            content="Invalid", tool_call_id="invalid", name=PLANNING, artifact=invalid_artifact
        ),
    ):
        assert plan_report(message) is None


@pytest.mark.parametrize("source", ["harness_todos", "octop_planning"])
async def test_restored_unfinished_plan_without_current_step_cannot_dispatch(source):
    plan = TaskPlan.model_validate(
        {
            "source": source,
            "plan_id": "historical-plan" if source == "octop_planning" else None,
            "revision": 1,
            "steps": [
                {
                    "step_id": "historical-step" if source == "octop_planning" else None,
                    "content": "An unfinished historical step",
                    "status": "pending",
                }
            ],
        }
    )
    manager = TaskPlanning(plan, "en")
    with pytest.raises(ValueError, match=tr("projects.planning_step_not_current", "en")):
        manager.binding()
    assert manager.plan == plan


def test_restored_completed_plan_without_current_step_stays_readable():
    plan = TaskPlan.model_validate(
        {"revision": 1, "steps": [{"content": "Done", "status": "completed"}]}
    )
    manager = TaskPlanning(plan, "en")
    assert manager.binding() == (None, None)
    assert manager.phase == "completed" and manager.plan == plan
