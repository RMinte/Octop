"""Owned IM bindings use optional experts and projects without agent runtimes."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _enable_docs(monkeypatch: Any) -> None:
    monkeypatch.setenv("OCTOP_ENABLE_API_DOCS", "true")


async def test_general_channel_crud_reloads_adapter_and_keeps_binding_out_of_credentials(
    env: Any, monkeypatch: Any
) -> None:
    client, server, auth = env
    gateway = server.app_runtime.gateway
    reload = AsyncMock()
    monkeypatch.setattr(gateway, "reload_channel", reload)
    response = await client.post(
        "/api/channels",
        headers=auth,
        json={
            "kind": "feishu",
            "name": "general source",
            "config": {"app_id": "temporary"},
            "enabled": False,
        },
    )
    assert response.status_code == 201, response.text
    channel = response.json()
    identifier = channel["id"]
    assert channel["selected_expert_id"] is None and channel["project_id"] is None
    assert channel["action_mode"] == "craft" and channel["enabled"] is False
    assert channel["config"] == {"app_id": "temporary"}
    reload.assert_awaited_once_with(identifier)
    listing = await client.get("/api/channels", headers=auth)
    assert [row["id"] for row in listing.json()] == [identifier]
    loaded = await client.get(f"/api/channels/{identifier}", headers=auth)
    assert loaded.json()["config"] == channel["config"]
    changed = await client.patch(
        f"/api/channels/{identifier}",
        headers=auth,
        json={"name": "renamed source", "action_mode": "ask"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "renamed source" and changed.json()["action_mode"] == "ask"
    removed = await client.delete(f"/api/channels/{identifier}", headers=auth)
    assert removed.status_code == 204, removed.text
    assert reload.await_count == 3
    assert (await client.get(f"/api/channels/{identifier}", headers=auth)).status_code == 404
    assert server.services.channel_repo.get(identifier) is None


async def test_explicit_null_clears_expert_and_project_but_omitted_fields_preserve_them(
    env_alice_bob_agent: Any, monkeypatch: Any
) -> None:
    client, server, auth, _, expert = env_alice_bob_agent
    monkeypatch.setattr(server.app_runtime.gateway, "reload_channel", AsyncMock())
    project = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "channel project"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    created = await client.post(
        "/api/channels",
        headers=auth,
        json={
            "kind": "feishu",
            "name": "bound source",
            "config": {},
            "selected_expert_id": expert,
            "project_id": project_id,
            "action_mode": "plan",
        },
    )
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    renamed = await client.patch(
        f"/api/channels/{identifier}", headers=auth, json={"name": "changed"}
    )
    assert (
        renamed.json()["selected_expert_id"] == expert
        and renamed.json()["project_id"] == project_id
    )
    cleared = await client.patch(
        f"/api/channels/{identifier}",
        headers=auth,
        json={"selected_expert_id": None, "project_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["selected_expert_id"] is None and cleared.json()["project_id"] is None
    assert cleared.json()["action_mode"] == "plan"
    row = server.services.channel_repo.get(identifier)
    assert row.agent_id is None
    assert json.loads(row.config_json)["_octop_task"]["project_id"] is None
    assert "_octop_task" not in cleared.json()["config"]


async def test_channel_owner_isolation_and_foreign_project_rejection(
    env_alice_bob_agent: Any, monkeypatch: Any
) -> None:
    client, server, alice, bob, expert = env_alice_bob_agent
    monkeypatch.setattr(server.app_runtime.gateway, "reload_channel", AsyncMock())
    response = await client.post(
        "/api/channels",
        headers=alice,
        json={"kind": "feishu", "name": "private", "config": {}, "enabled": False},
    )
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    assert (await client.get("/api/channels", headers=bob)).json() == []
    for method, body in [("GET", None), ("PATCH", {"name": "take over"}), ("DELETE", None)]:
        response = await client.request(
            method, f"/api/channels/{identifier}", headers=bob, json=body
        )
        assert response.status_code == 404, response.text
    foreign_expert = await client.post(
        "/api/channels",
        headers=bob,
        json={"kind": "feishu", "name": "forbidden", "selected_expert_id": expert},
    )
    assert foreign_expert.status_code == 404
    project = (
        await client.post(
            "/api/projects",
            headers={**alice, "Idempotency-Key": str(uuid4())},
            json={"name": "private project"},
        )
    ).json()["project_id"]
    foreign_project = await client.post(
        "/api/channels",
        headers=bob,
        json={"kind": "feishu", "name": "forbidden project", "project_id": project},
    )
    assert foreign_project.status_code == 404


@pytest.mark.parametrize("field", ["kind", "name", "config", "enabled", "action_mode"])
async def test_null_non_binding_patch_is_validation_error(env: Any, field: str) -> None:
    client, _, auth = env
    response = await client.patch("/api/channels/opaque", headers=auth, json={field: None})
    assert response.status_code == 422, response.text


async def test_channel_contract_docs_expose_typed_optional_binding(env: Any) -> None:
    client, _, _ = env
    assert (await client.get("/api/docs")).status_code == 200
    schema = (await client.get("/api/openapi.json")).json()
    path = schema["paths"]["/api/channels"]
    assert path["post"]["summary"] and path["get"]["summary"]
    dto = schema["components"]["schemas"]["TaskChannelCreateBody"]
    assert {"selected_expert_id", "project_id", "action_mode"} <= dto["properties"].keys()
    assert "selected_expert_id" not in dto.get("required", [])
    assert path["post"]["responses"]["201"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("TaskChannelView")
    assert "/api/agents/{agent_id}/channels" in schema["paths"]


async def test_archived_project_cannot_be_bound_to_new_channel(env: Any, monkeypatch: Any) -> None:
    client, server, auth = env
    monkeypatch.setattr(server.app_runtime.gateway, "reload_channel", AsyncMock())
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "archived source"},
        )
    ).json()
    archived = await client.patch(
        f"/api/projects/{project['project_id']}/archive",
        headers=auth,
        json={"archived": True, "expected_revision": project["revision"]},
    )
    assert archived.status_code == 200, archived.text
    created = await client.post(
        "/api/channels",
        headers=auth,
        json={"kind": "feishu", "name": "archived", "project_id": project["project_id"]},
    )
    assert created.status_code == 404, created.text
    assert created.json()["error"]["code"] == "NOT_FOUND"
    assert (await client.get("/api/channels", headers=auth)).json() == []


async def test_resident_remains_dedicated_and_is_never_a_modern_selected_expert(
    env_with_provider: Any, monkeypatch: Any
) -> None:
    import asyncio

    client, server, auth = env_with_provider
    monkeypatch.setattr(server.app_runtime.gateway, "reload_channel", AsyncMock())
    response = await client.put("/api/agents/resident-conversation", headers=auth, json={})
    assert response.status_code in {200, 201, 202}, response.text
    async with asyncio.timeout(10):
        while True:
            binding = (await client.get("/api/agents/resident-conversation", headers=auth)).json()
            if binding.get("agent_id"):
                break
            await asyncio.sleep(0.01)
    resident = binding["agent_id"]
    response = await client.post(
        "/api/channels",
        headers=auth,
        json={"kind": "feishu", "name": "forbidden resident", "selected_expert_id": resident},
    )
    assert response.status_code == 404, response.text
    services = server.services
    owner = services.agent_repo.get(resident).user_id
    services.channel_repo.create(
        channel_id="dedicated-resident",
        agent_id=resident,
        user_id=owner,
        kind="feishu",
        name="resident source",
        config_json="{}",
    )
    assert (await client.get("/api/channels", headers=auth)).json() == []
    assert (await client.get("/api/channels/dedicated-resident", headers=auth)).status_code == 404
    assert (
        await client.delete("/api/channels/dedicated-resident", headers=auth)
    ).status_code == 404
    assert services.channel_repo.get("dedicated-resident") is not None


async def test_shared_expert_channel_names_are_owner_scoped_and_own_duplicates_conflict(
    env_alice_bob_agent: Any, monkeypatch: Any
) -> None:
    client, server, alice, bob, expert = env_alice_bob_agent
    monkeypatch.setattr(server.app_runtime.gateway, "reload_channel", AsyncMock())
    server.services.agent_repo.set_shared(expert, True)
    body = {
        "kind": "feishu",
        "name": "same shared source",
        "selected_expert_id": expert,
        "config": {},
        "enabled": False,
    }
    first = await client.post("/api/channels", headers=alice, json=body)
    second = await client.post("/api/channels", headers=bob, json=body)
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json()["id"] != second.json()["id"]
    assert [row["id"] for row in (await client.get("/api/channels", headers=bob)).json()] == [
        second.json()["id"]
    ]
    duplicate = await client.post("/api/channels", headers=alice, json=body)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "CHANNEL_NAME_TAKEN"
    general = {**body, "selected_expert_id": None, "name": "same general source"}
    assert (await client.post("/api/channels", headers=alice, json=general)).status_code == 201
    duplicate_general = await client.post("/api/channels", headers=alice, json=general)
    assert duplicate_general.status_code == 409, duplicate_general.text
    assert duplicate_general.json()["error"]["code"] == "CHANNEL_NAME_TAKEN"
    assert (await client.post("/api/channels", headers=bob, json=general)).status_code == 201
    rename_conflict = await client.patch(
        f"/api/channels/{first.json()['id']}",
        headers=alice,
        json={"selected_expert_id": None, "name": general["name"]},
    )
    assert rename_conflict.status_code == 409, rename_conflict.text
    unchanged = (await client.get(f"/api/channels/{first.json()['id']}", headers=alice)).json()
    assert unchanged["selected_expert_id"] == expert and unchanged["name"] == body["name"]


async def test_general_duplicate_race_still_returns_conflict_instead_of_database_error(
    env: Any, monkeypatch: Any
) -> None:
    client, server, auth = env
    monkeypatch.setattr(server.app_runtime.gateway, "reload_channel", AsyncMock())
    repo = server.services.channel_repo
    original = repo.get_by_owner_agent_name
    reads = 0

    def stale_check(owner: int, expert: str | None, name: str) -> Any:
        nonlocal reads
        reads += 1
        # The second writer observes the same pre-insert state as the first.
        return None if reads <= 2 else original(owner, expert, name)

    monkeypatch.setattr(repo, "get_by_owner_agent_name", stale_check)
    body = {"kind": "feishu", "name": "racing source", "enabled": False}
    assert (await client.post("/api/channels", headers=auth, json=body)).status_code == 201
    conflict = await client.post("/api/channels", headers=auth, json=body)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "CHANNEL_NAME_TAKEN"
    assert len((await client.get("/api/channels", headers=auth)).json()) == 1
