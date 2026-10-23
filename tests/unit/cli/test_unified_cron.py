"""Modern cron CLI selection does not require or create a legacy expert thread."""

# ruff: noqa: F811
from __future__ import annotations

import json
from contextlib import nullcontext

import pytest
from click.testing import CliRunner

from octop.cli.main import cli
from octop.infra.cron.configuration import validate_task_selection
from octop.infra.errors import OctopError
from tests.unit.projects.test_expert_execution import services  # noqa: F401


@pytest.fixture
def local_cli(services, monkeypatch, tmp_path):
    from octop.cli.support import db, offline_ops

    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    monkeypatch.delenv("OCTOP_AGENT", raising=False)
    monkeypatch.setattr(db, "open_cli_services", lambda _home=None: nullcontext(services))
    monkeypatch.setattr(offline_ops, "open_cli_services", lambda _home=None: nullcontext(services))
    return CliRunner()


def test_general_cron_create_list_delete_without_expert_or_empty_task(services, local_cli):
    result = local_cli.invoke(
        cli,
        [
            "--json",
            "--user",
            "owner",
            "cron",
            "create",
            "--trigger",
            "interval:3600",
            "--prompt",
            "Report",
            "--no-expert",
        ],
    )
    assert result.exit_code == 0, result.output
    created = json.loads(result.output)
    row = services.cron_repo.get(created["id"])
    assert row.agent_id is None and row.selected_expert_id is None
    assert row.conversation_policy == "per_job" and row.session_key == "cron:" + row.cron_id
    listed = local_cli.invoke(cli, ["--json", "--user", "owner", "cron", "list"])
    assert listed.exit_code == 0 and json.loads(listed.output)[0]["id"] == row.cron_id
    other = local_cli.invoke(cli, ["--json", "--user", "another", "cron", "list"])
    assert other.exit_code == 0 and json.loads(other.output) == []
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 0
    denied = local_cli.invoke(cli, ["--user", "another", "cron", "delete", row.cron_id])
    assert denied.exit_code == 1 and services.cron_repo.get(row.cron_id) is not None
    deleted = local_cli.invoke(cli, ["--user", "owner", "cron", "delete", row.cron_id])
    assert deleted.exit_code == 0 and services.cron_repo.get(row.cron_id) is None


def test_owned_and_shared_expert_selection_and_fresh_compatibility(services, local_cli):
    for shared in (False, True):
        with services.db.transaction() as conn:
            conn.execute(
                "UPDATE agents SET user_id=?,is_shared=? WHERE agent_id='expert-one'",
                (2 if shared else 1, int(shared)),
            )
        result = local_cli.invoke(
            cli,
            [
                "--json",
                "--user",
                "owner",
                "cron",
                "create",
                "--trigger",
                "interval:3600",
                "--prompt",
                "Report",
                "--expert",
                "expert-one",
                "--fresh-thread",
            ],
        )
        assert result.exit_code == 0, result.output
        created = json.loads(result.output)
        assert created["selected_expert_id"] == "expert-one"
        assert created["conversation_policy"] == "per_run" and created["fresh_thread"]
    with services.db.transaction() as conn:
        conn.execute("UPDATE agents SET is_shared=0 WHERE agent_id='expert-one'")
    denied = local_cli.invoke(
        cli,
        [
            "--json",
            "--user",
            "owner",
            "cron",
            "create",
            "--trigger",
            "interval:3600",
            "--prompt",
            "Report",
            "--expert",
            "expert-one",
        ],
    )
    assert denied.exit_code == 1


def test_explicit_target_project_and_policy_are_validated(services, local_cli):
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES('project',1,'Project',1,1)"
        )
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,project_id,title,request_id,created_at,updated_at) VALUES('target',1,'project','Target','target-request',1,1)"
        )
    result = local_cli.invoke(
        cli,
        [
            "--json",
            "--user",
            "owner",
            "cron",
            "create",
            "--trigger",
            "interval:3600",
            "--prompt",
            "Report",
            "--conversation-policy",
            "task",
            "--task-id",
            "target",
            "--project",
            "project",
        ],
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["target_task_id"] == "target" and value["project_id"] == "project"
    with pytest.raises(OctopError):
        validate_task_selection(
            services,
            2,
            conversation_policy="task",
            target_task_id="target",
            project_id=None,
            selected_expert_id=None,
        )
    for policy, target in (("task", None), ("per_job", "target")):
        with pytest.raises(OctopError):
            validate_task_selection(
                services,
                1,
                conversation_policy=policy,
                target_task_id=target,
                project_id=None,
                selected_expert_id=None,
            )
    with services.db.transaction() as conn:
        conn.execute("UPDATE project_tasks SET internal_kind='resident' WHERE task_id='target'")
    with pytest.raises(OctopError):
        validate_task_selection(
            services,
            1,
            conversation_policy="task",
            target_task_id="target",
            project_id=None,
            selected_expert_id=None,
        )
