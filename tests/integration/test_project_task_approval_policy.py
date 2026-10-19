"""Task approval choices are owner-scoped, versioned and durable across retries."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from octop.infra.projects import approval_policy, runtime
from octop.infra.projects.approval_models import APPROVAL_MODES, TaskApprovalSettings
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected
from tests.support.auth import create_user


@pytest.fixture
async def policy_env(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    calls = []

    async def run(services, owner, turn, history):
        calls.append(turn)
        yield {"type": "token", "content": "Done"}

    monkeypatch.setattr(runtime, "run_turn", run)
    return client, server, auth, calls


def test_approval_settings_only_configure_mandatory_human_tools():
    assert TaskApprovalSettings().model_dump() == {"require_human_tools": []}
    with pytest.raises(ValidationError):
        TaskApprovalSettings.model_validate({"allowed_modes": ["ask_for_approval"]})
    with pytest.raises(ValidationError):
        TaskApprovalSettings(require_human_tools=["execute", "execute"])


@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_all_modes_are_available_without_configuration(policy_env, mode):
    client, server, auth, _ = policy_env
    resource = (await client.get("/api/project-tasks/resources", headers=auth)).json()
    assert resource["approval"] == {
        "allowed_modes": list(APPROVAL_MODES),
        "reviewer_ready": True,
        "execution_profile": "host_process",
    }
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "auto-review-mode",
            "prompt": "First",
            "approval_mode": mode,
        },
    )
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["approval_policy"]["mode"] == mode
    assert data["turns"][0]["approval_policy_at_start"] == data["approval_policy"]
    assert not approval_policy.capabilities(server.services, "missing/model").reviewer_ready


async def test_saved_mode_limits_are_ignored_while_human_tools_are_preserved(policy_env):
    client, server, auth, _ = policy_env
    stored = json.dumps({"allowed_modes": ["ask_for_approval"], "require_human_tools": ["execute"]})
    server.services.settings_repo.set(approval_policy.SETTINGS_KEY, stored)
    resource = (await client.get("/api/project-tasks/resources", headers=auth)).json()
    assert resource["approval"]["allowed_modes"] == list(APPROVAL_MODES)
    settings = await client.get("/api/admin/security/task-approvals", headers=auth)
    assert settings.json() == {"require_human_tools": ["execute"]}
    assert server.services.settings_repo.get(approval_policy.SETTINGS_KEY) == stored
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "saved-mode-limits", "prompt": "First", "approval_mode": "full_access"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["approval_policy"]["mode"] == "full_access"


async def test_reviewer_readiness_does_not_block_mode_selection(policy_env, monkeypatch):
    from octop.infra.projects import tasks

    client, _, auth, _ = policy_env
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "before-reviewer-unavailable", "prompt": "First"},
    )
    task = await settled(client, auth, response.json()["task_id"])
    monkeypatch.setattr(tasks, "resolve_model_ref", lambda services: "missing/model")
    resource = (await client.get("/api/project-tasks/resources", headers=auth)).json()
    assert resource["approval"]["reviewer_ready"] is False
    changed = await client.put(
        f"/api/project-tasks/{task['task_id']}/approval-policy",
        headers=auth,
        json={
            "request_id": "choose-unready-reviewer",
            "expected_revision": 1,
            "mode": "approve_for_me",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["approval_policy"]["mode"] == "approve_for_me"


async def test_policy_cas_retries_turn_snapshot_and_owner_isolation(policy_env):
    client, server, auth, calls = policy_env
    first = {"request_id": "create-policy-task", "prompt": "First"}
    response = await client.post("/api/project-tasks", headers=auth, json=first)
    assert response.status_code == 202
    original = await settled(client, auth, response.json()["task_id"])
    task_id = original["task_id"]
    url = f"/api/project-tasks/{task_id}/approval-policy"
    body = {"request_id": "change-to-full", "expected_revision": 1, "mode": "full_access"}
    changed = await client.put(url, headers=auth, json=body)
    assert changed.status_code == 200, changed.text
    current = changed.json()
    assert current["approval_policy"]["revision"] == 2
    assert current["approval_policy"]["mode"] == "full_access"
    assert current["list_revision"] > original["list_revision"]
    assert current["turns"] == original["turns"]
    duplicate = await client.put(url, headers=auth, json=body)
    assert duplicate.status_code == 200 and duplicate.json() == current
    collision = await client.put(url, headers=auth, json={**body, "mode": "approve_for_me"})
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "TASK_REQUEST_CONFLICT"
    stale = await client.put(url, headers=auth, json={**body, "request_id": "stale-change-mode"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "TASK_APPROVAL_POLICY_CONFLICT"
    outsider = await create_user(client, auth, username="policy-other")
    hidden = await client.put(url, headers=outsider, json=body)
    assert hidden.status_code == 404
    repeated_create = await client.post("/api/project-tasks", headers=auth, json=first)
    assert repeated_create.status_code == 202
    altered_create = await client.post(
        "/api/project-tasks", headers=auth, json={**first, "approval_mode": "full_access"}
    )
    assert altered_create.status_code == 409
    sent = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={"request_id": "second-policy-turn", "prompt": "Again"},
    )
    assert sent.status_code == 202
    final = await settled(client, auth, task_id)
    assert final["turns"][1]["approval_policy_at_start"] == current["approval_policy"]
    assert (
        json.loads(calls[0].configuration_json)["approval_policy_at_start"]["mode"]
        == "ask_for_approval"
    )
    assert (
        json.loads(calls[1].configuration_json)["approval_policy_at_start"]["mode"] == "full_access"
    )


async def test_ws_policy_setting_uses_same_durable_contract(policy_env):
    client, _, auth, _ = policy_env
    session, ws = await connected(client, auth)
    try:
        created = await command(
            ws,
            "task.create",
            "create-via-socket",
            data={
                "request_id": "socket-policy-task",
                "prompt": "First",
            },
        )
        task_id = created["data"]["task_id"]
        body = {"request_id": "socket-switch-mode", "expected_revision": 1, "mode": "full_access"}
        result = await command(
            ws, "task.approval.set", "switch-via-socket", task_id=task_id, data=body
        )
        assert result["data"]["approval_policy"]["mode"] == "full_access"
        retry = await command(
            ws, "task.approval.set", "switch-retransmit", task_id=task_id, data=body
        )
        assert retry["data"]["approval_policy"]["revision"] == 2
    finally:
        await session.__aexit__(None, None, None)


async def test_competing_policy_writes_and_late_retry_never_restore_old_mode(policy_env):
    client, _, auth, _ = policy_env
    created = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={
                "request_id": "competing-policy-task",
                "prompt": "First",
            },
        )
    ).json()
    url = f"/api/project-tasks/{created['task_id']}/approval-policy"
    candidates = [
        {"request_id": "competing-policy-one", "expected_revision": 1, "mode": "full_access"},
        {"request_id": "competing-policy-two", "expected_revision": 1, "mode": "approve_for_me"},
    ]
    results = await asyncio.gather(
        *(client.put(url, headers=auth, json=body) for body in candidates)
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(
        body for body, result in zip(candidates, results, strict=True) if result.status_code == 200
    )
    changed = await client.put(
        url,
        headers=auth,
        json={
            "request_id": "return-to-human",
            "expected_revision": 2,
            "mode": "ask_for_approval",
        },
    )
    assert changed.status_code == 200
    late = await client.put(url, headers=auth, json=winner)
    assert late.status_code == 200
    assert late.json()["approval_policy"] == changed.json()["approval_policy"]
    assert late.json()["approval_policy"]["revision"] == 3
