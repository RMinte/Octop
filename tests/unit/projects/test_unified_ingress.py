"""Durable source admission and transport receipts against real SQLite."""

from __future__ import annotations

import json

import pytest
from tests.unit.projects.test_expert_execution import services  # noqa: F401

from octop.infra.db.repos.unified_sources import UnifiedDeliveryRepo, UnifiedSourceRouteRepo
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import deliveries, ingress, source_routes, tasks


def message(bundle, text="hello"):
    bundle.secret_repo.get_or_create("jwt", lambda: b"test-only-ingress-secret")
    if bundle.provider_repo.get_by_name("ingress") is None:
        bundle.provider_repo.create(
            name="ingress",
            kind="openai",
            base_url="http://ingress.invalid/v1",
            api_key="fake",
            models_json=json.dumps([{"id": "model", "enabled": True}]),
        )
    return tasks.TaskCreate(
        request_id="transport-placeholder",
        prompt=text,
        options=tasks.TaskExecutionOptions(model_ref="ingress/model"),
    )


def source(event="event-one", key="same-chat"):
    return ingress.SourceEnvelope(
        source_kind="im", source_instance="test-channel", source_key=key, event_id=event
    )


async def test_source_replay_fifo_and_exact_cancellation(services):  # noqa: F811
    first = await ingress.submit(services, 1, source(), message(services))
    repeated = await ingress.submit(services, 1, source(), message(services))
    assert first.task_id == repeated.task_id and first.turn_id == repeated.turn_id
    second = await ingress.submit(services, 1, source("event-two"), message(services, "again"))
    assert second.status == "received"
    tasks.cancel(services, 1, first.task_id, first.turn_id)
    admitted = await ingress.admit(services, 1, second.inbox_id)
    assert admitted.task_id == first.task_id and admitted.turn_id != first.turn_id
    tasks.cancel(services, 1, first.task_id, first.turn_id)
    assert services.project_task_turn_repo.get(admitted.turn_id).status == "queued"
    with pytest.raises(OctopError) as exc:
        ingress.receive(services, 1, source(), message(services, "changed"))
    assert exc.value.code == ErrorCode.TASK_REQUEST_CONFLICT


async def test_different_channel_instances_and_users_are_isolated(services):  # noqa: F811
    first = await ingress.submit(services, 1, source(), message(services))
    second = await ingress.submit(services, 2, source(), message(services))
    other = await ingress.submit(
        services,
        1,
        source().model_copy(update={"source_instance": "other-channel"}),
        message(services),
    )
    assert len({first.task_id, second.task_id, other.task_id}) == 3
    with pytest.raises(OctopError):
        ingress.get_receipt(services, 2, first.inbox_id)


async def test_legacy_route_draining_and_task_publication_are_shared(services):  # noqa: F811
    route = source_routes.legacy_route(services, 1, "legacy-thread", "expert-one")
    event = source().model_copy(
        update={"legacy_thread_id": "legacy-thread", "legacy_agent_id": "expert-one"}
    )
    receipt = ingress.receive(services, 1, event, message(services))
    assert receipt.route_id == route.route_id
    with services.db.transaction() as conn:
        assert UnifiedSourceRouteRepo.transition(
            conn,
            1,
            route.route_id,
            route.revision,
            "legacy",
            "draining",
            pending_task_id="future-task",
            now=1,
        )
    with pytest.raises(OctopError) as exc:
        await ingress.admit(services, 1, receipt.inbox_id)
    assert exc.value.code == ErrorCode.TASK_BUSY
    with (
        source_routes.writer_fence(services, "legacy-thread"),
        pytest.raises(OctopError),
        source_routes.writer_fence(services, "legacy-thread"),
    ):
        pass


async def test_published_deleted_target_is_not_recreated(services):  # noqa: F811
    receipt = await ingress.submit(services, 1, source(), message(services))
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE unified_source_routes SET task_id=NULL WHERE route_id=?", (receipt.route_id,)
        )
    with pytest.raises(OctopError) as exc:
        ingress.receive(services, 1, source("event-two"), message(services))
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_unknown_outbound_receipt_is_never_reclaimed(services):  # noqa: F811
    receipt = await ingress.submit(services, 1, source(), message(services))
    row = deliveries.enqueue(
        services, 1, receipt.route_id, receipt.task_id, receipt.turn_id, "final", {"text": "answer"}
    )
    claimed = deliveries.claim(services, row)
    assert claimed is not None
    with services.db.transaction() as conn:
        assert (
            UnifiedDeliveryRepo.claim(
                conn, row.delivery_id, "other-worker", source_routes.now_ms() + 120_000
            )
            is None
        )
        saved = conn.execute(
            "SELECT status FROM unified_source_deliveries WHERE delivery_id=?", (row.delivery_id,)
        ).fetchone()
    assert saved["status"] == "uncertain"
    assert services.project_task_turn_repo.get(receipt.turn_id).status == "queued"


async def test_deleted_source_event_replay_is_typed_not_found_without_recreation(services):  # noqa: F811
    receipt = await ingress.submit(services, 1, source(), message(services))
    tasks.cancel(services, 1, receipt.task_id, receipt.turn_id)
    tasks.delete(services, 1, receipt.task_id)
    for operation in (
        lambda: ingress.lookup(services, 1, source()),
        lambda: ingress.get_receipt(services, 1, receipt.inbox_id),
        lambda: ingress.receive(services, 1, source(), message(services)),
    ):
        with pytest.raises(OctopError) as exc:
            operation()
        assert exc.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(OctopError) as exc:
        await ingress.admit(services, 1, receipt.inbox_id)
    assert exc.value.code == ErrorCode.NOT_FOUND
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_tasks").fetchone()["n"] == 0
        route = UnifiedSourceRouteRepo.get(conn, 1, receipt.route_id)
    assert route.task_id is None and route.revision > 1
