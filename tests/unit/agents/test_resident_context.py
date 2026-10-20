"""Compaction uses an owned idle thread and settles checkpoint writes before unlocking."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Response
from octop_harness.compaction import CompactResult
from octop_harness.context_usage import ContextUsage

from octop.config import OctopConfig
from octop.infra.agents.experts.catalog import ExpertCatalog
from octop.infra.agents.resident_context import compact_context, context_usage
from octop.infra.agents.resident_conversation import ResidentConversationRuntime
from octop.infra.agents.resident_identity import resident_execution_allowed
from octop.infra.agents.resident_profiles import (
    ResidentProfileArchive,
    ResidentProfileCreate,
    archive_profile,
    create_profile,
    get_profile,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout


@pytest.fixture
async def context_env(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    services.user_repo.create(username="owner", password_hash="x", role="user")
    services.user_repo.create(username="other", password_hash="x", role="user")
    root = tmp_path / "experts"
    directory = root / "writer"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": "writer",
                "label": {"en": "Writer", "zh": "写作助手"},
                "prompt_files": ["SOUL.md"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "SOUL.md").write_text("Ask about the audience.", encoding="utf-8")
    catalog = ExpertCatalog(root)
    catalog.refresh()
    profile = await create_profile(
        services,
        catalog,
        1,
        ResidentProfileCreate(
            request_id="create-writer",
            name="Writer",
            template_kind="builtin",
            template_id="writer",
        ),
    )
    agent = SimpleNamespace(
        aget_context_usage=AsyncMock(
            return_value=ContextUsage(
                max_tokens=4096,
                used_tokens=200,
                input_tokens=150,
                output_tokens=50,
                source="model_request",
                segments={"conversation": 180, "system_prompt": 20},
            )
        ),
        acompact_conversation=AsyncMock(
            return_value=CompactResult(ok=False, reason="nothing_to_compact")
        ),
    )
    registry = SimpleNamespace(
        get_agent=Mock(return_value=agent),
        start=AsyncMock(),
        resolve_context_max_tokens=Mock(return_value=4096),
        providers=SimpleNamespace(is_model_ref_usable=Mock(return_value=True)),
    )
    runtime = ResidentConversationRuntime(services, registry, Mock())
    runtime._configuration = Mock(return_value={"model_ref": "configured/model"})
    yield services, runtime, profile, agent
    db.close()


@pytest.mark.asyncio
async def test_context_usage_reads_only_owned_thread_and_returns_typed_counts(context_env):
    _, runtime, profile, agent = context_env
    usage = await context_usage(runtime, 1, profile.agent_id, profile.thread_id)
    assert usage.max_tokens == 4096 and usage.used_tokens == 200
    assert {segment.key: segment.tokens for segment in usage.segments} == {
        "conversation": 180,
        "system_prompt": 20,
    }
    assert usage.source == "model_request"
    agent.acompact_conversation.assert_not_called()
    with pytest.raises(OctopError) as error:
        await context_usage(runtime, 2, profile.agent_id, profile.thread_id)
    assert error.value.code == ErrorCode.NOT_FOUND
    agent.aget_context_usage.assert_awaited_once_with(profile.thread_id, max_tokens=4096)


@pytest.mark.asyncio
async def test_compaction_uses_resolved_model_and_omits_host_paths_and_errors(context_env):
    services, runtime, profile, agent = context_env
    before = services.thread_repo.get(profile.thread_id)

    async def compact(thread_id, *, model):
        assert resident_execution_allowed(1, thread_id)
        assert model == "configured/model"
        return CompactResult(
            ok=True,
            summarized_count=10,
            preserved_count=6,
            removed_tokens=120,
            file_path="/private/credentials/host-secret.md",
            error="sensitive diagnostic",
            display_path=".octop/conversation_history/stable.md",
        )

    agent.acompact_conversation.side_effect = compact
    result = await compact_context(runtime, 1, profile.agent_id, profile.thread_id)
    assert result.ok and result.removed_tokens == 120
    assert result.display_path == ".octop/conversation_history/stable.md"
    assert (
        "host-secret" not in result.model_dump_json()
        and "sensitive" not in result.model_dump_json()
    )
    assert services.thread_repo.get(profile.thread_id) == before
    assert services.resident_request_repo.list(profile.thread_id, 1) == []


@pytest.mark.asyncio
async def test_compaction_refuses_queued_turn_and_archived_instance(context_env):
    services, runtime, profile, agent = context_env
    turn, _ = services.resident_request_repo.create(
        1, profile.thread_id, "send", "turn", "Hello", 1800000000000
    )
    with pytest.raises(OctopError) as error:
        await compact_context(runtime, 1, profile.agent_id, profile.thread_id)
    assert error.value.code == ErrorCode.AGENT_BUSY
    services.resident_request_repo.cancel(
        1, profile.thread_id, "cancel", turn.turn_id, 1800000000001
    )
    archive_profile(
        services,
        1,
        profile.profile_id,
        ResidentProfileArchive(expected_revision=profile.revision, archived=True),
    )
    with pytest.raises(OctopError) as error:
        await compact_context(runtime, 1, profile.agent_id, profile.thread_id)
    assert error.value.code == ErrorCode.RESIDENT_PROFILE_ARCHIVED
    agent.acompact_conversation.assert_not_called()
    # Archived context remains readable.
    assert (await context_usage(runtime, 1, profile.agent_id, profile.thread_id)).used_tokens == 200


@pytest.mark.asyncio
async def test_disconnect_and_repeated_cancel_drain_compaction_before_unlock(context_env):
    _, runtime, profile, agent = context_env
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def compact(thread_id, *, model):
        entered.set()
        await release.wait()
        settled.set()
        return CompactResult(ok=True)

    agent.acompact_conversation.side_effect = compact
    request = asyncio.create_task(compact_context(runtime, 1, profile.agent_id, profile.thread_id))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done() and not settled.is_set()
        with pytest.raises(OctopError) as error:
            async with runtime.thread_scope(profile.thread_id, require_idle=True):
                pytest.fail("Checkpoint lock was released while the operation was pending")
        assert error.value.code == ErrorCode.AGENT_BUSY
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert settled.is_set()
    async with runtime.thread_scope(profile.thread_id, require_idle=True):
        pass


@pytest.mark.asyncio
async def test_compaction_reports_unusable_model_without_invoking_summary(context_env):
    _, runtime, profile, agent = context_env
    runtime.registry.providers.is_model_ref_usable.return_value = False
    with pytest.raises(OctopError) as error:
        await compact_context(runtime, 1, profile.agent_id, profile.thread_id)
    assert error.value.code == ErrorCode.RESIDENT_MODEL_UNAVAILABLE
    agent.acompact_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_archive_disconnect_drains_transaction_before_releasing_thread_lock(
    context_env, monkeypatch
):
    from octop.api.routers import resident_profiles as router

    services, runtime, profile, _ = context_env
    entered, release = threading.Event(), threading.Event()
    original = router.archive_profile

    def slow_archive(*args):
        entered.set()
        release.wait(3)
        return original(*args)

    monkeypatch.setattr(router, "archive_profile", slow_archive)
    server = SimpleNamespace(
        services=services, app_runtime=SimpleNamespace(resident_conversations=runtime)
    )
    request = asyncio.create_task(
        router.archive(
            profile.profile_id,
            ResidentProfileArchive(expected_revision=profile.revision, archived=True),
            Response(),
            user=SimpleNamespace(id=1),
            server=server,
        )
    )
    assert await asyncio.to_thread(entered.wait, 3)
    try:
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        with pytest.raises(OctopError):
            async with runtime.thread_scope(profile.thread_id, require_idle=True):
                pytest.fail("Archive transaction still owns the thread lock")
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert get_profile(services, 1, profile.profile_id).archived
