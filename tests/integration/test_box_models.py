"""Box connections share the model catalog without storing or sending credentials."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from octop.api.routers.setup import ProviderDraftBody, _apply_provider_draft
from octop.infra.agents.providers.store import ProviderStore
from octop.infra.projects import runtime
from tests.support.auth import create_user
from tests.support.http import ws_connect, ws_token


async def test_box_and_remote_models_coexist_without_box_credentials(env: Any) -> None:
    client, server, auth = env
    remote = await client.post(
        "/api/admin/providers",
        headers=auth,
        json={
            "name": "remote",
            "kind": "openai",
            "base_url": "https://models.example/v1",
            "api_key": "remote-secret",
            "models": [{"id": "cloud-chat", "enabled": True}],
        },
    )
    assert remote.status_code == 201
    response = await client.post(
        "/api/admin/providers",
        headers=auth,
        json={
            "name": "box",
            "kind": "box",
            "base_url": "http://127.0.0.1:58000/v1/",
            "api_key": "must-not-be-stored",
            "models": [
                {"id": "RM-01 VLM", "enabled": True, "input": ["text", "image"]},
                {"id": "qwen3-asr", "enabled": True},
                {"id": "qwen3-tts", "enabled": True},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["api_key"] == ""
    store = ProviderStore(server.services.provider_repo)
    assert store.is_model_ref_usable("box/RM-01 VLM")
    assert store.is_model_ref_usable("remote/cloud-chat")
    assert not store.is_model_ref_usable("box/qwen3-asr")
    assert not store.is_model_ref_usable("box/qwen3-tts")
    assert len(store.build_harness_configs()) == 2

    user_auth = await create_user(client, auth, username="model-client")
    resources = await client.get("/api/project-tasks/resources", headers=user_auth)
    assert resources.status_code == 200, resources.text
    models = {item["model_ref"]: item for item in resources.json()["models"]}
    assert models["box/RM-01 VLM"]["provider_kind"] == "box"
    assert models["box/RM-01 VLM"]["supports_images"] is True
    assert set(models) == {"box/RM-01 VLM", "remote/cloud-chat"}
    assert resources.json()["default_model_ref"] in models
    assert resources.headers["cache-control"] == "no-store"
    assert all(
        not {"api_key", "base_url", "headers", "extra_json"}.intersection(model)
        for model in models.values()
    )
    resolved = (await client.get("/api/providers/resolved", headers=auth)).json()
    assert {item["model"] for item in resolved} == {"RM-01 VLM", "cloud-chat"}
    assert "remote-secret" not in resources.text

    patched = await client.patch(
        f"/api/admin/providers/{response.json()['id']}",
        headers=auth,
        json={"api_key": "still-not-stored"},
    )
    assert patched.status_code == 200
    assert patched.json()["api_key"] == ""


async def test_box_model_discovery_omits_auth_and_preserves_context(env: Any) -> None:
    client, _server, auth = env
    wire: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        wire.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "RM-01 VLM", "max_model_len": 262144}]},
        )

    transport = httpx.MockTransport(answer)
    original_client = httpx.AsyncClient
    with patch(
        "octop.infra.agents.providers.probe.httpx.AsyncClient",
        side_effect=lambda **kwargs: original_client(transport=transport, **kwargs),
    ):
        response = await client.post(
            "/api/admin/providers/fetch-models",
            headers=auth,
            json={
                "kind": "box",
                "base_url": "http://box.internal:58000/v1",
                "api_key": "ignored",
                "extra_json": json.dumps({"headers": {"aUtHoRiZaTiOn": "ignored"}}),
            },
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "models": [{"id": "RM-01 VLM", "name": "RM-01 VLM", "context_window": 262144}],
    }
    assert len(wire) == 1
    assert wire[0].url == "http://box.internal:58000/v1/models"
    assert "authorization" not in wire[0].headers


async def test_box_draft_probe_and_initial_setup_accept_empty_key(env: Any) -> None:
    client, server, auth = env
    with patch(
        "octop.api.routers.providers.probe_provider_row",
        new=AsyncMock(return_value={"ok": True, "latency_ms": 1}),
    ) as probe:
        response = await client.post(
            "/api/admin/providers/test-draft",
            headers=auth,
            json={
                "name": "box",
                "kind": "box",
                "base_url": "http://localhost:58000/v1",
                "model_id": "RM-01 VLM",
            },
        )
    assert response.status_code == 200 and response.json()["ok"] is True
    assert probe.await_args.args[0].api_key == ""
    await _apply_provider_draft(
        server,
        ProviderDraftBody.model_validate(
            {
                "name": "setup-box",
                "type": "box",
                "base_url": "http://localhost:58000/v1",
                "models": [
                    {
                        "id": "RM-01 VLM",
                        "name": "RM-01 VLM",
                        "context_window": 262144,
                        "max_tokens": 32768,
                    }
                ],
            }
        ),
    )
    row = server.services.provider_repo.get_by_name("setup-box")
    assert row.api_key == ""
    assert row.get_models()[0]["context_window"] == 262144
    config = next(
        item
        for item in ProviderStore(server.services.provider_repo).build_harness_configs()
        if item.id == "setup-box"
    )
    assert config.models[0].context_window == 262144
    assert config.models[0].max_output_tokens == 32768
    assert server.services.settings_repo.get_active_model() == ("setup-box", "RM-01 VLM")


@pytest.mark.parametrize("url", ["file:///models", "http://user:pass@box/v1", ""])
async def test_box_provider_rejects_invalid_connection_url(env: Any, url: str) -> None:
    client, _server, auth = env
    response = await client.post(
        "/api/admin/providers",
        headers=auth,
        json={"name": "box", "kind": "box", "base_url": url},
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "base_url"


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_business_turn_model_choice_overrides_default_and_reaches_worker(
    env: Any, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    client, _server, auth = env
    called_models: list[str] = []

    async def run_turn(services, owner, turn, history):
        called_models.append(turn.model_ref)
        yield {"type": "token", "content": f"Executed with {turn.model_ref}"}

    monkeypatch.setattr(runtime, "run_turn", run_turn)
    for name, kind, url, model, key in (
        ("Box", "box", "http://box.internal:58000/v1", "RM-01 VLM", ""),
        ("remote", "openai", "https://models.example/v1", "cloud-chat", "fixture-key"),
    ):
        response = await client.post(
            "/api/admin/providers",
            headers=auth,
            json={
                "name": name,
                "kind": kind,
                "base_url": url,
                "api_key": key,
                "models": [{"id": model, "enabled": True}],
            },
        )
        assert response.status_code == 201, response.text
    response = await client.put(
        "/api/providers/active-model",
        headers=auth,
        json={"provider_name": "Box", "model": "RM-01 VLM"},
    )
    assert response.status_code == 200

    async def settled(task_id: str) -> dict[str, Any]:
        for _ in range(300):
            response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
            assert response.status_code == 200
            detail = response.json()
            if detail["active_turn_id"] is None:
                return detail
            await asyncio.sleep(0.01)
        pytest.fail("task worker did not finish")

    async def submit(body: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
        if transport == "rest":
            url = f"/api/project-tasks/{task_id}/messages" if task_id else "/api/project-tasks"
            response = await client.post(url, headers=auth, json=body)
            assert response.status_code == 202, response.text
            return response.json()
        async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
            await ws.send_json({"type": "authenticate", "token": ws_token(auth)})
            assert (await ws.receive_json())["type"] == "ready"
            command_id = "command-" + body["request_id"]
            await ws.send_json(
                {
                    "type": "task.message" if task_id else "task.create",
                    "id": command_id,
                    **({"task_id": task_id} if task_id else {}),
                    "data": body,
                }
            )
            for _ in range(20):
                frame = await ws.receive_json(timeout=5)
                if frame.get("id") == command_id:
                    assert frame["type"] == "result", frame
                    return frame["data"]
            pytest.fail("model selection command did not return a result")

    created = await submit(
        {
            "request_id": "choose-remote",
            "prompt": "Use the remote model for this turn",
            "options": {"model_ref": "remote/cloud-chat"},
        },
    )
    task_id = created["task_id"]
    await settled(task_id)
    for request_id, options in (
        ("choose-box", {"model_ref": "Box/RM-01 VLM"}),
        ("follow-default", {}),
    ):
        await submit(
            {"request_id": request_id, "prompt": request_id, "options": options},
            task_id,
        )
        detail = await settled(task_id)
    expected = ["remote/cloud-chat", "Box/RM-01 VLM", "Box/RM-01 VLM"]
    assert called_models == expected
    assert [turn["model_ref"] for turn in detail["turns"]] == expected
    assert all(turn["reply"] == f"Executed with {turn['model_ref']}" for turn in detail["turns"])
    active = (await client.get("/api/providers/active-model", headers=auth)).json()
    assert active == {"provider_name": "Box", "model": "RM-01 VLM"}
