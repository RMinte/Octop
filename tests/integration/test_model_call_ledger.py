"""Independent invocation receipts preserve ownership, provenance and admission failures."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langchain.agents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from octop_harness.compaction import CompactResult
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.registry import AgentEntry, AgentRegistry
from octop_harness.teams.team_manager import TeamManager
from octop_harness.teams.util import derive_peer_thread_id

from octop.infra.agents import resident_context
from octop.infra.agents.providers.box import OctopChatModelFactory, build_box_provider_config
from octop.infra.cron.delivery import CronDeliveryCommand, CronDeliveryService
from octop.infra.db.repos.model_calls import ModelCallRow
from octop.infra.proactive.service import ProactiveCareService
from octop.infra.projects import runtime, task_submission
from octop.infra.projects.routing_classifier import DifficultyAssessment
from octop.infra.usage.model_calls import current_model_call_scope, model_call_scope
from tests.integration.test_project_task_routing import _configure, _seed_models, _settled
from tests.support.auth import create_agent, create_user, resolve_user_id


@pytest.fixture
def observed_model(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Use the production Octop factory; replace only its protocol's transport builder."""
    import octop_harness.llm.factory as harness_factory

    options: dict[str, Any] = {}

    def build(provider: Any, model: Any) -> FakeMessagesListChatModel:
        return options["instance"]

    monkeypatch.setattr(harness_factory, "_build_openai", build)

    def create(
        *,
        source: str = "third_party",
        usage: dict[str, Any] | None = None,
        instance: FakeMessagesListChatModel | None = None,
        provider_name: str = "configured",
        model_id: str = "actual-model",
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        options["instance"] = instance or FakeMessagesListChatModel(
            responses=[
                AIMessage(
                    content="private model response",
                    usage_metadata=usage,
                    response_metadata=(
                        {
                            "token_usage": {
                                "prompt_tokens": usage["input_tokens"],
                                "completion_tokens": usage["output_tokens"],
                                "total_tokens": usage["total_tokens"],
                            }
                        }
                        if usage is not None
                        else {}
                    ),
                )
            ],
            metadata=metadata,
        )
        model = ModelConfig(id=model_id)
        provider = (
            build_box_provider_config(
                id=provider_name,
                name=provider_name,
                base_url="http://127.0.0.1:58000/v1",
                models=[model],
                headers={},
            )
            if source == "box"
            else ProviderConfig(
                id=provider_name,
                api_key="fixture-provider-secret",
                base_url="http://127.0.0.1:58000/v1",
                models=[model],
            )
        )
        factory = OctopChatModelFactory([provider])
        return factory.get_chat_model(f"{provider_name}/{model_id}")

    return create


async def _calls(client: Any, auth: dict[str, str], **params: Any) -> dict[str, Any]:
    response = await client.get("/api/usage/calls", headers=auth, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("source", ["box", "third_party"])
async def test_factory_records_actual_source_and_missing_usage_without_changing_summary(
    env: Any, observed_model: Any, source: str
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    server.services.usage_repo.record(
        agent_id=server.services.agent_repo.list_all()[0].agent_id,
        user_id=owner,
        thread_id="legacy-thread",
        model="legacy/model",
        input_tokens=17,
        output_tokens=3,
    )
    assert (await _calls(client, auth))["items"] == []
    before = (await client.get("/api/usage/summary", headers=auth)).json()
    model = observed_model(
        source=source,
        metadata={"execution_source": "third_party" if source == "box" else "box"},
    )
    with model_call_scope(
        server.services,
        user_id=owner,
        purpose="main",
        task_id="task-ledger",
        turn_id="turn-ledger",
        submission_id="submission-ledger",
    ):
        await model.ainvoke([HumanMessage(content="private prompt must not be stored")])
    page = await _calls(client, auth)
    assert page["measurement_unit"] == "logical_model_invocation"
    assert page["recording_scope"] == "new_calls_only"
    assert page["physical_attempts_available"] is False
    assert page["included_in_legacy_summary"] is False
    assert page["timezone"] == server.config.default_timezone
    assert page["generated_at"] >= page["items"][0]["started_at"]
    assert page["next_before_id"] is None
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["provider"] == "configured" and item["model_ref"] == "configured/actual-model"
    assert item["source"] == source and item["purpose"] == "main"
    assert item["task_id"] == "task-ledger" and item["turn_id"] == "turn-ledger"
    assert item["submission_id"] == "submission-ledger" and item["user_id"] == owner
    assert item["status"] == "succeeded" and item["finished_at"] >= item["started_at"]
    assert all(value is None for key, value in item["usage"].items() if key != "presence")
    assert not any(item["usage"]["presence"].values())
    assert "pk" not in item and "usage_json" not in item and "metadata_json" not in item
    wire = json.dumps(page)
    for secret in (
        "private prompt",
        "private model response",
        "fixture-provider-secret",
        "base_url",
    ):
        assert secret not in wire
    after = (await client.get("/api/usage/summary", headers=auth)).json()
    for field in ("input_tokens", "output_tokens", "total_tokens", "turns", "model_calls"):
        assert after[field] == before[field]


async def test_receipt_api_is_owner_scoped_filtered_and_cursor_paginated(
    env: Any, observed_model: Any
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    alice = await create_user(client, auth, username="ledger-alice")
    alice_id = await resolve_user_id(client, auth, "ledger-alice")
    model = observed_model(
        source="third_party", usage={"input_tokens": 0, "output_tokens": 2, "total_tokens": 2}
    )
    for user_id, submission in (
        (owner, "admin-call"),
        (alice_id, "alice-first"),
        (alice_id, "alice-second"),
        (None, "owner-unknown"),
    ):
        with model_call_scope(
            server.services,
            user_id=user_id,
            purpose="main",
            agent_id="agent-filter",
            thread_id="thread-filter",
            project_id="project-filter",
            task_id="task-filter",
            turn_id="turn-filter",
            submission_id=submission,
        ):
            await model.ainvoke("private content")
    own = await _calls(client, alice, user_id=owner)
    assert [item["submission_id"] for item in own["items"]] == ["alice-second", "alice-first"]
    assert own["items"][0]["usage"]["input_tokens"] == 0
    assert own["items"][0]["usage"]["presence"]["input_tokens"] is True
    assert own["items"][0]["usage"]["cache_read_tokens"] is None
    assert (await client.get("/api/usage/calls")).status_code == 401
    assert (await client.get("/api/admin/usage/calls", headers=alice)).status_code == 403
    all_users = await client.get("/api/admin/usage/calls", headers=auth)
    assert all_users.status_code == 200 and len(all_users.json()["items"]) == 4
    scoped = await client.get("/api/admin/usage/calls", headers=auth, params={"user_id": alice_id})
    assert scoped.status_code == 200 and scoped.json()["items"] == own["items"]
    first = await _calls(client, alice, limit=1)
    cursor = first["next_before_id"]
    assert cursor is not None
    with model_call_scope(server.services, user_id=alice_id, purpose="title"):
        await model.ainvoke("new call after first page")
    older = await _calls(client, alice, limit=1, before_id=cursor)
    assert older["next_before_id"] is None
    assert older["items"][0]["submission_id"] == "alice-first"
    assert older["items"][0]["call_id"] != first["items"][0]["call_id"]
    filtered = await _calls(
        client,
        alice,
        agent_id="agent-filter",
        thread_id="thread-filter",
        project_id="project-filter",
        task_id="task-filter",
        turn_id="turn-filter",
        submission_id="alice-second",
        purpose="main",
        provider="configured",
        model_ref="configured/actual-model",
        source="third_party",
        status="succeeded",
    )
    assert [item["submission_id"] for item in filtered["items"]] == ["alice-second"]
    assert (await _calls(client, alice, task_id="foreign-task"))["items"] == []
    for params in (
        {"limit": 0},
        {"limit": 101},
        {"before_id": 0},
        {"source": "cloud"},
        {"source": "self_hosted"},
        {"source": "unknown"},
        {"status": "done"},
    ):
        response = await client.get("/api/usage/calls", headers=alice, params=params)
        assert response.status_code == 422, response.text
    for source in ("self_hosted", "unknown"):
        response = await client.get(
            "/api/admin/usage/calls", headers=auth, params={"source": source}
        )
        assert response.status_code == 422, response.text


async def test_running_call_cancellation_preserves_receipt_without_fabricating_tokens(
    env: Any, observed_model: Any
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    started = asyncio.Event()

    class PendingModel(FakeMessagesListChatModel):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

    model = observed_model(instance=PendingModel(responses=[AIMessage(content="unreached")]))
    with model_call_scope(server.services, user_id=owner, purpose="main", turn_id="cancelled-turn"):
        operation = asyncio.create_task(model.ainvoke("private pending request"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        running = (await _calls(client, auth))["items"][0]
        assert running["status"] == "running" and running["finished_at"] is None
    finally:
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    cancelled = (await _calls(client, auth))["items"][0]
    assert cancelled["call_id"] == running["call_id"]
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"] is not None
    assert cancelled["usage"]["input_tokens"] is None
    assert current_model_call_scope() is None


@pytest.mark.parametrize("operation", ["stream", "call", "resume_hitl"])
async def test_manager_uses_trusted_thread_owner_and_restores_scope(
    env: Any, observed_model: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    client, server, auth = env
    alice = await create_user(client, auth, username="manager-ledger-alice")
    alice_id = await resolve_user_id(client, auth, "manager-ledger-alice")
    agent_id = await create_agent(client, auth)
    thread_id = "manager-ledger-thread"
    server.services.thread_repo.insert(
        thread_id=thread_id,
        agent_id=agent_id,
        user_id=alice_id,
        channel_type="dashboard",
        session_key="manager-ledger-session",
    )
    manager = server.app_runtime.agent_registry
    model = observed_model()

    async def stream(*args: Any, **kwargs: Any) -> Any:
        result = await model.ainvoke("manager invocation")
        yield {"type": "token", "content": result.content}

    async def call(*args: Any, **kwargs: Any) -> Any:
        result = await model.ainvoke("manager invocation")
        return {"text": result.content}

    monkeypatch.setattr(manager._harness_manager, "stream", stream)
    monkeypatch.setattr(manager._harness_manager, "call", call)
    monkeypatch.setattr(manager._harness_manager, "resume_hitl", stream)
    request = {"thread_id": thread_id, "user": "999999", "messages": "manager invocation"}
    if operation == "call":
        await manager.call(agent_id, request)
    elif operation == "resume_hitl":
        assert [item async for item in manager.resume_hitl(agent_id, thread_id, [])]
    else:
        assert [item async for item in manager.stream(agent_id, request)]
    records = (await _calls(client, alice))["items"]
    assert len(records) == 1
    assert records[0]["user_id"] == alice_id and records[0]["agent_id"] == agent_id
    assert records[0]["thread_id"] == thread_id and records[0]["purpose"] == "chat"
    assert (await _calls(client, auth))["items"] == []
    assert current_model_call_scope() is None


async def test_async_peer_creates_its_scope_from_owner_and_preserves_outer_turn(
    env: Any, observed_model: Any
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    agent_id = await create_agent(client, auth)
    manager = server.app_runtime.agent_registry
    model = observed_model()

    async def invoke(agent_id: str, request: Any) -> Any:
        return await model.ainvoke("peer invocation")

    team = SimpleNamespace(_call_agent=invoke, inbox=SimpleNamespace(_call_agent=invoke))
    manager._install_peer_call_scope(team)
    manager._install_peer_call_scope(team)
    await asyncio.create_task(
        team.inbox._call_agent(agent_id, {"thread_id": "peer-thread", "user": "999999"})
    )
    with model_call_scope(
        server.services,
        user_id=owner,
        purpose="main",
        task_id="outer-task",
        turn_id="outer-turn",
    ):
        await team._call_agent(agent_id, {"thread_id": "peer-thread"})
    records = (await _calls(client, auth))["items"]
    assert len(records) == 2 and all(record["purpose"] == "peer" for record in records)
    assert all(record["user_id"] == owner and record["agent_id"] == agent_id for record in records)
    assert records[0]["task_id"] == "outer-task" and records[0]["turn_id"] == "outer-turn"
    assert records[1]["thread_id"] == "peer-thread"
    assert current_model_call_scope() is None


async def test_inbox_peer_does_not_inherit_another_users_stale_scope(
    env: Any, observed_model: Any
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    alice = await create_user(client, auth, username="stale-ledger-alice")
    alice_id = await resolve_user_id(client, auth, "stale-ledger-alice")
    manager = server.app_runtime.agent_registry
    model = observed_model()
    registry = AgentRegistry()
    source_id, target_id = "inbox-source-agent", "inbox-target-agent"
    source_thread = "inbox-source-thread"
    target_thread = derive_peer_thread_id(source_thread, target_id)
    done = asyncio.Event()
    replies = []

    async def invoke(request: Any) -> Any:
        result = await model.ainvoke("inbox invocation")
        return {"messages": [result]}

    async def on_reply(event: Any) -> None:
        replies.append(event)
        done.set()

    for agent_id, thread_id in ((source_id, source_thread), (target_id, target_thread)):
        server.services.agent_repo.create(agent_id=agent_id, user_id=alice_id, name=agent_id)
        server.services.thread_repo.insert(
            thread_id=thread_id,
            agent_id=agent_id,
            user_id=alice_id,
            channel_type="dashboard",
            session_key=agent_id + "-session",
        )
        registry.add(
            AgentEntry(
                agent_id=agent_id,
                agent=SimpleNamespace(call=invoke),
                config=SimpleNamespace(name=agent_id, language="en", team_peers=None),
                metadata={"user_id": alice_id},
                tags=[],
                created_at=datetime.now(UTC),
            )
        )
    team = TeamManager(registry)
    team.set_processor(
        SimpleNamespace(compose_followup=lambda *args, **kwargs: "Peer followup", on_reply=on_reply)
    )
    manager._install_peer_call_scope(team)
    try:
        with model_call_scope(
            server.services,
            user_id=owner,
            purpose="main",
            project_id="stale-project",
            task_id="stale-task",
            turn_id="stale-turn",
            submission_id="stale-submission",
            thread_id="stale-thread",
        ):
            team.submit_peer(
                from_agent_id=source_id,
                to_agent_id=target_id,
                user_id=alice_id,
                source_thread_id=source_thread,
                message="Async peer request",
            )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await team.aclose()
    assert replies[0].status == "done"
    records = (await _calls(client, alice))["items"]
    assert len(records) == 2 and all(record["user_id"] == alice_id for record in records)
    assert {record["agent_id"] for record in records} == {source_id, target_id}
    assert {record["thread_id"] for record in records} == {source_thread, target_thread}
    assert all(record["purpose"] == "peer" for record in records)
    for record in records:
        assert (
            record["project_id"]
            == record["task_id"]
            == record["turn_id"]
            == record["submission_id"]
            == ""
        )
    assert (await _calls(client, auth))["items"] == []


async def test_automatic_summary_records_its_actual_seed_model(
    env: Any, observed_model: Any
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    seed = observed_model(provider_name="seed-provider", model_id="summary-model")
    summarizer = SummarizationMiddleware(model=seed, trigger=("messages", 3), keep=("messages", 1))
    with model_call_scope(server.services, user_id=owner, purpose="main", turn_id="main-turn"):
        await summarizer._acreate_summary(
            [HumanMessage(content="old input"), AIMessage(content="old reply")]
        )
    records = (await _calls(client, auth))["items"]
    assert len(records) == 1 and records[0]["purpose"] == "summarization"
    assert records[0]["model_ref"] == "seed-provider/summary-model"
    assert records[0]["turn_id"] == "main-turn"


async def test_resident_explicit_compaction_has_its_own_scope(
    env: Any, observed_model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    model = observed_model()

    async def compact(*args: Any, **kwargs: Any) -> CompactResult:
        await model.ainvoke(
            "private compact input", config={"metadata": {"lc_source": "summarization"}}
        )
        return CompactResult(ok=True, summarized_count=2)

    resident_runtime = SimpleNamespace(
        services=server.services,
        _configuration=lambda *args: {"model_ref": "configured/actual-model"},
        registry=SimpleNamespace(providers=SimpleNamespace(is_model_ref_usable=lambda ref: True)),
    )
    monkeypatch.setattr(resident_context, "_require_compactable", lambda *args: None)
    monkeypatch.setattr(
        resident_context,
        "_agent",
        AsyncMock(return_value=SimpleNamespace(acompact_conversation=compact)),
    )
    await resident_context._compact(resident_runtime, owner, "resident-agent", "resident-thread")
    records = (await _calls(client, auth))["items"]
    assert len(records) == 1 and records[0]["purpose"] == "compaction"
    assert (
        records[0]["agent_id"] == "resident-agent" and records[0]["thread_id"] == "resident-thread"
    )
    assert records[0]["user_id"] == owner
    assert current_model_call_scope() is None


@pytest.mark.parametrize("purpose", ["cron", "proactive"])
async def test_background_business_calls_keep_purpose_and_owner(
    env: Any, observed_model: Any, monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    agent_id = await create_agent(client, auth)
    thread_id = "background-ledger-thread"
    server.services.thread_repo.insert(
        thread_id=thread_id,
        agent_id=agent_id,
        user_id=owner,
        channel_type="dashboard",
        session_key="background-ledger-session",
    )
    manager = server.app_runtime.agent_registry
    model = observed_model()
    session = SimpleNamespace(user_id=owner, agent_id=agent_id, thread_id=thread_id)
    gateway = SimpleNamespace(
        push_session_text=AsyncMock(),
        push_text_from_session=AsyncMock(),
        require_session=lambda *args: session,
    )
    if purpose == "cron":

        async def stream(*args: Any, **kwargs: Any) -> Any:
            result = await model.ainvoke("cron business request")
            yield {"type": "token", "content": result.content}

        monkeypatch.setattr(manager._harness_manager, "stream", stream)
        service = CronDeliveryService(
            gateway=gateway, agent_manager=manager, repos=server.services.repos
        )
        monkeypatch.setattr(
            service,
            "_build_agent_request",
            AsyncMock(
                return_value={
                    "thread_id": thread_id,
                    "messages": [{"role": "user", "content": "cron"}],
                }
            ),
        )
        monkeypatch.setattr(service, "_project_best_effort", Mock())
        monkeypatch.setattr(service, "_notify_best_effort", AsyncMock())
        command = CronDeliveryCommand(
            cron_id="cron-ledger",
            cron_name="Ledger cron",
            agent_id=agent_id,
            user_id=owner,
            session_key="background-ledger-session",
            prompt="Cron request",
            fresh_thread=False,
            task_type="agent",
            model=None,
            mcp_servers=(),
        )
        await service._deliver_agent(command, session)
    else:
        episode = SimpleNamespace(
            id="care-ledger-episode",
            occurred_at=datetime.now(UTC),
            people=[],
            summary="Private care summary",
            verbatim_quote="Private quote",
            emotion="neutral",
            intensity=1,
        )
        agent = SimpleNamespace(
            memory=SimpleNamespace(list_episodes=lambda **kwargs: [episode]),
            workspace=SimpleNamespace(aread_text=AsyncMock(return_value="")),
            model_factory=SimpleNamespace(get=lambda ref: model),
            config=SimpleNamespace(
                locale="en", pick_default_model_ref=lambda: "configured/actual-model"
            ),
        )
        monkeypatch.setattr(manager, "get_agent", lambda agent_id: agent)
        service = ProactiveCareService(
            gateway=gateway,
            care_push_repo=server.services.care_push_repo,
            agent_manager=manager,
        )
        monkeypatch.setattr(
            service._picker,
            "pick",
            lambda *args, **kwargs: SimpleNamespace(episodes=[episode], window_days=7),
        )
        await service.run(agent_id, "background-ledger-session")
        gateway.push_text_from_session.assert_awaited_once()
    records = (await _calls(client, auth))["items"]
    assert len(records) == 1 and records[0]["purpose"] == purpose
    assert records[0]["user_id"] == owner and records[0]["agent_id"] == agent_id
    assert records[0]["thread_id"] == thread_id
    assert current_model_call_scope() is None


async def test_classifier_call_survives_rejected_task_admission(
    env: Any, observed_model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, server, auth = env
    await _seed_models(client, auth)
    await _configure(client, auth)
    model = observed_model(source="box", provider_name="Box", model_id="RM-01 VLM")
    classified, release = asyncio.Event(), asyncio.Event()

    async def classify(*args: Any) -> DifficultyAssessment:
        await model.ainvoke("private classifier input")
        classified.set()
        await release.wait()
        return DifficultyAssessment(9, "basic")

    monkeypatch.setattr(task_submission, "classify_difficulty", classify)
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": uuid4().hex},
            json={"name": "Admission race", "instructions": "Initial"},
        )
    ).json()
    operation = asyncio.create_task(
        client.post(
            "/api/project-tasks",
            headers=auth,
            json={
                "request_id": "rejected-classifier",
                "project_id": project["project_id"],
                "prompt": "Classify before admission",
                "options": {"routing": {"mode": "hybrid", "level": 100}},
            },
        )
    )
    try:
        await asyncio.wait_for(classified.wait(), timeout=5)
        changed = await client.patch(
            f"/api/projects/{project['project_id']}",
            headers=auth,
            json={"expected_revision": 1, "instructions": "Changed"},
        )
        assert changed.status_code == 200, changed.text
    finally:
        release.set()
        response = await operation
    assert response.status_code == 409, response.text
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []
    records = (await _calls(client, auth, submission_id="rejected-classifier"))["items"]
    assert len(records) == 1
    assert records[0]["purpose"] == "routing_classifier" and records[0]["status"] == "succeeded"
    assert records[0]["source"] == "box" and records[0]["project_id"] == project["project_id"]
    assert records[0]["task_id"] == records[0]["turn_id"] == ""


async def test_concurrent_classifier_calls_are_kept_when_only_one_turn_is_admitted(
    env: Any, observed_model: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _server, auth = env
    await _seed_models(client, auth)
    await _configure(client, auth)
    model = observed_model(source="box", provider_name="Box", model_id="RM-01 VLM")
    both_started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def classify(*args: Any) -> DifficultyAssessment:
        nonlocal count
        await model.ainvoke("duplicate classifier input")
        count += 1
        if count == 2:
            both_started.set()
        await release.wait()
        return DifficultyAssessment(9, "basic")

    async def run_turn(*args: Any) -> Any:
        yield {"type": "token", "content": "Completed"}

    monkeypatch.setattr(task_submission, "classify_difficulty", classify)
    monkeypatch.setattr(runtime, "run_turn", run_turn)
    body = {
        "request_id": "concurrent-classifiers",
        "prompt": "One business round",
        "options": {"routing": {"mode": "hybrid", "level": 100}},
    }
    operations = [
        asyncio.create_task(client.post("/api/project-tasks", headers=auth, json=body))
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(both_started.wait(), timeout=5)
    finally:
        release.set()
        responses = await asyncio.gather(*operations)
    assert all(response.status_code == 202 for response in responses)
    assert responses[0].json()["task_id"] == responses[1].json()["task_id"]
    done = await _settled(client, auth, responses[0].json()["task_id"])
    assert len(done["turns"]) == 1
    records = (await _calls(client, auth, submission_id=body["request_id"]))["items"]
    assert len(records) == 2 and len({record["call_id"] for record in records}) == 2
    assert all(
        record["purpose"] == "routing_classifier" and record["status"] == "succeeded"
        for record in records
    )
    replay = await client.post("/api/project-tasks", headers=auth, json=body)
    assert replay.status_code == 202
    assert len((await _calls(client, auth, submission_id=body["request_id"]))["items"]) == 2


async def test_public_receipts_filter_metadata_and_preserve_interrupted_status(env: Any) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    row = ModelCallRow(
        call_id="interrupted-call",
        started_at=100,
        user_id=owner,
        metadata_json=json.dumps(
            {"trace_id": "safe-trace", "prompt": "private prompt", "api_key": "private key"}
        ),
    )
    server.services.model_call_repo.start(row)
    server.services.model_call_repo.finish(replace(row, finished_at=200, status="interrupted"))
    response = await _calls(client, auth, status="interrupted")
    assert len(response["items"]) == 1
    assert response["items"][0]["metadata"]["trace_id"] == "safe-trace"
    assert "private prompt" not in json.dumps(response) and "private key" not in json.dumps(
        response
    )
