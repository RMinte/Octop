"""The HTTP engine surface is typed, passive, and usable without an Octop model."""

from __future__ import annotations

import asyncio

from octop.infra.projects import runtime
from tests.support.auth import resolve_user_id
from tests.unit.projects.test_execution_engines import ready


async def test_http_catalog_and_acp_task_have_separate_model_identity(env, monkeypatch):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    selection, _ = ready(server.services, monkeypatch, owner)
    calls = []

    async def run(services, user_id, turn, history):
        calls.append(turn)
        yield {"type": "token", "content": "native answer"}

    monkeypatch.setattr(runtime, "run_turn", run)
    response = await client.get("/api/project-tasks/engines", headers=auth)
    assert response.status_code == 200
    entries = response.json()
    selected = next(item for item in entries if item["engine"]["connection_id"] == "native-test")
    assert selected["available"] is True and selected["capabilities"]["pause"] is False
    assert "secret-never-expose" not in response.text
    resources = await client.get("/api/project-tasks/resources", headers=auth)
    assert resources.status_code == 200
    assert resources.json()["execution_engines"] == entries
    assert resources.json()["models"] == []
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "http-native-create",
            "prompt": "work",
            "engine": selection.model_dump(),
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    for _ in range(100):
        task = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
        if task["active_turn_id"] is None:
            break
        await asyncio.sleep(0.01)
    assert task["status"] == "completed"
    assert task["engine"] == selection.model_dump()
    assert task["turns"][0]["execution_engine"]["connection_id"] == "native-test"
    assert task["turns"][0]["model_ref"] == ""
    assert len(calls) == 1
