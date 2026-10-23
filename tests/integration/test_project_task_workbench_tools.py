"""Recorded skills and tool UIs operate on a real Task workspace and journal."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from octop_browser.record.models import RecordingManifest, StepsDocument
from octop_browser.record.settings import RecordReplaySettings
from octop_browser.record.store import RecordingStore

from octop.infra.projects import workbench, workbench_browser
from tests.integration.test_project_tasks import settled, task_env  # noqa: F401
from tests.support.auth import resolve_user_id


@pytest.fixture
async def workspace_task(task_env, tmp_path):  # noqa: F811
    client, server, auth, _calls = task_env
    directory = tmp_path / "project"
    directory.mkdir()
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Workbench", "directory": str(directory)},
        )
    ).json()
    created = await client.post(
        f"/api/projects/{project['project_id']}/tasks",
        headers=auth,
        json={"prompt": "Work", "request_id": str(uuid4())},
    )
    assert created.status_code == 202, created.text
    task = await settled(client, auth, created.json()["task_id"])
    owner_id = await resolve_user_id(client, auth, "admin")
    scope = workbench.resolve(server.services, owner_id, task["task_id"])
    return client, server, auth, task, scope


@pytest.fixture
def recording_store(tmp_path, monkeypatch):
    import octop_browser.record.store as module

    store = RecordingStore(RecordReplaySettings(base_dir=tmp_path / "recordings"))
    monkeypatch.setattr(module, "RecordingStore", lambda: store)
    return store


async def test_recording_draft_recovery_explicit_apply_and_next_turn_skill(
    workspace_task, recording_store, monkeypatch
):
    client, _server, auth, task, scope = workspace_task
    store = recording_store
    store.create(
        RecordingManifest(
            recordingId="owned", createdAt="2026-10-23T00:00:00Z", profile=scope.browser_profile
        )
    )
    store.write_steps("owned", StepsDocument(recordingId="owned", steps=[]))
    commands = []

    async def fake_request(body):
        commands.append(body)
        assert body["command"] in {"stop", "status"}
        return {"ok": True, "recordingId": "owned", "active": None}

    monkeypatch.setattr(workbench_browser, "record_request", fake_request)
    base = f"/api/project-tasks/{task['task_id']}"
    stopped = await client.post(
        base + "/browser/record-replay/stop-and-generate-skill",
        headers=auth,
        json={
            "recordingId": "owned",
            "name": "workflow",
            "expected_scope_revision": scope.view.scope_revision,
        },
    )
    assert stopped.status_code == 200, stopped.text
    draft = stopped.json()["skillContent"]
    assert draft and not (scope.runtime_dir / "workbench/skills.json").exists()
    recovered = await client.get(
        base + "/browser/record-replay/skill-content", headers=auth, params={"recordingId": "owned"}
    )
    assert recovered.json()["skillContent"] == draft
    body = {
        "request_id": "apply-recording",
        "name": "workflow",
        "content": draft,
        "expected_scope_revision": scope.view.scope_revision,
    }
    first, repeated = await asyncio.gather(
        *[client.post(base + "/recordings/owned/apply", headers=auth, json=body) for _ in range(2)]
    )
    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    assert await workbench_browser.task_skill_files(scope) == [
        ("workflow", [("SKILL.md", draft.encode())])
    ]
    moved = replace(
        scope, view=scope.view.model_copy(update={"server_directory": "another-directory"})
    )
    assert await workbench_browser.task_skill_files(moved) == []
    conflict = await client.post(
        base + "/recordings/owned/apply", headers=auth, json={**body, "content": "different"}
    )
    assert conflict.status_code == 409
    assert commands == [
        {"command": "stop", "recording_id": "owned", "generate_steps": True, "name": "workflow"}
    ]


async def test_recording_other_task_is_unreadable_and_cannot_be_applied(
    workspace_task, recording_store
):
    client, _server, auth, task, scope = workspace_task
    recording_store.create(
        RecordingManifest(
            recordingId="foreign",
            createdAt="2026-10-23T00:00:00Z",
            profile=workbench_browser.profile_prefix("other-task") + "scope",
        )
    )
    base = f"/api/project-tasks/{task['task_id']}"
    assert (
        await client.get(
            base + "/browser/record-replay/skill-content",
            headers=auth,
            params={"recordingId": "foreign"},
        )
    ).status_code == 404
    applied = await client.post(
        base + "/recordings/foreign/apply",
        headers=auth,
        json={
            "request_id": "foreign-request",
            "name": "foreign",
            "content": "private",
            "expected_scope_revision": scope.view.scope_revision,
        },
    )
    assert applied.status_code == 404
    assert await workbench_browser.task_skill_files(scope) == []


async def test_recording_start_boots_daemon_and_uses_task_profile_with_strict_privacy(
    workspace_task, monkeypatch
):
    client, _server, auth, task, scope = workspace_task
    ensure = AsyncMock(return_value={"ok": True})
    browser = AsyncMock()
    send = AsyncMock(return_value={"ok": True, "recordingId": "new"})
    monkeypatch.setattr("octop_browser.record.daemon.ensure_daemon", ensure)
    monkeypatch.setattr(workbench_browser, "browser", browser)
    monkeypatch.setattr(workbench_browser, "record_request", send)
    result = await client.post(
        f"/api/project-tasks/{task['task_id']}/browser/record-replay/start",
        headers=auth,
        json={
            "name": "Workflow",
            "privacy": "strict",
            "expected_scope_revision": scope.view.scope_revision,
        },
    )
    assert result.status_code == 200, result.text
    ensure.assert_awaited_once_with()
    browser.assert_awaited_once()
    send.assert_awaited_once_with(
        {
            "command": "start",
            "profile": scope.browser_profile,
            "name": "Workflow",
            "privacy": "mask-all",
            "screenshots": "off",
        }
    )


async def test_recording_status_hides_other_task_active_and_latest(
    workspace_task, recording_store, monkeypatch
):
    client, _server, auth, task, scope = workspace_task
    recording_store.create(
        RecordingManifest(
            recordingId="mine", createdAt="2026-10-22T00:00:00Z", profile=scope.browser_profile
        )
    )
    recording_store.create(
        RecordingManifest(
            recordingId="foreign",
            createdAt="2026-10-23T00:00:00Z",
            profile=workbench_browser.profile_prefix("another-task") + "scope",
        )
    )
    monkeypatch.setattr(
        workbench_browser,
        "record_request",
        AsyncMock(
            return_value={
                "ok": True,
                "active": {"profile": "private-profile", "recordingId": "private"},
            }
        ),
    )
    result = await client.get(
        f"/api/project-tasks/{task['task_id']}/browser/record-replay/status", headers=auth
    )
    assert result.json() == {"ok": True, "active": None, "latestRecordingId": "mine"}


async def test_tool_ui_requires_own_call_and_current_configuration(workspace_task):
    client, server, auth, task, scope = workspace_task
    turn = task["turns"][0]
    turn_id = turn["turn_id"]
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET content_blocks_json=? WHERE turn_id=?",
            (
                json.dumps(
                    [
                        {
                            "block_id": "tool-1",
                            "type": "tool",
                            "status": "completed",
                            "tool_call_id": "my-call",
                        }
                    ]
                ),
                turn_id,
            ),
        )
    base = f"/api/project-tasks/{task['task_id']}"
    endpoint = base + f"/tool-ui/{turn_id}/my-call/request"
    body = {
        "expected_scope_revision": scope.view.scope_revision,
        "path": "/workspace/file",
        "method": "PUT",
        "body": {"path": "result.txt", "content": "saved"},
    }
    assert (await client.post(endpoint, headers=auth, json=body)).status_code == 200
    assert (
        await client.get(base + "/workspace/file", headers=auth, params={"path": "result.txt"})
    ).json()["content"] == "saved"
    for path in (
        "/agents/private/workspace/file",
        "/project-tasks/foreign/workspace/file",
        "https://example.org/workspace/file",
    ):
        assert (
            await client.post(endpoint, headers=auth, json={**body, "path": path})
        ).status_code == 403
    assert (
        await client.post(endpoint.replace("my-call", "other-call"), headers=auth, json=body)
    ).status_code == 404
    changed = await client.patch(
        base,
        headers=auth,
        json={
            "action_mode": "ask",
            "expected_revision": task["list_revision"],
            "expected_configuration_revision": task["configuration_revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    fresh = (await client.get(base + "/workbench", headers=auth)).json()
    assert (
        await client.post(
            endpoint,
            headers=auth,
            json={**body, "expected_scope_revision": fresh["scope_revision"]},
        )
    ).status_code == 409
    assert (await workbench.read(scope, "result.txt")).content == "saved"
