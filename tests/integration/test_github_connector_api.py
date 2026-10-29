"""GitHub connections reuse credential isolation and project/Task selection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

from octop.infra.connectors import probe
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks import task_env as task_env
from tests.support.auth import create_user


async def test_github_catalog_and_missing_pat(env):
    client, _, auth = env
    catalog = await client.get("/api/connectors/catalog", headers=auth)
    assert catalog.status_code == 200, catalog.text
    entry = next(item for item in catalog.json() if item["kind"] == "github")
    assert entry["phase"] == "available"
    assert entry["auth_kind"] == "personal_token"
    assert entry["mcp_mode"] == "remote"
    assert entry["oauth_mode"] is None
    assert entry["oauth_ready"] is False

    rejected = await client.post(
        "/api/connector-instances",
        headers=auth,
        json={"kind": "github", "display_name": "GitHub", "credentials": {}},
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "CONNECTOR_INVALID_CREDENTIALS"
    assert (await client.get("/api/connector-instances", headers=auth)).json() == []


async def test_github_crud_redacts_pat_and_keeps_accounts_isolated(env):
    client, server, auth = env
    other = await create_user(client, auth, username="github-other")
    created = await client.post(
        "/api/connector-instances",
        headers=auth,
        json={
            "kind": "github",
            "display_name": "GitHub",
            "credentials": {"token": "github-test-pat-owner"},
        },
    )
    assert created.status_code == 201, created.text
    instance = created.json()
    base = f"/api/connector-instances/{instance['instance_id']}"
    assert instance["mcp_server_name"] == f"github__{instance['instance_id']}"
    assert "github-test-pat-owner" not in created.text
    stored = server.services.connector_repo.get(instance["instance_id"])
    assert stored is not None and stored.credential_blob
    assert b"github-test-pat-owner" not in stored.credential_blob

    detail = await client.get(base, headers=auth)
    assert detail.status_code == 200, detail.text
    assert detail.json()["credentials_preview"] == {"token_configured": True}
    assert "github-test-pat-owner" not in detail.text
    listed = await client.get("/api/connector-instances", headers=other)
    assert listed.json() == []
    assert (await client.get(base, headers=other)).status_code == 403
    assert (await client.post(base + "/test", headers=other)).status_code == 403
    assert (await client.patch(base, headers=other, json={"status": "disabled"})).status_code == 403
    assert (await client.delete(base, headers=other)).status_code == 403

    # The same display name belongs to each account's independent connection.
    second = await client.post(
        "/api/connector-instances",
        headers=other,
        json={
            "kind": "github",
            "display_name": "GitHub",
            "credentials": {"token": "github-test-pat-other"},
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["mcp_server_name"] != instance["mcp_server_name"]
    patched = await client.patch(
        base,
        headers=auth,
        json={
            "display_name": "Work GitHub",
            "credentials": {"token": "github-test-pat-updated"},
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "Work GitHub"
    assert "github-test-pat-updated" not in patched.text
    assert (await client.delete(base, headers=auth)).status_code == 204
    assert (await client.get(base, headers=auth)).status_code == 404
    remaining = (await client.get("/api/connector-instances", headers=other)).json()
    assert [item["instance_id"] for item in remaining] == [second.json()["instance_id"]]


async def test_github_saved_and_unsaved_probes_use_the_pat_without_exposing_it(env, monkeypatch):
    client, _, auth = env
    expected = {
        "ok": True,
        "tool_count": 1,
        "tools": [{"name": "get_me", "description": "Get the authenticated user"}],
    }
    streamable = AsyncMock(return_value=expected)
    monkeypatch.setattr(probe, "probe_streamable_http_mcp", streamable)
    body = {
        "kind": "github",
        "display_name": "Probe GitHub",
        "credentials": {"token": "github-test-pat"},
    }
    unsaved = await client.post(
        "/api/connectors/test-credentials",
        headers=auth,
        json={"kind": body["kind"], "credentials": body["credentials"]},
    )
    assert unsaved.status_code == 200 and unsaved.json() == expected
    created = await client.post("/api/connector-instances", headers=auth, json=body)
    assert created.status_code == 201, created.text
    saved = await client.post(
        f"/api/connector-instances/{created.json()['instance_id']}/test", headers=auth
    )
    assert saved.status_code == 200 and saved.json() == expected
    assert streamable.await_count == 2
    for call in streamable.await_args_list:
        args, kwargs = call
        assert args[0] == "https://api.githubcopilot.com/mcp/"
        assert args[1]["Authorization"] == "Bearer github-test-pat"
        assert kwargs == {"kind": "github"}
    assert "github-test-pat" not in unsaved.text + saved.text


async def test_github_project_and_task_resources_reuse_the_connection_reference(task_env):
    client, _, auth, calls = task_env
    other = await create_user(client, auth, username="github-resource-other")
    created = await client.post(
        "/api/connector-instances",
        headers=auth,
        json={
            "kind": "github",
            "display_name": "Work GitHub",
            "credentials": {"token": "github-test-pat-resource"},
        },
    )
    assert created.status_code == 201, created.text
    server_name = created.json()["mcp_server_name"]
    for path in ("/api/projects/resources", "/api/project-tasks/resources"):
        response = await client.get(path, headers=auth)
        assert response.status_code == 200, response.text
        assert next(
            item for item in response.json()["mcp_servers"] if item["server_name"] == server_name
        ) == {"server_name": server_name, "name": "Work GitHub", "state": "configured"}
        assert "github-test-pat-resource" not in response.text
        foreign = await client.get(path, headers=other)
        assert server_name not in {item["server_name"] for item in foreign.json()["mcp_servers"]}

    project_response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "GitHub project", "mcp_servers": [server_name]},
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "github-inherited-turn",
            "project_id": project["project_id"],
            "prompt": "Review my GitHub issues",
        },
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    assert task["turns"][0]["options"]["mcp_servers"] == [server_name]
    inherited = json.loads(calls[0][0].configuration_json)
    assert inherited["mcp_servers"] == [server_name]
    assert "github-test-pat-resource" not in json.dumps(inherited)

    follow_up = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={
            "request_id": "github-disabled-turn",
            "prompt": "Continue without GitHub tools",
            "options": {"mcp_servers": []},
        },
    )
    assert follow_up.status_code == 202, follow_up.text
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][-1]["options"]["mcp_servers"] == []
    assert json.loads(calls[-1][0].configuration_json)["mcp_servers"] == []
    unchanged = await client.get(f"/api/projects/{project['project_id']}", headers=auth)
    assert unchanged.json()["mcp_servers"] == [server_name]
