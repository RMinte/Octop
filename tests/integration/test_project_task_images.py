"""Task image inputs survive transport, history, retries and native checkpoints."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import replace

import pytest
from PIL import Image

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import runtime, tasks
from tests.integration.test_project_task_artifacts import real_model as real_model
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_client_tools import CLIENT, OPTIONS, claim, result
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks import task_env as task_env
from tests.integration.test_project_tasks_ws import command, connected, until
from tests.support.auth import create_provider, create_user, resolve_user_id


def image_bytes(format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color=(30, 90, 180)).save(output, format=format)
    return output.getvalue()


async def upload(client, auth, *, content=None, filename="截图.png", media_type="image/png"):
    response = await client.post(
        "/api/project-tasks/attachments",
        headers=auth,
        files={"file": (filename, content if content is not None else image_bytes(), media_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def image_urls(messages):
    from langchain_core.messages import HumanMessage

    return [
        block["image_url"]["url"]
        for message in messages
        if isinstance(message, HumanMessage) and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]


def assert_durable_references(server, task_id, attachment_id):
    context = server.services.project_task_turn_repo.context(task_id)
    if context:
        assert f"task-image://{attachment_id}" in context[1]
        assert "data:image/" not in context[1]
    root = server.services.paths.root / "project-tasks" / task_id
    persisted = [path.read_bytes() for path in root.rglob("*") if path.is_file()]
    assert persisted
    assert any(f"task-image://{attachment_id}".encode() in content for content in persisted)
    assert all(b"data:image/" not in content for content in persisted)


@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_image_only_create_and_followup_restore_input_metadata(
    env_with_provider, real_model, transport
):
    from langchain_core.messages import AIMessage

    client, server, auth = env_with_provider
    experts = server.services.agent_repo.list_all()
    observed, _ = real_model([AIMessage(content="A blue image"), AIMessage(content="Same image")])
    content = image_bytes()
    image = await upload(client, auth, content=content)
    assert set(image) == {
        "attachment_id",
        "name",
        "media_type",
        "byte_count",
        "sha256",
        "preview_url",
        "workspace_path",
        "created_at",
    }
    assert image["name"] == "截图.png" and image["media_type"] == "image/png"
    assert image["byte_count"] == len(content)
    assert image["sha256"] == hashlib.sha256(content).hexdigest()
    body = {
        "request_id": "image-first-001",
        "prompt": "",
        "attachment_ids": [image["attachment_id"]],
    }
    session = None
    try:
        if transport == "http":
            response = await client.post("/api/project-tasks", headers=auth, json=body)
            assert response.status_code == 202, response.text
            task = response.json()
        else:
            session, ws = await connected(client, auth)
            response = await command(ws, "task.create", "image-create-001", data=body)
            assert response["type"] == "result", response
            task = response["data"]
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "completed", done
        assert done["turns"][0]["prompt"] == ""
        assert done["turns"][0]["attachments"] == [image]
        assert base64.b64decode(image_urls(observed[0])[0].split(",", 1)[1]) == content
        followup = {"request_id": "image-followup-001", "prompt": "What color was it?"}
        response = await client.post(
            f"/api/project-tasks/{task['task_id']}/messages", headers=auth, json=followup
        )
        assert response.status_code == 202, response.text
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "completed"
        assert len(observed) == 2 and image_urls(observed[1]) == image_urls(observed[0])
        assert done["turns"][1]["attachments"] == []
        assert "data:image/" not in json.dumps(done)
        assert_durable_references(server, task["task_id"], image["attachment_id"])
        if session is not None:
            await ws.send_json(
                {"type": "task.subscribe", "id": "image-watch-001", "task_id": task["task_id"]}
            )
            snapshot = await until(ws, lambda frame: frame["type"] == "snapshot")
            assert snapshot["data"]["turns"][0]["attachments"] == [image]
        preview = await client.get(image["preview_url"], headers=auth)
        assert preview.status_code == 200 and preview.content == content
        assert preview.headers["content-type"].startswith("image/png")
        assert preview.headers["x-content-type-options"] == "nosniff"
        assert server.services.agent_repo.list_all() == experts
    finally:
        if session is not None:
            await session.__aexit__(None, None, None)


async def test_images_survive_project_working_directory_change(
    env_with_provider, tmp_path, real_model
):
    from langchain_core.messages import AIMessage

    client, server, auth = env_with_provider
    observed, _ = real_model(
        [AIMessage(content="First answer"), AIMessage(content="Still visible")]
    )
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    project_response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": "image-project-001"},
        json={"name": "Images", "directory": str(first)},
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    image = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "project_id": project["project_id"],
            "request_id": "project-image-001",
            "prompt": "Describe this",
            "attachment_ids": [image["attachment_id"]],
        },
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    changed = await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={"expected_revision": project["revision"], "directory": str(second)},
    )
    assert changed.status_code == 200, changed.text
    response = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={"request_id": "project-image-002", "prompt": "Look at the original again"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed"
    assert done["turns"][1]["project_revision"] == changed.json()["revision"]
    assert image_urls(observed[1]) == image_urls(observed[0])
    assert (await client.get(image["preview_url"], headers=auth)).content == image_bytes()
    assert_durable_references(server, task["task_id"], image["attachment_id"])


async def test_read_task_image_restores_bytes_after_image_blocks_are_compacted(
    env_with_provider, real_model
):
    from langchain_core.messages import AIMessage, ToolMessage, message_to_dict, messages_from_dict

    client, server, auth = env_with_provider
    image = await upload(client, auth)
    observed, names = real_model(
        [
            AIMessage(content="Image received."),
            AIMessage(
                content="",
                tool_calls=[{"id": "list-compacted-images", "name": "read_task_image", "args": {}}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "reread-compacted-image",
                        "name": "read_task_image",
                        "args": {"attachment_id": image["attachment_id"]},
                    }
                ],
            ),
            AIMessage(content="The original picture is blue."),
        ]
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "compacted-image-first",
            "attachment_ids": [image["attachment_id"]],
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    assert task["status"] == "completed"
    # Simulate a compacted durable context: the prior image block is gone and
    # only its opaque reference survives in the conversation summary.
    summary = AIMessage(
        content=f"Conversation summary: the user uploaded task-image://{image['attachment_id']}."
    )
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_contexts SET messages_json=? WHERE task_id=?",
            (json.dumps([message_to_dict(summary)]), task["task_id"]),
        )
    context = server.services.project_task_turn_repo.context(task["task_id"])
    assert context and image_urls(messages_from_dict(json.loads(context[1]))) == []
    response = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={
            "request_id": "compacted-image-second",
            "options": {"action_timeline_enabled": False},
            "prompt": "Read the original upload again. What color is it?",
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed", done
    assert "read_task_image" in names and len(observed) == 4
    assert image_urls(observed[1]) == image_urls(observed[2]) == []
    assert image_urls(observed[3]) == image_urls(observed[0])
    assert base64.b64decode(image_urls(observed[3])[0].split(",", 1)[1]) == image_bytes()
    tool_messages = [message for message in observed[3] if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 2
    assert tool_messages[0].tool_call_id == "list-compacted-images"
    assert image["attachment_id"] in str(tool_messages[0].content)
    assert image["name"] in str(tool_messages[0].content)
    assert tool_messages[1].tool_call_id == "reread-compacted-image"
    assert f"task-image://{image['attachment_id']}" in str(tool_messages[1].content)
    assert all("data:image/" not in str(message.content) for message in tool_messages)
    assert_durable_references(server, task["task_id"], image["attachment_id"])


async def test_attachment_ownership_binding_and_idempotency(task_env):
    client, _, auth, calls = task_env
    image = await upload(client, auth)
    another = await upload(client, auth, filename="another.png")
    alice = await create_user(client, auth, username="image-alice")
    assert (await client.get(image["preview_url"], headers=alice)).status_code == 404
    foreign = await client.post(
        "/api/project-tasks",
        headers=alice,
        json={"request_id": "foreign-image-001", "attachment_ids": [image["attachment_id"]]},
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "TASK_ATTACHMENT_UNAVAILABLE"
    body = {
        "request_id": "bound-image-001",
        "prompt": "Describe",
        "attachment_ids": [image["attachment_id"]],
    }
    first = await client.post("/api/project-tasks", headers=auth, json=body)
    assert first.status_code == 202, first.text
    task = await settled(client, auth, first.json()["task_id"])
    again = await client.post("/api/project-tasks", headers=auth, json=body)
    assert again.status_code == 202 and again.json()["task_id"] == task["task_id"]
    changed = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={**body, "attachment_ids": [another["attachment_id"]]},
    )
    assert changed.status_code == 409
    cross_task = await client.post(
        "/api/project-tasks", headers=auth, json={**body, "request_id": "cross-task-image-001"}
    )
    assert cross_task.status_code == 404
    assert cross_task.json()["error"]["code"] == "TASK_ATTACHMENT_UNAVAILABLE"
    same_task = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={**body, "request_id": "same-task-image-001"},
    )
    assert same_task.status_code == 202, same_task.text
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][1]["attachments"] == [image] and len(calls) == 2
    # An unsuccessful idempotency conflict must not claim the other image.
    independent = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "unused-image-001", "attachment_ids": [another["attachment_id"]]},
    )
    assert independent.status_code == 202, independent.text


@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_nine_images_keep_order_and_reordered_replay_conflicts(task_env, transport):
    client, server, auth, calls = task_env
    images = [await upload(client, auth, filename=f"image-{i}.png") for i in range(10)]
    all_ids = [image["attachment_id"] for image in images]
    invalid = {"request_id": "ten-images-001", "attachment_ids": all_ids}
    # Bypass the request model once to prove the domain also rejects ten real
    # uploads and rolls back task creation and every attachment binding.
    owner = await resolve_user_id(client, auth, "admin")
    with pytest.raises(OctopError) as rejected:
        tasks.send(server.services, owner, tasks.TaskMessage.model_construct(**invalid))
    assert rejected.value.code == ErrorCode.TASK_ATTACHMENT_LIMIT
    response = await client.post("/api/project-tasks", headers=auth, json=invalid)
    assert response.status_code == 422, response.text
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []
    body = {
        "request_id": "nine-images-001",
        "prompt": "Compare these nine pictures",
        "attachment_ids": all_ids[:9],
    }
    session, ws = await connected(client, auth)
    try:
        rejected = await command(ws, "task.create", "ten-images-create", data=invalid)
        assert rejected["type"] == "error" and rejected["status"] == 422
        if transport == "http":
            response = await client.post("/api/project-tasks", headers=auth, json=body)
            assert response.status_code == 202, response.text
            created = response.json()
        else:
            response = await command(ws, "task.create", "nine-images-create", data=body)
            assert response["type"] == "result", response
            created = response["data"]
        task = await settled(client, auth, created["task_id"])
        assert task["turns"][0]["attachments"] == images[:9]
        response = await client.post(
            "/api/project-tasks",
            headers=auth,
            json={**body, "attachment_ids": list(reversed(body["attachment_ids"]))},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TASK_REQUEST_CONFLICT" and len(calls) == 1
        response = await command(
            ws,
            "task.message",
            "image-ws-followup-001",
            task_id=task["task_id"],
            data={
                "request_id": "image-ws-followup-001",
                "attachment_ids": body["attachment_ids"],
            },
        )
        assert response["type"] == "result", response
        done = await settled(client, auth, task["task_id"])
        assert done["turns"][1]["attachments"] == images[:9] and len(calls) == 2
        # The tenth upload remains unbound after both oversized requests and
        # can still be used by a separate task.
        response = await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "tenth-image-standalone", "attachment_ids": all_ids[9:]},
        )
        assert response.status_code == 202, response.text
    finally:
        await session.__aexit__(None, None, None)


async def test_real_harness_receives_all_nine_input_images(env_with_provider, real_model):
    from langchain_core.messages import AIMessage

    client, server, auth = env_with_provider
    observed, _ = real_model([AIMessage(content="All nine images are visible.")])
    contents = []
    images = []
    for index in range(9):
        output = io.BytesIO()
        Image.new("RGB", (3, 2), color=(index * 20, 90, 180)).save(output, format="PNG")
        contents.append(output.getvalue())
        images.append(
            await upload(client, auth, content=contents[-1], filename=f"image-{index}.png")
        )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "nine-real-images-001",
            "attachment_ids": [image["attachment_id"] for image in images],
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "completed", done
    assert done["turns"][0]["attachments"] == images
    assert len(observed) == 1
    urls = image_urls(observed[0])
    assert len(urls) == 9
    assert [base64.b64decode(url.split(",", 1)[1]) for url in urls] == contents
    assert_durable_references(server, done["task_id"], images[0]["attachment_id"])


async def test_pending_image_expiry_and_task_deletion_remove_access(task_env):
    client, server, auth, _ = task_env
    expired = await upload(client, auth)
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_attachments SET created_at=0 WHERE attachment_id=?",
            (expired["attachment_id"],),
        )
    assert (await client.get(expired["preview_url"], headers=auth)).status_code == 404
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "expired-image-001", "attachment_ids": [expired["attachment_id"]]},
    )
    assert response.status_code == 404
    image = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "durable-image-001", "attachment_ids": [image["attachment_id"]]},
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_attachments SET created_at=0 WHERE attachment_id=?",
            (image["attachment_id"],),
        )
    assert (await client.get(image["preview_url"], headers=auth)).status_code == 200
    response = await client.delete(f"/api/project-tasks/{task['task_id']}", headers=auth)
    assert response.status_code == 204, response.text
    assert (await client.get(image["preview_url"], headers=auth)).status_code == 404


async def test_failed_image_turn_retry_reuses_original_image(task_env, monkeypatch):
    client, _, auth, _ = task_env
    captured = []

    async def run(services, owner, turn, history):
        captured.append(turn)
        if len(captured) == 1:
            raise RuntimeError("simulated provider failure")
        yield {"type": "token", "content": "Retried"}

    monkeypatch.setattr(runtime, "run_turn", run)
    image = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "retry-image-first", "attachment_ids": [image["attachment_id"]]},
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    failed = task["turns"][0]
    assert failed["status"] == "failed"
    path = f"/api/project-tasks/{task['task_id']}/turns/{failed['turn_id']}/retry"
    response = await client.post(path, headers=auth, json={"request_id": "retry-image-second"})
    assert response.status_code == 202, response.text
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "completed"
    assert done["turns"][1]["attachments"] == [image]
    assert done["turns"][1]["prompt"] == ""
    assert done["turns"][1]["retry_of_turn_id"] == failed["turn_id"]
    response = await client.post(path, headers=auth, json={"request_id": "retry-image-second"})
    assert response.status_code == 202 and len(captured) == 2


async def test_routed_nonvision_model_rejected_for_new_and_historical_images(task_env):
    client, _, auth, calls = task_env
    await create_provider(
        client,
        auth,
        name="text-only",
        base_url="https://example.invalid/v1",
        models=[{"id": "plain-chat", "enabled": True, "input": ["text"]}],
    )
    image = await upload(client, auth)
    body = {"request_id": "text-model-image", "attachment_ids": [image["attachment_id"]]}
    rejected = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            **body,
            "options": {"model_ref": "text-only/plain-chat", "routing": {"mode": "box"}},
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "TASK_IMAGE_MODEL_REQUIRED"
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []
    accepted = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={**body, "options": {"model_ref": "openai/gpt-4o"}},
    )
    assert accepted.status_code == 202, accepted.text
    task = await settled(client, auth, accepted.json()["task_id"])
    rejected = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={
            "request_id": "text-model-history",
            "prompt": "Remember the image",
            "options": {"model_ref": "text-only/plain-chat", "routing": {"mode": "box"}},
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "TASK_IMAGE_MODEL_REQUIRED" and len(calls) == 1


@pytest.mark.parametrize(
    "format,media_type",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
async def test_task_upload_accepts_real_supported_images(env, format, media_type):
    client, _, auth = env
    content = image_bytes(format)
    image = await upload(
        client, auth, content=content, filename=f"image.{format.lower()}", media_type=media_type
    )
    assert image["media_type"] == media_type
    assert (await client.get(image["preview_url"], headers=auth)).content == content


async def test_invalid_images_limits_and_message_references(task_env, monkeypatch):
    client, server, auth, calls = task_env
    for content, filename, media_type in [
        (b"not an image", "fake.png", "image/png"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "disguised.png", "image/png"),
    ]:
        response = await client.post(
            "/api/project-tasks/attachments",
            headers=auth,
            files={"file": (filename, content, media_type)},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "TASK_IMAGE_INVALID"
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, max_upload_mb=1)),
    )
    too_large = await client.post(
        "/api/project-tasks/attachments",
        headers=auth,
        files={"file": ("large.png", image_bytes() + b"\0" * (1024 * 1024), "image/png")},
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "TASK_ATTACHMENT_LIMIT"
    image = await upload(client, auth)
    for body in [
        {"request_id": "empty-message-001", "prompt": "  "},
        {"request_id": "duplicate-image-001", "attachment_ids": [image["attachment_id"]] * 2},
        {"request_id": "too-many-images-001", "attachment_ids": [f"image-{i}" for i in range(10)]},
    ]:
        response = await client.post("/api/project-tasks", headers=auth, json=body)
        assert response.status_code == 422, response.text
    missing = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "missing-image-001", "attachment_ids": ["image-does-not-exist"]},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_ATTACHMENT_UNAVAILABLE"
    assert calls == []


async def test_image_checkpoint_resumes_without_persisting_data_uri(
    env_with_provider, real_model, monkeypatch
):
    from langchain_core.messages import AIMessage

    from octop.api.routers import project_tasks_ws

    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    observed, _ = real_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "image-read-note",
                        "name": "client__read_note",
                        "args": {"path": "note.md"},
                    }
                ],
            ),
            AIMessage(content="Image and note considered"),
        ]
    )
    image = await upload(client, auth)
    session, ws = await connected(client, auth)
    try:
        await command(ws, "client.attach", "image-attach-client", client_id=CLIENT)
        created = await command(
            ws,
            "task.create",
            "image-create-wait",
            data={
                "request_id": "image-checkpoint-001",
                "attachment_ids": [image["attachment_id"]],
                "options": {**OPTIONS, "action_timeline_enabled": False},
            },
        )
        assert created["type"] == "result", created
        task = created["data"]
        call = (await until(ws, lambda frame: frame["type"] == "tool.call"))["data"]
        await parked(server, call["turn_id"])
        assert_durable_references(server, task["task_id"], image["attachment_id"])
        await server.project_task_worker.close()
        token = (await claim(ws, call))["data"]["claim_token"]
        assert (await result(ws, call, token, {"note": "Actual client note"}))["type"] == "result"
        server.project_task_worker = runtime.ProjectTaskWorker(server.services)
        await server.project_task_worker.start()
        done = await settled(client, auth, task["task_id"])
        assert done["status"] == "completed", done
        assert len(done["turns"]) == 1 and done["turns"][0]["attachments"] == [image]
        assert len(observed) == 2 and image_urls(observed[1]) == image_urls(observed[0])
        assert_durable_references(server, task["task_id"], image["attachment_id"])
    finally:
        await session.__aexit__(None, None, None)
