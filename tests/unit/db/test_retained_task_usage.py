"""Read retained task journals without truncating or adding invocation receipts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.model_calls import ModelCallRepo, ModelCallRow
from octop.infra.db.repos.usage import RetainedTaskUsage, RetainedTaskUsageEntry, UsageRepo
from octop.infra.db.repos.users import UserRepo


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "retained-task-usage.db")
    run_migrations(pool)
    yield pool
    pool.close()


def _owner(db: SqlitePool, name: str = "owner") -> int:
    return UserRepo(db).create(username=name, password_hash="x", role="user")


def _provider(db: SqlitePool, name: str, kind: str, *, created_at: int = 100) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO providers(name,kind,created_at,updated_at) VALUES(?,?,?,?)",
            (name, kind, created_at, created_at),
        )


def _task(
    db: SqlitePool,
    owner: int,
    task_id: str,
    *,
    ts: int = 200,
    project_id: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 10,
    archived: bool = False,
) -> None:
    with db.transaction() as conn:
        if project_id is not None:
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) "
                "VALUES(?,?,?,0,0) ON CONFLICT(project_id) DO NOTHING",
                (project_id, owner, "Project " + project_id),
            )
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,project_id,title,request_id,"
            "created_at,updated_at,archived) VALUES(?,?,?,'Same title',?,?,?,?)",
            (task_id, owner, project_id, "request-" + task_id, ts * 1000, ts * 1000, int(archived)),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at,input_tokens,output_tokens) "
            "VALUES(?,?,?,'Hello','completed','{}','provider/model',?,?,?,?)",
            (
                "turn-" + task_id,
                task_id,
                "request-" + task_id,
                ts * 1000,
                ts * 1000,
                input_tokens,
                output_tokens,
            ),
        )


def test_retained_tasks_isolate_users_and_exclude_expert_and_call_ledger(db: SqlitePool) -> None:
    alice, bob = _owner(db, "alice"), _owner(db, "bob")
    _task(db, alice, "alice-task")
    _task(db, bob, "bob-task", input_tokens=500)
    AgentRepo(db).create(agent_id="expert", user_id=alice, name="Expert")
    repo = UsageRepo(db)
    repo.record(agent_id="expert", user_id=alice, input_tokens=800, output_tokens=90, ts=200)
    ModelCallRepo(db).start(
        ModelCallRow(
            call_id="new-ledger-call",
            started_at=200_000,
            user_id=alice,
            task_id="alice-task",
            usage_json=json.dumps({"input_tokens": 1000, "output_tokens": 200}),
        )
    )

    mine = repo.list_retained_tasks(user_id=alice, range_start=100, range_end=300)

    assert mine == [RetainedTaskUsage("alice-task", "Same title", "", "", 100, 10, 110, 1, 1, 200)]
    assert (
        repo.list_retained_tasks(user_id=alice, range_start=100, range_end=300, task_id="bob-task")
        == []
    )
    assert {
        row.task_id
        for row in repo.list_retained_tasks(user_id=None, range_start=100, range_end=300)
    } == {"alice-task", "bob-task"}


def test_retained_title_and_review_tokens_do_not_add_turns(db: SqlitePool) -> None:
    owner = _owner(db)
    _task(db, owner, "task", project_id="project", archived=True)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at,input_tokens,output_tokens) "
            "VALUES('second','task','followup','Hello','completed','{}','provider/model',"
            "210000,210000,20,2)"
        )
        conn.execute(
            "INSERT INTO project_task_titles(task_id,status,input_tokens,output_tokens) "
            "VALUES('task','completed',11,3)"
        )
        conn.execute(
            "INSERT INTO project_task_reviews(review_id,task_id,turn_id,owner_id,"
            "execution_host,target_id,action_digest,proposal_json,model_ref,"
            "created_at,updated_at,expires_at,input_tokens,output_tokens) "
            "VALUES('review','task','turn-task',?,'server','target','digest','{}',"
            "'provider/model',220000,220000,300000,17,5)",
            (owner,),
        )

    rows = UsageRepo(db).list_retained_tasks(user_id=owner, range_start=100, range_end=300)

    assert rows == [
        RetainedTaskUsage(
            "task", "Same title", "project", "Project project", 148, 20, 168, 2, 4, 220
        )
    ]
    entries = UsageRepo(db).list_retained_task_entries(
        user_id=owner, range_start=100, range_end=300
    )
    assert len(entries) == 4
    assert sum(entry.total_tokens for entry in entries) == 168
    assert all(entry.provider_kind is None for entry in entries)


def test_retained_tasks_do_not_use_the_summary_bucket_cap(db: SqlitePool) -> None:
    owner = _owner(db)
    for index in range(105):
        _task(db, owner, f"task-{index:03}")

    rows = UsageRepo(db).list_retained_tasks(user_id=owner, range_start=100, range_end=300)

    assert len(rows) == 105
    assert len({row.task_id for row in rows}) == 105
    assert sum(row.total_tokens for row in rows) == 105 * 110


def test_retained_tasks_respect_half_open_seconds_and_project_filters(db: SqlitePool) -> None:
    owner = _owner(db)
    _task(db, owner, "before", ts=199, project_id="project")
    _task(db, owner, "at-start", ts=200, project_id="project", input_tokens=0)
    _task(db, owner, "no-project", ts=299)
    _task(db, owner, "at-end", ts=300, project_id="project")
    _task(db, owner, "unmeasured", ts=250, input_tokens=0, output_tokens=0)
    repo = UsageRepo(db)

    rows = repo.list_retained_tasks(user_id=owner, range_start=200, range_end=300)

    assert {row.task_id for row in rows} == {"at-start", "no-project"}
    assert [row.input_tokens for row in rows if row.task_id == "at-start"] == [0]
    assert [
        row.task_id
        for row in repo.list_retained_tasks(
            user_id=owner, range_start=200, range_end=300, project_id="project"
        )
    ] == ["at-start"]
    assert [
        row.task_id
        for row in repo.list_retained_tasks(
            user_id=owner, range_start=200, range_end=300, project_id=""
        )
    ] == ["no-project"]


def test_retained_entries_follow_each_record_model_when_task_changes_provider(
    db: SqlitePool,
) -> None:
    owner = _owner(db)
    _provider(db, "Appliance", "box")
    _provider(db, "External", "openai")
    _task(db, owner, "task")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET model_ref='Appliance/model' WHERE task_id='task'"
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at,input_tokens,output_tokens) "
            "VALUES('second','task','followup','Hello','completed','{}','External/model',"
            "210000,210000,50,10)"
        )
        conn.execute(
            "INSERT INTO project_task_titles(task_id,status,input_tokens,output_tokens) "
            "VALUES('task','completed',2,3)"
        )
        conn.execute(
            "INSERT INTO project_task_reviews(review_id,task_id,turn_id,owner_id,"
            "execution_host,target_id,action_digest,proposal_json,model_ref,"
            "created_at,updated_at,expires_at,input_tokens,output_tokens) "
            "VALUES('review','task','turn-task',?,'server','target','digest','{}',"
            "'External/model',220000,220000,300000,8,1)",
            (owner,),
        )

    entries = UsageRepo(db).list_retained_task_entries(
        user_id=owner, range_start=100, range_end=300
    )

    assert entries == [
        RetainedTaskUsageEntry("task", "Appliance/model", 100, 10, 110, 200, "box"),
        RetainedTaskUsageEntry("task", "Appliance/model", 2, 3, 5, 200, "box"),
        RetainedTaskUsageEntry("task", "External/model", 50, 10, 60, 210, "openai"),
        RetainedTaskUsageEntry("task", "External/model", 8, 1, 9, 220, "openai"),
    ]


@pytest.mark.parametrize(
    "case", ["deleted", "ambiguous", "created_later", "empty_model", "missing"]
)
def test_retained_entries_keep_tokens_when_provider_evidence_is_unknown(
    db: SqlitePool, case: str
) -> None:
    owner = _owner(db)
    _task(db, owner, "task")
    model_ref = "Provider/model"
    if case == "deleted":
        _provider(db, "Provider", "box")
        with db.transaction() as conn:
            conn.execute("DELETE FROM providers WHERE name='Provider'")
    elif case == "ambiguous":
        _provider(db, "Provider", "box")
        _provider(db, "Provider/model", "openai")
        model_ref = "Provider/model/nested"
    elif case == "created_later":
        _provider(db, "Provider", "box", created_at=201)
    elif case == "empty_model":
        _provider(db, "Provider", "box")
        model_ref = "Provider/"
    with db.transaction() as conn:
        conn.execute("UPDATE project_task_turns SET model_ref=? WHERE task_id='task'", (model_ref,))

    entries = UsageRepo(db).list_retained_task_entries(
        user_id=owner, range_start=100, range_end=300
    )

    assert entries == [RetainedTaskUsageEntry("task", model_ref, 100, 10, 110, 200, None)]


def test_retained_entries_keep_user_resource_and_range_filters(db: SqlitePool) -> None:
    alice, bob = _owner(db, "alice"), _owner(db, "bob")
    _provider(db, "provider", "openai")
    _task(db, alice, "mine", project_id="project", ts=200)
    _task(db, alice, "unassigned", ts=299)
    _task(db, alice, "at-end", project_id="project", ts=300)
    _task(db, bob, "theirs", ts=200)
    repo = UsageRepo(db)

    assert [
        entry.task_id
        for entry in repo.list_retained_task_entries(
            user_id=alice, range_start=200, range_end=300, project_id="project"
        )
    ] == ["mine"]
    assert [
        entry.task_id
        for entry in repo.list_retained_task_entries(
            user_id=alice, range_start=200, range_end=300, project_id="", task_id="unassigned"
        )
    ] == ["unassigned"]
    assert (
        repo.list_retained_task_entries(
            user_id=alice, range_start=200, range_end=300, task_id="theirs"
        )
        == []
    )


def test_retained_bundle_totals_and_entries_share_one_select(db: SqlitePool) -> None:
    owner = _owner(db)
    _task(db, owner, "task-a", input_tokens=10, output_tokens=1)
    _task(db, owner, "task-b", input_tokens=20, output_tokens=2)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at,input_tokens,output_tokens) "
            "VALUES('second','task-a','followup','Hello','completed','{}','provider/model',"
            "210000,210000,30,3)"
        )
        conn.execute(
            "INSERT INTO project_task_titles(task_id,status,input_tokens,output_tokens) "
            "VALUES('task-a','completed',4,0)"
        )
    statements: list[str] = []
    with db.connect() as conn:
        conn.set_trace_callback(statements.append)
        try:
            totals, entries = UsageRepo(db).read_retained_task_usage(
                user_id=owner, range_start=100, range_end=300
            )
        finally:
            conn.set_trace_callback(None)

    assert len(statements) == 1
    assert [total.task_id for total in totals] == ["task-a", "task-b"]
    assert len(entries) == 4
    for total in totals:
        task_entries = [entry for entry in entries if entry.task_id == total.task_id]
        assert total.usage_records == len(task_entries)
        assert total.input_tokens == sum(entry.input_tokens for entry in task_entries)
        assert total.output_tokens == sum(entry.output_tokens for entry in task_entries)
        assert total.total_tokens == sum(entry.total_tokens for entry in task_entries)
        assert total.last_usage_at == max(entry.ts for entry in task_entries)
    assert [total.turns for total in totals] == [2, 1]
