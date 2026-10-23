"""Real Task graph execution for transport and private resident adapters."""
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from octop_gateway.models import ChannelSubject, InboundMessage, TextContent
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory
from tests.unit.projects.test_unified_ingress import message, services  # noqa: F401

from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.cron.delivery import CronDeliveryCommand, CronDeliveryService
from octop.infra.errors import OctopError
from octop.infra.gateway.process.task_adapter import TaskChannelAdapter
from octop.infra.projects import resident_tasks, runtime, tasks


@pytest.fixture(autouse=True)
def no_title_network(monkeypatch):
    from octop.infra.projects.title import TaskTitleAgent, TitleResult

    monkeypatch.setattr(TaskTitleAgent, "generate", AsyncMock(return_value=TitleResult()))


class LocalModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def real_factory(monkeypatch, target, observed, responses=None):
    model = LocalModel(responses=responses or [AIMessage(content="canonical answer")])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    def factory(cfg, **kwargs):
        observed.append(cfg)
        return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

    monkeypatch.setattr(target, "HarnessAgent", factory)


async def settled_turn(bundle, turn_id):
    async with asyncio.timeout(8):
        while True:
            turn = bundle.project_task_turn_repo.get(turn_id)
            if turn.status in {"completed", "failed", "cancelled"}:
                return turn
            await asyncio.sleep(0.02)


async def test_real_im_graph_deduplicates_native_event_and_reset_is_draft(services, monkeypatch):  # noqa: F811
    message(services)
    observed = []
    real_factory(monkeypatch, runtime, observed)
    services.channel_repo.create(
        channel_id="im-channel",
        agent_id=None,
        user_id=1,
        kind="telegram",
        name="IM",
        config_json="{}",
    )
    worker = runtime.ProjectTaskWorker(services)
    await worker.start()
    try:
        adapter = TaskChannelAdapter(services, worker)
        inbound = InboundMessage(
            channel_id="im-channel",
            channel_type="telegram",
            tenant_id="general",
            channel_subject=ChannelSubject(subject_id="recipient"),
            content=[TextContent(text="hello")],
            metadata={"message_id": "native-one", "model_ref": "ingress/model"},
        )
        first, _ = await adapter.submit(inbound)
        turn = await settled_turn(services, first.turn_id)
        assert turn.status == "completed", turn.error_code
        assert turn.reply == "canonical answer"
        repeated, _ = await adapter.submit(inbound)
        assert repeated.task_id == first.task_id and repeated.turn_id == first.turn_id
        assert len(observed) == 1
        fresh = inbound.model_copy(
            update={
                "content": [TextContent(text="/new")],
                "metadata": {"message_id": "native-reset"},
            }
        )
        reset, _ = await adapter.submit(fresh)
        assert reset.status == "admitted" and reset.task_id is None
        with services.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 1
        followup = inbound.model_copy(
            update={"metadata": {"message_id": "native-two", "model_ref": "ingress/model"}}
        )
        second, _ = await adapter.submit(followup)
        assert second.task_id != first.task_id
        assert (await settled_turn(services, second.turn_id)).status == "completed"
    finally:
        await worker.close()


async def test_resident_internal_task_uses_real_graph_preserves_identity_and_notification(
    services, monkeypatch
):  # noqa: F811
    message(services)
    binding = await ensure_resident_identity(services, 1)
    observed = []
    real_factory(monkeypatch, resident_tasks, observed)
    queued, _ = services.resident_request_repo.create(
        1,
        binding.thread_id,
        "resident-proof",
        "resident-turn",
        "hello privately",
        tasks.now_ms(),
        configuration_json=json.dumps(
            {"model_ref": "ingress/model", "system_prompt": "frozen private identity"}
        ),
    )
    assert services.resident_request_repo.mark_running(
        queued.turn_id, "resident-worker", tasks.now_ms(), tasks.now_ms() + 30000
    )
    identifier = await resident_tasks.admit(services, queued)
    assert services.project_task_repo.get(identifier, 1) is None
    assert services.project_task_repo.get_internal(identifier, 1).internal_kind == "resident"
    worker = runtime.ProjectTaskWorker(services)
    await worker.start()
    try:
        turn = await settled_turn(services, queued.turn_id)
        assert turn.status == "completed", turn.error_code
        assert turn.reply == "canonical answer"
        assert observed[0].system_prompt == "frozen private identity"
        assert observed[0].subagents == [] and observed[0].mcp_server_configs == {}
        assert observed[0].memory_namespace == "agent_" + binding.agent_id
        assert isinstance(observed[0].checkpointer, object)
        assert services.project_task_turn_repo.context(identifier)
        assert services.thread_message_repo.head(binding.thread_id) == 0
        notice = AIMessage(id="resident-notice", content="delegated result")
        await resident_tasks.append_message(services, 1, binding.thread_id, notice)
        await resident_tasks.append_message(services, 1, binding.thread_id, notice)
        assert len(services.project_task_turn_repo.list(identifier)) == 2
        with pytest.raises(OctopError):
            tasks.detail(services, 1, identifier)
        with pytest.raises(OctopError):
            await resident_tasks.ensure_task(services, 2, binding.thread_id, "foreign")
    finally:
        await worker.close()


async def test_cron_text_zero_model_per_job_and_per_run_are_canonical(services):  # noqa: F811
    assert services.provider_repo.list_all() == []
    pushed = AsyncMock()
    gateway = SimpleNamespace(
        thread_registry=SimpleNamespace(get_session=lambda key: None),
        ws_hub=SimpleNamespace(push_to_user=pushed),
        _task_services=services,
        _task_cron_delivery=None,
    )
    service = CronDeliveryService(
        gateway=gateway, agent_manager=SimpleNamespace(), repos=services.repos, services=services
    )
    services.cron_repo.create(
        cron_id="text-job",
        agent_id=None,
        user_id=1,
        name="note",
        trigger="interval:60",
        prompt="a note",
        session_key="note-session",
        conversation_policy="per_job",
        selected_expert_id=None,
        task_type="text",
    )
    command = CronDeliveryCommand(
        cron_id="text-job",
        cron_name="note",
        agent_id=None,
        user_id=1,
        session_key="note-session",
        prompt="a note",
        fresh_thread=False,
        task_type="text",
        model=None,
        mcp_servers=(),
        event_key="manual:one",
        conversation_policy="per_job",
    )
    await service.deliver(command)
    await service.deliver(command)
    await service.deliver(CronDeliveryCommand(**{**command.__dict__, "event_key": "manual:two"}))
    assert pushed.await_count == 2
    with services.db.connect() as conn:
        rows = conn.execute(
            "SELECT task_id,model_ref,status,reply,content_blocks_json FROM project_task_turns"
        ).fetchall()
    assert len(rows) == 2 and len({row["task_id"] for row in rows}) == 1
    assert all(row["model_ref"] == "" and row["status"] == "completed" for row in rows)
    assert all(
        row["reply"] == "a note" and json.loads(row["content_blocks_json"])[0]["text"] == "a note"
        for row in rows
    )
    await service.deliver(
        CronDeliveryCommand(
            **{**command.__dict__, "event_key": "manual:three", "conversation_policy": "per_run"}
        )
    )
    with services.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(DISTINCT task_id) AS n FROM project_task_turns").fetchone()[
                "n"
            ]
            == 2
        )


async def test_im_modes_pending_and_private_resident_rejection(services):  # noqa: F811
    message(services)
    services.channel_repo.create(
        channel_id="commands",
        agent_id=None,
        user_id=1,
        kind="telegram",
        name="commands",
        config_json="{}",
    )
    adapter = TaskChannelAdapter(services)

    def inbound(text, native):
        return InboundMessage(
            channel_id="commands",
            channel_type="telegram",
            tenant_id="general",
            channel_subject=ChannelSubject(subject_id="recipient"),
            content=[TextContent(text=text)],
            metadata={"message_id": native, "model_ref": "ingress/model"},
        )

    mode, _ = await adapter.submit(inbound("/mode ask", "mode"))
    assert tasks.detail(services, 1, mode.task_id).action_mode == "ask"
    first, _ = await adapter.submit(inbound("work", "work"))
    assert (
        json.loads(services.project_task_turn_repo.get(first.turn_id).configuration_json)[
            "action_mode"
        ]
        == "ask"
    )
    status, _ = await adapter.submit(inbound("/status", "status"))
    assert status.task_id == first.task_id
    assert tasks.detail(services, 1, first.task_id).active_turn_id == first.turn_id
    replay, _ = await adapter.submit(inbound("/mode ask", "mode"))
    assert replay.turn_id == mode.turn_id
    binding = await ensure_resident_identity(services, 1)
    services.channel_repo.update("commands", agent_id=binding.agent_id)
    with pytest.raises(OctopError):
        await adapter.submit(inbound("private", "private"))
    assert services.project_task_turn_repo.get(first.turn_id).status == "queued"


async def test_outbound_disconnect_recovery_never_reruns_model_or_unknown_send(
    services, monkeypatch
):  # noqa: F811
    from octop.infra.projects import deliveries

    message(services)
    observed = []
    real_factory(monkeypatch, runtime, observed)
    services.channel_repo.create(
        channel_id="recovery",
        agent_id=None,
        user_id=1,
        kind="telegram",
        name="recovery",
        config_json="{}",
    )
    worker = runtime.ProjectTaskWorker(services)
    await worker.start()
    try:
        adapter = TaskChannelAdapter(services, worker)
        inbound = InboundMessage(
            channel_id="recovery",
            channel_type="telegram",
            tenant_id="general",
            channel_subject=ChannelSubject(subject_id="recipient"),
            content=[TextContent(text="hello")],
            metadata={"message_id": "disconnect", "model_ref": "ingress/model"},
        )
        receipt, _ = await adapter.submit(inbound)
        assert (await settled_turn(services, receipt.turn_id)).status == "completed"
        with services.db.transaction() as conn:
            conn.execute(
                "UPDATE project_task_turns SET finished_at=? WHERE turn_id=?",
                (tasks.now_ms() - 5000, receipt.turn_id),
            )
        push = AsyncMock()
        gateway = SimpleNamespace(push_text=push)
        await deliveries.recover_im(services, gateway)
        await deliveries.recover_im(services, gateway)
        assert push.await_count == 1 and push.call_args.args[-1] == "canonical answer"
        assert len(observed) == 1
        with services.db.transaction() as conn:
            conn.execute("UPDATE unified_source_deliveries SET status='uncertain'")
        await deliveries.recover_im(services, gateway)
        assert push.await_count == 1 and len(observed) == 1
    finally:
        await worker.close()


async def test_im_multiple_questions_resume_same_turn_and_response_event_replay(
    services, monkeypatch
):  # noqa: F811
    message(services)
    observed = []
    real_factory(
        monkeypatch,
        runtime,
        observed,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "questions",
                        "name": "ask_user_question",
                        "args": {
                            "questions": [
                                {"question": "First?", "options": []},
                                {"question": "Second?", "options": []},
                            ]
                        },
                    }
                ],
            ),
            AIMessage(content="answered both"),
        ],
    )
    services.channel_repo.create(
        channel_id="questions",
        agent_id=None,
        user_id=1,
        kind="telegram",
        name="questions",
        config_json="{}",
    )
    worker = runtime.ProjectTaskWorker(services)
    await worker.start()
    try:
        adapter = TaskChannelAdapter(services, worker)

        def inbound(text, native):
            return InboundMessage(
                channel_id="questions",
                channel_type="telegram",
                tenant_id="general",
                channel_subject=ChannelSubject(subject_id="recipient"),
                content=[TextContent(text=text)],
                metadata={"message_id": native, "model_ref": "ingress/model"},
            )

        first, _ = await adapter.submit(inbound("ask me", "ask"))
        async with asyncio.timeout(8):
            while tasks.detail(services, 1, first.task_id).turns[0].status != "waiting_answer":
                await asyncio.sleep(0.02)
        before = tasks.detail(services, 1, first.task_id).turns[0]
        assert len([item for item in before.interactions if item.status == "pending"]) == 2
        answer, _ = await adapter.submit(inbound("one", "answer-one"))
        assert answer.turn_id == first.turn_id
        replay, _ = await adapter.submit(inbound("one", "answer-one"))
        assert replay.turn_id == first.turn_id
        pending = [
            item
            for item in tasks.detail(services, 1, first.task_id).turns[0].interactions
            if item.status == "pending"
        ]
        assert len(pending) == 1
        second, _ = await adapter.submit(inbound("two", "answer-two"))
        assert second.turn_id == first.turn_id
        final = await settled_turn(services, first.turn_id)
        assert final.status == "completed" and final.reply == "answered both"
        assert len(services.project_task_turn_repo.list(first.task_id)) == 1
    finally:
        await worker.close()


async def test_cron_reserved_before_dispatch_is_recovered_without_model(services):
    from octop.infra.cron.task_delivery import recover, reserve

    services.cron_repo.create(
        cron_id="durable-note",
        agent_id=None,
        user_id=1,
        name="note",
        trigger="interval:60",
        prompt="durable note",
        session_key="durable-note",
        conversation_policy="per_job",
        selected_expert_id=None,
        task_type="text",
    )
    gateway = SimpleNamespace(
        thread_registry=SimpleNamespace(get_session=lambda key: None),
        ws_hub=SimpleNamespace(push_to_user=AsyncMock()),
    )
    service = CronDeliveryService(
        gateway=gateway, agent_manager=SimpleNamespace(), repos=services.repos, services=services
    )
    command = CronDeliveryCommand(
        cron_id="durable-note",
        cron_name="note",
        agent_id=None,
        user_id=1,
        session_key="durable-note",
        prompt="durable note",
        fresh_thread=False,
        task_type="text",
        model=None,
        mcp_servers=(),
        event_key="manual:crash-before-job",
        conversation_policy="per_job",
    )
    reserved = reserve(service, command)
    assert reserved["status"] == "received"
    await recover(service)
    await recover(service)
    assert gateway.ws_hub.push_to_user.await_count == 1
    assert services.cron_repo.get("durable-note").last_status == "ok"
    assert services.provider_repo.list_all() == []


async def test_reset_receipt_atomic_replay_cannot_detach_followup(services):
    from octop.infra.projects import ingress

    message(services)
    services.channel_repo.create(
        channel_id="reset",
        agent_id=None,
        user_id=1,
        kind="telegram",
        name="reset",
        config_json="{}",
    )
    adapter = TaskChannelAdapter(services)

    def inbound(text, native):
        return InboundMessage(
            channel_id="reset",
            channel_type="telegram",
            tenant_id="general",
            channel_subject=ChannelSubject(subject_id="recipient"),
            content=[TextContent(text=text)],
            metadata={"message_id": native, "model_ref": "ingress/model"},
        )

    reset, _ = await adapter.submit(inbound("/new", "reset-first"))
    assert reset.task_id is None
    first, _ = await adapter.submit(inbound("hello", "first"))
    repeated, _ = await adapter.submit(inbound("/new", "reset-first"))
    assert repeated.inbox_id == reset.inbox_id
    tasks.cancel(services, 1, first.task_id, first.turn_id)
    next_receipt, _ = await adapter.submit(inbound("next", "next"))
    assert next_receipt.task_id == first.task_id
    assert ingress.get_receipt(services, 1, reset.inbox_id).status == "admitted"


async def test_production_global_processor_uses_tasks_and_localizes_unsupported_commands(
    services, monkeypatch
):
    from octop.infra.gateway.process.processor import GlobalProcessor
    from octop.infra.gateway.slash.dispatcher import SlashDispatcher

    message(services)
    observed = []
    real_factory(monkeypatch, runtime, observed)
    services.channel_repo.create(
        channel_id="production",
        agent_id=None,
        user_id=1,
        kind="telegram",
        name="production",
        config_json="{}",
    )
    worker = runtime.ProjectTaskWorker(services)
    processor = GlobalProcessor(
        agent_manager=None,
        thread_registry=None,
        audit_repo=services.audit_repo,
        agent_repo=services.agent_repo,
        user_repo=services.user_repo,
        connector_repo=services.connector_repo,
        dispatcher=SlashDispatcher(),
        services=services,
        task_worker=worker,
    )
    inbound = InboundMessage(
        channel_id="production",
        channel_type="telegram",
        tenant_id="general",
        channel_subject=ChannelSubject(subject_id="recipient"),
        content=[TextContent(text="hello")],
        metadata={"message_id": "global", "model_ref": "ingress/model"},
    )
    await worker.start()
    try:
        events = [event async for event in processor(inbound)]
        assert "canonical answer" in "".join(
            part.text for event in events for part in event.content if isinstance(part, TextContent)
        )
        assert len(observed) == 1
        unsupported = inbound.model_copy(
            update={
                "content": [TextContent(text="/switch old-thread")],
                "metadata": {"message_id": "unsupported"},
            }
        )
        errors = [event async for event in processor(unsupported)]
        assert any(event.error and "/switch" in event.error for event in errors)
        assert len(observed) == 1
    finally:
        await worker.close()


async def test_unverified_im_strips_private_expert_memory_and_only_local_owner_can_approve(
    services, monkeypatch
):
    from octop.infra.projects import approval_policy, interactions
    from octop.infra.projects.approval_models import ApprovalPolicyUpdate
    from octop.infra.projects.interaction_models import InteractionResponse

    message(services)
    from octop.infra.projects.expert_memory import ExpertMemory

    recalled = []

    def private_snapshot(*args, **kwargs):
        recalled.append(True)
        return "owner-private-recall-marker"

    monkeypatch.setattr(ExpertMemory, "_prompt_snapshot", private_snapshot)
    policy = approval_policy.current(services, 1)
    approval_policy.set_global_policy(
        services,
        1,
        ApprovalPolicyUpdate(
            request_id="local-full-access", mode="full_access", expected_revision=policy.revision
        ),
    )
    observed = []
    real_factory(
        monkeypatch,
        runtime,
        observed,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "external-write",
                        "name": "write_file",
                        "args": {"file_path": "/approved.txt", "content": "owner approved"},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "publish-external",
                        "name": "publish_workspace_file",
                        "args": {"path": "approved.txt"},
                    }
                ],
            ),
            AIMessage(content="approved action"),
        ],
    )
    services.channel_repo.create(
        channel_id="external",
        agent_id="expert-one",
        user_id=1,
        kind="telegram",
        name="external",
        config_json="{}",
    )
    worker = runtime.ProjectTaskWorker(services)
    await worker.start()
    try:
        adapter = TaskChannelAdapter(services, worker)

        def inbound(text, native):
            return InboundMessage(
                channel_id="external",
                channel_type="telegram",
                tenant_id="expert-one",
                channel_subject=ChannelSubject(subject_id="unverified-sender"),
                content=[TextContent(text=text)],
                metadata={"message_id": native, "model_ref": "ingress/model"},
            )

        first, _ = await adapter.submit(inbound("write a file", "first"))
        async with asyncio.timeout(8):
            while True:
                detail = tasks.detail(services, 1, first.task_id)
                if detail.turns[0].status == "waiting_approval":
                    break
                await asyncio.sleep(0.02)
        turn = detail.turns[0]
        item = next(item for item in turn.interactions if item.status == "pending")
        frozen = json.loads(services.project_task_turn_repo.get(first.turn_id).configuration_json)
        assert frozen["source_origin"]["source"]["authority"]["principal"] == "external_unverified"
        assert frozen["expert_memory_enabled"] is False
        assert frozen["expert_profile"]["memory"]["capture_enabled"] is False
        assert "Personal preferences" not in json.dumps(frozen)
        assert "Personal preferences" not in observed[0].system_prompt
        assert "owner-private-recall-marker" not in observed[0].system_prompt and not recalled
        assert "Expert identity" in observed[0].system_prompt
        assert not any(tool.name.startswith("expert_memory_") for tool in observed[0].tools)
        with pytest.raises(OctopError):
            await adapter.submit(inbound("/approve " + item.interaction_id, "im-approval"))
        assert not (
            services.paths.root / "project-tasks" / first.task_id / "workspace" / "approved.txt"
        ).exists()
        interactions.respond(
            services,
            1,
            first.task_id,
            first.turn_id,
            item.interaction_id,
            InteractionResponse.model_validate(
                {
                    "request_id": "local-owner",
                    "expected_revision": item.revision,
                    "response": {
                        "kind": "approval",
                        "decision": "allow_once",
                        "action_digest": item.payload.action_digest,
                    },
                }
            ),
        )
        worker.wake()
        final = await settled_turn(services, first.turn_id)
        assert final.status == "completed" and final.reply == "approved action"
        assert (
            services.paths.root / "project-tasks" / first.task_id / "workspace" / "approved.txt"
        ).read_text() == "owner approved"
        with services.db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM expert_memory_jobs WHERE task_id=?", (first.task_id,)
                ).fetchone()["n"]
                == 0
            )
    finally:
        await worker.close()
