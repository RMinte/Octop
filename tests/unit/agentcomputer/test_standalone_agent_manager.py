"""Standalone Tasks reserve a private Agent and workspace without a project."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from octop.config import OctopConfig
from octop.infra.agentcomputer.commands import content_digest
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import ProjectConfiguration
from octop.infra.agents.manager import AgentCreateSpec, AgentManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")


@pytest.fixture
def control(tmp_path: Path):
    paths = PathLayout(tmp_path.resolve() / ".octop")
    paths.ensure_root()
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(db=pool, paths=paths, config=OctopConfig())
    owner_id = services.user_repo.create(username="owner", role="user")
    manager = AgentManager(repos=services.repos, paths=paths)
    yield services, manager, owner_id
    pool.close()


def reserve(control, *, title: str = "Standalone task"):
    services, _manager, owner_id = control
    task_id = str(uuid4())
    workspace_id = str(uuid4())
    agent_id = "ac-" + uuid4().hex[:16]
    box_id = str(get_or_create_identity(services.paths.agentcomputer_identity).boxID)
    configuration = ProjectConfiguration(
        schemaVersion=1, instructions="Task instructions", modelRef="pending/model"
    )
    root = services.paths.root.resolve() / "agentcomputer-task-workspaces" / workspace_id
    with services.db.transaction() as conn:
        task = services.ac_task_repo.insert(
            conn,
            task_id=task_id,
            project_id=None,
            owner_id=owner_id,
            session_id=str(uuid4()),
            octop_thread_id=None,
            title=title,
            created_at=1,
            updated_at=1,
            preparation_state="pending",
            standalone_box_id=box_id,
            standalone_workspace_id=workspace_id,
            standalone_agent_id=agent_id,
            standalone_config_json=configuration.model_dump_json(),
            standalone_config_digest=content_digest(configuration.model_dump(mode="json")),
        )
    assert task is not None and task.project_id is None
    spec = AgentCreateSpec(
        agent_id=agent_id,
        user_id=owner_id,
        name=title,
        default_model=configuration.modelRef,
        system_prompt=configuration.instructions,
        config={
            "backend": {"type": "filesystem", "root_dir": str(root), "virtual_mode": True},
            "workspace_dir": f"/.octop/workspaces/{agent_id}",
        },
    )
    return task_id, spec, root


async def test_task_agent_is_disabled_and_bound_to_private_workspace(control, monkeypatch):
    services, manager, owner_id = control
    task_id, spec, root = reserve(control)
    start = AsyncMock(side_effect=AssertionError("runtime must not start"))
    monkeypatch.setattr(manager, "_start_agent", start)
    monkeypatch.setattr(manager, "_complete_create_bootstrap", start)

    row = await manager.create_task_stopped(spec, task_id=task_id)

    assert row.user_id == owner_id
    assert (row.enabled, row.last_state) == (0, "stopped")
    assert (row.managed_by, row.managed_project_id, row.managed_task_id) == (
        "agentcomputer",
        None,
        task_id,
    )
    assert manager._harness_manager is None
    start.assert_not_awaited()
    workspace = manager.workspace_for_task_preparation(row.agent_id, task_id)
    assert Path(workspace.workspace_dir) == root / ".octop" / "workspaces" / row.agent_id
    assert Path(workspace.resolve_path("/")) == root
    assert workspace.system_files_path == ".octop"
    await workspace.awrite_text("draft.txt", "task content")
    assert await workspace.aread_text("draft.txt") == "task content"
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0


async def test_task_workspaces_are_isolated_and_wrong_task_cannot_open_agent(control):
    _services, manager, _owner_id = control
    first_task, first_spec, first_root = reserve(control, title="First")
    second_task, second_spec, second_root = reserve(control, title="Second")
    first_row = await manager.create_task_stopped(first_spec, task_id=first_task)
    second_row = await manager.create_task_stopped(second_spec, task_id=second_task)

    first_workspace = manager.workspace_for_task_preparation(first_row.agent_id, first_task)
    second_workspace = manager.workspace_for_task_preparation(second_row.agent_id, second_task)
    assert first_root != second_root
    await first_workspace.awrite_text("shared-name.txt", "first")
    await second_workspace.awrite_text("shared-name.txt", "second")
    assert await first_workspace.aread_text("shared-name.txt") == "first"
    assert await second_workspace.aread_text("shared-name.txt") == "second"
    with pytest.raises(OctopError) as error:
        manager.workspace_for_task_preparation(first_row.agent_id, second_task)
    assert error.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize(
    "mutation",
    ["wrong_task", "owner", "model", "prompt", "backend", "workspace"],
)
async def test_invalid_creation_binding_does_not_create_agent_or_workspace(control, mutation):
    services, manager, _owner_id = control
    task_id, spec, root = reserve(control)
    if mutation == "wrong_task":
        task_id = str(uuid4())
    elif mutation == "owner":
        other = services.user_repo.create(username="other", role="user")
        spec = replace(spec, user_id=other)
    elif mutation == "model":
        spec = replace(spec, default_model="other/model")
    elif mutation == "prompt":
        spec = replace(spec, system_prompt="changed")
    else:
        config = json.loads(json.dumps(spec.config))
        if mutation == "backend":
            config["backend"]["root_dir"] = str(root.parent / "different")
        else:
            config["workspace_dir"] = "/.octop/workspaces/different"
        spec = replace(spec, config=config)

    with pytest.raises(OctopError) as error:
        await manager.create_task_stopped(spec, task_id=task_id)
    assert error.value.code == ErrorCode.FORBIDDEN
    assert manager.get_row(spec.agent_id) is None
    assert not root.exists()


@pytest.mark.parametrize(
    "mutation",
    ["owner", "model", "box_id", "planned_agent", "config_json", "config_digest", "backend"],
)
async def test_changed_task_or_agent_binding_blocks_workspace(control, mutation):
    services, manager, _owner_id = control
    task_id, spec, _root = reserve(control)
    row = await manager.create_task_stopped(spec, task_id=task_id)
    other = (
        services.user_repo.create(username="other", role="user") if mutation == "owner" else None
    )
    with services.db.transaction() as conn:
        if mutation == "owner":
            conn.execute("UPDATE agents SET user_id=? WHERE agent_id=?", (other, row.agent_id))
        elif mutation == "model":
            conn.execute(
                "UPDATE agents SET default_model='other/model' WHERE agent_id=?", (row.agent_id,)
            )
        elif mutation == "box_id":
            conn.execute(
                "UPDATE ac_tasks SET standalone_box_id=? WHERE task_id=?",
                (str(uuid4()), task_id),
            )
        elif mutation == "planned_agent":
            conn.execute(
                "UPDATE ac_tasks SET standalone_agent_id=? WHERE task_id=?",
                ("ac-" + uuid4().hex[:16], task_id),
            )
        elif mutation == "config_json":
            conn.execute(
                "UPDATE ac_tasks SET standalone_config_json=? WHERE task_id=?",
                (
                    '{"schemaVersion":1,"instructions":"changed","modelRef":"pending/model"}',
                    task_id,
                ),
            )
        elif mutation == "config_digest":
            conn.execute(
                "UPDATE ac_tasks SET standalone_config_digest='bad' WHERE task_id=?", (task_id,)
            )
        else:
            config = json.loads(row.config_json)
            config["backend"]["type"] = "local_shell"
            conn.execute(
                "UPDATE agents SET config_json=? WHERE agent_id=?",
                (json.dumps(config), row.agent_id),
            )

    with pytest.raises(OctopError) as error:
        manager.workspace_for_task_preparation(row.agent_id, task_id)
    assert error.value.code == ErrorCode.FORBIDDEN


@posix_only
async def test_symlinked_task_workspace_cannot_rebind_to_external_files(control, tmp_path: Path):
    _services, manager, _owner_id = control
    task_id, spec, root = reserve(control)
    row = await manager.create_task_stopped(spec, task_id=task_id)
    workspace_dir = root / ".octop" / "workspaces" / row.agent_id
    workspace_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OctopError) as error:
        manager.workspace_for_task_preparation(row.agent_id, task_id)
    assert error.value.code == ErrorCode.FORBIDDEN
    assert list(outside.iterdir()) == []
