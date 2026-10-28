"""Private assistants reuse immutable Task image inputs and model expansion."""

from __future__ import annotations

import asyncio
import base64
import json
from itertools import count
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, message_to_dict

from octop.infra.projects import input_attachments, resident_tasks, runtime
from tests.integration.test_project_task_artifacts import real_model as real_model
from tests.integration.test_project_task_images import (
    assert_durable_references,
    image_bytes,
    image_urls,
    upload,
)
from tests.integration.test_project_tasks_ws import connected
from tests.integration.test_resident_conversations import _base, _binding, _turn
from tests.integration.test_resident_stream_delivery import subscribe
from tests.support.auth import create_provider, create_user
from tests.support.harness import patch_resident_stream


def resident_model(real_model, monkeypatch, responses):
    observed, _names = real_model(responses)
    monkeypatch.setattr(resident_tasks, "HarnessAgent", runtime.HarnessAgent)
    return observed


async def test_image_only_and_followup_preserve_private_receipts_history_and_model_context(
    env_with_provider, real_model, monkeypatch
):
    client, server, auth = env_with_provider
    observed = resident_model(
        real_model,
        monkeypatch,
        [AIMessage(content="A blue image."), AIMessage(content="Still blue.")],
    )
    binding = await _binding(client, auth)
    base = _base(binding)
    image = await upload(client, auth)
    body = {"request_id": uuid4().hex, "prompt": "", "attachment_ids": [image["attachment_id"]]}
    accepted = await client.post(base + "/messages", headers=auth, json=body)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["attachments"] == [image]
    assert accepted.json()["authorization"] == {
        "read_tools": False,
        "task_context": None,
        "knowledge_base_ids": [],
    }
    first = await _turn(client, auth, base, body["request_id"], "completed")
    assert first["attachments"] == [image]
    assert base64.b64decode(image_urls(observed[0])[0].split(",", 1)[1]) == image_bytes()
    repeated = await client.post(base + "/messages", headers=auth, json=body)
    assert repeated.status_code == 202 and repeated.json() == first
    changed = await client.post(
        base + "/messages",
        headers=auth,
        json={**body, "prompt": "Use text now.", "attachment_ids": []},
    )
    assert changed.status_code == 409, changed.text
    other = await upload(client, auth)
    changed = await client.post(
        base + "/messages", headers=auth, json={**body, "attachment_ids": [other["attachment_id"]]}
    )
    assert changed.status_code == 409, changed.text
    with monkeypatch.context() as limited:
        limited.setattr(
            server.app_runtime.agent_registry.providers,
            "is_model_ref_multimodal",
            lambda _ref: False,
        )
        rejected = await client.post(
            base + "/messages",
            headers=auth,
            json={"request_id": uuid4().hex, "prompt": "What color was that image?"},
        )
        assert (
            rejected.status_code == 422
            and rejected.json()["error"]["code"] == "TASK_IMAGE_MODEL_REQUIRED"
        ), rejected.text
    followup = {"request_id": uuid4().hex, "prompt": "What color was that image?"}
    sent = await client.post(base + "/messages", headers=auth, json=followup)
    assert sent.status_code == 202, sent.text
    second = await _turn(client, auth, base, followup["request_id"], "completed")
    assert second["attachments"] == []
    assert len(observed) == 2 and image_urls(observed[1]) == image_urls(observed[0])
    snapshot = await client.get(base + "/conversation", headers=auth)
    assert snapshot.status_code == 200 and snapshot.json()["turns"] == [first, second]
    history = await client.get(base + "/conversation/history", headers=auth)
    assert history.status_code == 200, history.text
    assert [item["turn"] for item in history.json()["items"]] == [first, second]
    item_id = history.json()["items"][0]["item_id"]
    location = await client.get(base + "/conversation/history/items/" + item_id, headers=auth)
    assert location.status_code == 200 and location.json()["items"][0]["turn"] == first
    session, ws = await connected(client, auth)
    try:
        live = await subscribe(ws, binding, "resident-image-snapshot")
        assert live["turns"] == [first, second]
    finally:
        await session.__aexit__(None, None, None)
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    identifier = resident_tasks.task_id(owner, binding["thread_id"])
    assert_durable_references(server, identifier, image["attachment_id"])
    receipt = server.services.resident_request_repo.get(owner, body["request_id"])
    assert json.loads(receipt.input_json)["attachment_ids"] == [image["attachment_id"]]
    assert "data:image/" not in json.dumps(snapshot.json())
    assert "data:image/" not in receipt.configuration_json + receipt.input_json
    assert server.services.project_task_repo.get(identifier, owner) is None
    hidden = await client.get("/api/project-tasks/" + identifier, headers=auth)
    assert hidden.status_code == 404
    preview = await client.get(image["preview_url"], headers=auth)
    assert preview.status_code == 200 and preview.content == image_bytes()
    capabilities = await client.get("/api/agents/resident-capabilities", headers=auth)
    assert capabilities.json()["message_types"] == ["text", "image", "file"]


@pytest.mark.parametrize("selection", ["preference", "pin", "explicit-task-context"])
async def test_image_turn_temporarily_upgrades_current_model_and_freezes_actual_scope(
    env_with_provider, real_model, monkeypatch, selection
):
    client, server, auth = env_with_provider
    observed = resident_model(
        real_model,
        monkeypatch,
        [AIMessage(content="Hello."), AIMessage(content="Blue."), AIMessage(content="Still blue.")],
    )
    await create_provider(
        client,
        auth,
        name="selected",
        base_url="https://selected.example/v1",
        models=[
            {"id": "text-one", "input": ["text"]},
            {"id": "text-two", "input": ["text"]},
            {"id": "image-reader", "input": ["text", "image"]},
        ],
    )
    binding = await _binding(client, auth)
    base = _base(binding)
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    preferred = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_model": "selected/text-one"}
    )
    assert preferred.status_code == 200, preferred.text
    if selection == "pin":
        server.services.agent_repo.update_config(
            binding["agent_id"], default_model="selected/text-one"
        )
    text_body = {"request_id": uuid4().hex, "prompt": "Hello."}
    sent = await client.post(base + "/messages", headers=auth, json=text_body)
    assert sent.status_code == 202, sent.text
    text_turn = await _turn(client, auth, base, text_body["request_id"], "completed")
    text_receipt = server.services.resident_request_repo.get(owner, text_body["request_id"])
    assert json.loads(text_receipt.configuration_json)["model_ref"] == "selected/text-one"
    assert image_urls(observed[0]) == []

    image = await upload(client, auth)
    image_body = {
        "request_id": uuid4().hex,
        "prompt": "Describe this image.",
        "attachment_ids": [image["attachment_id"]],
    }
    if selection == "explicit-task-context":
        image_body["task_context"] = {
            "project_id": None,
            "message": {
                "request_id": uuid4().hex,
                "prompt": "An explicitly selected task.",
                "options": {"model_ref": "selected/text-one"},
            },
        }
    sent = await client.post(base + "/messages", headers=auth, json=image_body)
    assert sent.status_code == 202, sent.text
    await _turn(client, auth, base, image_body["request_id"], "completed")
    receipt = server.services.resident_request_repo.get(owner, image_body["request_id"])
    frozen = json.loads(receipt.configuration_json)
    assert frozen["model_ref"] == "selected/image-reader"
    assert frozen["automatic_model_route"] is True
    delegated_model = frozen["resident_task_context"]["message"]["options"]["model_ref"]
    assert delegated_model == (
        "selected/text-one" if selection == "explicit-task-context" else "selected/image-reader"
    )
    assert image_urls(observed[1])
    assert json.loads(server.services.user_repo.get(owner).preferences_json)["preferred_model"] == (
        "selected/text-one"
    )
    assert server.services.agent_repo.get(binding["agent_id"]).default_model == (
        "selected/text-one" if selection == "pin" else None
    )
    changed = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_model": "selected/text-two"}
    )
    assert changed.status_code == 200, changed.text
    if selection == "pin":
        server.services.agent_repo.update_config(
            binding["agent_id"], default_model="selected/text-two"
        )
    followup = {"request_id": uuid4().hex, "prompt": "What color was it?"}
    sent = await client.post(base + "/messages", headers=auth, json=followup)
    assert sent.status_code == 202, sent.text
    await _turn(client, auth, base, followup["request_id"], "completed")
    followup_receipt = server.services.resident_request_repo.get(owner, followup["request_id"])
    assert json.loads(followup_receipt.configuration_json)["model_ref"] == "selected/image-reader"
    assert image_urls(observed[2]) == image_urls(observed[1])
    repeated = await client.post(base + "/messages", headers=auth, json=text_body)
    assert repeated.status_code == 202 and repeated.json() == text_turn
    assert server.services.resident_request_repo.get(owner, text_body["request_id"]) == text_receipt


async def test_imported_private_image_context_also_upgrades_a_text_followup(
    env_with_provider, real_model, monkeypatch
):
    from octop.infra.history import migration_source

    client, server, auth = env_with_provider
    observed = resident_model(real_model, monkeypatch, [AIMessage(content="Still blue.")])
    await create_provider(
        client,
        auth,
        name="selected",
        base_url="https://selected.example/v1",
        models=[{"id": "text", "input": ["text"]}],
    )
    changed = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_model": "selected/text"}
    )
    assert changed.status_code == 200, changed.text
    binding = await _binding(client, auth)
    image_url = "data:image/png;base64," + base64.b64encode(image_bytes()).decode()
    wires = [
        message_to_dict(
            HumanMessage(content=[{"type": "image_url", "image_url": {"url": image_url}}])
        ),
        message_to_dict(AIMessage(content="A blue image.")),
    ]

    async def read_source(*_args, **_kwargs):
        return migration_source.SourceSnapshot(wires, "imported-image-history", {}, [], [])

    monkeypatch.setattr(migration_source, "read_source", read_source)
    request_id = uuid4().hex
    sent = await client.post(
        _base(binding) + "/messages",
        headers=auth,
        json={"request_id": request_id, "prompt": "What color was the previous image?"},
    )
    assert sent.status_code == 202, sent.text
    turn = await _turn(client, auth, _base(binding), request_id, "completed")
    assert turn["attachments"] == []
    assert image_urls(observed[0]) == [image_url]
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    receipt = server.services.resident_request_repo.get(owner, request_id)
    assert json.loads(receipt.configuration_json)["model_ref"] == "openai/gpt-4o"


async def test_missing_vision_candidate_rejects_without_binding_private_image(env_with_provider):
    client, server, auth = env_with_provider
    await create_provider(
        client,
        auth,
        name="selected",
        base_url="https://selected.example/v1",
        models=[{"id": "text", "input": ["text"]}],
    )
    provider = server.services.provider_repo.get_by_name("openai")
    server.services.provider_repo.update(provider.id, enabled=False)
    changed = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_model": "selected/text"}
    )
    assert changed.status_code == 200, changed.text
    binding = await _binding(client, auth)
    image = await upload(client, auth)
    request_id = uuid4().hex
    sent = await client.post(
        _base(binding) + "/messages",
        headers=auth,
        json={"request_id": request_id, "attachment_ids": [image["attachment_id"]]},
    )
    assert sent.status_code == 422 and sent.json()["error"]["code"] == "TASK_IMAGE_MODEL_REQUIRED"
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    assert server.services.resident_request_repo.get(owner, request_id) is None
    with server.services.db.read_snapshot() as conn:
        assert (
            server.services.project_task_attachment_repo.get(
                conn, owner, image["attachment_id"]
            ).task_id
            is None
        )


async def test_image_admission_is_owner_scoped_and_rejected_inputs_remain_unbound(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    other_auth = await create_user(client, auth, username="private-image-other")
    other_image = await upload(client, other_auth)
    image = await upload(client, auth)

    async def stream(_message):
        yield {"type": "token", "content": "Image inspected."}

    patch_resident_stream(monkeypatch, stream)
    for headers in (other_auth, {}):
        response = await client.post(
            base + "/messages",
            headers=headers,
            json={
                "request_id": uuid4().hex,
                "prompt": "Inspect",
                "attachment_ids": [image["attachment_id"]],
            },
        )
        assert response.status_code in {401, 403, 404}
        preview = await client.get(image["preview_url"], headers=headers)
        assert preview.status_code in {401, 403, 404}
    rejected = await client.post(
        base + "/messages",
        headers=auth,
        json={
            "request_id": uuid4().hex,
            "prompt": "Inspect",
            "attachment_ids": [other_image["attachment_id"]],
        },
    )
    assert rejected.status_code == 404, rejected.text
    duplicate_id = uuid4().hex
    rejected = await client.post(
        base + "/messages",
        headers=auth,
        json={
            "request_id": duplicate_id,
            "prompt": "Inspect",
            "attachment_ids": [image["attachment_id"], image["attachment_id"]],
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert server.services.resident_request_repo.list(binding["thread_id"], owner) == []
    with server.services.db.read_snapshot() as conn:
        assert (
            server.services.project_task_attachment_repo.get(
                conn, owner, image["attachment_id"]
            ).task_id
            is None
        )
    sent = await client.post(
        base + "/messages",
        headers=auth,
        json={
            "request_id": uuid4().hex,
            "prompt": "Inspect",
            "attachment_ids": [image["attachment_id"]],
        },
    )
    assert sent.status_code == 202, sent.text
    await _turn(client, auth, base, sent.json()["request_id"], "completed")
    # Private images are not transferable into ordinary public tasks, even by their owner.
    rejected = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": uuid4().hex,
            "prompt": "Inspect",
            "attachment_ids": [image["attachment_id"]],
        },
    )
    assert rejected.status_code == 404, rejected.text


async def test_nonvision_model_and_busy_turn_do_not_consume_uploaded_images(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    image = await upload(client, auth)
    registry = server.app_runtime.agent_registry
    with monkeypatch.context() as limited:
        limited.setattr(registry.providers, "is_model_ref_multimodal", lambda _ref: False)
        rejected = await client.post(
            base + "/messages",
            headers=auth,
            json={"request_id": uuid4().hex, "attachment_ids": [image["attachment_id"]]},
        )
    assert (
        rejected.status_code == 422
        and rejected.json()["error"]["code"] == "TASK_IMAGE_MODEL_REQUIRED"
    ), rejected.text
    assert server.services.resident_request_repo.list(binding["thread_id"], owner) == []
    with server.services.db.read_snapshot() as conn:
        assert (
            server.services.project_task_attachment_repo.get(
                conn, owner, image["attachment_id"]
            ).task_id
            is None
        )
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(_message):
        entered.set()
        await release.wait()
        yield {"type": "token", "content": "Done."}

    patch_resident_stream(monkeypatch, stream)
    active = await client.post(
        base + "/messages", headers=auth, json={"request_id": uuid4().hex, "prompt": "Wait."}
    )
    assert active.status_code == 202, active.text
    try:
        await asyncio.wait_for(entered.wait(), 5)
        rejected = await client.post(
            base + "/messages",
            headers=auth,
            json={
                "request_id": uuid4().hex,
                "prompt": "Inspect",
                "attachment_ids": [image["attachment_id"]],
            },
        )
        assert rejected.status_code == 409, rejected.text
        with server.services.db.read_snapshot() as conn:
            assert (
                server.services.project_task_attachment_repo.get(
                    conn, owner, image["attachment_id"]
                ).task_id
                is None
            )
    finally:
        release.set()
    await _turn(client, auth, base, active.json()["request_id"], "completed")


async def test_expired_images_and_legacy_runtime_are_rejected_before_admission(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    owner = server.services.agent_repo.get(binding["agent_id"]).user_id
    image = await upload(client, auth)
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_attachments SET created_at=0 WHERE attachment_id=?",
            (image["attachment_id"],),
        )
    rejected = await client.post(
        base + "/messages",
        headers=auth,
        json={"request_id": uuid4().hex, "attachment_ids": [image["attachment_id"]]},
    )
    assert rejected.status_code == 404, rejected.text
    image = await upload(client, auth)
    with monkeypatch.context() as legacy:
        legacy.setattr(server.app_runtime.resident_conversations.gateway, "_task_worker", None)
        rejected = await client.post(
            base + "/messages",
            headers=auth,
            json={"request_id": uuid4().hex, "attachment_ids": [image["attachment_id"]]},
        )
    assert (
        rejected.status_code == 422 and rejected.json()["error"]["code"] == "RESIDENT_TEXT_ONLY"
    ), rejected.text
    assert server.services.resident_request_repo.list(binding["thread_id"], owner) == []
    with server.services.db.read_snapshot() as conn:
        assert (
            server.services.project_task_attachment_repo.get(
                conn, owner, image["attachment_id"]
            ).task_id
            is None
        )


async def test_concurrent_image_replay_uses_original_receipt_before_revalidating_model(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    base = _base(binding)
    resident = server.app_runtime.resident_conversations
    await resident.close()
    image = await upload(client, auth)
    body = {
        "request_id": uuid4().hex,
        "prompt": "Inspect",
        "attachment_ids": [image["attachment_id"]],
    }
    first_prepared, second_prepared = asyncio.Event(), asyncio.Event()
    release_first, release_second = Event(), Event()
    loop = asyncio.get_running_loop()
    configuration = resident._configuration
    calls = count()

    def prepare(owner_id, agent_id):
        index = next(calls)
        frozen = configuration(owner_id, agent_id)
        prepared, release = (
            (first_prepared, release_first) if index == 0 else (second_prepared, release_second)
        )
        if index:
            frozen["model_ref"] = "openai/text-only"
        loop.call_soon_threadsafe(prepared.set)
        assert release.wait(30), "test did not release the configuration barrier"
        return frozen

    monkeypatch.setattr(resident, "_configuration", prepare)
    bind = Mock(wraps=input_attachments.bind)
    monkeypatch.setattr(input_attachments, "bind", bind)
    first = asyncio.create_task(client.post(base + "/messages", headers=auth, json=body))
    second = None
    try:
        await asyncio.wait_for(first_prepared.wait(), 10)
        second = asyncio.create_task(client.post(base + "/messages", headers=auth, json=body))
        await asyncio.wait_for(second_prepared.wait(), 10)
        # Both sends read no receipt before either enters the admission transaction.
        release_first.set()
        accepted = await first
        assert accepted.status_code == 202, accepted.text
        release_second.set()
        replay = await second
        assert replay.status_code == 202, replay.text
        assert replay.json() == accepted.json()
        assert bind.call_count == 1
        owner = server.services.agent_repo.get(binding["agent_id"]).user_id
        receipt = server.services.resident_request_repo.get(owner, body["request_id"])
        assert json.loads(receipt.configuration_json)["model_ref"] != "openai/text-only"
        assert len(server.services.resident_request_repo.list(binding["thread_id"], owner)) == 1
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None), return_exceptions=True
        )
