"""Cron confirmation choices reach real Task graphs without changing account policy."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from tests.integration.test_project_task_interactions import interaction_env  # noqa: F401


async def wait_for_task(client, server, auth, cron_id, status):
    async with asyncio.timeout(15):
        while True:
            with server.services.db.connect() as conn:
                row = conn.execute(
                    "SELECT task_id FROM unified_cron_occurrences WHERE cron_id=? "
                    "AND task_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (cron_id,),
                ).fetchone()
            if row is not None:
                result = await client.get(f"/api/project-tasks/{row['task_id']}", headers=auth)
                assert result.status_code == 200, result.text
                detail = result.json()
                if detail["status"] == status:
                    return detail
                assert detail["status"] not in {"failed", "cancelled"}, detail
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("skip", [False, True])
async def test_cron_confirmation_reaches_graph_and_does_not_leak_into_followup(
    interaction_env, skip
):
    client, server, auth, effects, *_ = interaction_env
    policy = (await client.get("/api/project-tasks/approval-policy", headers=auth)).json()
    assert policy["mode"] == "ask_for_approval"
    created = await client.post(
        "/api/cron",
        headers=auth,
        json={
            "trigger": "interval:3600",
            "prompt": "Scheduled external action",
            "task_type": "agent",
            "skip_confirmation": skip,
            "enabled": False,
        },
    )
    assert created.status_code == 201, created.text
    cron_id = created.json()["id"]
    assert (await client.post(f"/api/cron/{cron_id}/run-now", headers=auth)).status_code == 204
    detail = await wait_for_task(
        client, server, auth, cron_id, "completed" if skip else "waiting_approval"
    )
    first = detail["turns"][0]
    assert first["interactions"][0]["payload"]["tool_name"] == "guarded_action"
    assert effects == (["0"] if skip else [])
    stored = server.services.project_task_turn_repo.get(first["turn_id"])
    origin = json.loads(stored.configuration_json)["source_origin"]
    assert origin["source"]["authority"]["skip_confirmation"] is skip
    assert (await client.get("/api/project-tasks/approval-policy", headers=auth)).json() == policy
    if not skip:
        assert first["interactions"][0]["status"] == "pending"
        return
    assert first["interactions"][0]["resolution"]["source"] == "cron_skip_confirmation"
    assert first["interactions"][0]["dispatch_started"] is True
    followup = await client.post(
        f"/api/project-tasks/{detail['task_id']}/messages",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "Ordinary manual followup"},
    )
    assert followup.status_code == 202, followup.text
    current = await wait_for_task(client, server, auth, cron_id, "waiting_approval")
    manual = current["turns"][-1]
    assert manual["turn_id"] != first["turn_id"]
    assert manual["interactions"][0]["status"] == "pending"
    assert effects == ["0"]
    stored = server.services.project_task_turn_repo.get(manual["turn_id"])
    assert "source_origin" not in json.loads(stored.configuration_json)
