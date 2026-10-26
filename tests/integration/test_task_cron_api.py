"""Owner-scoped cron schedules do not require an expert or legacy thread."""

from __future__ import annotations

import asyncio

from tests.support.auth import create_user


async def test_general_schedule_delivers_canonical_notification_without_model(env):
    client, server, auth = env
    created = await client.post(
        "/api/cron",
        headers=auth,
        json={
            "trigger": "interval:3600",
            "prompt": "Canonical scheduled notice",
            "task_type": "text",
            "enabled": False,
        },
    )
    assert created.status_code == 201
    row = created.json()
    assert row["agent_id"] is None and row["selected_expert_id"] is None
    assert row["conversation_policy"] == "per_job"
    cron_id = row["id"]
    result = await client.post(f"/api/cron/{cron_id}/run-now", headers=auth)
    assert result.status_code == 204
    queried = await client.get(f"/api/cron/{cron_id}", headers=auth)
    assert queried.json()["last_status"] is not None
    async with asyncio.timeout(5):
        while (await client.get(f"/api/cron/{cron_id}", headers=auth)).json()[
            "last_status"
        ] == "queued":
            await asyncio.sleep(0.05)
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM project_tasks WHERE selected_expert_id IS NULL"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT status FROM unified_cron_occurrences WHERE cron_id=?", (cron_id,)
            ).fetchone()[0]
            == "completed"
        )
    tasks = (await client.get("/api/project-tasks", headers=auth)).json()["items"]
    detail = (await client.get("/api/project-tasks/" + tasks[0]["task_id"], headers=auth)).json()
    assert detail["turns"][-1]["reply"] == "Canonical scheduled notice"
    assert (await client.delete(f"/api/cron/{cron_id}", headers=auth)).status_code == 204
    assert (
        await client.get("/api/project-tasks/" + tasks[0]["task_id"], headers=auth)
    ).status_code == 200


async def test_schedule_owner_and_configuration_validation(env):
    client, _server, auth = env
    other = await create_user(client, auth, username="task-cron-other")
    response = await client.post(
        "/api/cron",
        headers=auth,
        json={
            "trigger": "interval:3600",
            "prompt": "Owner notification",
            "enabled": False,
        },
    )
    assert response.status_code == 201
    cron_id = response.json()["id"]
    assert (await client.get("/api/cron", headers=other)).json() == []
    assert (await client.get(f"/api/cron/{cron_id}", headers=other)).status_code == 404
    assert (
        await client.patch(f"/api/cron/{cron_id}", headers=other, json={"prompt": "changed"})
    ).status_code == 404
    assert (await client.post(f"/api/cron/{cron_id}/run-now", headers=other)).status_code == 404
    assert (await client.delete(f"/api/cron/{cron_id}", headers=other)).status_code == 404
    assert (
        await client.post(
            "/api/cron",
            headers=auth,
            json={
                "trigger": "interval:3600",
                "prompt": "Invalid target",
                "conversation_policy": "task",
                "target_task_id": "missing-task",
            },
        )
    ).status_code == 404
    changed = await client.patch(
        f"/api/cron/{cron_id}",
        headers=auth,
        json={"conversation_policy": "per_run", "selected_expert_id": None},
    )
    assert changed.status_code == 200 and changed.json()["conversation_policy"] == "per_run"


async def test_recovery_and_active_job_wait_for_the_same_delivery_outcome(env, monkeypatch):
    from octop.infra.cron import task_delivery

    client, server, auth = env
    service = server.app_runtime.cron_manager._delivery_service
    delivery_started = asyncio.Event()
    job_entered = asyncio.Event()
    release = asyncio.Event()
    sends = []
    original_deliver = service.deliver

    async def delayed_job(command):
        await delivery_started.wait()
        job_entered.set()
        await original_deliver(command)

    async def send(_owner, payload):
        sends.append(payload)
        delivery_started.set()
        await release.wait()

    monkeypatch.setattr(service, "deliver", delayed_job)
    monkeypatch.setattr(service._gateway.ws_hub, "push_to_user", send)
    created = await client.post(
        "/api/cron",
        headers=auth,
        json={
            "trigger": "interval:3600",
            "prompt": "Send exactly once",
            "task_type": "text",
            "enabled": False,
        },
    )
    assert created.status_code == 201
    cron_id = created.json()["id"]
    assert (await client.post(f"/api/cron/{cron_id}/run-now", headers=auth)).status_code == 204
    recovering = asyncio.create_task(task_delivery.recover(service))
    try:
        await asyncio.wait_for(delivery_started.wait(), 20)
        await asyncio.wait_for(job_entered.wait(), 20)
        await asyncio.sleep(0.05)
        # Recovery owns the send; the original CronJob must not report success
        # merely because it did not acquire the same outbound receipt.
        assert (await client.get(f"/api/cron/{cron_id}", headers=auth)).json()[
            "last_status"
        ] == "queued"
    finally:
        release.set()
        await recovering
    async with asyncio.timeout(20):
        while (await client.get(f"/api/cron/{cron_id}", headers=auth)).json()[
            "last_status"
        ] == "queued":
            await asyncio.sleep(0.01)
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM unified_cron_occurrences WHERE cron_id=?", (cron_id,)
            ).fetchone()[0]
            == "completed"
        )
    assert len(sends) == 1
