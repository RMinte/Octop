"""Persistent identity choices, list scopes and configuration concurrency."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.project_sync import ProjectSyncRepo, all_task_scope, task_scope
from octop.infra.db.repos.project_tasks import ProjectTaskRepo, ProjectTaskRow


@pytest.fixture
def task_db(tmp_path):
    db = SqlitePool(tmp_path / "unified.db")
    migrate.run_migrations(db)
    with db.transaction() as conn:
        owner = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) "
            "VALUES('task-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        other = conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) "
            "VALUES('other-owner','x','user',1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) "
            "VALUES('project',?,'Project',1,1)",
            (owner,),
        )
    agents = AgentRepo(db)
    agents.create(agent_id="expert", user_id=owner, name="Expert")
    try:
        yield db, ProjectTaskRepo(db), agents, owner, other
    finally:
        db.close()


def task(owner, task_id="task", **changes):
    return replace(ProjectTaskRow(task_id, owner, None, "Task", task_id, None, 1, 1), **changes)


def test_defaults_and_expert_foreign_key_preserve_task(task_db):
    db, repo, agents, owner, _other = task_db
    with db.transaction() as conn:
        repo.insert(conn, task(owner))
        repo.insert(conn, task(owner, "specialized", selected_expert_id="expert"))
    row = repo.get("task", owner)
    assert (row.selected_expert_id, row.configuration_revision, row.action_mode, row.pinned) == (
        None,
        1,
        "craft",
        False,
    )
    with pytest.raises(sqlite3.IntegrityError):
        agents.delete("expert")
    assert repo.get("specialized", owner) is not None
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        repo.insert(conn, task(owner, "invalid", selected_expert_id="missing"))


def test_cli_delete_preserves_referenced_expert_workspace(task_db, monkeypatch, tmp_path):
    from octop.cli.support import offline_ops
    from octop.infra.errors import ErrorCode, OctopError
    from octop.infra.utils.paths import PathLayout

    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    db, repo, agents, owner, _ = task_db
    workspace = tmp_path / "expert-workspace"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("Preserve persona", encoding="utf-8")
    agents.update_config("expert", config_json=json.dumps({"workspace_dir": str(workspace)}))
    with db.transaction() as conn:
        repo.insert(conn, task(owner, selected_expert_id="expert"))
    services = SimpleNamespace(agent_repo=agents, paths=PathLayout(tmp_path))
    monkeypatch.setattr(offline_ops, "open_cli_services", lambda home: nullcontext(services))
    with pytest.raises(OctopError) as error:
        offline_ops.delete_agent_offline("expert", home=tmp_path)
    assert error.value.code == ErrorCode.EXPERT_IN_USE
    assert agents.get("expert") is not None
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == "Preserve persona"


def test_idle_configuration_compare_and_swap_and_clear_expert(task_db):
    db, repo, _agents, owner, _other = task_db
    with db.transaction() as conn:
        repo.insert(conn, task(owner))
        original = repo.get_in_transaction(conn, "task", owner)
        assert repo.update_configuration(conn, original, 1) == original
        assert repo.configuration_events(conn, "task") == []
        changed = repo.update_configuration(
            conn, replace(original, selected_expert_id="expert", action_mode="ask", updated_at=2), 1
        )
        assert (changed.configuration_revision, changed.list_revision) == (2, 2)
        assert repo.update_configuration(conn, original, 1) is None
        assert repo.update_configuration(conn, replace(changed, list_revision=1), 2) is None
        cleared = repo.update_configuration(
            conn, replace(changed, selected_expert_id=None, updated_at=3), 2
        )
        assert cleared.selected_expert_id is None and cleared.configuration_revision == 3
        events = repo.configuration_events(conn, "task")
        assert [event.configuration_revision for event in events] == [2, 3]
        assert (events[0].previous_expert_id, events[0].selected_expert_id) == (None, "expert")
        assert events[0].selected_expert_name == "Expert"
        assert (events[0].previous_action_mode, events[0].action_mode) == ("craft", "ask")
        assert (events[1].previous_expert_id, events[1].selected_expert_id) == ("expert", None)


def test_active_turn_blocks_configuration_even_while_waiting(task_db):
    db, repo, _agents, owner, _other = task_db
    with db.transaction() as conn:
        repo.insert(
            conn, task(owner, active_turn_id="turn", status="running", wait_kind="approval")
        )
        row = repo.get_in_transaction(conn, "task", owner)
        assert repo.update_configuration(conn, replace(row, selected_expert_id="expert"), 1) is None
        assert repo.configuration_events(conn, "task") == []


def test_expert_delete_preserves_current_and_historical_task_references(task_db):
    db, repo, agents, owner, _other = task_db
    with db.transaction() as conn:
        repo.insert(conn, task(owner, selected_expert_id="expert"))
    assert not agents.delete_if_unreferenced("expert")
    with db.transaction() as conn:
        row = repo.get_in_transaction(conn, "task", owner)
        assert repo.update_configuration(conn, replace(row, selected_expert_id=None), 1)
    assert not agents.delete_if_unreferenced("expert")
    assert agents.get("expert") is not None
    with db.transaction() as conn:
        repo.delete(conn, repo.get_in_transaction(conn, "task", owner))
    assert agents.delete_if_unreferenced("expert")
    assert agents.get("expert") is None
    assert agents.delete_if_unreferenced("expert")


@pytest.mark.parametrize(
    "configuration,blocks",
    [
        ({"expert_profile": {"expert_id": "expert", "name": "Historical expert"}}, []),
        (
            {"legacy_attachments": [{"source_agent_id": "expert", "workspace_path": "source.txt"}]},
            [],
        ),
        ({"expert_profile": {"team": {"members": [{"expert_id": "expert"}]}}}, []),
        ({}, [{"type": "text", "speaker_agent_id": "expert"}]),
    ],
)
def test_expert_delete_preserves_snapshot_and_source_file_references(
    task_db, configuration, blocks
):
    db, repo, agents, owner, _other = task_db
    with db.transaction() as conn:
        repo.insert(conn, task(owner))
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,content_blocks_json,model_ref,created_at,updated_at) "
            "VALUES('history-turn','task','history-request','History','completed',?,?,'',1,1)",
            (json.dumps(configuration), json.dumps(blocks)),
        )
    assert not agents.delete_if_unreferenced("expert")
    with db.transaction() as conn:
        repo.delete(conn, repo.get_in_transaction(conn, "task", owner))
    assert agents.delete_if_unreferenced("expert")


@pytest.mark.parametrize(
    "wait_kind,public_status",
    [
        ("approval", "waiting_approval"),
        ("answer", "waiting_answer"),
        ("client_tool", "waiting_client_tool"),
        ("user", "paused"),
        ("interrupted", "interrupted"),
    ],
)
def test_status_filter_matches_public_wait_status(task_db, wait_kind, public_status):
    db, repo, _agents, owner, _other = task_db
    with db.transaction() as conn:
        repo.insert(conn, task(owner, status="running"))
        conn.execute("UPDATE project_tasks SET wait_kind=? WHERE task_id='task'", (wait_kind,))
        assert len(repo.page(conn, owner, None, 10, status=public_status)) == 1
        assert repo.page(conn, owner, None, 10, status="running") == []


def test_global_scope_filters_pin_order_and_stable_cursor(task_db):
    db, repo, _agents, owner, other = task_db
    rows = [
        task(owner, "standalone", title="Quarterly_report", updated_at=6),
        task(
            owner, "project-task", project_id="project", selected_expert_id="expert", updated_at=8
        ),
        task(owner, "pinned", pinned=True, updated_at=2),
        task(owner, "failed", status="failed", title="Quarterly report", updated_at=4),
        task(owner, "archived", archived=True, updated_at=9),
        task(other, "not-owned", pinned=True, updated_at=9),
    ]
    with db.transaction() as conn:
        for row in rows:
            repo.insert(conn, row)
        assert [row.task_id for row in repo.page(conn, owner, None, 20)] == [
            "pinned",
            "standalone",
            "failed",
        ]
        assert [row.task_id for row in repo.page(conn, owner, "project", 20)] == ["project-task"]
        all_rows = repo.page(conn, owner, None, 20, scope="all")
        assert [row.task_id for row in all_rows] == [
            "pinned",
            "project-task",
            "standalone",
            "failed",
        ]
        boundary = (int(all_rows[0].pinned), all_rows[0].updated_at, all_rows[0].task_id)
        assert repo.page(conn, owner, None, 20, boundary, scope="all") == all_rows[1:]
        assert repo.page(conn, owner, None, 20, (2, "pinned"), scope="all") == all_rows[1:]
        assert [
            row.task_id
            for row in repo.page(conn, owner, None, 20, scope="all", selected_expert_id="expert")
        ] == ["project-task"]
        assert len(repo.page(conn, owner, None, 20, scope="all", selected_expert_id=None)) == 3
        assert [
            row.task_id for row in repo.page(conn, owner, None, 20, scope="all", status="failed")
        ] == ["failed"]
        assert [
            row.task_id for row in repo.page(conn, owner, None, 20, scope="all", query="quarterly_")
        ] == ["standalone"]
        assert [
            row.task_id for row in repo.page(conn, owner, None, 20, scope="all", archived=True)
        ] == ["archived"]


def test_global_incremental_stream_includes_project_changes_and_deletes(task_db):
    db, repo, _agents, owner, _other = task_db
    with db.transaction() as conn:
        repo.insert(conn, task(owner))
        repo.insert(conn, task(owner, "project-task", project_id="project"))
        before = ProjectSyncRepo.head(conn, owner, all_task_scope())
        assert before == 2
        assert ProjectSyncRepo.head(conn, owner, task_scope(None)) == 1
        assert ProjectSyncRepo.head(conn, owner, task_scope("project")) == 1
        row = repo.get_in_transaction(conn, "project-task", owner)
        updated = repo.update_metadata(conn, replace(row, pinned=True, updated_at=9), 1)
        assert updated.pinned and updated.configuration_revision == 1
        repo.delete(conn, updated)
        changes = ProjectSyncRepo.changes(
            conn,
            owner,
            all_task_scope(),
            before,
            ProjectSyncRepo.head(conn, owner, all_task_scope()),
            "",
            10,
        )
        assert len(changes) == 1 and changes[0].entity_id == "project-task"
        assert json.loads(changes[0].payload_json)["deleted"] is True


def test_agent_configuration_revision_ignores_runtime_status(task_db):
    db, _repo, agents, owner, _other = task_db
    assert agents.get("expert").config_revision == 1
    agents.set_state("expert", "running")
    assert agents.get("expert").config_revision == 1
    agents.update_config("expert", system_prompt="New persona", mcp_servers='["docs"]')
    assert agents.get("expert").config_revision == 2
    agents.update_config("expert")
    agents.set_state("expert", "stopped", error="retained runtime failure")
    assert agents.get("expert").config_revision == 2
    with db.transaction() as conn:
        assert agents.update_identity_prompt_in_transaction(
            conn, agent_id="expert", user_id=owner, system_prompt="Updated persona", now=5
        )
    assert agents.get("expert").config_revision == 3


@pytest.mark.parametrize("direct_sql", [False, True])
def test_upgrade_from_55_preserves_existing_data(tmp_path, monkeypatch, direct_sql):
    old_migrations = tmp_path / "v55"
    old_migrations.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 55:
            shutil.copy2(source, old_migrations / source.name)
    db = SqlitePool(tmp_path / "upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old_migrations)
            migrate.run_migrations(db)
        with db.transaction() as conn:
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 55
            owner = conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) "
                "VALUES('old-owner','x','user',1) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) "
                "VALUES('old-expert',?,'Expert',1,1)",
                (owner,),
            )
            conn.execute(
                "INSERT INTO project_tasks(task_id,owner_id,title,request_id,created_at,updated_at) "
                "VALUES('old-task',?,'Preserved task','old-request',1,2)",
                (owner,),
            )
            before = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
        if direct_sql:
            with db.connect() as conn:
                conn.executescript(
                    (migrate._MIGRATIONS_DIR / "056_unified_task_conversations.sql").read_text(
                        "utf-8"
                    )
                )
        migrate.run_migrations(db)
        migrate.run_migrations(db)
        with db.connect() as conn:
            after = dict(conn.execute("SELECT * FROM project_tasks").fetchone())
            assert {key: after[key] for key in before} == before
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 58
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert AgentRepo(db).get("old-expert").config_revision == 1
        assert ProjectTaskRepo(db).get("old-task", owner).selected_expert_id is None
    finally:
        db.close()
