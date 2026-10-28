"""Busy durable sources cannot block admission or control for other inputs."""

from __future__ import annotations

import asyncio

import pytest
from tests.unit.projects.test_unified_ingress import message, services, source  # noqa: F401

from octop.infra.projects import ingress, tasks


async def admitted(bundle, owner, receipt):
    async with asyncio.timeout(8):
        while True:
            current = ingress.get_receipt(bundle, owner, receipt.inbox_id)
            if current.status != "received":
                assert current.status == "admitted", current.error_code
                return current
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("distinct_sources", [False, True])
async def test_worker_reaches_another_owner_past_a_busy_batch(services, distinct_sources):  # noqa: F811
    body = message(services)
    blocked = []
    for index in range(100):
        key = f"busy-{index}" if distinct_sources else "busy"
        if distinct_sources or index == 0:
            await ingress.submit(services, 1, source(f"active-{index}", key), body)
        blocked.append(ingress.receive(services, 1, source(f"queued-{index}", key), body))
    free = ingress.receive(services, 2, source("free", "another-owner"), body)

    worker = ingress.TaskIngressWorker(services)
    await worker.start()
    try:
        accepted = await admitted(services, 2, free)
        assert services.project_task_repo.get(accepted.task_id, 2) is not None
        assert all(
            ingress.get_receipt(services, 1, item.inbox_id).status == "received" for item in blocked
        )
    finally:
        await worker.close()


async def test_control_past_a_busy_batch_cancels_exact_turn_and_preserves_fifo(services):  # noqa: F811
    body = message(services)
    first = await ingress.submit(services, 1, source("active"), body)
    queued = [
        ingress.receive(
            services,
            1,
            source(f"queued-{index}"),
            body.model_copy(update={"prompt": f"message {index}"}),
        )
        for index in range(101)
    ]
    stop = ingress.receive(
        services,
        1,
        source("stop").model_copy(update={"operation": "cancel", "turn_id": first.turn_id}),
        body.model_copy(update={"prompt": "/stop"}),
    )

    worker = ingress.TaskIngressWorker(services)
    await worker.start()
    try:
        stopped = await admitted(services, 1, stop)
        next_turn = await admitted(services, 1, queued[0])
        assert stopped.turn_id == first.turn_id
        assert services.project_task_turn_repo.get(first.turn_id).status == "cancelled"
        assert next_turn.task_id == first.task_id
        task = tasks.detail(services, 1, first.task_id)
        assert task.active_turn_id == next_turn.turn_id
        assert task.turns[-1].prompt == "message 0"
        assert all(
            ingress.get_receipt(services, 1, item.inbox_id).status == "received"
            for item in queued[1:]
        )
        # A replay of the old control must not cancel the newly admitted head.
        replay = await ingress.admit(services, 1, stop.inbox_id)
        assert replay.turn_id == first.turn_id
        assert services.project_task_turn_repo.get(next_turn.turn_id).status == "queued"
    finally:
        await worker.close()


async def test_new_arrivals_do_not_prevent_retrying_a_source_that_becomes_idle(
    services,  # noqa: F811
    monkeypatch,
):
    body = message(services)
    first = await ingress.submit(services, 1, source("active"), body)
    head = ingress.receive(services, 1, source("head"), body)
    tail = ingress.receive(services, 1, source("tail"), body)
    original = ingress.admit
    attempts = 0

    async def with_new_input(bundle, owner, inbox_id):
        nonlocal attempts
        try:
            return await original(bundle, owner, inbox_id)
        finally:
            attempts += 1
            if attempts == 1:
                await asyncio.to_thread(tasks.cancel, bundle, 1, first.task_id, first.turn_id)
            # Keep higher input IDs arriving throughout the worker's scan.
            await asyncio.to_thread(
                ingress.receive,
                bundle,
                2,
                source(f"arrival-{attempts}", f"new-source-{attempts}"),
                body,
            )

    monkeypatch.setattr(ingress, "admit", with_new_input)
    worker = ingress.TaskIngressWorker(services)
    await worker.start()
    try:
        accepted = await admitted(services, 1, head)
        assert accepted.task_id == first.task_id
        assert tasks.detail(services, 1, first.task_id).active_turn_id == accepted.turn_id
        assert ingress.get_receipt(services, 1, tail.inbox_id).status == "received"
    finally:
        await worker.close()
