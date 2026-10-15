"""The opt-in worker executes an accepted text Turn without an HTTP request owner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from octop_harness.llm.factory import ChatModelFactory
from tests.support.agentcomputer_history import open_history_archive
from tests.unit.agentcomputer.test_standalone_turn_registration import (
    _message as standalone_message,
)
from tests.unit.agentcomputer.test_standalone_turn_registration import _task as standalone_task
from tests.unit.agentcomputer.test_standalone_turn_registration import (
    control as standalone_control,  # noqa: F401
)
from tests.unit.agentcomputer.test_submission_runs import control as control  # noqa: F401
from tests.unit.agentcomputer.test_submission_runs import request

from octop.config import OctopConfig
from octop.infra.agentcomputer import text_worker
from octop.infra.agentcomputer.active_runs import ActiveRunRegistry
from octop.infra.agentcomputer.event_listing import list_task_events
from octop.infra.agentcomputer.results import get_run_result
from octop.infra.agentcomputer.runs import get_run
from octop.infra.agentcomputer.submission import submit_new_message
from octop.infra.agentcomputer.task_snapshot import get_task_control_snapshot
from octop.infra.agentcomputer.tasks import get_task
from octop.infra.agentcomputer.text_worker import LocalTextTaskWorker
from octop.infra.agentcomputer.turn_listing import list_turns
from octop.infra.agents.manager import AgentManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


class StaticFactory(ChatModelFactory):
    def __init__(self, answer_id: UUID) -> None:
        super().__init__([])
        self.model = BindableFakeModel(
            responses=[AIMessage(content="盒子任务已完成", id=str(answer_id))]
        )

    def get_chat_model(self, model_ref: str) -> FakeMessagesListChatModel:
        assert model_ref == "fixture/no-model"
        return self.model


class BindableFakeModel(FakeMessagesListChatModel):
    calls: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> BindableFakeModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.calls += 1
        return super()._generate(messages, stop, run_manager, **kwargs)


async def test_fresh_enabled_install_with_no_queued_runs_needs_no_box_identity(tmp_path) -> None:
    paths = PathLayout(tmp_path)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(
        db=pool,
        paths=paths,
        config=OctopConfig(
            agentcomputer_text_enabled=True,
            agentcomputer_text_model_ref="fixture/no-model",
            history_v2_enabled=True,
        ),
    )
    services.provider_repo.create(
        name="fixture",
        kind="openai",
        base_url="https://provider.invalid/v1",
        api_key="fixture-key",
        models_json=json.dumps([{"id": "no-model", "enabled": True}]),
    )
    archive = open_history_archive(services)
    worker = LocalTextTaskWorker(
        services,
        AgentManager(repos=services.repos, paths=paths),
        archive,
        ActiveRunRegistry(),
    )
    try:
        await worker.start()
        assert not paths.agentcomputer_identity.exists()
    finally:
        await worker.close()
        archive.store.close()
        pool.close()


async def test_standalone_task_worker_completes_two_turns_without_project(
    standalone_control,  # noqa: F811
    monkeypatch,
) -> None:
    services, manager = standalone_control
    provider_id = services.provider_repo.create(
        name="fixture",
        kind="openai",
        base_url="https://provider.invalid/v1",
        api_key="fixture-key",
        models_json=json.dumps([{"id": "no-model", "enabled": True}]),
    )
    eligible = replace(
        services,
        config=replace(
            services.config,
            agentcomputer_text_enabled=True,
            agentcomputer_text_model_ref="fixture/no-model",
            history_v2_enabled=True,
        ),
    )
    task = await standalone_task(eligible, manager)
    assert task.executionReady
    first = submit_new_message(
        eligible, owner_id=1, task_id=task.taskID, command=standalone_message(task)
    )
    factory = StaticFactory(first.assistantMessageID)
    monkeypatch.setattr(text_worker, "ChatModelFactory", lambda _providers: factory)
    archive = open_history_archive(eligible)
    worker = LocalTextTaskWorker(eligible, manager, archive, ActiveRunRegistry())

    async def completed(run_id: UUID) -> bool:
        for _ in range(300):
            await asyncio.sleep(0.01)
            current = get_run(eligible, owner_id=1, run_id=run_id)
            if current is not None and current.state == "succeeded":
                return True
        return False

    try:
        await worker.start()
        assert await completed(first.runID)
        result = get_run_result(eligible, owner_id=1, run_id=first.runID)
        assert result is not None and result.text == "盒子任务已完成"
        current_task = get_task(eligible, 1, task.taskID)
        assert current_task is not None and current_task.executionReady
        second = submit_new_message(
            eligible,
            owner_id=1,
            task_id=task.taskID,
            command=standalone_message(current_task, prompt="继续这个会话。"),
        )
        factory.model.responses.append(
            AIMessage(content="第二轮已完成", id=str(second.assistantMessageID))
        )
        factory.model.i = 1
        await worker.scan_queued()
        assert await completed(second.runID)
        second_result = get_run_result(eligible, owner_id=1, run_id=second.runID)
        assert second_result is not None and second_result.text == "第二轮已完成"
        turns = list_turns(eligible, 1, task.taskID, 10, None)
        assert turns is not None and len(turns.items) == 2
        snapshot = get_task_control_snapshot(eligible, 1, task.taskID)
        assert snapshot is not None and snapshot.latestRun is not None
        assert snapshot.latestRun.runID == second.runID
        events = list_task_events(eligible, 1, task.taskID, 20, None)
        assert (
            events is not None and [item.type for item in events.items].count("run.finished") == 2
        )
        assert (
            eligible.paths.root / "agentcomputer-checkpoints" / f"task-{task.taskID}.sqlite"
        ).exists()
        with eligible.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 0
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM ac_runs WHERE task_id=? AND project_id IS NULL AND state='succeeded'",
                    (str(task.taskID),),
                ).fetchone()[0]
                == 2
            )
            assert (
                conn.execute(
                    "SELECT active_run_id FROM ac_tasks WHERE task_id=?", (str(task.taskID),)
                ).fetchone()[0]
                is None
            )
        assert factory.model.calls == 2
        services.provider_repo.update(provider_id, enabled=False)
        unavailable = get_task(eligible, 1, task.taskID)
        assert unavailable is not None and not unavailable.executionReady
    finally:
        await worker.close()
        archive.store.close()


async def test_startup_redrives_only_never_dispatched_text_run(  # noqa: F811
    control, monkeypatch
) -> None:
    services, manager, task_id = control
    services.provider_repo.create(
        name="fixture",
        kind="openai",
        base_url="https://provider.invalid/v1",
        api_key="fixture-key",
        models_json=json.dumps([{"id": "no-model", "enabled": True}]),
    )
    eligible = replace(
        services,
        config=replace(
            services.config,
            agentcomputer_text_enabled=True,
            agentcomputer_text_model_ref="fixture/no-model",
            history_v2_enabled=True,
        ),
    )
    receipt = submit_new_message(eligible, owner_id=1, task_id=task_id, command=request())
    factory = StaticFactory(receipt.assistantMessageID)
    monkeypatch.setattr(text_worker, "ChatModelFactory", lambda _providers: factory)
    archive = open_history_archive(eligible)
    worker = LocalTextTaskWorker(eligible, manager, archive, ActiveRunRegistry())
    try:
        await worker.start()

        async def completed() -> bool:
            for _ in range(300):
                await asyncio.sleep(0.01)
                current = get_run(eligible, owner_id=1, run_id=receipt.runID)
                if current is not None and current.state == "succeeded":
                    return True
            return False

        assert await completed()
        result = get_run_result(eligible, owner_id=1, run_id=receipt.runID)
        assert result is not None
        assert result.state == "succeeded"
        assert result.text == "盒子任务已完成"
        assert result.assistantMessageID == str(receipt.assistantMessageID)
        assert factory.model.calls == 1
        assert worker.schedule(owner_id=1, run_id=receipt.runID)
        await asyncio.sleep(0.05)
        assert factory.model.calls == 1
    finally:
        await worker.close()
        archive.store.close()
