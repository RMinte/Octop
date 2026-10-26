"""Business scopes retain the caller identity through real model callbacks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from octop_harness import HarnessAgent
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.agents.providers.box import OctopChatModelFactory, OctopHarnessAgent
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.model_calls import ModelCallRepo
from octop.infra.db.repos.project_task_reviews import TaskReviewRow
from octop.infra.db.repos.project_task_titles import TaskTitleJob
from octop.infra.projects import approval_reviewer, narration, runtime, title
from octop.infra.usage.model_calls import (
    current_model_call_scope,
    model_call_scope,
    override_model_call_scope,
)


@pytest.fixture
def services(tmp_path) -> Iterator[SimpleNamespace]:
    db = SqlitePool(tmp_path / "scope-ledger.db")
    run_migrations(db)
    tasks = {
        (f"task-{owner}", owner): SimpleNamespace(project_id=f"trusted-project-{owner}")
        for owner in (7, 8)
    }
    service = SimpleNamespace(
        model_call_repo=ModelCallRepo(db),
        project_task_repo=SimpleNamespace(
            get=MagicMock(side_effect=tasks.get),
            get_internal=MagicMock(
                side_effect=lambda task_id, owner_id: tasks.get((task_id, owner_id))
            ),
        ),
        user_repo=SimpleNamespace(
            get=MagicMock(return_value=SimpleNamespace(locale="en", disabled=False))
        ),
        user_policy_repo=object(),
        usage_repo=object(),
        provider_repo=object(),
    )
    # Repo.get accepts task_id and owner_id independently, rather than a tuple.
    service.project_task_repo.get.side_effect = lambda task_id, owner_id: tasks.get(
        (task_id, owner_id)
    )
    try:
        yield service
    finally:
        db.close()


@pytest.fixture
def model_factory(monkeypatch):
    base = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="Reading the selected file.",
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            )
        ],
        metadata={"execution_source": "third_party"},
    )
    monkeypatch.setattr(ChatModelFactory, "_build", lambda *_args, **_kwargs: base)
    provider = ProviderConfig(
        id="ActualProvider",
        name="Actual provider",
        api_key="fixture-only",
        base_url="https://provider.invalid/v1",
        models=[ModelConfig(id="actual-model")],
    )
    factory = OctopChatModelFactory([provider])
    for module in (title, approval_reviewer, narration):
        store = MagicMock()
        store.is_model_ref_usable.return_value = True
        store.get_model_reasoning_capability.return_value = None
        store.build_harness_configs.return_value = [provider]
        monkeypatch.setattr(module, "ProviderStore", MagicMock(return_value=store))
        monkeypatch.setattr(module, "assert_token_quota_available", MagicMock())
        monkeypatch.setattr(module, "ChatModelFactory", MagicMock(return_value=factory))
    return factory


def turn(owner: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=f"task-{owner}",
        turn_id=f"turn-{owner}",
        request_id=f"request-{owner}",
        model_ref="ActualProvider/actual-model",
    )


@pytest.mark.parametrize("purpose", ["title", "approval_review", "progress_narration"])
async def test_auxiliary_scopes_bind_owner_and_trusted_project(
    services, model_factory, purpose: str
) -> None:
    selected = "ActualProvider/actual-model"
    if purpose == "title":
        await title.TaskTitleAgent(services).generate(
            TaskTitleJob(
                task_id="task-7",
                owner_id=7,
                title_revision=1,
                prompt="private title request",
                model_ref=selected,
                worker_id="worker",
                attempts=1,
            )
        )
    elif purpose == "approval_review":
        await approval_reviewer.TaskApprovalReviewer(services).generate(
            TaskReviewRow(
                review_id="review-7",
                task_id="task-7",
                turn_id="turn-7",
                owner_id=7,
                execution_host="server",
                target_id="target-7",
                action_digest="digest",
                proposal_json='{"command":"private command"}',
                model_ref=selected,
                created_at=1000,
                updated_at=1000,
                expires_at=2000,
            )
        )
    else:
        await narration.TaskProgressAgent(
            services,
            7,
            selected,
            "en",
            task_id="task-7",
            turn_id="turn-7",
            submission_id="request-7",
        ).generate(narration.ProgressSnapshot("opening", {"goal": "private goal"}))
    records = services.model_call_repo.list_page(user_id=7)
    assert len(records) == 1
    row = records[0]
    assert row.purpose == purpose and row.user_id == 7
    assert row.project_id == "trusted-project-7" and row.task_id == "task-7"
    assert row.model_ref == selected and row.provider == "ActualProvider"
    assert row.source == "third_party" and row.status == "succeeded"
    assert json.loads(row.usage_json)["input_tokens"] == 11
    assert row.turn_id == ("" if purpose == "title" else "turn-7")
    assert "private" not in row.usage_json + row.metadata_json
    services.project_task_repo.get.assert_called_once_with("task-7", 7)
    assert current_model_call_scope() is None


async def test_shared_factory_does_not_mix_concurrent_owners_or_projects(
    services, model_factory
) -> None:
    async def name(owner: int) -> None:
        await title.TaskTitleAgent(services).generate(
            TaskTitleJob(
                task_id=f"task-{owner}",
                owner_id=owner,
                title_revision=1,
                prompt="private goal",
                model_ref="ActualProvider/actual-model",
                worker_id=f"worker-{owner}",
                attempts=1,
            )
        )

    await asyncio.gather(name(7), name(8))
    for owner in (7, 8):
        rows = services.model_call_repo.list_page(user_id=owner)
        assert len(rows) == 1
        assert rows[0].task_id == f"task-{owner}"
        assert rows[0].project_id == f"trusted-project-{owner}"
    assert current_model_call_scope() is None


async def test_run_turn_binds_main_scope_and_cleans_it_when_the_stream_closes(
    services, model_factory, monkeypatch
) -> None:
    closed = []
    model = model_factory.get_chat_model("ActualProvider/actual-model")

    async def execute(*_args):
        try:
            scope = current_model_call_scope()
            assert scope is not None and scope.turn_id == "turn-7"
            await model.ainvoke("private main request")
            yield {"type": "token", "content": "done"}
        finally:
            closed.append(True)

    monkeypatch.setattr(runtime, "_run_turn", execute)
    stream = runtime.run_turn(services, 7, turn(), [])
    assert await anext(stream) == {"type": "token", "content": "done"}
    await stream.aclose()
    assert closed == [True] and current_model_call_scope() is None
    row = services.model_call_repo.list_page(user_id=7)[0]
    assert row.purpose == "main" and row.agent_id == "task-7"
    assert row.project_id == "trusted-project-7" and row.turn_id == "turn-7"
    assert row.submission_id == "request-7" and row.status == "succeeded"


async def test_main_cancellation_finishes_the_call_without_leaking_scope(
    services, model_factory, monkeypatch
) -> None:
    started = asyncio.Event()

    async def pending_model(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(FakeMessagesListChatModel, "_agenerate", pending_model)
    model = model_factory.get_chat_model("ActualProvider/actual-model")

    async def execute(*_args):
        await model.ainvoke("private cancelled request")
        yield {"type": "token", "content": "unreachable"}

    monkeypatch.setattr(runtime, "_run_turn", execute)

    async def consume() -> None:
        async for _event in runtime.run_turn(services, 7, turn(), []):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = services.model_call_repo.list_page(user_id=7)[0]
    assert row.purpose == "main" and row.status == "cancelled"
    assert row.finished_at is not None
    assert json.loads(row.usage_json)["input_tokens"] is None
    assert current_model_call_scope() is None


async def test_consumer_failure_closes_in_its_task_without_finishing_an_independent_child(
    services, model_factory, monkeypatch
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    pending = []
    closed = []

    async def child_model(*_args, **_kwargs):
        started.set()
        await release.wait()
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="done",
                        usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                    )
                )
            ]
        )

    monkeypatch.setattr(FakeMessagesListChatModel, "_agenerate", child_model)
    model = model_factory.get_chat_model("ActualProvider/actual-model")

    async def execute(*_args):
        try:
            pending.append(asyncio.create_task(model.ainvoke("private child request")))
            await asyncio.wait_for(started.wait(), timeout=3)
            yield {"type": "token", "content": "consumer will reject this"}
        finally:
            closed.append(True)

    monkeypatch.setattr(runtime, "_run_turn", execute)
    stream = runtime.run_turn(services, 7, turn(), [])
    try:
        with pytest.raises(RuntimeError, match="consumer failure"):
            try:
                async for _event in stream:
                    raise RuntimeError("consumer failure")
            finally:
                await stream.aclose()
        assert closed == [True] and current_model_call_scope() is None
        assert services.model_call_repo.list_page(user_id=7)[0].status == "running"
    finally:
        release.set()
        await asyncio.gather(*pending)
    assert services.model_call_repo.list_page(user_id=7)[0].status == "succeeded"
    assert current_model_call_scope() is None


async def test_peer_call_keeps_origin_identity_and_changes_only_peer_purpose_and_agent(
    services, model_factory, monkeypatch
) -> None:
    model = model_factory.get_chat_model("ActualProvider/actual-model")

    async def invoke(_self, _request, *, protocol=None):
        await model.ainvoke("private peer request")
        return {"response": "done"}

    monkeypatch.setattr(HarnessAgent, "call", invoke)
    host = object.__new__(OctopHarnessAgent)
    host._agent_id = "task-7"
    peer = object.__new__(OctopHarnessAgent)
    peer._agent_id = "task-7:peer"
    with model_call_scope(
        services,
        user_id=7,
        purpose="main",
        agent_id="task-7",
        thread_id="thread-7",
        task_id="task-7",
        turn_id="turn-7",
        project_id="trusted-project-7",
    ):
        assert await peer.call("private peer request") == {"response": "done"}
        assert current_model_call_scope().agent_id == "task-7"
        await host.call("private main request")
    main, delegated = services.model_call_repo.list_page(user_id=7)
    assert (main.purpose, main.agent_id) == ("main", "task-7")
    assert (delegated.purpose, delegated.agent_id) == ("subagent", "task-7:peer")
    assert main.thread_id == delegated.thread_id == "thread-7"
    assert main.project_id == delegated.project_id == "trusted-project-7"
    assert main.turn_id == delegated.turn_id == "turn-7"
    assert main.model_ref == delegated.model_ref == "ActualProvider/actual-model"
    assert current_model_call_scope() is None


async def test_sync_model_in_worker_thread_retains_explicit_actor(services, model_factory) -> None:
    model = model_factory.get_chat_model("ActualProvider/actual-model")
    with model_call_scope(services, user_id=7, purpose="knowledge_ocr"):
        await asyncio.to_thread(model.invoke, "private image extraction request")
    row = services.model_call_repo.list_page(user_id=7)[0]
    assert row.purpose == "knowledge_ocr" and row.status == "succeeded"
    assert row.model_ref == "ActualProvider/actual-model"
    assert current_model_call_scope() is None


@pytest.mark.parametrize("explicit_compaction", [False, True])
async def test_summary_metadata_and_per_call_callbacks_keep_factory_receipt(
    services, model_factory, explicit_compaction: bool
) -> None:
    model = model_factory.get_chat_model("ActualProvider/actual-model")
    callback = UsageMetadataCallbackHandler()
    with (
        model_call_scope(services, user_id=7, purpose="main", task_id="task-7"),
        override_model_call_scope(purpose="compaction" if explicit_compaction else None),
    ):
        await model.with_config(callbacks=[callback]).ainvoke(
            "private summary request", config={"metadata": {"lc_source": "summarization"}}
        )
    rows = services.model_call_repo.list_page(user_id=7)
    assert len(rows) == 1
    assert rows[0].purpose == ("compaction" if explicit_compaction else "summarization")
    assert rows[0].status == "succeeded" and rows[0].task_id == "task-7"
    assert json.loads(rows[0].usage_json)["input_tokens"] == 11
