"""Metadata-only project experts cannot enter legacy execution or workspace routes."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from octop.api.common.agent import assert_agent_access_row, assert_agent_owner, require_agent_row
from octop.cli.support.offline_ops import (
    create_channel_offline,
    create_cron_offline,
    create_thread_offline,
    delete_agent_offline,
)
from octop.cli.support.skills import set_skill_enabled
from octop.config import OctopConfig
from octop.infra.agentcomputer.commands import content_digest
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import ProjectConfiguration, ProjectPreparationIntent
from octop.infra.agents.manager import AgentCreateSpec, AgentManager
from octop.infra.agents.providers.store import clear_stale_pins_for_provider
from octop.infra.cron.manager import CronCreateSpec, CronManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.gateway import ChannelCreateSpec, Gateway
from octop.infra.users.resource_policy import POLICY_MAX_AGENTS
from octop.infra.utils.paths import PathLayout

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")


@pytest.fixture
def env(tmp_path):
    paths = PathLayout(tmp_path.resolve() / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    owner_id = services.user_repo.create(username="owner", role="user")
    manager = AgentManager(repos=services.repos, paths=paths)
    yield services, manager, owner_id
    db.close()


def reserve(env, *, name="project"):
    services, _manager, owner_id = env
    project_id, workspace_id = str(uuid4()), str(uuid4())
    agent_id = "ac-" + uuid4().hex[:16]
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    config = ProjectConfiguration(instructions="Fixture instructions", modelRef="pending/model")
    root = services.paths.root.resolve() / "agentcomputer-workspaces" / workspace_id
    intent = ProjectPreparationIntent(
        schemaVersion=1,
        boxID=identity.boxID,
        projectID=project_id,
        workspaceID=workspace_id,
        ownerID=owner_id,
        plannedAgentID=agent_id,
        serverRoot=str(root),
        configurationDigest=content_digest(config.model_dump(mode="json")),
    )
    with services.db.transaction() as conn:
        services.ac_project_repo.insert(
            conn,
            project_id=project_id,
            owner_id=owner_id,
            box_id=str(identity.boxID),
            workspace_id=workspace_id,
            name=name,
            config_json=config.model_dump_json(),
            config_digest=intent.configurationDigest,
            created_at=1,
            updated_at=1,
        )
        services.ac_project_preparation_repo.insert(
            conn,
            preparation_id=str(uuid4()),
            project_id=project_id,
            owner_id=owner_id,
            planned_agent_id=agent_id,
            intent_json=intent.model_dump_json(),
            intent_digest=content_digest(intent.model_dump(mode="json")),
            restore_epoch=1,
            created_at=1,
            updated_at=1,
        )
    spec = AgentCreateSpec(
        agent_id=agent_id,
        user_id=owner_id,
        name=name,
        default_model=config.modelRef,
        system_prompt=config.instructions,
        config={
            "backend": {"type": "filesystem", "root_dir": str(root), "virtual_mode": True},
            "workspace_dir": f"/.octop/workspaces/{agent_id}",
        },
    )
    return project_id, spec, root


async def test_registers_exact_disabled_metadata_without_runtime_or_model(env, monkeypatch):
    services, manager, owner_id = env
    project_id, spec, root = reserve(env)
    start = AsyncMock(side_effect=AssertionError("runtime must not start"))
    monkeypatch.setattr(manager, "_start_agent", start)
    monkeypatch.setattr(manager, "_complete_create_bootstrap", start)
    manager._proactive_scheduler = MagicMock()
    row = await manager.create_stopped(spec, project_id=project_id)
    assert row.agent_id == spec.agent_id
    assert row.user_id == owner_id
    assert (row.enabled, row.last_state) == (0, "stopped")
    assert (row.managed_by, row.managed_project_id) == ("agentcomputer", project_id)
    assert manager._harness_manager is None
    assert services.provider_repo.list_all() == []
    start.assert_not_awaited()
    manager._proactive_scheduler.ensure_scheduled.assert_not_called()
    workspace = manager.workspace_for_project_preparation(row.agent_id, project_id)
    assert workspace.workspace_dir == root / ".octop" / "workspaces" / row.agent_id
    assert Path(workspace.resolve_path("/")) == root
    assert workspace.system_files_path == ".octop"
    await workspace.awrite_text("preparation.txt", "prepared")
    assert await workspace.aread_text("preparation.txt") == "prepared"
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0


async def test_registration_io_uses_worker_thread(env, monkeypatch):
    _services, manager, _owner = env
    project_id, spec, _root = reserve(env)
    thread_ids = []
    original = manager._create_stopped

    def observed(*args):
        thread_ids.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(manager, "_create_stopped", observed)
    await manager.create_stopped(spec, project_id=project_id)
    assert thread_ids and thread_ids[0] != threading.get_ident()


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", None),
        ("is_shared", True),
        ("kind", "team"),
        ("template_name", "demo"),
        ("mcp_servers", ["mcp"]),
        ("runtime_config", {"memory": {}}),
        ("default_model", "other/model"),
        ("system_prompt", "changed"),
    ],
)
async def test_invalid_spec_rejected_before_metadata_or_workspace(env, field, value):
    _services, manager, _owner = env
    project_id, spec, root = reserve(env)
    with pytest.raises(OctopError) as error:
        await manager.create_stopped(replace(spec, **{field: value}), project_id=project_id)
    assert error.value.code == ErrorCode.FORBIDDEN
    assert manager.get_row(spec.agent_id) is None
    assert not root.exists()


async def test_disabled_managed_experts_count_toward_quota(env):
    services, manager, owner = env
    services.user_policy_repo.set(owner, POLICY_MAX_AGENTS, "1")
    project_id, spec, _root = reserve(env)
    await manager.create_stopped(spec, project_id=project_id)
    project2, spec2, root2 = reserve(env, name="second")
    with pytest.raises(OctopError) as error:
        await manager.create_stopped(spec2, project_id=project2)
    assert error.value.code == ErrorCode.AGENT_QUOTA_EXCEEDED
    assert services.agent_repo.count_by_user(owner) == 1
    assert not root2.exists()


@pytest.mark.parametrize(
    "mutation", ["enabled", "model", "prompt", "backend", "workspace", "owner", "journal"]
)
async def test_preparation_workspace_rejects_changed_bindings(env, mutation):
    services, manager, _owner = env
    project_id, spec, _root = reserve(env)
    row = await manager.create_stopped(spec, project_id=project_id)
    other_owner = (
        services.user_repo.create(username="other", role="user") if mutation == "owner" else None
    )
    with services.db.transaction() as conn:
        if mutation == "enabled":
            conn.execute("UPDATE agents SET enabled=1 WHERE agent_id=?", (row.agent_id,))
        elif mutation in ("model", "prompt"):
            column = "default_model" if mutation == "model" else "system_prompt"
            conn.execute(f"UPDATE agents SET {column}='changed' WHERE agent_id=?", (row.agent_id,))
        elif mutation in ("backend", "workspace"):
            cfg = json.loads(row.config_json)
            if mutation == "backend":
                cfg["backend"]["type"] = "local_shell"
            else:
                cfg["workspace_dir"] = "other"
            conn.execute(
                "UPDATE agents SET config_json=? WHERE agent_id=?", (json.dumps(cfg), row.agent_id)
            )
        elif mutation == "owner":
            conn.execute(
                "UPDATE agents SET user_id=? WHERE agent_id=?", (other_owner, row.agent_id)
            )
        else:
            conn.execute(
                "UPDATE ac_project_preparations SET intent_digest='bad' WHERE project_id=?",
                (project_id,),
            )
    with pytest.raises(OctopError) as error:
        manager.workspace_for_project_preparation(row.agent_id, project_id)
    assert error.value.code == ErrorCode.FORBIDDEN


@posix_only
async def test_symlink_workspace_cannot_be_rebound_to_external_content(env, tmp_path):
    _services, manager, _owner = env
    project_id, spec, root = reserve(env)
    row = await manager.create_stopped(spec, project_id=project_id)
    host = root / ".octop" / "workspaces" / row.agent_id
    host.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    host.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OctopError):
        manager.workspace_for_project_preparation(row.agent_id, project_id)
    assert list(outside.iterdir()) == []


async def test_legacy_manager_and_admin_http_guards_reject_managed_agent(env):
    _services, manager, owner = env
    project_id, spec, _root = reserve(env)
    row = await manager.create_stopped(spec, project_id=project_id)
    for operation in (
        lambda: manager.start(row.agent_id),
        lambda: manager.stop(row.agent_id),
        lambda: manager.reload(row.agent_id),
        lambda: manager.update(row.agent_id, name="changed"),
        lambda: manager.delete(row.agent_id),
        lambda: manager.set_shared(row.agent_id, True),
        lambda: manager.persist_skill_package_ids(row.agent_id, []),
        lambda: manager.call(row.agent_id, {}),
        lambda: manager.delete_thread_checkpoint(row.agent_id, "fixture"),
    ):
        with pytest.raises(OctopError) as error:
            await operation()
        assert error.value.code == ErrorCode.FORBIDDEN
    admin = SimpleNamespace(id=owner, is_admin=True)
    server = SimpleNamespace(app_runtime=SimpleNamespace(agent_registry=manager))
    for operation in (
        lambda: manager.get_agent(row.agent_id),
        lambda: manager.workspace_for_agent(row.agent_id),
        lambda: manager.resolve_workspace_dir(row.agent_id),
        lambda: manager.persist_harness_config(row.agent_id, {}),
        lambda: manager.set_icon_url(row.agent_id, "changed"),
        lambda: manager.persist_knowledge_base_ids(row.agent_id, []),
        lambda: manager.persist_mcp_servers(row.agent_id, []),
        lambda: manager.cancel_stream(row.agent_id, "fixture"),
        lambda: manager.set_thread_model(row.agent_id, "fixture", "other/model"),
        lambda: manager.clear_thread_model(row.agent_id, "fixture"),
        lambda: assert_agent_owner(row, admin),
        lambda: assert_agent_access_row(row, admin),
        lambda: require_agent_row(row.agent_id, user=admin, as_user=None, server=server),
    ):
        with pytest.raises(OctopError) as error:
            operation()
        assert error.value.code == ErrorCode.FORBIDDEN
    assert manager.get_row(row.agent_id) == row


async def test_cli_delete_and_binding_fail_before_side_effects(env, monkeypatch):
    services, manager, owner = env
    project_id, spec, root = reserve(env)
    row = await manager.create_stopped(spec, project_id=project_id)
    monkeypatch.setenv("OCTOP_HOME", str(services.paths.root))
    for operation in (
        lambda: delete_agent_offline(row.agent_id, home=services.paths.root),
        lambda: set_skill_enabled(row.agent_id, "fixture", enabled=False),
        lambda: create_thread_offline(
            agent_id=row.agent_id, user_id=owner, home=services.paths.root
        ),
        lambda: create_cron_offline(
            agent_id=row.agent_id,
            user_id=owner,
            trigger="interval:3600",
            prompt="fixture",
            home=services.paths.root,
        ),
        lambda: create_channel_offline(
            agent_id=row.agent_id,
            user_id=owner,
            kind="telegram",
            name="fixture",
            config={},
            home=services.paths.root,
        ),
    ):
        with pytest.raises(OctopError) as error:
            await asyncio.to_thread(operation)
        assert error.value.code == ErrorCode.FORBIDDEN
    assert root.exists() and manager.get_row(row.agent_id) == row
    with services.db.connect() as conn:
        for table in ("channels", "threads", "cron_jobs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_cron_and_channel_binding_and_boot_skip_managed_agent(env, monkeypatch):
    services, manager, owner = env
    project_id, spec, _root = reserve(env)
    row = await manager.create_stopped(spec, project_id=project_id)
    gateway = Gateway(agent_manager=manager, repos=services.repos)
    cron = CronManager(gateway=gateway, delivery_service=MagicMock(), repos=services.repos)
    with pytest.raises(OctopError):
        await cron.create(
            CronCreateSpec(
                cron_id="fixture",
                agent_id=row.agent_id,
                user_id=owner,
                trigger="interval:3600",
                prompt="fixture",
            )
        )
    with pytest.raises(OctopError):
        await gateway.create_channel(
            ChannelCreateSpec(
                channel_id="fixture",
                agent_id=row.agent_id,
                user_id=owner,
                kind="telegram",
                name="fixture",
                config={},
            )
        )
    services.channel_repo.create(
        channel_id="fixture",
        agent_id=row.agent_id,
        user_id=owner,
        kind="telegram",
        name="fixture",
        config_json="{}",
    )
    channel = services.channel_repo.get("fixture")
    register = AsyncMock(side_effect=AssertionError("must not register"))
    monkeypatch.setattr(gateway, "_register_channel", register)
    await gateway._safe_register_channel(channel)
    register.assert_not_awaited()
    fake_scheduler = MagicMock()
    cron._scheduler = fake_scheduler
    cron._schedule(SimpleNamespace(agent_id=row.agent_id, enabled=True))
    fake_scheduler.add_job.assert_not_called()
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE agents SET enabled=1, last_state='created' WHERE agent_id=?", (row.agent_id,)
        )
    assert services.proactive_care_config_repo.list_enabled() == []
    changed = manager.get_row(row.agent_id)
    with pytest.raises(OctopError) as error:
        await manager._start_agent(changed)
    assert error.value.code == ErrorCode.FORBIDDEN
    start = AsyncMock(side_effect=AssertionError("managed runtime must not start"))
    monkeypatch.setattr(manager, "_start_agent", start)
    monkeypatch.setattr("octop.infra.agents.manager.HarnessAgentManager", MagicMock())
    await manager.boot()
    await manager.reload_all()
    start.assert_not_awaited()


async def test_provider_pin_cleanup_preserves_fixed_managed_configuration(env):
    services, manager, owner = env
    project_id, spec, _root = reserve(env)
    managed = await manager.create_stopped(spec, project_id=project_id)
    services.agent_repo.create(
        agent_id="ordinary", user_id=owner, name="ordinary", default_model=spec.default_model
    )
    cleared = clear_stale_pins_for_provider(
        agent_repo=services.agent_repo,
        settings_repo=services.settings_repo,
        provider_name="pending",
        models=[],
    )
    assert cleared == ["ordinary"]
    assert manager.get_row(managed.agent_id) == managed
    assert manager.get_row("ordinary").default_model is None


def test_ordinary_agent_repo_defaults_remain_enabled_and_unmanaged(env):
    services, manager, owner = env
    services.agent_repo.create(agent_id="ordinary", user_id=owner, name="ordinary")
    row = manager.get_row("ordinary")
    assert row.enabled == 1
    assert row.managed_by is row.managed_project_id is None
    assert manager.workspace_for_agent("ordinary") is not None
