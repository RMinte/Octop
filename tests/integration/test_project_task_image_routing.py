"""Image capability selection is per turn and preserves model settings."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict

from tests.integration.test_project_task_artifacts import real_model as real_model
from tests.integration.test_project_task_images import image_urls, upload
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks import task_env as task_env
from tests.integration.test_project_tasks_ws import command, connected
from tests.support.auth import create_provider
from tests.unit.projects.test_execution_engines import ready

TEXT_MODEL = "text-only/plain-chat"
VISION_MODEL = "openai/gpt-4o"


async def select_text_default(client, auth):
    await create_provider(
        client,
        auth,
        name="text-only",
        base_url="https://example.invalid/v1",
        models=[{"id": "plain-chat", "enabled": True, "input": ["text"]}],
    )
    active = await client.put(
        "/api/providers/active-model",
        headers=auth,
        json={"provider_name": "text-only", "model": "plain-chat"},
    )
    assert active.status_code == 200, active.text
    preferred = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_model": TEXT_MODEL}
    )
    assert preferred.status_code == 200, preferred.text


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_image_upgrades_default_or_selected_text_model_for_one_turn(
    task_env, transport, explicit
):
    client, server, auth, calls = task_env
    await select_text_default(client, auth)
    image = await upload(client, auth)
    original_options = {"model_ref": TEXT_MODEL} if explicit else {}
    body = {
        "request_id": uuid4().hex,
        "prompt": "Inspect this image",
        "attachment_ids": [image["attachment_id"]],
        "options": original_options,
    }
    if transport == "http":
        response = await client.post("/api/project-tasks", headers=auth, json=body)
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
    else:
        session, ws = await connected(client, auth)
        try:
            response = await command(ws, "task.create", uuid4().hex, data=body)
            assert response["type"] == "result", response
            task_id = response["data"]["task_id"]
        finally:
            await session.__aexit__(None, None, None)
    done = await settled(client, auth, task_id)
    assert done["turns"][0]["model_ref"] == calls[0][0].model_ref == VISION_MODEL
    frozen = json.loads(calls[0][0].configuration_json)
    assert frozen["execution_options"].get("model_ref") == original_options.get("model_ref")
    assert frozen["capability_model_route"] is True
    assert "routing" not in frozen
    assert server.services.settings_repo.get_active_model() == ("text-only", "plain-chat")
    preferred = await client.get("/api/preferences", headers=auth)
    assert preferred.json()["preferred_model"] == TEXT_MODEL

    # A receipt replay retains the admitted vision model even after it is disabled.
    vision_provider = server.services.provider_repo.get_by_name("openai")
    assert vision_provider is not None
    disabled = await client.patch(
        f"/api/admin/providers/{vision_provider.id}", headers=auth, json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    replay = await client.post("/api/project-tasks", headers=auth, json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["turns"][0]["model_ref"] == VISION_MODEL
    assert len(calls) == 1


async def test_plain_text_and_ordinary_file_keep_the_selected_text_model(task_env):
    client, _, auth, calls = task_env
    await select_text_default(client, auth)
    attachment = await client.post(
        "/api/project-tasks/attachments",
        headers=auth,
        files={"file": ("notes.txt", b"Ordinary text", "text/plain")},
    )
    assert attachment.status_code == 201, attachment.text
    for attachment_ids in ([], [attachment.json()["attachment_id"]]):
        response = await client.post(
            "/api/project-tasks",
            headers=auth,
            json={
                "request_id": uuid4().hex,
                "prompt": "Read the notes",
                "attachment_ids": attachment_ids,
                "options": {"model_ref": TEXT_MODEL},
            },
        )
        assert response.status_code == 202, response.text
        done = await settled(client, auth, response.json()["task_id"])
        assert done["turns"][0]["model_ref"] == TEXT_MODEL
    assert all(turn.model_ref == TEXT_MODEL for turn, _ in calls)


async def test_reasoning_settings_are_validated_against_the_upgraded_model(task_env):
    client, server, auth, calls = task_env
    await select_text_default(client, auth)
    provider = server.services.provider_repo.get_by_name("openai")
    assert provider is not None
    server.services.provider_repo.update(
        provider.id,
        models_json=json.dumps(
            [
                {
                    "id": "gpt-4o",
                    "input": ["text", "image"],
                    "reasoning_config": {
                        "adapter": "openai_reasoning_effort",
                        "toggle": True,
                        "default_mode": "enabled",
                        "efforts": ["low", "high"],
                        "default_effort": "low",
                    },
                }
            ]
        ),
    )
    image = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": uuid4().hex,
            "attachment_ids": [image["attachment_id"]],
            "options": {"model_ref": TEXT_MODEL, "reasoning_effort": "high"},
        },
    )
    assert response.status_code == 202, response.text
    await settled(client, auth, response.json()["task_id"])
    turn = calls[0][0]
    configuration = json.loads(turn.configuration_json)
    assert turn.model_ref == VISION_MODEL
    assert configuration["reasoning_effort"] == "high"
    assert configuration["reasoning_overrides"] == {"model_fields": {"reasoning_effort": "high"}}


async def test_missing_vision_model_rejects_without_consuming_the_image(task_env):
    client, server, auth, calls = task_env
    await select_text_default(client, auth)
    provider = server.services.provider_repo.get_by_name("openai")
    assert provider is not None
    disabled = await client.patch(
        f"/api/admin/providers/{provider.id}", headers=auth, json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    image = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": uuid4().hex, "attachment_ids": [image["attachment_id"]]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "TASK_IMAGE_MODEL_REQUIRED"
    assert calls == []
    with server.services.db.read_snapshot() as conn:
        owner = server.services.user_repo.get_by_username("admin")
        assert owner is not None
        row = server.services.project_task_attachment_repo.get(
            conn, owner.id, image["attachment_id"]
        )
        assert row is not None and row.task_id is None
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []


async def test_historical_upload_keeps_followup_on_a_vision_model(env_with_provider, real_model):
    client, _, auth = env_with_provider
    await select_text_default(client, auth)
    observed, _ = real_model([AIMessage(content="Blue."), AIMessage(content="Still blue.")])
    image = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": uuid4().hex, "attachment_ids": [image["attachment_id"]]},
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    await settled(client, auth, task_id)
    followup = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={
            "request_id": uuid4().hex,
            "prompt": "What color was it?",
            "options": {"model_ref": TEXT_MODEL},
        },
    )
    assert followup.status_code == 202, followup.text
    done = await settled(client, auth, task_id)
    assert [turn["model_ref"] for turn in done["turns"]] == [VISION_MODEL, VISION_MODEL]
    assert done["turns"][1]["attachments"] == []
    assert len(observed) == 2 and image_urls(observed[0]) == image_urls(observed[1])


@pytest.mark.parametrize(
    "history",
    [
        HumanMessage(
            content=[
                {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}
            ]
        ),
        HumanMessage(
            content=[{"type": "input_image", "image_url": "https://example.invalid/image.png"}]
        ),
        ToolMessage(
            content=[{"type": "image", "url": "https://example.invalid/image.png"}],
            tool_call_id="read-old-image",
        ),
    ],
    ids=["image-url", "input-image", "tool-image"],
)
async def test_effective_context_image_selects_vision_without_upload_metadata(task_env, history):
    client, server, auth, calls = task_env
    await select_text_default(client, auth)
    first = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": uuid4().hex, "prompt": "Imported conversation"},
    )
    assert first.status_code == 202, first.text
    done = await settled(client, auth, first.json()["task_id"])
    with server.services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_task_contexts(task_id,turn_id,messages_json) VALUES (?,?,?)",
            (
                done["task_id"],
                done["turns"][0]["turn_id"],
                json.dumps([message_to_dict(history)]),
            ),
        )
    followup = await client.post(
        f"/api/project-tasks/{done['task_id']}/messages",
        headers=auth,
        json={"request_id": uuid4().hex, "prompt": "Describe the old image"},
    )
    assert followup.status_code == 202, followup.text
    done = await settled(client, auth, done["task_id"])
    assert [turn["model_ref"] for turn in done["turns"]] == [TEXT_MODEL, VISION_MODEL]
    assert calls[-1][0].model_ref == VISION_MODEL


@pytest.mark.parametrize("supports_images", [False, True])
async def test_native_engine_retains_its_own_image_capability_and_model(
    task_env, monkeypatch, supports_images
):
    client, server, auth, calls = task_env
    owner = server.services.user_repo.get_by_username("admin")
    assert owner is not None
    selection, store = ready(server.services, monkeypatch, owner.id)
    status = store.load_setup_status(owner.id, "native-test")
    status["engine_capabilities"]["images"] = supports_images
    store.save_setup_status(owner.id, "native-test", status)
    selection = selection.model_copy(update={"model_id": "native-model"})
    image = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": uuid4().hex,
            "prompt": "Inspect with the native engine",
            "attachment_ids": [image["attachment_id"]],
            "engine": selection.model_dump(),
        },
    )
    if not supports_images:
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "TASK_SETTINGS_INVALID"
        assert calls == []
        return
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    turn = calls[0][0]
    assert done["turns"][0]["model_ref"] == turn.model_ref == ""
    configuration = json.loads(turn.configuration_json)
    assert "capability_model_route" not in configuration
    assert configuration["execution_engine"]["model_id"] == "native-model"
