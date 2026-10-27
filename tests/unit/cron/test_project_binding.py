"""Project schedules keep their scope and apply the next occurrence's settings."""
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from tests.unit.cron.test_cron_manager import _make_gateway
from tests.unit.projects.test_unified_entrypoint_runtime import real_factory
from tests.unit.projects.test_unified_ingress import message, services  # noqa: F401

from octop.infra.cron.delivery import CronDeliveryCommand, CronDeliveryService
from octop.infra.cron.manager import CronManager
from octop.infra.db.repos.unified_sources import UnifiedSourceRouteRepo
from octop.infra.projects import ingress, runtime, source_routes


def _projects(services):
    with services.db.transaction() as conn:
        for identifier in ("first", "second"):
            conn.execute(
                "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) "
                "VALUES(?,1,?,1,1)",
                (identifier, identifier),
            )


def _delivery(services, worker=None):
    gateway = SimpleNamespace(
        thread_registry=SimpleNamespace(get_session=lambda key: None),
        ws_hub=SimpleNamespace(push_to_user=AsyncMock()),
        _task_services=services,
        _task_cron_delivery=None,
    )
    service = CronDeliveryService(
        gateway=gateway, agent_manager=SimpleNamespace(), repos=services.repos, services=services
    )
    service._task_worker = worker
    return service


def _command(services, event, **changes):
    row = services.cron_repo.get("project-job")
    return CronDeliveryCommand(
        **{
            "cron_id": row.cron_id,
            "cron_name": row.name,
            "agent_id": row.agent_id,
            "user_id": row.user_id,
            "session_key": row.session_key,
            "prompt": row.prompt,
            "fresh_thread": False,
            "task_type": row.task_type,
            "model": row.model,
            "mcp_servers": (),
            "event_key": event,
            "conversation_policy": row.conversation_policy,
            "project_id": row.project_id,
            "selected_expert_id": row.selected_expert_id,
            **changes,
        }
    )


async def test_per_job_rebinding_preserves_history_and_uses_each_project(services):  # noqa: F811
    _projects(services)
    services.cron_repo.create(
        cron_id="project-job",
        agent_id=None,
        user_id=1,
        trigger="interval:3600",
        prompt="Notice",
        session_key="cron:project-job",
        conversation_policy="per_job",
        project_id="first",
        task_type="text",
    )
    delivery = _delivery(services)
    for event, project in enumerate(("first", "second", "first", None), 1):
        services.cron_repo.update("project-job", project_id=project)
        await delivery.deliver(_command(services, f"manual:{event}"))
    with services.db.connect() as conn:
        rows = conn.execute(
            "SELECT project_id,count(*) AS turns FROM project_tasks "
            "JOIN project_task_turns USING(task_id) GROUP BY project_id"
        ).fetchall()
        assert {row["project_id"]: row["turns"] for row in rows} == {
            "first": 2,
            "second": 1,
            None: 1,
        }


@pytest.mark.parametrize("original_project", [None, "first"])
async def test_existing_unscoped_task_is_reused_only_for_its_project(  # noqa: F811
    services, original_project
):
    _projects(services)
    seed = ingress.record_notification(
        services,
        1,
        ingress.SourceEnvelope(
            source_kind="cron",
            source_instance=source_routes.local_instance(services),
            source_key="cron:project-job",
            event_id="old-occurrence",
        ),
        "Original notice",
        project_id=original_project,
    )
    services.cron_repo.create(
        cron_id="project-job",
        agent_id=None,
        user_id=1,
        trigger="interval:3600",
        prompt="Notice",
        session_key="cron:project-job",
        conversation_policy="per_job",
        project_id=original_project,
        task_type="text",
    )
    delivery = _delivery(services)
    await delivery.deliver(_command(services, "manual:matching"))
    assert len(services.project_task_turn_repo.list(seed.task_id)) == 2
    services.cron_repo.update("project-job", project_id="second")
    await delivery.deliver(_command(services, "manual:changed"))
    services.cron_repo.update("project-job", project_id=None)
    await delivery.deliver(_command(services, "manual:cleared"))
    with services.db.connect() as conn:
        cleared = conn.execute(
            "SELECT project_id FROM project_tasks WHERE task_id=("
            "SELECT task_id FROM unified_cron_occurrences WHERE event_key='manual:cleared')"
        ).fetchone()
        assert cleared["project_id"] is None
        assert conn.execute("SELECT count(*) FROM project_tasks").fetchone()[0] == (
            2 if original_project is None else 3
        )


@pytest.mark.parametrize("policy", ["per_job", "continue_target"])
async def test_expert_change_applies_next_turn_and_keeps_snapshot(  # noqa: F811
    services, monkeypatch, policy
):
    from octop.infra.projects.title import TaskTitleAgent, TitleResult

    monkeypatch.setattr(TaskTitleAgent, "generate", AsyncMock(return_value=TitleResult()))
    message(services)
    _projects(services)
    observed = []
    real_factory(
        monkeypatch,
        runtime,
        observed,
        responses=[
            AIMessage(id="expert-answer", content="Expert answer"),
            AIMessage(id="general-answer", content="General answer"),
        ],
    )
    services.cron_repo.create(
        cron_id="project-job",
        agent_id=None,
        user_id=1,
        trigger="interval:3600",
        prompt="Question",
        session_key="cron:project-job",
        model="ingress/model",
        conversation_policy=policy,
        project_id="first",
        selected_expert_id="expert-one",
    )
    worker = runtime.ProjectTaskWorker(services)
    delivery = _delivery(services, worker)
    await worker.start()
    try:
        async with asyncio.timeout(10):
            await delivery.deliver(_command(services, "manual:expert"))
            services.cron_repo.update("project-job", selected_expert_id=None)
            await delivery.deliver(_command(services, "manual:general"))
        with services.db.connect() as conn:
            rows = conn.execute(
                "SELECT task_id,configuration_json FROM project_task_turns ORDER BY id"
            ).fetchall()
        assert len(rows) == 2 and rows[0]["task_id"] == rows[1]["task_id"]
        first, second = (json.loads(row["configuration_json"]) for row in rows)
        assert first["expert_profile"]["expert_id"] == "expert-one"
        assert second["expert_profile"] is None
        assert second["configuration_revision"] == first["configuration_revision"] + 1
        assert observed[0].system_prompt != observed[1].system_prompt
    finally:
        await worker.close()


async def test_continue_target_fresh_thread_keeps_the_previous_task_identity(services):
    _projects(services)
    services.cron_repo.create(
        cron_id="project-job",
        agent_id=None,
        user_id=1,
        trigger="interval:3600",
        prompt="Notice",
        session_key="cron:project-job",
        conversation_policy="continue_target",
        project_id="first",
        selected_expert_id="expert-one",
        task_type="text",
    )
    delivery = _delivery(services)
    await delivery.deliver(_command(services, "manual:first"))
    services.cron_repo.update("project-job", selected_expert_id=None)
    await delivery.deliver(_command(services, "manual:fresh", fresh_thread=True))
    with services.db.connect() as conn:
        rows = conn.execute(
            "SELECT selected_expert_id,configuration_revision FROM project_tasks ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("expert-one", 1), (None, 1)]


@pytest.mark.parametrize("legacy_session", [False, True])
async def test_clearing_project_from_legacy_continue_target_keeps_project_isolation(
    services, legacy_session
):
    _projects(services)
    seed = ingress.record_notification(
        services,
        1,
        ingress.SourceEnvelope(
            source_kind="cron",
            source_instance=source_routes.local_instance(services),
            source_key="legacy-session",
            event_id="old-occurrence",
        ),
        "Original notice",
        project_id="first",
    )
    if legacy_session:
        legacy = source_routes.legacy_route(services, 1, "legacy-thread", "expert-one")
        with services.db.transaction() as conn:
            assert UnifiedSourceRouteRepo.transition(
                conn,
                1,
                legacy.route_id,
                legacy.revision,
                "legacy",
                "task",
                task_id=seed.task_id,
                now=1,
            )
    services.cron_repo.create(
        cron_id="project-job",
        agent_id="expert-one" if legacy_session else None,
        user_id=1,
        trigger="interval:3600",
        prompt="Notice",
        session_key="legacy-session",
        conversation_policy="continue_target",
        project_id="first",
        selected_expert_id=None,
        task_type="text",
    )
    delivery = _delivery(services)
    if legacy_session:
        session = SimpleNamespace(
            thread_id="legacy-thread",
            user_id=1,
            agent_id="expert-one",
            channel_type="dashboard",
            channel_id="octop-dashboard",
            channel_subject_id="1",
            channel_metadata={},
        )
        delivery._gateway.thread_registry.get_session = lambda key: session
    services.cron_repo.update("project-job", project_id=None)
    await delivery.deliver(_command(services, "manual:cleared"))
    with services.db.connect() as conn:
        cleared = conn.execute(
            "SELECT task_id,project_id FROM project_tasks WHERE task_id=("
            "SELECT task_id FROM unified_cron_occurrences WHERE event_key='manual:cleared')"
        ).fetchone()
    assert cleared["project_id"] is None and cleared["task_id"] != seed.task_id
    assert services.project_task_repo.get(seed.task_id, 1).project_id == "first"


async def test_scheduled_occurrence_reads_updates_without_changing_deadline(services):  # noqa: F811
    _projects(services)
    gateway = _make_gateway()
    delivery = CronDeliveryService(
        gateway=gateway, agent_manager=SimpleNamespace(), repos=services.repos
    )
    delivery.deliver = AsyncMock()
    manager = CronManager(gateway=gateway, delivery_service=delivery, repos=services.repos)
    row = await manager.create(
        cron_id="project-job",
        agent_id=None,
        user_id=1,
        trigger="interval:3600",
        prompt="Original",
        project_id="first",
        conversation_policy="per_job",
    )
    assert row.session_key == "cron:project-job"
    manager._scheduler.start(paused=True)
    try:
        scheduled = manager._scheduler.get_job(row.cron_id)
        pending = scheduled.next_run_time - timedelta(minutes=30)
        manager._scheduler.modify_job(row.cron_id, next_run_time=pending)
        await manager.update(
            row.cron_id,
            project_id="second",
            selected_expert_id="expert-one",
            prompt="Updated",
        )
        assert manager._scheduler.get_job(row.cron_id).next_run_time == pending
        manager._scheduled_occurrences[row.cron_id] = [datetime.now(UTC)]
        await scheduled.func()
        command = delivery.deliver.await_args.args[0]
        assert command.project_id == "second"
        assert command.selected_expert_id == "expert-one" and command.prompt == "Updated"
        assert manager._scheduler.get_job(row.cron_id).next_run_time == pending
    finally:
        await manager.shutdown()


async def test_binding_legacy_job_to_project_detaches_expert_ownership(services):  # noqa: F811
    _projects(services)
    gateway = _make_gateway()
    manager = CronManager(
        gateway=gateway,
        delivery_service=CronDeliveryService(
            gateway=gateway, agent_manager=SimpleNamespace(), repos=services.repos
        ),
        repos=services.repos,
    )
    await manager.create(
        cron_id="legacy-job",
        agent_id="expert-one",
        user_id=1,
        trigger="interval:3600",
        prompt="Notice",
        enabled=False,
    )
    changed = await manager.update("legacy-job", project_id="first")
    assert changed.agent_id is None and changed.selected_expert_id == "expert-one"
    assert changed.session_key == "cron:legacy-job"
    services.agent_repo.delete("expert-one")
    kept = services.cron_repo.get("legacy-job")
    assert kept is not None and kept.project_id == "first" and kept.selected_expert_id is None


@pytest.mark.parametrize("selection", [{}, {"selected_expert_id": None}])
async def test_creating_legacy_job_in_project_detaches_ownership(services, selection):
    _projects(services)
    gateway = _make_gateway()
    manager = CronManager(
        gateway=gateway,
        delivery_service=CronDeliveryService(
            gateway=gateway, agent_manager=SimpleNamespace(), repos=services.repos
        ),
        repos=services.repos,
    )
    row = await manager.create(
        cron_id="legacy-project-job",
        agent_id="expert-one",
        user_id=1,
        trigger="interval:3600",
        prompt="Notice",
        enabled=False,
        project_id="first",
        session_key="legacy-session",
        **selection,
    )
    assert row.agent_id is None and row.session_key == "cron:legacy-project-job"
    assert row.selected_expert_id == (None if selection else "expert-one")
    services.agent_repo.delete("expert-one")
    assert services.cron_repo.get(row.cron_id).project_id == "first"
