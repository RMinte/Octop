"""Project resources survive retries without starting a harness or model."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from octop.config import OctopConfig
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    get_command_receipt,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import ProjectCreateCommand, ProjectCreateReceipt
from octop.infra.agentcomputer.projects import (
    _accept_project,
    create_project,
    get_project,
    project_command_digest,
)
from octop.infra.agents.manager import AgentManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


def request(**changes):
    data = {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "projectID": str(uuid4()),
        "name": "本地项目",
        "configuration": {
            "schemaVersion": 1,
            "modelRef": "local/qwen",
            "instructions": "用中文\n回复",
        },
    }
    data.update(changes)
    return ProjectCreateCommand.model_validate(data)


@pytest.fixture
def control(tmp_path):
    paths = PathLayout(tmp_path)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(db=pool, paths=paths, config=OctopConfig())
    services.user_repo.create(username="one", password_hash="fixture", role="admin")
    services.user_repo.create(username="two", password_hash="fixture", role="user")
    registry = AgentManager(repos=services.repos, paths=paths)
    registry._start_agent = AsyncMock(side_effect=AssertionError("model must not start"))
    registry._complete_create_bootstrap = AsyncMock(
        side_effect=AssertionError("bootstrap must not start")
    )
    registry._proactive_scheduler = Mock()
    yield services, registry
    pool.close()


def counts(services):
    with services.db.connect() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "ac_projects",
                "ac_project_preparations",
                "ac_commands",
                "agents",
                "ac_runs",
                "ac_events",
            )
        }


def test_golden_digest_preserves_names_and_instructions():
    command = request(projectID="22222222-2222-4222-8222-222222222222")
    digest = project_command_digest(UUID("11111111-1111-4111-8111-111111111111"), command)
    assert digest == "sha256:ab1044b41f9c40b8b7375c18c37460bb85368bb14873eb733bb3c90c688d2376"
    retry = request(
        **{
            **command.model_dump(mode="json"),
            "commandID": str(uuid4()),
            "idempotencyKey": "another",
        }
    )
    assert project_command_digest(UUID("11111111-1111-4111-8111-111111111111"), retry) == digest
    changed = request(**{**command.model_dump(mode="json"), "name": " 本地项目"})
    assert project_command_digest(UUID("11111111-1111-4111-8111-111111111111"), changed) != digest


@pytest.mark.parametrize(
    "change",
    [
        {"name": " "},
        {"name": "line\nbreak"},
        {"name": "x" * 129},
        {"expectedRestoreEpoch": True},
        {"ownerID": 2},
        {"workspaceID": str(UUID("44444444-4444-4444-8444-444444444444"))},
        {"configuration": {"modelRef": "local/qwen", "instructions": "x" * 8193}},
    ],
)
def test_strict_project_input(change):
    with pytest.raises(ValidationError):
        request(**change)


async def test_prepare_without_provider_is_durable_and_never_ready(control):
    services, registry = control
    command = request()
    receipt = await create_project(services, registry, owner_id=1, command=command)
    assert isinstance(receipt, ProjectCreateReceipt) and receipt.accepted
    assert receipt.preparationStateAtAcceptance == "pending"
    current = get_project(services, 1, command.projectID)
    assert current.preparationState == "failed" and current.reasonCode == "providerUnavailable"
    assert current.retryable and not current.executionReady
    assert current.steps.agent == current.steps.workspace == "verified"
    project = services.ac_project_repo.get(str(command.projectID), 1)
    agent = registry.get_row(project.default_agent_id)
    assert agent.managed_by == "agentcomputer" and agent.managed_project_id == str(
        command.projectID
    )
    assert agent.enabled == 0 and agent.last_state == "stopped"
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert counts(services) == {
        "ac_projects": 1,
        "ac_project_preparations": 1,
        "ac_commands": 1,
        "agents": 1,
        "ac_runs": 0,
        "ac_events": 0,
    }
    registry._start_agent.assert_not_called()
    registry._complete_create_bootstrap.assert_not_called()
    registry._proactive_scheduler.ensure_scheduled.assert_not_called()
    assert registry._harness_manager is None
    assert get_project(services, 2, command.projectID) is None


async def test_retry_returns_original_ids_and_receipt_bytes(control):
    services, registry = control
    command = request()
    first = await create_project(services, registry, owner_id=1, command=command)
    before = services.ac_command_repo.get(str(first.commandID), 1).receipt_json
    second = await create_project(
        services,
        registry,
        owner_id=1,
        command=request(**{**command.model_dump(mode="json"), "commandID": str(uuid4())}),
    )
    assert second == first.model_copy(update={"replayed": True})
    assert services.ac_command_repo.get(str(first.commandID), 1).receipt_json == before
    assert counts(services)["agents"] == counts(services)["ac_projects"] == 1


async def test_repeated_cancellation_keeps_preparation_lock_until_operation_finishes(
    control, monkeypatch
):
    services, registry = control
    command = request()
    entered, release = asyncio.Event(), asyncio.Event()
    original = registry.create_stopped
    calls = 0

    async def paused_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        row = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return row

    monkeypatch.setattr(registry, "create_stopped", paused_create)
    pending = asyncio.create_task(create_project(services, registry, owner_id=1, command=command))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        replay = await create_project(services, registry, owner_id=1, command=command)
        assert replay.replayed and calls == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert get_project(services, 1, command.projectID).steps.workspace == "verified"
    finally:
        release.set()
        if not pending.done():
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("field", ["workspaceID", "preparationID"])
async def test_mismatched_project_receipt_does_not_replay_or_query(control, field):
    services, registry = control
    command = request()
    first = await create_project(services, registry, owner_id=1, command=command)
    damaged = first.model_dump(mode="json")
    damaged[field] = str(uuid4())
    with services.db.connect() as conn:
        conn.execute("UPDATE ac_commands SET receipt_json=?", (json.dumps(damaged),))
    with pytest.raises(StorageDataError):
        get_command_receipt(services, 1, first.commandID)
    with pytest.raises(StorageDataError):
        await create_project(services, registry, owner_id=1, command=command)
    assert counts(services)["agents"] == 1


async def test_invalid_claim_utf8_is_preserved_and_diagnosed(control):
    services, registry = control
    command = request()
    receipt = await create_project(services, registry, owner_id=1, command=command)
    claim = (
        services.paths.agentcomputer_identity.parent
        / "workspace-claims"
        / (str(receipt.workspaceID) + ".json")
    )
    claim.write_bytes(b"\xff")
    await create_project(services, registry, owner_id=1, command=command)
    assert claim.read_bytes() == b"\xff"
    assert get_project(services, 1, command.projectID).reasonCode == "resourceBindingInvalid"


def test_replay_uses_the_existing_transaction_connection(control, monkeypatch):
    services, _registry = control
    command = request()
    first = _accept_project(services, 1, command)

    def second_connection_forbidden(*args, **kwargs):
        raise AssertionError("binding lookup borrowed another pool connection")

    monkeypatch.setattr(services.ac_project_repo, "get", second_connection_forbidden)
    monkeypatch.setattr(services.ac_project_preparation_repo, "get", second_connection_forbidden)
    assert _accept_project(services, 1, command) == first.model_copy(update={"replayed": True})


@pytest.mark.parametrize(
    "change",
    [
        {"name": "changed"},
        {"idempotencyKey": "new-key"},
        {"configuration": {"modelRef": "other/model", "instructions": "changed"}},
    ],
)
async def test_changed_command_conflicts_without_new_resources(control, change):
    services, registry = control
    command = request()
    await create_project(services, registry, owner_id=1, command=command)
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        await create_project(
            services,
            registry,
            owner_id=1,
            command=request(**{**command.model_dump(mode="json"), **change}),
        )
    assert counts(services)["agents"] == 1


async def test_new_key_cannot_recreate_project_and_owners_have_separate_keys(control):
    services, registry = control
    command = request(idempotencyKey="common")
    await create_project(services, registry, owner_id=1, command=command)
    with pytest.raises(CommandAdmissionError, match="resourceConflict"):
        await create_project(
            services, registry, owner_id=1, command=request(projectID=command.projectID)
        )
    second = await create_project(
        services, registry, owner_id=2, command=request(idempotencyKey="common")
    )
    assert get_command_receipt(services, 1, second.commandID) is None
    assert counts(services)["agents"] == 2


@pytest.mark.parametrize(
    "repo_name", ["ac_project_repo", "ac_project_preparation_repo", "ac_command_repo"]
)
def test_registration_failure_is_atomic(control, monkeypatch, repo_name):
    services, _registry = control
    repo = getattr(services, repo_name)
    original = repo.insert

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(repo, "insert", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _accept_project(services, 1, request())
    assert all(value == 0 for value in counts(services).values())
    assert not (services.paths.root / "agentcomputer-workspaces").exists()


async def test_created_agent_with_lost_confirmation_is_reused(control, monkeypatch):
    services, registry = control
    command = request()
    original = registry.create_stopped

    async def lost_confirmation(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected lost confirmation")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "create_stopped", lost_confirmation)
        with pytest.raises(RuntimeError, match="injected"):
            await create_project(services, registry, owner_id=1, command=command)
    prep = services.ac_project_preparation_repo.get(str(command.projectID), 1)
    row = registry.get_row(prep.planned_agent_id)
    assert row.managed_by == "agentcomputer" and row.enabled == 0 and prep.agent_state == "pending"
    replay = await create_project(services, registry, owner_id=1, command=command)
    assert replay.replayed and counts(services)["agents"] == 1
    assert get_project(services, 1, command.projectID).steps.agent == "verified"


async def test_workspace_write_with_lost_confirmation_preserves_marker(control, monkeypatch):
    services, registry = control
    command = request()
    original = services.ac_project_preparation_repo.confirm_workspace

    def lost_confirmation(*args, **kwargs):
        raise RuntimeError("injected workspace confirmation failure")

    with monkeypatch.context() as patch:
        patch.setattr(services.ac_project_preparation_repo, "confirm_workspace", lost_confirmation)
        with pytest.raises(RuntimeError, match="injected"):
            await create_project(services, registry, owner_id=1, command=command)
    prep = services.ac_project_preparation_repo.get(str(command.projectID), 1)
    ws = registry.workspace_for_project_preparation(prep.planned_agent_id, str(command.projectID))
    marker = ws.read_text(".octop/agentcomputer-project.json")
    await create_project(services, registry, owner_id=1, command=command)
    assert ws.read_text(".octop/agentcomputer-project.json") == marker
    assert get_project(services, 1, command.projectID).steps.workspace == "verified"
    assert original is not None


async def test_restart_reuses_prepared_resources_and_receipt(control):
    services, registry = control
    command = request()
    first = await create_project(services, registry, owner_id=1, command=command)
    services.db.close()
    reopened = SqlitePool(services.paths.db)
    try:
        run_migrations(reopened)
        restarted = build_shared_services(db=reopened, paths=services.paths, config=services.config)
        new_registry = AgentManager(repos=restarted.repos, paths=restarted.paths)
        result = await create_project(restarted, new_registry, owner_id=1, command=command)
        assert result == first.model_copy(update={"replayed": True})
        assert counts(restarted)["agents"] == 1
        assert get_command_receipt(restarted, 1, first.commandID) == first
    finally:
        reopened.close()


async def test_two_connections_prepare_same_project_once(control):
    services, registry = control
    pool_two = SqlitePool(services.paths.db)
    other = build_shared_services(db=pool_two, paths=services.paths, config=services.config)
    registry_two = AgentManager(repos=other.repos, paths=other.paths)
    command = request()
    try:
        first, second = await asyncio.gather(
            create_project(services, registry, owner_id=1, command=command),
            create_project(other, registry_two, owner_id=1, command=command),
        )
        assert first.commandID == second.commandID
        assert sorted([first.replayed, second.replayed]) == [False, True]
        assert counts(services)["agents"] == 1
        assert get_project(services, 1, command.projectID).preparationState == "failed"
    finally:
        pool_two.close()


async def test_old_epoch_rejected_before_replay(control):
    services, registry = control
    command = request()
    first = await create_project(services, registry, owner_id=1, command=command)
    path = services.paths.agentcomputer_identity
    identity = json.loads(path.read_text())
    identity["restoreEpoch"] = 3
    path.write_text(json.dumps(identity))
    with pytest.raises(CommandAdmissionError, match="restoreEpochConflict"):
        await create_project(services, registry, owner_id=1, command=command)
    assert get_command_receipt(services, 1, first.commandID) == first


async def test_corrupt_workspace_marker_not_overwritten(control):
    services, registry = control
    command = request()
    await create_project(services, registry, owner_id=1, command=command)
    prep = services.ac_project_preparation_repo.get(str(command.projectID), 1)
    workspace = registry.workspace_for_project_preparation(
        prep.planned_agent_id, str(command.projectID)
    )
    workspace.write_text(".octop/agentcomputer-project.json", "foreign-resource", force=True)
    await create_project(services, registry, owner_id=1, command=command)
    assert workspace.read_text(".octop/agentcomputer-project.json") == "foreign-resource"
    assert get_project(services, 1, command.projectID).reasonCode == "resourceBindingInvalid"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
async def test_symlink_server_root_never_falls_back_or_overwrites(control, tmp_path):
    services, registry = control
    command = request()
    receipt = _accept_project(services, 1, command)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "existing.txt"
    sentinel.write_text("preserve")
    root = services.paths.root / "agentcomputer-workspaces" / str(receipt.workspaceID)
    root.parent.mkdir()
    root.symlink_to(external, target_is_directory=True)
    await create_project(services, registry, owner_id=1, command=command)
    assert sentinel.read_text() == "preserve" and len(list(external.iterdir())) == 1
    assert counts(services)["agents"] == 0
    assert get_project(services, 1, command.projectID).reasonCode == "resourceBindingInvalid"


def test_corrupt_intent_stays_corrupt(control):
    services, _registry = control
    command = request()
    _accept_project(services, 1, command)
    with services.db.connect() as conn:
        conn.execute("UPDATE ac_project_preparations SET intent_json='{}'")
    from octop.infra.agentcomputer.projects import _intent_for

    with pytest.raises(StorageDataError):
        _intent_for(services, command.projectID, 1)
    assert get_or_create_identity(services.paths.agentcomputer_identity).restoreEpoch == 1
