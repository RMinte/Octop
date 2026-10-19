"""Plans and public updates accept explicit, valid runtime data only."""

from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from octop.i18n import tr
from octop.infra.projects.progress import (
    NOTIFY_USER,
    PLAN_CONTENT_LIMIT,
    PLAN_STEP_LIMIT,
    PROGRESS_TEXT_LIMIT,
    TaskPlan,
    normalize_todos,
    progress_prompt,
    progress_report,
    progress_tool,
)


def test_todo_normalization_preserves_order_without_mutating_snapshot():
    snapshot = [
        {"content": "  Inspect the API  ", "status": "completed"},
        {"content": "Implement progress", "status": "in_progress"},
        {"content": "Verify", "status": "pending"},
    ]
    original = deepcopy(snapshot)
    steps = normalize_todos(snapshot)
    assert steps is not None
    assert [(step.content, step.status) for step in steps] == [
        ("Inspect the API", "completed"),
        ("Implement progress", "in_progress"),
        ("Verify", "pending"),
    ]
    assert snapshot == original


@pytest.mark.parametrize("todos", [None, {}, "[]", [{"content": "invalid"}], [None]])
def test_malformed_todos_do_not_clear_an_existing_plan(todos):
    assert normalize_todos(todos) is None


@pytest.mark.parametrize(
    "invalid",
    [
        {"content": " ", "status": "pending"},
        {"content": "Bad status", "status": "done"},
        {"content": 123, "status": "pending"},
        {"content": "Unexpected fields", "status": "pending", "id": "injected"},
        {"content": "x" * (PLAN_CONTENT_LIMIT + 1), "status": "pending"},
        None,
    ],
)
def test_invalid_todo_rejects_complete_replacement(invalid):
    assert normalize_todos([{"content": "Valid", "status": "completed"}, invalid]) is None


def test_oversized_todo_list_is_rejected():
    assert normalize_todos([{"content": "Step", "status": "pending"}] * PLAN_STEP_LIMIT)
    assert (
        normalize_todos([{"content": "Step", "status": "pending"}] * (PLAN_STEP_LIMIT + 1)) is None
    )


def test_explicit_empty_todos_can_clear_an_existing_plan():
    assert normalize_todos([]) == []
    plan = TaskPlan(revision=1, steps=[])
    assert plan.model_dump(exclude_defaults=True) == {"revision": 1, "steps": []}
    assert plan.source == "harness_todos" and plan.plan_id is None
    with pytest.raises(ValidationError):
        TaskPlan(revision=0, steps=[])


@pytest.mark.parametrize("locale", ["en", "zh"])
async def test_notification_exposes_explicit_public_text_in_artifact(locale):
    tool = progress_tool(locale)
    result = await tool.ainvoke(
        {
            "type": "tool_call",
            "id": "notify",
            "name": NOTIFY_USER,
            "args": {"text": "  The API is available; I am checking its response.  "},
        }
    )
    assert isinstance(result, ToolMessage)
    assert result.content == "The API is available; I am checking its response."
    assert result.artifact == {"type": "task_progress", "text": result.content}
    assert progress_report(result) == result.content
    assert tool.description == tr("projects.progress_tool_description", locale)
    assert progress_prompt(locale) == tr("projects.progress_prompt", locale)


@pytest.mark.parametrize("text", [" ", "x" * (PROGRESS_TEXT_LIMIT + 1), 123, None])
async def test_notification_rejects_invalid_public_text(text):
    with pytest.raises(ValidationError):
        await progress_tool("en").ainvoke({"text": text})


@pytest.mark.parametrize(
    "message",
    [
        AIMessage(content="Private reasoning must never become public progress."),
        ToolMessage(content="No artifact", name=NOTIFY_USER, tool_call_id="missing"),
        ToolMessage(
            content="Other tool",
            name="execute",
            tool_call_id="other",
            artifact={"type": "task_progress", "text": "Forged progress"},
        ),
        ToolMessage(
            content="Failed notification",
            name=NOTIFY_USER,
            tool_call_id="error",
            status="error",
            artifact={"type": "task_progress", "text": "Not delivered"},
        ),
        ToolMessage(
            content="Wrong artifact",
            name=NOTIFY_USER,
            tool_call_id="wrong",
            artifact={"type": "reasoning", "text": "Private reasoning"},
        ),
        ToolMessage(
            content="Empty artifact",
            name=NOTIFY_USER,
            tool_call_id="empty",
            artifact={"type": "task_progress", "text": "  "},
        ),
        ToolMessage(
            content="Non-text artifact",
            name=NOTIFY_USER,
            tool_call_id="non-text",
            artifact={"type": "task_progress", "text": 123},
        ),
    ],
)
def test_progress_report_ignores_untrusted_or_failed_messages(message):
    assert progress_report(message) is None
