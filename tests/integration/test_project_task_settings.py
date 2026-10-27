"""Global task defaults, admission snapshots, and safe environment writes."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from octop.infra.projects import runtime
from octop.infra.projects.settings import ACTION_TIMELINE_ENV, STEP_PANEL_ENV
from octop.infra.projects.tasks import turn_view
from octop.infra.utils.env_file import load_env_file, save_env_file
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
    schemas = client._octop_app.openapi()["components"]["schemas"]
    assert schemas["TaskTurn"]["properties"]["presentation"]["$ref"].endswith("/TaskPresentation")
    assert set(schemas["TaskPresentation"]["properties"]) == {
        "action_timeline_enabled",
        "step_panel_enabled",
    }


async def test_saving_settings_requires_env_permission_and_preserves_unrelated_values(
    env, monkeypatch
):
    client, server, admin = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "true")
    monkeypatch.setenv(STEP_PANEL_ENV, "true")
    monkeypatch.setenv("TAVILY_API_KEY", "process-secret")
    path = server.paths.root / "env"
    other_values = {"TAVILY_API_KEY": "persisted-secret", "OCTOP_CAPTCHA_SECRET": "captcha-secret"}
    save_env_file(path, {**other_values, ACTION_TIMELINE_ENV: "true"})
    before = path.read_bytes()
    body = {"action_timeline_enabled": False, "step_panel_enabled": True}
    reader = await create_user(client, admin, username="task-settings-readonly", permissions=[])
    assert (await client.put("/api/project-tasks/settings", json=body)).status_code == 401
    assert (
        await client.put("/api/project-tasks/settings", headers=reader, json=body)
    ).status_code == 403
    assert path.read_bytes() == before
    editor = await create_user(client, admin, username="task-settings-editor", permissions=["envs"])
    assert server.app_runtime is not None
    reload_all = AsyncMock()
    monkeypatch.setattr(server.app_runtime.agent_registry, "reload_all", reload_all)
    response = await client.put("/api/project-tasks/settings", headers=editor, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == body
    assert load_env_file(path) == {
        **other_values,
        ACTION_TIMELINE_ENV: "false",
        STEP_PANEL_ENV: "true",
    }
    assert os.environ[ACTION_TIMELINE_ENV] == "false"
    assert os.environ[STEP_PANEL_ENV] == "true"
    assert os.environ["TAVILY_API_KEY"] == "process-secret"
    reload_all.assert_not_called()
    assert (await client.get("/api/project-tasks/settings", headers=reader)).json() == body
    operation = client._octop_app.openapi()["paths"]["/api/project-tasks/settings"]["put"]
    assert operation["summary"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskSettingsUpdate"
    )
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskSettings"
    )
    schema = client._octop_app.openapi()["components"]["schemas"]["TaskSettingsUpdate"]
    assert set(schema["required"]) == set(body)
    assert all(field["type"] == "boolean" for field in schema["properties"].values())


async def test_settings_update_requires_both_strict_booleans_before_any_write(env, monkeypatch):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "true")
    monkeypatch.setenv(STEP_PANEL_ENV, "false")
    path = server.paths.root / "env"
    save_env_file(path, {ACTION_TIMELINE_ENV: "true", STEP_PANEL_ENV: "false"})
    before = path.read_bytes()
    valid = {"action_timeline_enabled": True, "step_panel_enabled": False}
    bodies = [
        {},
        {"action_timeline_enabled": True},
        {"step_panel_enabled": False},
        {**valid, "unrelated": True},
        *[{**valid, key: value} for key in valid for value in ["true", "false", 0, 1, None]],
    ]
    for body in bodies:
        response = await client.put("/api/project-tasks/settings", headers=auth, json=body)
        assert response.status_code == 422, response.text
        assert path.read_bytes() == before
        assert os.environ[ACTION_TIMELINE_ENV] == "true"
        assert os.environ[STEP_PANEL_ENV] == "false"


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


async def test_env_save_preserves_latest_task_settings_and_replaces_other_variables(
    env, monkeypatch
):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "true")
    monkeypatch.setenv(STEP_PANEL_ENV, "true")
    monkeypatch.setenv("OTHER", "before")
    monkeypatch.setenv("REMOVED", "before")
    path = server.paths.root / "env"
    save_env_file(
        path,
        {
            ACTION_TIMELINE_ENV: "true",
            STEP_PANEL_ENV: "true",
            "OTHER": "before",
            "REMOVED": "before",
        },
    )
    assert (await client.get("/api/envs", headers=auth)).status_code == 200
    latest = {"action_timeline_enabled": False, "step_panel_enabled": False}
    assert (
        await client.put("/api/project-tasks/settings", headers=auth, json=latest)
    ).status_code == 200
    # The environment form submits ordinary rows without its earlier task-setting snapshot.
    response = await client.put("/api/envs", headers=auth, json={"OTHER": "after"})
    assert response.status_code == 200
    assert load_env_file(path) == {
        ACTION_TIMELINE_ENV: "false",
        STEP_PANEL_ENV: "false",
        "OTHER": "after",
    }
    assert os.environ[ACTION_TIMELINE_ENV] == "false"
    assert os.environ[STEP_PANEL_ENV] == "false"
    assert os.environ["OTHER"] == "after"
    assert "REMOVED" not in os.environ
    assert (await client.get("/api/project-tasks/settings", headers=auth)).json() == latest


@pytest.mark.parametrize("env_file_exists", [False, True])
async def test_env_save_uses_process_task_defaults_only_before_the_file_exists(
    env, monkeypatch, env_file_exists
):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "false")
    monkeypatch.setenv(STEP_PANEL_ENV, "false")
    monkeypatch.setenv("OTHER", "before")
    path = server.paths.root / "env"
    if env_file_exists:
        save_env_file(path, {ACTION_TIMELINE_ENV: "false"})
    else:
        assert not path.exists()
    response = await client.put("/api/envs", headers=auth, json={"OTHER": "after"})
    assert response.status_code == 200
    expected = {ACTION_TIMELINE_ENV: "false", "OTHER": "after"}
    if not env_file_exists:
        expected[STEP_PANEL_ENV] = "false"
    assert load_env_file(path) == expected
    assert (await client.get("/api/project-tasks/settings", headers=auth)).json() == {
        "action_timeline_enabled": False,
        "step_panel_enabled": env_file_exists,
    }


@pytest.mark.parametrize("key", [ACTION_TIMELINE_ENV, STEP_PANEL_ENV])
async def test_env_save_can_explicitly_change_one_task_default(env, monkeypatch, key):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "false")
    monkeypatch.setenv(STEP_PANEL_ENV, "false")
    path = server.paths.root / "env"
    save_env_file(path, {ACTION_TIMELINE_ENV: "false", STEP_PANEL_ENV: "false"})
    response = await client.put("/api/envs", headers=auth, json={key: "on"})
    assert response.status_code == 200
    expected = {ACTION_TIMELINE_ENV: "false", STEP_PANEL_ENV: "false"}
    expected[key] = "on"
    assert load_env_file(path) == expected
    assert (await client.get("/api/project-tasks/settings", headers=auth)).json() == {
        "action_timeline_enabled": key == ACTION_TIMELINE_ENV,
        "step_panel_enabled": key == STEP_PANEL_ENV,
    }


@pytest.mark.parametrize("env_file_exists", [False, True])
@pytest.mark.parametrize("key", [ACTION_TIMELINE_ENV, STEP_PANEL_ENV, "OTHER"])
async def test_env_delete_preserves_other_task_defaults(env, monkeypatch, env_file_exists, key):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "false")
    monkeypatch.setenv(STEP_PANEL_ENV, "false")
    path = server.paths.root / "env"
    if env_file_exists:
        save_env_file(path, {ACTION_TIMELINE_ENV: "false", STEP_PANEL_ENV: "false"})
    else:
        assert not path.exists()
    response = await client.delete("/api/envs/" + key, headers=auth)
    assert response.status_code == 200
    expected = {ACTION_TIMELINE_ENV: "false", STEP_PANEL_ENV: "false"}
    expected.pop(key, None)
    assert load_env_file(path) == expected
    if key in {ACTION_TIMELINE_ENV, STEP_PANEL_ENV}:
        assert key not in os.environ
    assert (await client.get("/api/project-tasks/settings", headers=auth)).json() == {
        "action_timeline_enabled": key == ACTION_TIMELINE_ENV,
        "step_panel_enabled": key == STEP_PANEL_ENV,
    }


async def test_saved_and_deleted_keys_are_visible_despite_a_stale_worker_environment(
    env, monkeypatch
):
    client, server, auth = env
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "true")
    monkeypatch.setenv(STEP_PANEL_ENV, "true")
    save_env_file(server.paths.root / "env", {ACTION_TIMELINE_ENV: "true", STEP_PANEL_ENV: "true"})
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
    monkeypatch.setenv(ACTION_TIMELINE_ENV, "false")
    monkeypatch.setenv(STEP_PANEL_ENV, "false")
    save_env_file(
        server.paths.root / "env", {ACTION_TIMELINE_ENV: "false", STEP_PANEL_ENV: "false"}
    )
    original_presentation = {"action_timeline_enabled": False, "step_panel_enabled": False}
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
    assert task["turns"][0]["presentation"] == original_presentation
    await asyncio.wait_for(entered.wait(), 3)
    control = {"request_id": "settings-pause-request", "turn_id": turn_id}
    assert (
        await client.post(f"/api/project-tasks/{task_id}/pause", headers=auth, json=control)
    ).status_code == 200
    release.set()
    old_turn = await parked(server, turn_id)
    configuration = json.loads(old_turn.configuration_json)
    assert configuration["action_timeline_enabled"] is False
    assert configuration["presentation"] == original_presentation
    assert configuration["execution_options"] == {}
    updated = await client.put(
        "/api/project-tasks/settings",
        headers=auth,
        json={"action_timeline_enabled": True, "step_panel_enabled": True},
    )
    assert updated.status_code == 200, updated.text
    repeat = await client.post("/api/project-tasks", headers=auth, json=body)
    assert repeat.status_code == 202
    assert repeat.json()["task_id"] == task_id
    assert repeat.json()["turns"][0]["options"]["action_timeline_enabled"] is False
    assert repeat.json()["turns"][0]["presentation"] == original_presentation
    resumed = await client.post(
        f"/api/project-tasks/{task_id}/resume",
        headers=auth,
        json={"request_id": "settings-resume-request", "turn_id": turn_id},
    )
    assert resumed.status_code == 200
    resumed_detail = await settled(client, auth, task_id)
    assert resumed_detail["turns"][0]["presentation"] == original_presentation
    assert len(seen) == 2
    assert seen[0].configuration_json == seen[1].configuration_json
    response = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={"request_id": "settings-followup-request", "prompt": "New turn"},
    )
    assert response.status_code == 202
    assert response.json()["turns"][-1]["options"]["action_timeline_enabled"] is True
    assert response.json()["turns"][-1]["presentation"] == {
        "action_timeline_enabled": True,
        "step_panel_enabled": True,
    }
    assert response.json()["turns"][0]["presentation"] == original_presentation
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
    assert raw["presentation"] == {
        "action_timeline_enabled": False,
        "step_panel_enabled": True,
    }
    assert raw["execution_options"] == {"action_timeline_enabled": False}
    legacy = json.loads(old_turn.configuration_json)
    legacy.pop("action_timeline_enabled")
    legacy.pop("presentation")
    old_view = turn_view(replace(old_turn, configuration_json=json.dumps(legacy)))
    assert old_view.options.action_timeline_enabled is True
    assert old_view.presentation.model_dump() == {
        "action_timeline_enabled": True,
        "step_panel_enabled": True,
    }
    assert json.loads(old_turn.configuration_json) == configuration


@pytest.mark.parametrize(
    "actions,steps", [(True, True), (True, False), (False, True), (False, False)]
)
async def test_new_turns_freeze_both_defaults_and_history_and_ws_keep_them(
    env_with_provider, monkeypatch, actions, steps
):
    from tests.integration.test_project_tasks_ws import connected, until

    client, server, auth = env_with_provider

    async def fake_run(*args):
        yield {"type": "token", "content": "Completed"}

    monkeypatch.setattr(runtime, "run_turn", fake_run)
    path = server.paths.root / "env"
    save_env_file(path, {ACTION_TIMELINE_ENV: str(actions), STEP_PANEL_ENV: str(steps)})
    body = {"request_id": "presentation-snapshot-first", "prompt": "First turn"}
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    first = (await settled(client, auth, task_id))["turns"][0]
    expected = {"action_timeline_enabled": actions, "step_panel_enabled": steps}
    assert first["presentation"] == expected
    saved = server.services.project_task_turn_repo.get(first["turn_id"])
    assert json.loads(saved.configuration_json)["presentation"] == expected

    save_env_file(path, {ACTION_TIMELINE_ENV: str(not actions), STEP_PANEL_ENV: str(not steps)})
    replay = await client.post("/api/project-tasks", headers=auth, json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["turns"][0]["presentation"] == expected
    response = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={"request_id": "presentation-snapshot-followup", "prompt": "Follow-up"},
    )
    assert response.status_code == 202, response.text
    detail = await settled(client, auth, task_id)
    assert [turn["presentation"] for turn in detail["turns"]] == [
        expected,
        {"action_timeline_enabled": not actions, "step_panel_enabled": not steps},
    ]
    # Both REST history and WebSocket catch-up expose the persisted per-turn choice.
    session, ws = await connected(client, auth)
    try:
        await ws.send_json({"type": "task.subscribe", "id": "history-view", "task_id": task_id})
        snapshot = await until(ws, lambda frame: frame["type"] == "snapshot")
        assert [turn["presentation"] for turn in snapshot["data"]["turns"]] == [
            turn["presentation"] for turn in detail["turns"]
        ]
    finally:
        await session.__aexit__(None, None, None)
    assert server.services.project_task_turn_repo.get(first["turn_id"]).configuration_json == (
        saved.configuration_json
    )


async def test_manual_retry_freezes_new_defaults_without_changing_the_failed_turn(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider

    async def fake_run(services, owner, turn, history):
        yield {"type": "token", "content": "Saved result"}
        if turn.request_id == "presentation-original-failure":
            raise RuntimeError("Retry fixture failure")

    monkeypatch.setattr(runtime, "run_turn", fake_run)
    path = server.paths.root / "env"
    save_env_file(path, {ACTION_TIMELINE_ENV: "false", STEP_PANEL_ENV: "false"})
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "presentation-original-failure", "prompt": "Retry this task"},
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    original = (await settled(client, auth, task_id))["turns"][0]
    assert original["status"] == "failed"
    assert original["presentation"] == {
        "action_timeline_enabled": False,
        "step_panel_enabled": False,
    }
    save_env_file(path, {ACTION_TIMELINE_ENV: "true", STEP_PANEL_ENV: "true"})
    url = f"/api/project-tasks/{task_id}/turns/{original['turn_id']}/retry"
    body = {"request_id": "presentation-manual-retry"}
    response = await client.post(url, headers=auth, json=body)
    assert response.status_code == 202, response.text
    retried = await settled(client, auth, task_id)
    assert retried["turns"][0] == original
    expected = {"action_timeline_enabled": True, "step_panel_enabled": True}
    assert retried["turns"][1]["presentation"] == expected
    save_env_file(path, {ACTION_TIMELINE_ENV: "false", STEP_PANEL_ENV: "false"})
    repeated = await client.post(url, headers=auth, json=body)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["turns"][1]["presentation"] == expected
