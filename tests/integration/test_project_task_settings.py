"""Global task defaults, admission snapshots, and safe environment writes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from octop.infra.projects import runtime
from octop.infra.projects.settings import ACTION_TIMELINE_ENV, STEP_PANEL_ENV
from octop.infra.projects.tasks import turn_view
from octop.infra.utils.env_file import save_env_file
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_tasks import settled
from tests.support.auth import create_user


async def test_settings_are_public_to_authenticated_users_without_env_permission(env, monkeypatch):
    client, server, admin = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "false")
    monkeypatch.setenv(STEP_PANEL_ENV, "false")
    save_env_file(
        server.paths.root / "env",
        {ACTION_TIMELINE_ENV: "true", STEP_PANEL_ENV: "false", "PRIVATE_KEY": "not returned"},
    )
    auth = await create_user(client, admin, username="task-settings-reader", permissions=[])
    assert (await client.get("/api/project-tasks/settings")).status_code == 401
    response = await client.get("/api/project-tasks/settings", headers=auth)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"action_timeline_enabled": True, "step_panel_enabled": False}
    assert (await client.get("/api/envs", headers=auth)).status_code == 403
    operation = client._octop_app.openapi()["paths"]["/api/project-tasks/settings"]["get"]
    assert operation["summary"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskSettings"
    )


@pytest.mark.parametrize("key", [ACTION_TIMELINE_ENV, STEP_PANEL_ENV])
async def test_invalid_env_switch_is_rejected_before_any_write_or_process_change(
    env, monkeypatch, key
):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "true")
    path = server.paths.root / "env"
    save_env_file(path, {ACTION_TIMELINE_ENV: "true", "OTHER": "preserved"})
    before = path.read_bytes()
    response = await client.put(
        "/api/envs",
        headers=auth,
        json={key: "invalid-private-value", "OTHER": "would change"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TASK_SETTINGS_INVALID"
    assert response.json()["error"]["details"] == {"setting": key}
    assert "invalid-private-value" not in response.text
    assert path.read_bytes() == before
    assert (await client.get("/api/project-tasks/settings", headers=auth)).json()[
        "action_timeline_enabled"
    ]


async def test_saved_and_deleted_keys_are_visible_despite_a_stale_worker_environment(
    env, monkeypatch
):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "true")
    monkeypatch.setenv(STEP_PANEL_ENV, "true")
    response = await client.put(
        "/api/envs", headers=auth, json={ACTION_TIMELINE_ENV: "off", STEP_PANEL_ENV: "no"}
    )
    assert response.status_code == 200
    # Simulate another worker which did not receive the process-environment update.
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "true")
    monkeypatch.setenv(STEP_PANEL_ENV, "true")
    assert (await client.get("/api/project-tasks/settings", headers=auth)).json() == {
        "action_timeline_enabled": False,
        "step_panel_enabled": False,
    }
    assert (
        await client.delete("/api/envs/" + ACTION_TIMELINE_ENV, headers=auth)
    ).status_code == 200
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "false")
    assert (await client.get("/api/project-tasks/settings", headers=auth)).json() == {
        "action_timeline_enabled": True,
        "step_panel_enabled": False,
    }


async def test_per_turn_snapshot_survives_global_change_retransmission_pause_and_resume(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    save_env_file(server.paths.root / "env", {ACTION_TIMELINE_ENV: "false"})
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def fake_run(services, owner, turn, history):
        seen.append(turn)
        if not turn.checkpoint_id and turn.prompt == "Pause this turn":
            entered.set()
            await release.wait()
            yield {
                "type": "pause",
                "checkpoint_id": "settings-test-checkpoint",
                "calls": [],
                "groups": [],
                "new_approval_ids": [],
                "wait_kind": "user",
                "interactions_json": None,
            }
        else:
            yield {"type": "token", "content": "Completed"}

    monkeypatch.setattr(runtime, "run_turn", fake_run)
    body = {"request_id": "settings-first-request", "prompt": "Pause this turn"}
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 202, response.text
    task = response.json()
    task_id, turn_id = task["task_id"], task["turns"][0]["turn_id"]
    assert task["turns"][0]["options"]["action_timeline_enabled"] is False
    await asyncio.wait_for(entered.wait(), 3)
    control = {"request_id": "settings-pause-request", "turn_id": turn_id}
    assert (
        await client.post(f"/api/project-tasks/{task_id}/pause", headers=auth, json=control)
    ).status_code == 200
    release.set()
    old_turn = await parked(server, turn_id)
    configuration = json.loads(old_turn.configuration_json)
    assert configuration["action_timeline_enabled"] is False
    assert configuration["execution_options"] == {}
    save_env_file(server.paths.root / "env", {ACTION_TIMELINE_ENV: "true"})
    repeat = await client.post("/api/project-tasks", headers=auth, json=body)
    assert repeat.status_code == 202
    assert repeat.json()["task_id"] == task_id
    assert repeat.json()["turns"][0]["options"]["action_timeline_enabled"] is False
    resumed = await client.post(
        f"/api/project-tasks/{task_id}/resume",
        headers=auth,
        json={"request_id": "settings-resume-request", "turn_id": turn_id},
    )
    assert resumed.status_code == 200
    await settled(client, auth, task_id)
    assert len(seen) == 2
    assert seen[0].configuration_json == seen[1].configuration_json
    response = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={"request_id": "settings-followup-request", "prompt": "New turn"},
    )
    assert response.status_code == 202
    assert response.json()["turns"][-1]["options"]["action_timeline_enabled"] is True
    await settled(client, auth, task_id)
    response = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={
            "request_id": "settings-override-request",
            "prompt": "Explicit override",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["turns"][-1]["options"]["action_timeline_enabled"] is False
    done = await settled(client, auth, task_id)
    final = server.services.project_task_turn_repo.get(done["turns"][-1]["turn_id"])
    raw = json.loads(final.configuration_json)
    assert raw["action_timeline_enabled"] is False
    assert raw["execution_options"] == {"action_timeline_enabled": False}
    legacy = json.loads(old_turn.configuration_json)
    legacy.pop("action_timeline_enabled")
    old_view = turn_view(replace(old_turn, configuration_json=json.dumps(legacy)))
    assert old_view.options.action_timeline_enabled is True
    assert json.loads(old_turn.configuration_json) == configuration
