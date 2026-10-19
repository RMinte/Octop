"""Official task plans keep stable identities and host-owned lifecycle evidence."""

import asyncio
import json
from copy import deepcopy

import pytest
from langchain_core.messages import ToolMessage

from octop.i18n import tr
from octop.infra.projects.planning import PLANNING, TaskPlanning, plan_report
from octop.infra.projects.progress import (
    PLAN_CONTENT_LIMIT,
    PLAN_STEP_LIMIT,
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


async def created():
    manager = TaskPlanning(None, "en")
    result = await invoke(manager, "create", title="  Ship progress  ", steps=["Inspect", "Verify"])
    assert result.status == "success"
    return manager


@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_create_allocates_stable_ids_and_starts_first_step(locale):
    manager = TaskPlanning(None, locale)
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
    assert manager.binding() == (plan.plan_id, plan.steps[0].step_id)
    assert json.loads(response.content) == response.artifact["plan"]
    assert manager.tools()[0].description == tr("projects.planning_tool_description", locale)


async def test_reorder_and_edit_use_explicit_ids_and_never_match_text():
    manager = await created()
    original = manager.plan
    first, second = original.steps
    response = await invoke(
        manager,
        "update",
        expected_revision=original.revision,
        steps=[
            {"step_id": second.step_id, "content": "Inspect"},
            {"step_id": first.step_id, "content": "Renamed"},
            {"content": "Inspect"},
        ],
    )
    updated = plan_report(response)
    assert updated.revision == 2 and updated.plan_id == original.plan_id
    assert [step.step_id for step in updated.steps[:2]] == [second.step_id, first.step_id]
    assert updated.steps[2].step_id not in {first.step_id, second.step_id}
    assert updated.steps[1].started_at == first.started_at
    assert updated.steps[1].status == "in_progress"
    assert updated.active_step_id == first.step_id
    assert not updated.retired_steps


async def test_removed_steps_remain_in_history_and_cannot_be_reused():
    manager = await created()
    original = manager.plan
    first, second = original.steps
    response = await invoke(
        manager,
        "update",
        expected_revision=original.revision,
        steps=[{"step_id": second.step_id, "content": second.content}],
    )
    updated = plan_report(response)
    assert updated.active_step_id is None and manager.binding() == (None, None)
    assert updated.retired_steps[0].step_id == first.step_id
    assert updated.retired_steps[0].removed_at is not None
    assert updated.retired_steps[0].started_at == first.started_at
    failed = await invoke(
        manager,
        "update",
        expected_revision=updated.revision,
        steps=[{"step_id": first.step_id, "content": first.content}],
    )
    assert failed.status == "error" and manager.plan == updated
    with pytest.raises(ValueError, match=tr("projects.planning_step_unavailable", "en")):
        manager.binding(first.step_id)


async def test_step_results_and_times_follow_explicit_transitions():
    manager = await created()
    first, second = manager.plan.steps
    started = first.started_at
    second_result = await invoke(manager, "start_step", expected_revision=1, step_id=second.step_id)
    running = plan_report(second_result)
    assert running.active_step_id == second.step_id
    assert all(step.status == "in_progress" for step in running.steps)
    done = await invoke(
        manager,
        "mark_step",
        expected_revision=2,
        step_id=second.step_id,
        status="completed",
        result="  Relevant checks passed.  ",
    )
    completed = plan_report(done)
    assert completed.active_step_id is None
    assert completed.steps[0].status == "in_progress" and completed.steps[0].started_at == started
    assert completed.steps[1].result == "Relevant checks passed."
    assert completed.steps[1].finished_at >= completed.steps[1].started_at
    restarted = await invoke(manager, "start_step", expected_revision=3, step_id=second.step_id)
    resumed = plan_report(restarted)
    assert resumed.steps[1].started_at == completed.steps[1].started_at
    assert resumed.steps[1].finished_at is None
    assert resumed.steps[1].status == "in_progress"


@pytest.mark.parametrize("status", ["completed", "blocked", "skipped"])
async def test_terminal_step_requires_result_and_does_not_finish_another_step(status):
    manager = await created()
    first, second = manager.plan.steps
    response = await invoke(
        manager,
        "mark_step",
        expected_revision=1,
        step_id=first.step_id,
        status=status,
        result="Evidence or reason.",
    )
    plan = plan_report(response)
    assert plan.steps[0].status == status and plan.steps[0].finished_at is not None
    assert plan.steps[1] == second and plan.active_step_id is None


async def test_selection_controls_binding_without_changing_other_steps():
    manager = await created()
    first, second = manager.plan.steps
    with pytest.raises(ValueError, match=tr("projects.planning_step_not_running", "en")):
        manager.binding(second.step_id)
    invalid = await invoke(manager, "select_step", expected_revision=1, step_id=second.step_id)
    assert invalid.status == "error" and manager.plan.revision == 1
    cleared = await invoke(manager, "select_step", expected_revision=1, step_id=None)
    assert plan_report(cleared).active_step_id is None
    assert manager.binding() == (None, None)
    assert manager.binding(first.step_id) == (manager.plan.plan_id, first.step_id)
    chosen = await invoke(manager, "select_step", expected_revision=2, step_id=first.step_id)
    assert plan_report(chosen).active_step_id == first.step_id


async def test_empty_update_retires_all_steps_without_selecting_another():
    manager = await created()
    response = await invoke(manager, "update", expected_revision=1, steps=[])
    plan = plan_report(response)
    assert plan.steps == [] and len(plan.retired_steps) == 2
    assert all(step.removed_at is not None for step in plan.retired_steps)
    assert manager.binding() == (None, None)


async def test_historical_plan_is_not_migrated_without_explicit_create():
    historical = TaskPlan(
        revision=7,
        steps=[TaskPlanStep(content="Legacy step", status="in_progress")],
    )
    manager = TaskPlanning(historical, "en")
    assert manager.binding() == (None, None)
    assert plan_report(await invoke(manager, "get")) == historical
    rejected = await invoke(manager, "update", expected_revision=7, steps=[])
    assert rejected.status == "error"
    assert rejected.content == tr("projects.planning_legacy", "en")
    assert manager.plan == historical and manager.plan.steps[0].step_id is None
    replacement = plan_report(await invoke(manager, "create", steps=["New explicit plan"]))
    assert replacement.revision == 8 and replacement.plan_id is not None
    assert replacement.steps[0].step_id is not None
    assert historical.steps[0].step_id is None


async def test_restore_and_read_are_isolated_snapshots():
    manager = await created()
    restored = manager.plan
    resumed = TaskPlanning(restored, "en")
    restored.steps[0].content = "External mutation"
    snapshot = resumed.get()
    snapshot.steps[0].content = "Another external mutation"
    response = await invoke(resumed, "get")
    assert plan_report(response) == manager.plan
    assert resumed.binding() == manager.binding()


async def test_checkpoint_restore_only_accepts_newer_revision():
    manager = await created()
    initial = manager.plan
    resumed = TaskPlanning(initial, "en")
    latest = plan_report(
        await invoke(manager, "start_step", expected_revision=1, step_id=initial.steps[1].step_id)
    )
    resumed.restore(latest)
    resumed.restore(initial)
    assert resumed.plan == latest and resumed.binding() == manager.binding()
    latest.steps[0].content = "External mutation"
    assert resumed.plan == manager.plan


async def test_restore_call_replays_original_create_without_changing_newer_plan():
    manager = TaskPlanning(None, "en")
    arguments = {"title": "  Original plan  ", "steps": [" Inspect ", "Verify"]}
    created_result = await invoke(manager, "create", call_id="create-once", **arguments)
    created_plan = plan_report(created_result)
    await invoke(
        manager,
        "start_step",
        expected_revision=1,
        step_id=created_plan.steps[1].step_id,
    )
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
    assert replayed.status == "success" and replayed.artifact == created_result.artifact
    assert resumed.plan == latest and resumed.plan.revision == 2
    assert resumed.binding() == manager.binding()
    replayed.artifact["plan"]["steps"][0]["content"] = "Mutation in a returned artifact"
    again = await invoke(resumed, "create", call_id="create-once", **arguments)
    assert again.artifact == created_result.artifact


async def test_repeated_call_is_idempotent_and_different_payload_cannot_reuse_id():
    manager = TaskPlanning(None, "en")
    first, duplicate = await asyncio.gather(
        invoke(manager, "create", call_id="same-call", steps=["Inspect", "Verify"]),
        invoke(manager, "create", call_id="same-call", steps=["Inspect", "Verify"]),
    )
    assert first.artifact == duplicate.artifact
    assert manager.plan.revision == 1
    reused = await invoke(manager, "create", call_id="same-call", steps=["Different"])
    assert reused.status == "error" and manager.plan.revision == 1
    assert reused.content == tr("projects.planning_call_reused", "en")


async def test_parallel_mutations_cannot_overwrite_a_newer_revision():
    manager = await created()
    first, second = manager.plan.steps
    responses = await asyncio.gather(
        invoke(
            manager,
            "mark_step",
            expected_revision=1,
            step_id=first.step_id,
            status="completed",
            result="Checks passed.",
        ),
        invoke(manager, "start_step", expected_revision=1, step_id=second.step_id),
    )
    assert sorted(response.status for response in responses) == ["error", "success"]
    assert manager.plan.revision == 2
    rejected = next(response for response in responses if response.status == "error")
    assert rejected.content == tr("projects.planning_conflict", "en")


@pytest.mark.parametrize(
    "command,arguments",
    [
        ("create", {"steps": []}),
        ("create", {"steps": [" "]}),
        ("create", {"steps": ["x" * (PLAN_CONTENT_LIMIT + 1)]}),
        ("create", {"steps": ["Step"] * (PLAN_STEP_LIMIT + 1)}),
        ("create", {"steps": [{"content": "An update is not a create"}]}),
        ("update", {"steps": ["A create is not an update"]}),
        ("mark_step", {"status": "completed"}),
        ("mark_step", {"status": "completed", "result": " "}),
        ("mark_step", {"status": "completed", "result": "x" * (PROGRESS_TEXT_LIMIT + 1)}),
    ],
)
async def test_invalid_commands_do_not_mutate_plan(command, arguments):
    manager = await created()
    original = manager.plan
    arguments = {"expected_revision": 1, "step_id": original.steps[0].step_id, **arguments}
    result = await invoke(manager, command, **arguments)
    assert result.status == "error"
    assert manager.plan == original


async def test_unknown_and_duplicate_ids_are_atomic_errors():
    manager = await created()
    first = manager.plan.steps[0]
    original = manager.plan
    for updates in (
        [{"content": "New"}, {"step_id": "unknown", "content": "Invalid"}],
        [
            {"step_id": first.step_id, "content": first.content},
            {"step_id": first.step_id, "content": "Duplicate"},
        ],
    ):
        response = await invoke(manager, "update", expected_revision=1, steps=updates)
        assert response.status == "error" and manager.plan == original


async def test_plan_report_requires_successful_planning_artifact():
    manager = await created()
    response = await invoke(manager, "get")
    assert plan_report(response) == manager.plan
    invalid_artifact = deepcopy(response.artifact)
    invalid_artifact["plan"]["active_step_id"] = "missing"
    for message in (
        ToolMessage(content="raw plan text", tool_call_id="raw", name=PLANNING),
        ToolMessage(
            content="Other tool", tool_call_id="other", name="execute", artifact=response.artifact
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
