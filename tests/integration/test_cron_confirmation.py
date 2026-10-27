"""Schedule confirmation authorization is explicit, persistent and owner scoped."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from octop.api.app import build_app
from tests.support.auth import create_user


@pytest.mark.parametrize("skip_confirmation", [None, False, True])
async def test_schedule_confirmation_create_and_patch(env, skip_confirmation):
    client, server, auth = env
    body = {"trigger": "interval:3600", "prompt": "Scheduled work", "enabled": False}
    if skip_confirmation is not None:
        body["skip_confirmation"] = skip_confirmation
    created = await client.post("/api/cron", headers=auth, json=body)
    assert created.status_code == 201, created.text
    cron_id = created.json()["id"]
    assert created.json()["skip_confirmation"] is bool(skip_confirmation)
    assert server.services.cron_repo.get(cron_id).skip_confirmation is bool(skip_confirmation)
    assert (await client.get("/api/cron", headers=auth)).json()[0]["skip_confirmation"] is bool(
        skip_confirmation
    )
    enabled = await client.patch(
        f"/api/cron/{cron_id}", headers=auth, json={"skip_confirmation": True}
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["skip_confirmation"] is True
    for update in ({"prompt": "New work"}, {"skip_confirmation": None}):
        retained = await client.patch(f"/api/cron/{cron_id}", headers=auth, json=update)
        assert retained.status_code == 200, retained.text
        assert retained.json()["skip_confirmation"] is True
    cleared = await client.patch(
        f"/api/cron/{cron_id}", headers=auth, json={"skip_confirmation": False}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["skip_confirmation"] is False
    assert server.services.cron_repo.get(cron_id).skip_confirmation is False


async def test_schedule_confirmation_is_owner_scoped(env):
    client, server, auth = env
    other = await create_user(client, auth, username="cron-confirmation-other")
    created = await client.post(
        "/api/cron",
        headers=auth,
        json={"trigger": "interval:3600", "prompt": "Private work", "enabled": False},
    )
    assert created.status_code == 201, created.text
    cron_id = created.json()["id"]
    denied = await client.patch(
        f"/api/cron/{cron_id}", headers=other, json={"skip_confirmation": True}
    )
    assert denied.status_code == 404, denied.text
    assert server.services.cron_repo.get(cron_id).skip_confirmation is False
    assert (await client.get("/api/cron", headers=other)).json() == []


async def test_legacy_schedule_confirmation_round_trip(env_with_agent):
    client, _server, auth, agent_id = env_with_agent
    created = await client.post(
        f"/api/agents/{agent_id}/cron",
        headers=auth,
        json={
            "trigger": "interval:3600",
            "prompt": "Legacy work",
            "enabled": False,
            "skip_confirmation": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["skip_confirmation"] is True
    changed = await client.patch(
        f"/api/agents/{agent_id}/cron/{created.json()['id']}",
        headers=auth,
        json={"skip_confirmation": False},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["skip_confirmation"] is False


async def test_schedule_confirmation_openapi_is_typed_and_documented(env, monkeypatch):
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
    for name in ("CronCreateBody", "TaskCronCreateBody", "CronPatchBody", "CronResponse"):
        field = schema["components"]["schemas"][name]["properties"]["skip_confirmation"]
        assert field["description"]
        if name == "CronPatchBody":
            assert {"type": "boolean"} in field["anyOf"]
        else:
            assert field["type"] == "boolean"
