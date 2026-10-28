"""Private resident identities do not reuse or expose existing Agent state."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from octop.config import OctopConfig
from octop.i18n import tr
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.resident_identity import (
    ResidentTextOnlyMiddleware,
    ensure_resident_identity,
    resident_execution_scope,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    services.user_repo.create(username="resident-owner", password_hash="x", role="user")
    services.user_repo.create(username="second-owner", password_hash="x", role="user")
    yield services
    db.close()


@pytest.fixture
def manager(services):
    return AgentManager(repos=services.repos, paths=services.paths)


@pytest.mark.asyncio
async def test_initialization_is_stable_concurrent_and_private(services, manager):
    services.agent_repo.create(agent_id="old", user_id=1, name=tr("agents.resident.name", "zh"))
    first, second = await asyncio.gather(
        ensure_resident_identity(services, 1),
        ensure_resident_identity(services, 1),
    )
    assert first.agent_id == second.agent_id != "old"
    assert first.thread_id == second.thread_id
    assert first.status == second.status == "ready"
    # Recreating the service/manager does not replace the durable identity.
    restarted = build_shared_services(db=services.db, paths=services.paths, config=services.config)
    again = await ensure_resident_identity(restarted, 1)
    assert again == services.resident_conversation_repo.get(1)
    assert again.agent_id == first.agent_id
    assert len(services.agent_repo.list_by_user(1)) == 2
    row = services.agent_repo.get(first.agent_id)
    assert row is not None and not row.is_shared and row.kind == "expert"
    config = json.loads(row.config_json)
    assert config["workspace_dir"] == str(services.paths.agent_workspace(row.agent_id))
    assert config["backend"]["root_dir"] == config["workspace_dir"]
    assert config["backend"]["type"] == "filesystem"
    assert not services.proactive_care_config_repo.get(row.agent_id).enabled
    thread = services.thread_repo.get(first.thread_id)
    assert thread.user_id == 1 and thread.agent_id == first.agent_id
    assert thread.session_key == f"{first.agent_id}:resident:1:dm"
    other = await ensure_resident_identity(services, 2)
    assert other.agent_id != first.agent_id and other.thread_id != first.thread_id


@pytest.mark.asyncio
async def test_failed_initialization_reuses_intent_and_original_ids(services, manager, monkeypatch):
    original = PathLayout.ensure_agent_workspace

    def unavailable(*args):
        raise OSError("workspace unavailable")

    monkeypatch.setattr(PathLayout, "ensure_agent_workspace", unavailable)
    failed = await ensure_resident_identity(services, 1)
    assert failed.status == "failed"
    assert failed.error_code == ErrorCode.AGENT_FAILED.value
    monkeypatch.setattr(PathLayout, "ensure_agent_workspace", original)
    ready = await ensure_resident_identity(services, 1)
    assert ready.status == "ready"
    assert ready.agent_id == failed.agent_id and ready.thread_id == failed.thread_id
    assert len(services.agent_repo.list_by_user(1)) == 1


@pytest.mark.asyncio
async def test_existing_private_identity_cannot_be_shared_reconfigured_or_deleted(
    services, manager
):
    binding = await ensure_resident_identity(services, 1)
    operations = [
        manager.set_shared(binding.agent_id, True),
        manager.update(binding.agent_id, kind="team"),
        manager.update_config_json(binding.agent_id, '{"backend":{"type":"local_shell"}}'),
        manager.delete(binding.agent_id),
        manager.delete_thread_checkpoint(binding.agent_id, binding.thread_id),
    ]
    for operation in operations:
        with pytest.raises(OctopError) as error:
            await operation
        assert error.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(OctopError):
        manager.persist_harness_config(binding.agent_id, {"memory": {"memory_enabled": True}})
    assert services.agent_repo.get(binding.agent_id) is not None
    assert services.thread_repo.get(binding.thread_id) is not None


@pytest.mark.asyncio
async def test_resident_thread_rejects_ordinary_reset_rebind_and_delete(services, manager):
    binding = await ensure_resident_identity(services, 1)
    registry = ThreadRegistry(session_repo=services.session_repo, thread_repo=services.thread_repo)
    key = services.thread_repo.get(binding.thread_id).session_key
    with pytest.raises(OctopError):
        await registry.reset_by_session_key(key)
    with pytest.raises(OctopError):
        await registry.reset(
            agent_id=binding.agent_id, user_id=1, channel_type="resident", channel_subject_id="1"
        )
    with pytest.raises(OctopError):
        await registry.rebind(
            session_key="other:dashboard:1:dm",
            thread_id=binding.thread_id,
            agent_id=binding.agent_id,
        )
    with pytest.raises(OctopError):
        registry.delete_thread(binding.thread_id)
    assert services.resident_conversation_repo.get(1).thread_id == binding.thread_id
    with pytest.raises(OctopError):
        registry.update_composer(binding.thread_id, model_ref="other/model")
    assert services.thread_repo.get(binding.thread_id).model_ref is None
    with pytest.raises(OctopError):
        manager.set_thread_model(binding.agent_id, binding.thread_id, "other/model")
    with pytest.raises(OctopError):
        manager.assert_standard_chat_allowed(binding.agent_id)
    services.agent_repo.create(agent_id="colleague", user_id=1, name="Colleague")
    with pytest.raises(OctopError) as error:
        manager.teams.validate_member_ids(
            SimpleNamespace(id=1, is_admin=False), [binding.agent_id, "colleague"]
        )
    assert error.value.code == ErrorCode.TEAM_MEMBER_INVALID


@pytest.mark.asyncio
async def test_scope_is_required_for_each_invocation_and_cancel(services, manager):
    binding = await ensure_resident_identity(services, 1)
    for owner, thread in [(None, None), (2, binding.thread_id), (1, "different-thread")]:
        if owner is None:
            with pytest.raises(OctopError):
                manager._assert_resident_execution(binding.agent_id, binding.thread_id)
        else:
            with resident_execution_scope(owner, thread), pytest.raises(OctopError):
                manager._assert_resident_execution(binding.agent_id, binding.thread_id)
    with pytest.raises(OctopError):
        manager.cancel_stream(binding.agent_id, binding.thread_id)
    with pytest.raises(OctopError):
        await manager.call(binding.agent_id, {})
    with pytest.raises(OctopError):
        async for _ in manager.stream(binding.agent_id, {"thread_id": binding.thread_id}):
            pass
    with resident_execution_scope(1, binding.thread_id):
        manager._assert_resident_execution(binding.agent_id, binding.thread_id)
        manager.cancel_stream(binding.agent_id, binding.thread_id)
    with pytest.raises(OctopError):
        manager._assert_resident_execution(binding.agent_id, binding.thread_id)


@pytest.mark.asyncio
async def test_runtime_has_no_capture_bootstrap_or_tool_execution(services, manager):
    binding = await ensure_resident_identity(services, 1)
    config = manager._build_harness_config(services.agent_repo.get(binding.agent_id))
    assert not config.memory_enabled and not config.memory_capture_enabled
    assert not config.memory_recall_inject_enabled and not config.memory_jsonl_enabled
    assert not config.bootstrap_enabled and not config.team_enabled
    assert not config.acp_runners and not config.acp_delegate_enabled
    assert not config.mcp_server_configs and not config.tools
    assert config.web_search_tools is True
    assert not config.subagents and not config.subagents_auto_load
    guard = next(item for item in config.middleware if isinstance(item, ResidentTextOnlyMiddleware))
    request = SimpleNamespace(override=lambda **values: values)
    handler = Mock(return_value="response")
    with pytest.raises(OctopError):
        guard.wrap_model_call(request, handler)
    handler.assert_not_called()
    with resident_execution_scope(1, binding.thread_id):
        assert guard.wrap_model_call(request, handler) == "response"
    handler.assert_called_once_with({"tools": []})
    tool_handler = AsyncMock()
    result = await guard.awrap_tool_call(
        SimpleNamespace(tool_call={"id": "unsafe", "name": "task"}), tool_handler
    )
    assert result.status == "error"
    tool_handler.assert_not_called()
