"""Durable automatic extraction, conservative general promotion and revocation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from octop_memory.core import Memory
from octop_memory.ports.llm import MockLLMClient
from tests.unit.projects.test_expert_execution import services  # noqa: F401

from octop.infra.db.migrate import run_migrations
from octop.infra.errors import OctopError
from octop.infra.projects import expert_memory_extraction as extraction
from octop.infra.projects.expert_memory import ExpertMemory
from octop.infra.projects.expert_memory_management import (
    ExpertMemoryPolicyPatch,
    ExpertMemoryQuery,
    memory_policy,
    query_memory,
    update_memory_policy,
)
from octop.infra.projects.expert_profiles import resolve_expert_profile
from octop.infra.usage.model_calls import attach_model_observer

_PREFERENCE = "I always prefer concise answers across projects."
_PROJECT = "The Mercury project deploys on Tuesday."


async def capture_job(
    bundle,
    *,
    owner=1,
    task="memory-task",
    turn="memory-turn",
    project=None,
    expert="expert-one",
    prompt=_PREFERENCE + " " + _PROJECT,
    source_kind="user",
):
    with bundle.db.transaction() as conn:
        if project:
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES(?,?,?,1,1) ON CONFLICT(project_id) DO NOTHING",
                (project, owner, project),
            )
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,project_id,title,request_id,selected_expert_id,created_at,updated_at) VALUES(?,?,?,'Memory',?,?,1,1)",
            (task, owner, project, task, expert),
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,configuration_json,model_ref,created_at,updated_at) VALUES(?,?,?,?,'running','{}','local/model',1,1)",
            (turn, task, turn, prompt),
        )
    memory = ExpertMemory(
        bundle,
        owner,
        task,
        turn,
        project,
        resolve_expert_profile(bundle, owner, expert),
        enabled=None,
    )
    await memory.capture(prompt, "I have recorded the requested facts.", source_kind=source_kind)
    with bundle.db.transaction() as conn:
        conn.execute("UPDATE project_task_turns SET status='completed' WHERE turn_id=?", (turn,))
    rows = bundle.expert_memory_job_repo.list_for_task(task, owner)
    assert len(rows) == 1
    return memory, rows[0]


def response(turn="memory-turn"):
    raw_id = hashlib.sha256(f"{turn}:user_message".encode()).hexdigest()

    def item(content, kind, entity):
        return {
            "candidate_type": kind,
            "status": "pending",
            "title": kind,
            "assertion": content,
            "verbatim_quote": content,
            "quote_event_id": raw_id,
            "subject": {"name": "User" if entity == "User" else "Mercury", "entity_type": entity},
            "source_refs": [raw_id],
            "confidence": "high",
            "importance": "medium",
            "recommended_action": "promote",
            "promotion_reason": "Explicit user source.",
        }

    return json.dumps(
        {"candidates": [item(_PREFERENCE, "Preference", "User"), item(_PROJECT, "Fact", "Project")]}
    )


def prepared(worker, job, turn="memory-turn"):
    source = worker._sources(job)
    return worker._prepare(job, source, MockLLMClient(default_response=response(turn)))


async def test_capture_queue_is_durable_and_deduplicated_and_schema56_repairs(services):  # noqa: F811
    memory, original = await capture_job(services)
    await memory.capture(_PREFERENCE + " " + _PROJECT, "I have recorded the requested facts.")
    assert len(services.expert_memory_job_repo.list_for_task(memory.task_id, 1)) == 1
    assert original.status == "pending"
    run_migrations(services.db)
    assert services.expert_memory_job_repo.get(original.job_id) == original


async def test_scoped_extraction_and_only_explicit_user_preference_promotes_general(services):  # noqa: F811
    memory, _ = await capture_job(services, project="project-one")
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    result = prepared(worker, job)
    services.expert_memory_job_repo.update(
        job, phase="ready", result_json=result, now=extraction._now()
    )
    worker._apply(job, result)
    before = memory.management_operation("scope", lambda store: len(store.list_atoms()))
    worker._apply(job, result)
    assert (
        memory.management_operation("scope", lambda store: len(store.list_atoms())) == before == 2
    )
    general = memory.management_operation("general", lambda store: store.list_atoms())
    assert [item.assertion for item in general] == [_PREFERENCE]
    evidence = memory.management_operation("general", lambda store: store.list_raw())
    assert len(evidence) == 1 and evidence[0].content == _PREFERENCE
    assert evidence[0].payload["source_task_id"] == memory.task_id
    other = ExpertMemory(
        services, 1, "other-task", "other-turn", "project-two", memory.profile, enabled=None
    )
    assert "concise" in await other.prompt("concise")
    assert not other.management_operation("scope", lambda store: store.list_atoms())
    assert not memory._call("general", "search", query="Mercury")["hits"]


async def test_persisted_proposal_resumes_after_cross_store_ack_loss(services):  # noqa: F811
    memory, _ = await capture_job(services)
    first = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(first.worker_id, extraction._now())
    result = prepared(first, job)
    services.expert_memory_job_repo.update(
        job, phase="applying", result_json=result, now=extraction._now()
    )
    first._apply(job, result)
    with services.db.transaction() as conn:
        conn.execute("UPDATE expert_memory_jobs SET lease_until=0 WHERE job_id=?", (job.job_id,))
    second = extraction.ExpertMemoryWorker(services)
    recovered = services.expert_memory_job_repo.claim(second.worker_id, extraction._now())
    assert recovered.phase == "applying" and recovered.result_json == result
    await second.execute(recovered)
    assert services.expert_memory_job_repo.get(job.job_id).status == "completed"
    assert len(memory.management_operation("scope", lambda store: store.list_atoms())) == 2
    assert len(memory.management_operation("general", lambda store: store.list_atoms())) == 1


async def test_unknown_provider_result_is_not_replayed(services):  # noqa: F811
    _, original = await capture_job(services)
    job = services.expert_memory_job_repo.claim("dead-worker", extraction._now())
    services.expert_memory_job_repo.update(job, phase="extracting", now=0)
    assert services.expert_memory_job_repo.claim("new-worker", 200_000) is None
    assert services.expert_memory_job_repo.get(original.job_id).status == "uncertain"


@pytest.mark.parametrize(
    "revocation", ["capture", "extract", "expert", "shared", "user", "cancel", "external"]
)
async def test_revocation_or_cancel_prevents_all_canonical_writes(services, revocation):  # noqa: F811
    memory, _ = await capture_job(services)
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    result = prepared(worker, job)
    services.expert_memory_job_repo.update(
        job, phase="ready", result_json=result, now=extraction._now()
    )
    row = services.agent_repo.get("expert-one")
    config = json.loads(row.config_json)
    with services.db.transaction() as conn:
        if revocation in {"capture", "extract"}:
            config["memory"][
                "capture_enabled" if revocation == "capture" else "auto_extract_enabled"
            ] = False
            conn.execute(
                "UPDATE agents SET config_json=? WHERE agent_id='expert-one'", (json.dumps(config),)
            )
        elif revocation == "expert":
            conn.execute("UPDATE agents SET enabled=0 WHERE agent_id='expert-one'")
        elif revocation == "shared":
            conn.execute("UPDATE agents SET user_id=2,is_shared=0 WHERE agent_id='expert-one'")
        elif revocation == "user":
            conn.execute("UPDATE users SET disabled=1 WHERE id=1")
        elif revocation == "external":
            conn.execute(
                "UPDATE project_task_turns SET configuration_json=? WHERE turn_id=?",
                (json.dumps({"source_origin": {"source": {"source_kind": "im"}}}), job.turn_id),
            )
        else:
            conn.execute(
                "UPDATE project_task_turns SET cancel_requested=1 WHERE turn_id=?", (job.turn_id,)
            )
    await worker.execute(replace(job, phase="ready", result_json=result))
    assert services.expert_memory_job_repo.get(job.job_id).status == "cancelled"
    store = Memory(
        namespace=memory.namespaces["scope"],
        backend_config={"db_path": str(services.paths.root / "expert" / "memory.sqlite")},
    )
    try:
        assert store.list_atoms() == []
    finally:
        store.backend.close()


@pytest.mark.parametrize("aux_model", [None, "local/aux", "removed/aux"])
async def test_real_fake_model_uses_configured_model_and_records_usage(
    services,  # noqa: F811
    monkeypatch,
    aux_model,
):
    _, original = await capture_job(services)
    services.provider_repo.create(
        name="local",
        kind="openai",
        base_url="http://localhost.invalid/v1",
        api_key="fake",
        models_json=json.dumps([{"id": "model", "enabled": True}, {"id": "aux", "enabled": True}]),
    )
    config = json.loads(services.agent_repo.get("expert-one").config_json)
    config["memory"]["aux_model"] = aux_model
    services.agent_repo.update_config("expert-one", config_json=json.dumps(config))

    def build(factory, provider, model):
        fake = FakeMessagesListChatModel(
            responses=[
                AIMessage(
                    content=response(),
                    usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                )
            ]
        )
        return attach_model_observer(fake, provider, model)

    monkeypatch.setattr(extraction.OctopChatModelFactory, "_build", build)
    worker = extraction.ExpertMemoryWorker(services)
    if aux_model is None:
        worker.start()

        async def completed():
            while services.expert_memory_job_repo.get(original.job_id).status in {
                "pending",
                "running",
            }:
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(completed(), timeout=5)
        finally:
            await worker.close()
        job = services.expert_memory_job_repo.get(original.job_id)
    else:
        job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
        await worker.execute(job)
    assert services.expert_memory_job_repo.get(original.job_id).status == "completed"
    with services.db.connect() as conn:
        usage = conn.execute("SELECT * FROM usage_log WHERE source='memory_extract'").fetchone()
        calls = conn.execute("SELECT * FROM model_calls WHERE purpose='memory_extract'").fetchall()
    assert usage["input_tokens"] == 11 and usage["output_tokens"] == 7
    assert (
        usage["model"] == ("local/aux" if aux_model == "local/aux" else "local/model")
        and len(calls) == 1
    )
    assert calls[0]["task_id"] == job.task_id and calls[0]["turn_id"] == job.turn_id


async def test_response_metadata_usage_is_recorded_for_memory_extraction(services, monkeypatch):  # noqa: F811
    await capture_job(services)
    services.provider_repo.create(
        name="local",
        kind="openai",
        base_url="http://localhost.invalid/v1",
        api_key="fake",
        models_json=json.dumps([{"id": "model", "enabled": True}]),
    )

    def build(factory, provider, model):
        message = AIMessage(
            content=response(),
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 19,
                    "completion_tokens": 7,
                    "total_tokens": 26,
                    "prompt_tokens_details": {"cached_tokens": 5},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                }
            },
        )
        assert message.usage_metadata is None
        return attach_model_observer(
            FakeMessagesListChatModel(responses=[message]), provider, model
        )

    monkeypatch.setattr(extraction.OctopChatModelFactory, "_build", build)
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    await worker.execute(job)
    assert services.expert_memory_job_repo.get(job.job_id).status == "completed"
    with services.db.connect() as conn:
        usage = conn.execute("SELECT * FROM usage_log WHERE source='memory_extract'").fetchone()
        calls = conn.execute("SELECT * FROM model_calls WHERE purpose='memory_extract'").fetchall()
    assert (
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cache_read_tokens"],
        usage["reasoning_tokens"],
    ) == (19, 7, 5, 2)
    assert len(calls) == 1 and calls[0]["turn_id"] == job.turn_id
    recorded = json.loads(calls[0]["usage_json"])
    assert recorded["input_tokens"] == 19 and recorded["output_tokens"] == 7


@pytest.mark.parametrize(
    ("adapter", "toggle", "expected_thinking"),
    [
        ("thinking_nested_effort", True, "disabled"),
        ("thinking", False, "enabled"),
        ("status_only", False, "enabled"),
    ],
)
async def test_extraction_adapts_provider_thinking_without_mutating_model(
    adapter, toggle, expected_thinking
):
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model="glm-5",
        api_key="fake",
        base_url="http://memory-reasoning.invalid/v1",
        extra_body={"provider_option": True, "thinking": {"type": "enabled"}},
    )
    capability = {
        "adapter": adapter,
        "toggle": toggle,
        "default_mode": "enabled",
        "default_effort": "high",
        "efforts": ["low", "high"],
    }
    try:
        configured = extraction._single_attempt_model(model, capability)
        assert configured.extra_body == {
            "provider_option": True,
            "thinking": {"type": expected_thinking},
        }
        assert model.extra_body["thinking"] == {"type": "enabled"}
        assert capability["default_effort"] == "high"
        assert configured.root_async_client.max_retries == 0
    finally:
        await model.root_async_client.close()
        model.root_client.close()


async def test_shared_viewer_has_own_storage_and_cannot_read_owner_legacy(services):  # noqa: F811
    with services.db.transaction() as conn:
        conn.execute("UPDATE agents SET is_shared=1 WHERE agent_id='expert-one'")
    owner = Memory(
        "agent_expert-one",
        backend_config={"db_path": str(services.paths.root / "expert" / "memory.sqlite")},
    )
    owner.add_raw("Publisher private legacy memory", event_type="user_message")
    owner.backend.close()
    memory, _ = await capture_job(services, owner=2)
    assert "legacy" not in memory.namespaces
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    result = prepared(worker, job)
    worker._apply(job, result)
    assert memory.management_operation("scope", lambda store: len(store.list_atoms())) == 2
    assert (services.paths.root / "expert-memory-users" / "2" / "memory.sqlite").exists()
    publisher = ExpertMemory(
        services,
        1,
        "publisher-task",
        "publisher-turn",
        None,
        resolve_expert_profile(services, 1, "expert-one"),
        enabled=None,
    )
    assert publisher.management_operation("general", lambda store: store.list_atoms()) == []
    assert not memory_policy(services, 2, memory.task_id).policy_editable
    with pytest.raises(OctopError):
        update_memory_policy(
            services,
            2,
            memory.task_id,
            ExpertMemoryPolicyPatch(expected_revision=1, legacy_recall_enabled=True),
        )
    assert (
        query_memory(services, 2, memory.task_id, ExpertMemoryQuery()).extraction_jobs[0].job_id
        == job.job_id
    )


async def test_shutdown_preserves_known_result_for_recovery(services):  # noqa: F811
    _, _ = await capture_job(services)
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    result = prepared(worker, job)
    services.expert_memory_job_repo.update(
        job, phase="ready", result_json=result, now=extraction._now()
    )
    worker._stopping.set()
    await worker.execute(replace(job, phase="ready", result_json=result))
    saved = services.expert_memory_job_repo.get(job.job_id)
    assert saved.status == "pending" and saved.phase == "ready"


@pytest.mark.parametrize("kind", ["shared-owner", "team"])
async def test_owned_shared_and_team_memory_policy_is_editable(services, kind):  # noqa: F811
    await capture_job(services)
    with services.db.transaction() as conn:
        if kind == "team":
            conn.execute("UPDATE agents SET kind='team' WHERE agent_id='expert-one'")
        else:
            conn.execute("UPDATE agents SET is_shared=1 WHERE agent_id='expert-one'")
    policy = memory_policy(services, 1, "memory-task")
    assert policy.policy_editable
    updated = update_memory_policy(
        services,
        1,
        "memory-task",
        ExpertMemoryPolicyPatch(
            expected_revision=policy.config_revision,
            legacy_recall_enabled=False,
            auto_extract_enabled=False,
            general_promotion_enabled=False,
        ),
    )
    assert updated.config_revision == policy.config_revision + 1
    assert not updated.auto_extract_enabled and not updated.general_promotion_enabled


@pytest.mark.parametrize(
    "key", ["capture_enabled", "auto_extract_enabled", "extract_on_session_end"]
)
async def test_disabled_capture_or_extraction_does_not_enqueue(services, key):  # noqa: F811
    config = json.loads(services.agent_repo.get("expert-one").config_json)
    config["memory"][key] = False
    services.agent_repo.update_config("expert-one", config_json=json.dumps(config))
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,selected_expert_id,created_at,updated_at) VALUES('disabled-task',1,'Memory','disabled-task','expert-one',1,1)"
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,configuration_json,model_ref,created_at,updated_at) VALUES('disabled-turn','disabled-task','disabled-turn','Evidence','running','{}','local/model',1,1)"
        )
    memory = ExpertMemory(
        services,
        1,
        "disabled-task",
        "disabled-turn",
        None,
        resolve_expert_profile(services, 1, "expert-one"),
        enabled=None,
    )
    await memory.capture(_PREFERENCE, "Acknowledged.")
    assert services.expert_memory_job_repo.list_for_task("disabled-task", 1) == []


async def test_general_promotion_can_be_disabled_without_disabling_scope(services):  # noqa: F811
    memory, _ = await capture_job(services)
    config = json.loads(services.agent_repo.get("expert-one").config_json)
    config["memory"]["general_promotion_enabled"] = False
    services.agent_repo.update_config("expert-one", config_json=json.dumps(config))
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    result = prepared(worker, job)
    worker._apply(job, result)
    assert len(memory.management_operation("scope", lambda store: store.list_atoms())) == 2
    assert memory.management_operation("general", lambda store: store.list_atoms()) == []


async def test_delegated_member_instructions_are_not_user_preference_evidence(services):  # noqa: F811
    memory, _ = await capture_job(services, source_kind="delegation")
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    result = prepared(worker, job)
    assert json.loads(result)["general"] == []
    worker._apply(job, result)
    assert len(memory.management_operation("scope", lambda store: store.list_atoms())) == 2
    assert memory.management_operation("general", lambda store: store.list_atoms()) == []


@pytest.mark.parametrize(
    "prompt",
    [
        "For this project only, " + _PREFERENCE,
        "I no longer agree with this sentence: " + _PREFERENCE,
        _PREFERENCE + " Except for this project.",
        _PREFERENCE + " " + ("long context " * 1500),
    ],
)
async def test_general_promotion_preserves_source_qualifications(services, prompt):  # noqa: F811
    _, _ = await capture_job(services, prompt=prompt)
    worker = extraction.ExpertMemoryWorker(services)
    job = services.expert_memory_job_repo.claim(worker.worker_id, extraction._now())
    result = prepared(worker, job)
    assert json.loads(result)["general"] == []


async def test_disabled_user_cannot_read_or_capture_expert_memory(services):  # noqa: F811
    memory, _ = await capture_job(services)
    with services.db.transaction() as conn:
        conn.execute("UPDATE users SET disabled=1 WHERE id=1")
    with pytest.raises(ValueError, match="user is disabled"):
        memory.management_operation("scope", lambda store: store.list_raw())
    with pytest.raises(ValueError, match="user is disabled"):
        memory._call("scope", "capture", prompt="Later evidence", reply="Later reply")


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_memory_model_disables_sdk_retries_without_mutating_shared_factory_client(provider):
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model="memory-test",
            api_key="fake",
            base_url="http://memory-retry-guard.invalid/v1",
            max_retries=2,
        )
        before = model.root_async_client
    else:
        from langchain_anthropic import ChatAnthropic

        model = ChatAnthropic(
            model_name="memory-test",
            api_key="fake",
            base_url="http://memory-retry-guard.invalid/v1",
            max_retries=2,
        )
        before = model._async_client
    try:
        configured = extraction._single_attempt_model(model)
        after = configured.root_async_client if provider == "openai" else configured._async_client
        assert after.max_retries == 0 and before.max_retries == 2 and after is not before
    finally:
        await before.close()
        if provider == "openai":
            model.root_client.close()
