"""Task usage keeps immutable receipts and only links accepted, unambiguous submissions."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.model_calls import ModelCallRepo, ModelCallRow


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "task-model-call-usage.db")
    run_migrations(pool)
    with pool.transaction() as conn:
        for owner_id in (1, 2):
            conn.execute(
                "INSERT INTO users(id,username,password_hash,role,created_at) "
                "VALUES(?,?,'x','user',1)",
                (owner_id, f"owner-{owner_id}"),
            )
    try:
        yield pool
    finally:
        pool.close()


def project(db: SqlitePool, project_id: str, owner_id: int = 1) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) "
            "VALUES(?,?,?,1,1)",
            (project_id, owner_id, f"Project {project_id}"),
        )


def task(db: SqlitePool, task_id: str, *, owner_id: int = 1, project_id: str | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,project_id,title,request_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,1)",
            (task_id, owner_id, project_id, f"Task {task_id}", f"create-{task_id}"),
        )


def turn(db: SqlitePool, task_id: str, request_id: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at) "
            "VALUES(?,?,?,'Prompt','queued','{}','fixture/model',1,1)",
            (f"{task_id}-{request_id}", task_id, request_id),
        )


def call(repo: ModelCallRepo, call_id: str, **updates: Any) -> ModelCallRow:
    row = replace(
        ModelCallRow(call_id=call_id, started_at=1_000, user_id=1, source="box"), **updates
    )
    return repo.start(row)


def test_submission_matches_only_accepted_turns_for_the_call_owner(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    for owner_id in (1, 2):
        project(db, f"project-{owner_id}", owner_id)
        task(db, f"task-{owner_id}", owner_id=owner_id, project_id=f"project-{owner_id}")
        turn(db, f"task-{owner_id}", "same-request")
    first = call(repo, "first", submission_id="same-request")
    second = call(repo, "second", user_id=2, submission_id="same-request", source="third_party")
    no_owner = call(repo, "no-owner", user_id=None, submission_id="same-request")
    not_accepted = call(repo, "not-accepted", submission_id="create-task-1")
    rejected = call(repo, "rejected", submission_id="rejected-request", project_id="project-1")
    no_submission = call(repo, "no-submission")

    first_usage = repo.list_task_usage(user_id=1, range_start=1_000, range_end=2_000)
    assert [item.row for item in first_usage] == [first, not_accepted, rejected, no_submission]
    assert first_usage[0].row == first
    assert first_usage[0].task_id == "task-1"
    assert first_usage[0].task_title == "Task task-1"
    assert first_usage[0].project_id == "project-1"
    assert first_usage[0].project_name == "Project project-1"
    assert first_usage[0].attribution == "submission"
    assert first_usage[0].row.task_id == ""
    assert [item.attribution for item in first_usage[1:]] == ["unassigned"] * 3
    assert [item.task_id for item in first_usage[1:]] == [""] * 3
    assert [item.task_title for item in first_usage[1:]] == [""] * 3
    assert first_usage[2].project_id == "project-1"
    assert first_usage[2].project_name == ""
    assert [
        item.row
        for item in repo.list_task_usage(
            user_id=1, range_start=1_000, range_end=2_000, project_id="project-1"
        )
    ] == [first]
    second_usage = repo.list_task_usage(user_id=2, range_start=1_000, range_end=2_000)
    assert [item.row for item in second_usage] == [second]
    assert [item.task_id for item in second_usage] == ["task-2"]
    assert second_usage[0].row.source == "third_party"
    assert [
        item.row for item in repo.list_task_usage(user_id=None, range_start=1_000, range_end=2_000)
    ] == [first, second, no_owner, not_accepted, rejected, no_submission]


def test_direct_task_and_project_snapshots_win_over_submission_and_current_project(
    db: SqlitePool,
) -> None:
    repo = ModelCallRepo(db)
    project(db, "original")
    project(db, "current")
    task(db, "direct-task", project_id="current")
    task(db, "submission-task")
    turn(db, "submission-task", "request")
    snapshot = call(
        repo,
        "snapshot",
        task_id="direct-task",
        project_id="original",
        submission_id="request",
    )
    fallback = call(repo, "fallback", task_id="direct-task", submission_id="request")

    usage = repo.list_task_usage(user_id=1, range_start=0, range_end=2_000)
    assert [item.row for item in usage] == [snapshot, fallback]
    assert [item.task_id for item in usage] == ["direct-task", "direct-task"]
    assert [item.attribution for item in usage] == ["direct", "direct"]
    assert [item.project_id for item in usage] == ["original", "current"]
    assert [item.project_name for item in usage] == ["Project original", "Project current"]
    assert (
        repo.list_task_usage(user_id=1, range_start=0, range_end=2_000, task_id="submission-task")
        == []
    )
    assert [
        item.row
        for item in repo.list_task_usage(
            user_id=1, range_start=0, range_end=2_000, project_id="original"
        )
    ] == [snapshot]
    assert [
        item.row
        for item in repo.list_task_usage(
            user_id=1, range_start=0, range_end=2_000, project_id="current"
        )
    ] == [fallback]
    assert repo.get(fallback.call_id) == fallback


def test_deleted_resources_keep_direct_receipt_ids_and_unknown_names(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    project(db, "deleted-project")
    task(db, "deleted-task", project_id="deleted-project")
    snapshot = call(repo, "snapshot", task_id="deleted-task", project_id="deleted-project")
    no_snapshot = call(repo, "no-snapshot", task_id="deleted-task")
    with db.transaction() as conn:
        conn.execute("DELETE FROM project_tasks WHERE task_id='deleted-task'")
        conn.execute("DELETE FROM projects WHERE project_id='deleted-project'")

    usage = repo.list_task_usage(user_id=1, range_start=0, range_end=2_000)
    assert [item.row for item in usage] == [snapshot, no_snapshot]
    assert [item.task_id for item in usage] == ["deleted-task", "deleted-task"]
    assert [item.task_title for item in usage] == ["", ""]
    assert [item.project_id for item in usage] == ["deleted-project", ""]
    assert [item.project_name for item in usage] == ["", ""]
    assert [
        item.row
        for item in repo.list_task_usage(user_id=1, range_start=0, range_end=2_000, project_id="")
    ] == [no_snapshot]


def test_ambiguous_submission_is_not_resolved_by_task_or_project_filter(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    project(db, "project")
    task(db, "first", project_id="project")
    task(db, "second")
    turn(db, "first", "ambiguous")
    turn(db, "second", "ambiguous")
    ambiguous = call(repo, "ambiguous", submission_id="ambiguous")

    usage = repo.list_task_usage(user_id=1, range_start=0, range_end=2_000)
    assert [item.row for item in usage] == [ambiguous]
    assert usage[0].task_id == ""
    assert usage[0].attribution == "unassigned"
    assert repo.list_task_usage(user_id=1, range_start=0, range_end=2_000, task_id="first") == []
    assert (
        repo.list_task_usage(user_id=1, range_start=0, range_end=2_000, project_id="project") == []
    )


def test_direct_and_submission_links_return_each_call_once_with_effective_filters(
    db: SqlitePool,
) -> None:
    repo = ModelCallRepo(db)
    task(db, "accepted")
    turn(db, "accepted", "submission")
    first = call(repo, "routing", submission_id="submission")
    second = call(repo, "execution", task_id="accepted", submission_id="submission")
    third = call(repo, "review", task_id="accepted", submission_id="submission")

    usage = repo.list_task_usage(
        user_id=1, range_start=0, range_end=2_000, task_id="accepted", project_id=""
    )
    assert [item.row for item in usage] == [first, second, third]
    assert [item.attribution for item in usage] == ["submission", "direct", "direct"]
    assert repo.list_task_usage(user_id=1, range_start=0, range_end=2_000, task_id="") == []


def test_millisecond_window_uses_call_start_and_keeps_cancelled_unknown_usage(
    db: SqlitePool,
) -> None:
    repo = ModelCallRepo(db)
    call(repo, "before", task_id="task", started_at=999)
    start = call(repo, "start", task_id="task", started_at=1_000)
    last = call(repo, "last", task_id="task", started_at=1_999)
    cancelled = repo.finish(replace(last, status="cancelled", finished_at=3_000))
    call(repo, "end", task_id="task", started_at=2_000)

    usage = repo.list_task_usage(user_id=1, range_start=1_000, range_end=2_000)
    assert [item.row for item in usage] == [start, cancelled]
    assert cancelled.usage_json == last.usage_json
    assert repo.list_task_usage(user_id=1, range_start=2_000, range_end=2_000) == []


def test_resource_names_and_project_fallback_never_cross_the_call_owner(db: SqlitePool) -> None:
    repo = ModelCallRepo(db)
    project(db, "private-project", owner_id=2)
    task(db, "private-task", owner_id=2, project_id="private-project")
    turn(db, "private-task", "private-submission")
    unbound = call(repo, "unbound", submission_id="private-submission")
    direct = call(repo, "direct", task_id="private-task")
    snapshot = call(repo, "snapshot", task_id="private-task", project_id="private-project")

    usage = repo.list_task_usage(user_id=1, range_start=0, range_end=2_000)
    assert [item.row for item in usage] == [unbound, direct, snapshot]
    assert [item.task_id for item in usage] == ["", "private-task", "private-task"]
    assert [item.task_title for item in usage] == ["", "", ""]
    assert [item.project_id for item in usage] == ["", "", "private-project"]
    assert [item.project_name for item in usage] == ["", "", ""]
    assert repo.list_task_usage(user_id=2, range_start=0, range_end=2_000) == []
