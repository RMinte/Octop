"""Project schedules have optional experts and retain owner isolation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from octop.api.app import build_app
from tests.support.auth import create_user


async def test_project_schedule_creates_project_task_without_expert_or_model(env):
    client, server, auth = env
    agents_before = server.services.agent_repo.list_all()
    project = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Daily review"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    created = await client.post(
        "/api/cron",
        headers=auth,
        json={
            "name": "Project reminder",
            "trigger": "interval:3600",
            "prompt": "Review the project",
            "project_id": project_id,
            "task_type": "text",
            "enabled": False,
        },
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["project_id"] == project_id
    assert row["agent_id"] is None and row["selected_expert_id"] is None
    assert row["conversation_policy"] == "per_job"
    cron_id = row["id"]
    assert (await client.post(f"/api/cron/{cron_id}/run-now", headers=auth)).status_code == 204
    async with asyncio.timeout(5):
        while (await client.get(f"/api/cron/{cron_id}", headers=auth)).json()[
            "last_status"
        ] == "queued":
            await asyncio.sleep(0.01)
    tasks = (await client.get(f"/api/projects/{project_id}/tasks", headers=auth)).json()["items"]
    assert len(tasks) == 1
    assert tasks[0]["project_id"] == project_id and tasks[0]["selected_expert_id"] is None
    detail = (await client.get(f"/api/project-tasks/{tasks[0]['task_id']}", headers=auth)).json()
    assert detail["turns"][-1]["reply"] == "Review the project"
    assert server.services.agent_repo.list_all() == agents_before
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0


async def test_project_schedule_filter_and_owner_isolation(env):
    client, _server, auth = env
    other = await create_user(client, auth, username="project-cron-other")
    owned_ids = []
    projects = []
    for name in ("First project", "Second project"):
        project = await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": name},
        )
        assert project.status_code == 201, project.text
        projects.append(project.json()["project_id"])
    for project_id in (*projects, None):
        created = await client.post(
            "/api/cron",
            headers=auth,
            json={
                "trigger": "interval:3600",
                "prompt": "Owned reminder",
                "project_id": project_id,
                "enabled": False,
            },
        )
        assert created.status_code == 201, created.text
        owned_ids.append(created.json()["id"])
    other_created = await client.post(
        "/api/cron",
        headers=other,
        json={"trigger": "interval:3600", "prompt": "Other reminder", "enabled": False},
    )
    assert other_created.status_code == 201, other_created.text
    assert {row["id"] for row in (await client.get("/api/cron", headers=auth)).json()} == set(
        owned_ids
    )
    filtered = await client.get("/api/cron", headers=auth, params={"project_id": projects[0]})
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()] == owned_ids[:1]
    assert (
        await client.get("/api/cron", headers=other, params={"project_id": projects[0]})
    ).json() == []
    assert (
        await client.get(f"/api/cron/{other_created.json()['id']}", headers=auth)
    ).status_code == 404


async def test_foreign_project_cannot_be_bound_on_schedule_create_or_patch(env):
    client, _server, auth = env
    other = await create_user(client, auth, username="project-cron-foreign")
    project = await client.post(
        "/api/projects",
        headers={**other, "Idempotency-Key": str(uuid4())},
        json={"name": "Private project"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    body = {"trigger": "interval:3600", "prompt": "Private reminder", "enabled": False}
    denied = await client.post("/api/cron", headers=auth, json={**body, "project_id": project_id})
    assert denied.status_code == 404, denied.text
    created = await client.post("/api/cron", headers=auth, json=body)
    assert created.status_code == 201, created.text
    cron_id = created.json()["id"]
    denied = await client.patch(
        f"/api/cron/{cron_id}", headers=auth, json={"project_id": project_id}
    )
    assert denied.status_code == 404, denied.text
    assert (await client.get(f"/api/cron/{cron_id}", headers=auth)).json()["project_id"] is None


@pytest.mark.parametrize("bind_at_create", [False, True])
async def test_binding_legacy_schedule_to_project_survives_original_expert_deletion(
    env_with_agent,
    bind_at_create,
):
    client, server, auth, agent_id = env_with_agent
    project = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Independent schedule"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    created = await client.post(
        f"/api/agents/{agent_id}/cron",
        headers=auth,
        json={
            "trigger": "interval:3600",
            "prompt": "Legacy reminder",
            "enabled": False,
            **({"project_id": project_id} if bind_at_create else {}),
        },
    )
    assert created.status_code == 201, created.text
    cron_id = created.json()["id"]
    if bind_at_create:
        row = created.json()
    else:
        assert created.json()["agent_id"] == agent_id
        changed = await client.patch(
            f"/api/cron/{cron_id}", headers=auth, json={"project_id": project_id}
        )
        assert changed.status_code == 200, changed.text
        row = changed.json()
    assert row["agent_id"] is None
    assert row["project_id"] == project_id
    assert row["selected_expert_id"] == agent_id
    assert row["session_key"] == f"cron:{cron_id}"
    # Exercise the real FK actions without the agent API's historical-reference guard.
    server.services.agent_repo.delete(agent_id)
    retained = await client.get(f"/api/cron/{cron_id}", headers=auth)
    assert retained.status_code == 200, retained.text
    assert retained.json()["project_id"] == project_id
    assert retained.json()["agent_id"] is None
    assert retained.json()["selected_expert_id"] is None


async def test_project_schedule_openapi_has_typed_binding_and_project_filter(env, monkeypatch):
    _client, server, _auth = env
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, enable_api_docs=True)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(server)), base_url="http://testserver"
    ) as docs_client:
        docs = await docs_client.get("/api/docs")
        assert docs.status_code == 200 and "/api/openapi.json" in docs.text
        response = await docs_client.get("/api/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    path = schema["paths"]["/api/cron"]
    assert path["post"]["summary"] and path["get"]["summary"]
    assert any(p["name"] == "project_id" and p["in"] == "query" for p in path["get"]["parameters"])
    for name in ("TaskCronCreateBody", "CronPatchBody", "CronResponse"):
        dto = schema["components"]["schemas"][name]
        assert {"project_id", "selected_expert_id"} <= dto["properties"].keys()
        assert "selected_expert_id" not in dto.get("required", [])
    assert path["post"]["responses"]["201"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("CronResponse")
