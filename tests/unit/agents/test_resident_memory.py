"""Explicit resident facts reuse Agent memory and bounded recall snapshots."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from octop_harness.config import ModelConfig, ProviderConfig
from octop_memory.core import Memory
from octop_memory.types import Candidate

from octop.config import OctopConfig
from octop.infra.agents.memory.backend import open_memory_kwargs
from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.agents.resident_memory import (
    ResidentMemoryDeprecate,
    ResidentMemoryReplace,
    ResidentMemoryWrite,
    _memory,
    create_resident_memory,
    decide_resident_memory_candidate,
    deprecate_resident_memory,
    extract_resident_candidates,
    list_resident_memories,
    replace_resident_memory,
    resident_memory_candidates,
    resident_memory_context,
    resident_memory_sources,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.usage.model_calls import attach_model_observer
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def private_memory(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    services.user_repo.create(username="owner", password_hash="x", role="user")
    binding = asyncio.run(ensure_resident_identity(services, 1))
    yield services, binding
    db.close()


def test_manual_management_is_the_existing_agent_memory_namespace_without_a_model(private_memory):
    services, binding = private_memory
    args = (services, 1, binding.agent_id, binding.thread_id)
    assert resident_memory_context(*args) == ""
    created = create_resident_memory(
        *args, ResidentMemoryWrite(assertion="I prefer concise answers", kind="Preference")
    )
    assert created.source_event_ids and created.quote_event_id
    row = services.agent_repo.get(binding.agent_id)
    cfg = json.loads(row.config_json)
    namespace, backend, config = open_memory_kwargs(
        agent_id=binding.agent_id,
        cfg=cfg,
        octop_config=services.config,
        workspace_dir=services.paths.agent_workspace(binding.agent_id),
    )
    memory = Memory(namespace=namespace, backend=backend, backend_config=config)
    try:
        assert namespace == f"agent_{binding.agent_id}"
        assert memory.get_atom(created.atom_id).assertion == created.assertion
    finally:
        memory.backend.close()
    corrected = replace_resident_memory(
        *args, created.atom_id, ResidentMemoryReplace(assertion="I prefer detailed answers")
    )
    recalled = json.loads(resident_memory_context(*args))
    assert recalled == [{"atom_id": corrected.atom_id, "assertion": corrected.assertion}]
    old = next(
        item
        for item in list_resident_memories(*args, 50, True).items
        if item.atom_id == created.atom_id
    )
    assert old.deprecated_at and old.superseded_by == corrected.atom_id
    deprecate_resident_memory(
        *args, corrected.atom_id, ResidentMemoryDeprecate(reason="Preference changed")
    )
    assert resident_memory_context(*args) == ""


def test_recall_is_bounded_and_reads_current_valid_facts(private_memory):
    services, binding = private_memory
    args = (services, 1, binding.agent_id, binding.thread_id)
    for number in range(25):
        create_resident_memory(*args, ResidentMemoryWrite(assertion=f"fact-{number}"))
    context = json.loads(resident_memory_context(*args))
    assert len(context) == 20
    assert context[0]["assertion"] == "fact-24"
    assert context[-1]["assertion"] == "fact-5"
    for number in range(6):
        create_resident_memory(
            *args, ResidentMemoryWrite(assertion=f"large-{number}" + "海" * 2000)
        )
    encoded = resident_memory_context(*args)
    assert len(encoded.encode()) <= 8192
    assert json.loads(encoded)[0]["assertion"].startswith("large-5")
    # Management reads remain bounded independently of the recall budget.
    page = list_resident_memories(*args, 3, False)
    assert len(page.items) == 3 and page.has_more


def _seed_candidate(services, binding, assertion):
    with _memory(services, 1, binding.agent_id, binding.thread_id) as memory:
        raw = memory.add_raw(
            assertion,
            host="octop-harness",
            event_type="user_message",
            thread_id=binding.thread_id,
            session_id=binding.thread_id,
        )
        candidate = Candidate(
            id=str(uuid4()),
            raw_event_ids=[raw.id],
            candidate_type="Preference",
            status="needs_review",
            title=assertion,
            assertion=assertion,
            verbatim_quote=assertion,
            quote_event_id=raw.id,
            subject_name="owner",
            subject_entity_type="User",
            target_entity_id=None,
            confidence="high",
            importance="medium",
            recommended_action="needs_review",
            promotion_reason="",
            extractor_version="test/v1",
            created_at=datetime.now(UTC),
            session_id=binding.thread_id,
        )
        memory.add_candidate(candidate)
        return candidate


def test_candidate_decisions_reuse_canonical_memory_and_preserve_evidence(private_memory):
    services, binding = private_memory
    args = (services, 1, binding.agent_id, binding.thread_id)
    candidate = _seed_candidate(services, binding, "I prefer precise examples.")
    page = resident_memory_candidates(*args, 50, "needs_review")
    assert [item.candidate_id for item in page.items] == [candidate.id]
    assert resident_memory_context(*args) == ""
    evidence = resident_memory_sources(*args, candidate.id, candidate=True)
    assert evidence.items[0].event_id == candidate.quote_event_id
    assert evidence.items[0].content == candidate.assertion
    approved = decide_resident_memory_candidate(*args, candidate.id, approve=True)
    assert approved.status == "promoted" and approved.decided_by == "user"
    assert json.loads(resident_memory_context(*args))[0]["assertion"] == candidate.assertion
    assert decide_resident_memory_candidate(*args, candidate.id, approve=True) == approved
    assert len(list_resident_memories(*args, 50, False).items) == 1
    other = _seed_candidate(services, binding, "A candidate the user rejects.")
    rejected = decide_resident_memory_candidate(*args, other.id, approve=False, reason="Incorrect")
    assert rejected.status == "rejected" and rejected.decided_by == "user"
    assert decide_resident_memory_candidate(*args, other.id, approve=False) == rejected
    assert len(list_resident_memories(*args, 50, False).items) == 1


def test_memory_source_turn_is_verified_and_source_location_survives(private_memory):
    services, binding = private_memory
    args = (services, 1, binding.agent_id, binding.thread_id)
    now = int(datetime.now(UTC).timestamp() * 1000)
    source, _ = services.resident_request_repo.create(
        1,
        binding.thread_id,
        "source-request",
        "source-turn",
        "My preferred style.",
        now,
        timezone=services.config.default_timezone,
    )
    memory = create_resident_memory(
        *args,
        ResidentMemoryWrite(assertion="I prefer concise text.", source_turn_id=source.turn_id),
    )
    evidence = resident_memory_sources(*args, memory.atom_id)
    assert len(evidence.items) == 1
    assert evidence.items[0].source_turn_id == source.turn_id
    assert evidence.items[0].history_item_id == f"turn:{source.turn_id}"
    assert evidence.items[0].source_message_id == f"resident:{source.turn_id}:user"
    assert "My preferred style." not in evidence.items[0].content
    with pytest.raises(OctopError) as raised:
        create_resident_memory(
            *args, ResidentMemoryWrite(assertion="Unverified", source_turn_id="not-mine")
        )
    assert raised.value.code == "NOT_FOUND"


def _extraction_registry(message, *, failing=False):
    class FailingModel(FakeMessagesListChatModel):
        def _generate(self, *args, **kwargs):
            raise RuntimeError("synthetic-private-provider-body")

    provider = ProviderConfig(
        id="synthetic", api_key="fixture-only", base_url="http://fixture.invalid/v1"
    )
    model_type = FailingModel if failing else FakeMessagesListChatModel
    model = attach_model_observer(model_type(responses=[message]), provider, ModelConfig(id="test"))
    factory = SimpleNamespace(get_chat_model=Mock(return_value=model))
    return SimpleNamespace(
        providers=SimpleNamespace(is_model_ref_usable=Mock(return_value=True)),
        get_agent=Mock(return_value=SimpleNamespace(model_factory=factory)),
        start=Mock(),
    ), factory


def _captured_observation(services, binding):
    with _memory(services, 1, binding.agent_id, binding.thread_id) as memory:
        memory.add_raw(
            "Synthetic memory audit: I prefer short precise examples.",
            event_type="user_message",
            thread_id=binding.thread_id,
            session_id=binding.thread_id,
        )


async def test_extraction_blocks_exhausted_quota_before_starting_or_calling_a_model(private_memory):
    services, binding = private_memory
    registry, factory = _extraction_registry(AIMessage(content='{"candidates": []}'))
    services.user_policy_repo.set(1, "token_quota", "0")
    with pytest.raises(OctopError) as raised:
        await extract_resident_candidates(
            services, registry, 1, binding.agent_id, binding.thread_id, "synthetic/test"
        )
    assert raised.value.code == "TOKEN_QUOTA_EXCEEDED"
    registry.get_agent.assert_not_called()
    registry.start.assert_not_called()
    factory.get_chat_model.assert_not_called()
    assert services.model_call_repo.list_page(user_id=1) == []
    assert services.usage_repo.total_tokens_for_user(1) == 0


@pytest.mark.parametrize("usage_source", ["usage_metadata", "response_metadata"])
async def test_extraction_records_one_scoped_invocation_and_usage_without_private_content(
    private_memory, usage_source
):
    services, binding = private_memory
    _captured_observation(services, binding)
    metadata = (
        {"usage_metadata": {"input_tokens": 19, "output_tokens": 7, "total_tokens": 26}}
        if usage_source == "usage_metadata"
        else {
            "response_metadata": {
                "token_usage": {"prompt_tokens": 19, "completion_tokens": 7, "total_tokens": 26}
            }
        }
    )
    registry, factory = _extraction_registry(AIMessage(content='{"candidates": []}', **metadata))
    args = (services, registry, 1, binding.agent_id, binding.thread_id, "synthetic/test")
    result = await extract_resident_candidates(*args)
    assert result.status == "completed" and result.events_extracted == 1
    calls = services.model_call_repo.list_page(user_id=1)
    assert len(calls) == 1
    call = calls[0]
    assert (call.purpose, call.agent_id, call.thread_id, call.status) == (
        "resident_memory_extract",
        binding.agent_id,
        binding.thread_id,
        "succeeded",
    )
    assert not call.task_id and not call.project_id
    assert json.loads(call.usage_json)["input_tokens"] == 19
    assert "Synthetic memory audit" not in call.metadata_json + call.usage_json
    with services.db.connect() as conn:
        usage = conn.execute(
            "SELECT * FROM usage_log WHERE source='resident_memory_extract'"
        ).fetchall()
    assert len(usage) == 1
    assert (usage[0]["user_id"], usage[0]["agent_id"], usage[0]["thread_id"]) == (
        1,
        binding.agent_id,
        binding.thread_id,
    )
    assert (usage[0]["input_tokens"], usage[0]["output_tokens"], usage[0]["model_calls"]) == (
        19,
        7,
        1,
    )
    assert services.usage_repo.total_tokens_for_user(1) == 26
    assert (await extract_resident_candidates(*args)).status == "empty"
    factory.get_chat_model.assert_called_once_with("synthetic/test")
    assert len(services.model_call_repo.list_page(user_id=1)) == 1


async def test_failed_extraction_records_a_failed_invocation_without_private_error_text(
    private_memory, caplog
):
    services, binding = private_memory
    _captured_observation(services, binding)
    registry, factory = _extraction_registry(AIMessage(content="unused"), failing=True)
    result = await extract_resident_candidates(
        services, registry, 1, binding.agent_id, binding.thread_id, "synthetic/test"
    )
    assert result.status == "failed" and result.error_code == "RESIDENT_MEMORY_EXTRACTION_FAILED"
    calls = services.model_call_repo.list_page(user_id=1)
    assert len(calls) == 1 and calls[0].status == "failed"
    assert calls[0].purpose == "resident_memory_extract"
    assert "synthetic-private-provider-body" not in (
        calls[0].metadata_json + calls[0].usage_json + result.model_dump_json() + caplog.text
    )
    factory.get_chat_model.assert_called_once_with("synthetic/test")
    assert services.usage_repo.total_tokens_for_user(1) == 0


async def test_parser_retry_rechecks_quota_and_does_not_make_an_unbilled_second_call(
    private_memory,
):
    services, binding = private_memory
    _captured_observation(services, binding)
    registry, factory = _extraction_registry(
        AIMessage(
            content="not valid candidate JSON",
            usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        )
    )
    services.user_policy_repo.set(1, "token_quota", "18")
    with pytest.raises(OctopError) as raised:
        await extract_resident_candidates(
            services, registry, 1, binding.agent_id, binding.thread_id, "synthetic/test"
        )
    assert raised.value.code == "TOKEN_QUOTA_EXCEEDED"
    factory.get_chat_model.assert_called_once_with("synthetic/test")
    assert len(services.model_call_repo.list_page(user_id=1)) == 1
    assert services.usage_repo.total_tokens_for_user(1) == 18
    assert (
        resident_memory_candidates(services, 1, binding.agent_id, binding.thread_id, 50, None).items
        == []
    )
