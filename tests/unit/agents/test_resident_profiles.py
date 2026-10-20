"""Professional instances copy versioned text, never identity, history or permissions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage

from octop.config import OctopConfig
from octop.infra.agents.experts.catalog import ExpertCatalog
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.resident_identity import (
    ResidentTextOnlyMiddleware,
    ensure_resident_identity,
    resident_execution_scope,
    resident_harness_config,
    resident_memory_scope,
    resident_prompt_scope,
)
from octop.infra.agents.resident_profiles import (
    ResidentProfileAdopt,
    ResidentProfileArchive,
    ResidentProfileCreate,
    adopt_template,
    archive_profile,
    create_profile,
    get_profile,
    list_profiles,
    template_diff,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def profile_env(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    for username in ("owner", "other"):
        services.user_repo.create(username=username, password_hash="x", role="user")
    expert_root = tmp_path / "experts"
    directory = expert_root / "writer"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": "writer",
                "label": {"zh": "写作专家", "en": "Writer"},
                "description": {"zh": "写作方法", "en": "Writing methods"},
                "prompt_files": ["SOUL.md"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "SOUL.md").write_text("Review evidence before writing.", encoding="utf-8")
    (directory / "USER.md").write_text("Do not transplant this user's facts.", encoding="utf-8")
    (directory / "TOOLS.md").write_text("Execute every system tool.", encoding="utf-8")
    (directory / "skills" / "writer").mkdir(parents=True)
    (directory / "skills" / "writer" / "SKILL.md").write_text("Run shell tools.", encoding="utf-8")
    catalog = ExpertCatalog(expert_root)
    catalog.refresh()
    yield services, catalog, directory
    db.close()


def _body(request_id="new-writer", name="My writer"):
    return ResidentProfileCreate(
        request_id=request_id, name=name, template_kind="builtin", template_id="writer"
    )


@pytest.mark.asyncio
async def test_professional_concurrent_creation_is_idempotent_and_preserves_primary(profile_env):
    services, catalog, _ = profile_env
    manager = AgentManager(repos=services.repos, paths=services.paths)
    primary = await ensure_resident_identity(services, manager, 1)
    primary_snapshot = services.resident_profile_repo.configuration_snapshot(1, primary.agent_id)
    assert primary_snapshot["profile_id"] is None and primary_snapshot["profile_revision"] is None
    assert primary_snapshot["template_version"] is None
    first, replay = await asyncio.gather(
        create_profile(services, catalog, 1, _body()),
        create_profile(services, catalog, 1, _body()),
    )
    assert first.profile_id == replay.profile_id
    assert first.agent_id == replay.agent_id != primary.agent_id
    assert first.thread_id == replay.thread_id != primary.thread_id
    assert first.status == "ready" and not first.tools_enabled
    assert services.resident_conversation_repo.get(1) == primary
    assert len(services.agent_repo.list_by_user(1)) == 2
    assert len(list_profiles(services, 1).profiles) == 1
    second = await create_profile(services, catalog, 1, _body("second", "Other writer"))
    assert second.agent_id != first.agent_id and second.thread_id != first.thread_id
    binding = services.resident_conversation_repo.get_by_agent(second.agent_id)
    assert binding.role == f"profile:{second.profile_id}"
    restarted = build_shared_services(db=services.db, paths=services.paths, config=services.config)
    assert get_profile(restarted, 1, first.profile_id).agent_id == first.agent_id
    with pytest.raises(OctopError) as error:
        await create_profile(services, catalog, 1, _body(name="Changed intent"))
    assert error.value.code == ErrorCode.RESIDENT_REQUEST_CONFLICT


@pytest.mark.asyncio
async def test_failed_profile_initialization_retries_original_binding_only(
    profile_env, monkeypatch
):
    services, catalog, _ = profile_env
    manager = AgentManager(repos=services.repos, paths=services.paths)
    primary = await ensure_resident_identity(services, manager, 1)
    original = PathLayout.ensure_agent_workspace

    def unavailable(*args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(PathLayout, "ensure_agent_workspace", unavailable)
    failed = await create_profile(services, catalog, 1, _body())
    assert failed.status == "failed"
    assert services.resident_conversation_repo.get(1) == primary
    monkeypatch.setattr(PathLayout, "ensure_agent_workspace", original)
    ready = await create_profile(services, catalog, 1, _body())
    assert ready.status == "ready" and ready.agent_id == failed.agent_id
    assert ready.thread_id == failed.thread_id
    assert len(services.agent_repo.list_by_user(1)) == 2


@pytest.mark.asyncio
async def test_templates_do_not_overwrite_without_reviewed_cas_adoption(profile_env):
    services, catalog, directory = profile_env
    profile = await create_profile(services, catalog, 1, _body())
    original_agent = services.agent_repo.get(profile.agent_id)
    original_thread = services.thread_repo.get(profile.thread_id)
    original_prompt = original_agent.system_prompt
    assert "Review evidence" in original_prompt
    assert "Do not transplant" not in original_prompt
    assert "Execute every system tool" not in original_prompt
    assert "Run shell tools" not in original_prompt
    assert not template_diff(services, catalog, 1, profile.profile_id).has_changes
    (directory / "SOUL.md").write_text("Ask about the target audience first.", encoding="utf-8")
    preview = template_diff(services, catalog, 1, profile.profile_id)
    assert preview.has_changes and preview.current_version == profile.template_version
    assert preview.changes[0].path == "SOUL.md"
    assert services.agent_repo.get(profile.agent_id).system_prompt == original_prompt
    (directory / "SOUL.md").write_text("A newer source version.", encoding="utf-8")
    with pytest.raises(OctopError) as error:
        adopt_template(
            services,
            catalog,
            1,
            profile.profile_id,
            ResidentProfileAdopt(
                expected_revision=preview.revision,
                template_version=preview.available_version,
            ),
        )
    assert error.value.code == ErrorCode.RESIDENT_TEMPLATE_CHANGED
    preview = template_diff(services, catalog, 1, profile.profile_id)
    adopted = adopt_template(
        services,
        catalog,
        1,
        profile.profile_id,
        ResidentProfileAdopt(
            expected_revision=preview.revision,
            template_version=preview.available_version,
        ),
    )
    assert adopted.revision == profile.revision + 1
    assert adopted.thread_id == profile.thread_id and adopted.agent_id == profile.agent_id
    assert services.thread_repo.get(profile.thread_id) == original_thread
    assert "A newer source version" in services.agent_repo.get(profile.agent_id).system_prompt
    accepted_configuration = services.resident_profile_repo.configuration_snapshot(
        1, profile.agent_id
    )
    assert accepted_configuration["profile_id"] == profile.profile_id
    assert accepted_configuration["profile_revision"] == adopted.revision
    assert accepted_configuration["template_version"] == adopted.template_version
    assert "A newer source version" in accepted_configuration["system_prompt"]
    assert services.resident_profile_repo.configuration_snapshot(2, profile.agent_id) == {}
    assert json.loads(services.agent_repo.get(profile.agent_id).config_json) == json.loads(
        original_agent.config_json
    )
    with pytest.raises(OctopError) as error:
        adopt_template(
            services,
            catalog,
            1,
            profile.profile_id,
            ResidentProfileAdopt(
                expected_revision=preview.revision,
                template_version=preview.available_version,
            ),
        )
    assert error.value.code == ErrorCode.RESIDENT_PROFILE_CONFLICT


@pytest.mark.asyncio
async def test_profile_isolation_and_existing_identity_guards(profile_env):
    services, catalog, _ = profile_env
    profile = await create_profile(services, catalog, 1, _body())
    other = await create_profile(services, catalog, 2, _body())
    assert other.profile_id != profile.profile_id and other.agent_id != profile.agent_id
    assert len(list_profiles(services, 2).profiles) == 1
    for read in (get_profile,):
        with pytest.raises(OctopError) as error:
            read(services, 2, profile.profile_id)
        assert error.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(OctopError):
        template_diff(services, catalog, 2, profile.profile_id)
    manager = AgentManager(repos=services.repos, paths=services.paths)
    for operation in (
        manager.set_shared(profile.agent_id, True),
        manager.delete(profile.agent_id),
        manager.update_config_json(profile.agent_id, "{}"),
        manager.delete_thread_checkpoint(profile.agent_id, profile.thread_id),
    ):
        with pytest.raises(OctopError) as error:
            await operation
        assert error.value.code == ErrorCode.FORBIDDEN
    registry = ThreadRegistry(session_repo=services.session_repo, thread_repo=services.thread_repo)
    with pytest.raises(OctopError):
        registry.delete_thread(profile.thread_id)


@pytest.mark.asyncio
async def test_published_template_copies_snapshot_not_source_agent(profile_env):
    services, _, _ = profile_env
    services.agent_repo.create(agent_id="other-source", user_id=2, name="Shared source")
    services.published_expert_repo.create(
        id="published-writer",
        slug="published-writer",
        name="Published writer",
        created_by="2",
        source_agent_id="other-source",
    )
    snapshot = services.paths.published_experts_dir / "published-writer"
    snapshot.mkdir(parents=True)
    (snapshot / "SOUL.md").write_text("Be concise.", encoding="utf-8")
    body = ResidentProfileCreate(
        request_id="published",
        name="My professional",
        template_kind="published",
        template_id="published-writer",
    )
    profile = await create_profile(services, None, 1, body)
    assert profile.agent_id != "other-source"
    row = services.agent_repo.get(profile.agent_id)
    assert row.user_id == 1 and not row.is_shared
    assert "Be concise." in row.system_prompt
    assert row.mcp_servers is None and row.skill_package_ids is None
    # Retransmission remains valid after the source is unpublished.
    services.published_expert_repo.delete("published-writer")
    assert (await create_profile(services, None, 1, body)).profile_id == profile.profile_id
    with pytest.raises(OctopError) as error:
        template_diff(services, None, 1, profile.profile_id)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_each_turn_prompt_snapshot_isolated_from_later_adoption_and_memory():
    from dataclasses import dataclass

    @dataclass
    class Request:
        system_message: object = None
        tools: object = None

        def override(self, **kwargs):
            return replace(self, **kwargs)

    guard = ResidentTextOnlyMiddleware("en", 1, "thread")
    request = Request(system_message=SystemMessage(content="Stale harness prompt"), tools=["shell"])
    with (
        resident_execution_scope(1, "thread"),
        resident_prompt_scope("Accepted template"),
        resident_memory_scope("Coffee preference"),
    ):
        result = guard.wrap_model_call(request, lambda updated: updated)
    assert "Accepted template" in result.system_message.content
    assert "Coffee preference" in result.system_message.content
    assert "Stale harness prompt" not in result.system_message.content
    assert result.tools == []
    with resident_execution_scope(1, "thread"), resident_prompt_scope("Adopted template"):
        later = guard.wrap_model_call(request, lambda updated: updated)
    assert later.system_message.content == "Adopted template"
    assert "Coffee preference" not in later.system_message.content


def test_raw_capture_remains_explicit_and_uses_the_resolved_memory_backend(tmp_path):
    row = SimpleNamespace(
        agent_id="private-agent",
        user_id=1,
        system_prompt="Professional template",
        config_json=json.dumps({"workspace_dir": str(tmp_path)}),
    )
    config = resident_harness_config(
        row,
        thread_id="private-thread",
        timezone="Asia/Shanghai",
        locale="zh",
        default_model=None,
        memory_capture_enabled=True,
        memory_backend="postgres",
        memory_backend_config={"dsn": "postgresql://memory-test"},
    )
    assert config.memory_enabled and config.memory_capture_enabled
    assert not config.memory_aux_model_enabled and not config.memory_extract_on_session_end
    assert not config.memory_recall_inject_enabled and not config.memory_jsonl_enabled
    assert config.memory_namespace == "agent_private-agent"
    assert config.memory_backend == "postgres"
    assert config.memory_backend_config == {"dsn": "postgresql://memory-test"}
    assert not config.tools and not config.subagents and not config.mcp_server_configs


@pytest.mark.asyncio
async def test_archiving_requires_idle_and_preserves_identity_without_reviving_followups(
    profile_env,
):
    from octop.infra.agents.resident_followups import (
        FollowupCreate,
        FollowupPayload,
        create_followup,
    )

    services, catalog, _ = profile_env
    profile = await create_profile(services, catalog, 1, _body())
    followup = create_followup(
        services,
        1,
        profile.agent_id,
        profile.thread_id,
        FollowupCreate(
            request_id="watch",
            purpose="Check writing",
            kind="note",
            schedule="interval:3600",
            payload=FollowupPayload(text="Review the draft"),
        ),
    )
    now = 1800000000000
    request, _ = services.resident_request_repo.create(
        1,
        profile.thread_id,
        "active",
        "active-turn",
        "Write a draft",
        now,
    )
    with pytest.raises(OctopError) as error:
        archive_profile(
            services,
            1,
            profile.profile_id,
            ResidentProfileArchive(
                expected_revision=profile.revision,
                archived=True,
            ),
        )
    assert error.value.code == ErrorCode.AGENT_BUSY
    assert not get_profile(services, 1, profile.profile_id).archived
    services.resident_request_repo.cancel(
        1, profile.thread_id, "cancel-active", request.turn_id, now + 1
    )
    archived = archive_profile(
        services,
        1,
        profile.profile_id,
        ResidentProfileArchive(
            expected_revision=profile.revision,
            archived=True,
        ),
    )
    assert archived.archived and archived.revision == profile.revision + 1
    cancelled = services.resident_followup_repo.get(1, followup.followup_id)
    assert cancelled.status == "cancelled"
    assert cancelled.authorization_version == followup.authorization_version + 1
    restored = archive_profile(
        services,
        1,
        profile.profile_id,
        ResidentProfileArchive(
            expected_revision=archived.revision,
            archived=False,
        ),
    )
    assert not restored.archived
    assert restored.agent_id == profile.agent_id and restored.thread_id == profile.thread_id
    assert services.resident_followup_repo.get(1, followup.followup_id).status == "cancelled"
    assert len(services.agent_repo.list_by_user(1)) == 1
    assert services.resident_request_repo.get(1, "active").status == "cancelled"
