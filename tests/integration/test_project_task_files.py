"""Task files reach real workspace tools while immutable uploads remain downloadable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from octop.infra.projects import input_attachments
from tests.integration.test_project_task_artifacts import real_model as real_model
from tests.integration.test_project_task_images import image_bytes
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks import task_env as task_env
from tests.integration.test_project_tasks_ws import command, connected
from tests.support.auth import create_provider, create_user, resolve_user_id


async def upload(
    client, auth, name="报告.md", data=b"uploaded-file-proof", media_type="text/markdown"
):
    response = await client.post(
        "/api/project-tasks/attachments",
        headers=auth,
        files={"file": (name, data, media_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def text_provider(client, auth):
    await create_provider(
        client,
        auth,
        name="text-only",
        base_url="https://example.invalid/v1",
        models=[{"id": "plain-chat", "enabled": True, "input": ["text"]}],
    )


def read_message(path, call_id):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "read_file", "args": {"file_path": "/" + path}}],
    )


def tool_result(messages, call_id):
    from langchain_core.messages import ToolMessage

    return next(
        str(message.content)
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id == call_id
    )


@pytest.mark.parametrize(
    "name,declared,stored,data",
    [
        ("报告.pdf", "application/pdf", "application/pdf", b"%PDF-1.7\nfile-proof"),
        (
            "报告.docx",
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04document-proof",
        ),
        (
            "数据.xlsx",
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b"PK\x03\x04spreadsheet-proof",
        ),
        ("archive.zip", "application/zip", "application/zip", b"PK\x03\x04archive-proof"),
        ("vector.svg", "image/svg+xml", "image/svg+xml", b"<svg/>"),
        ("unknown.binaryblob", "application/octet-stream", "application/octet-stream", b"\0\xff"),
        ("empty.txt", "text/plain", "text/plain", b""),
    ],
)
async def test_generic_upload_metadata_and_safe_download(env, name, declared, stored, data):
    client, _, auth = env
    attachment = await upload(client, auth, name, data, declared)
    assert attachment["media_type"] == stored
    assert attachment["workspace_path"] == f"inbound/{attachment['attachment_id']}/{name}"
    assert attachment["byte_count"] == len(data)
    assert attachment["sha256"] == hashlib.sha256(data).hexdigest()
    response = await client.get(attachment["preview_url"], headers=auth)
    assert response.status_code == 200 and response.content == data
    assert response.headers["content-type"].startswith(stored)
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


async def test_filename_sanitization_and_real_image_detection(env):
    client, _, auth = env
    attachment = await upload(client, auth, "../../报告.md", media_type="application/octet-stream")
    assert attachment["name"] == "报告.md"
    assert attachment["media_type"] == "text/markdown"
    assert attachment["workspace_path"] == f"inbound/{attachment['attachment_id']}/报告.md"
    image = await upload(client, auth, "opaque.bin", image_bytes(), "application/octet-stream")
    assert image["media_type"] == "image/png" and image["workspace_path"] is None
    response = await client.get(image["preview_url"], headers=auth)
    assert response.headers["content-disposition"].startswith("inline;")


@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_file_only_task_reaches_real_tools_without_vision_model(
    env_with_provider, real_model, transport
):
    from langchain_core.messages import AIMessage, HumanMessage

    client, server, auth = env_with_provider
    await text_provider(client, auth)
    attachment = await upload(client, auth)
    path = attachment["workspace_path"]
    observed, _ = real_model([read_message(path, "read-upload"), AIMessage(content="Read file")])
    body = {
        "request_id": "only-file-001",
        "prompt": "",
        "attachment_ids": [attachment["attachment_id"]],
        "options": {"model_ref": "text-only/plain-chat", "action_timeline_enabled": False},
    }
    experts = server.services.agent_repo.list_all()
    session = None
    try:
        if transport == "http":
            response = await client.post("/api/project-tasks", headers=auth, json=body)
            assert response.status_code == 202, response.text
            created = response.json()
        else:
            session, ws = await connected(client, auth)
            response = await command(ws, "task.create", "only-file-create", data=body)
            assert response["type"] == "result", response
            created = response["data"]
        done = await settled(client, auth, created["task_id"])
        assert done["status"] == "completed", done
        assert done["turns"][0]["attachments"] == [attachment]
        assert done["turns"][0]["prompt"] == ""
        assert "uploaded-file-proof" in tool_result(observed[-1], "read-upload")
        user = next(message for message in observed[0] if isinstance(message, HumanMessage))
        assert path in str(user.content)
        assert all(block["type"] == "text" for block in user.content)
        assert "task-image://" not in str(user.content)
        workspace = server.services.paths.root / "project-tasks" / done["task_id"] / "workspace"
        assert (workspace / path).read_bytes() == b"uploaded-file-proof"
        assert server.services.agent_repo.list_all() == experts
        assert (
            await client.get(attachment["preview_url"], headers=auth)
        ).content == b"uploaded-file-proof"
    finally:
        if session is not None:
            await session.__aexit__(None, None, None)


async def test_file_followups_preserve_edits_and_restore_after_project_directory_change(
    env_with_provider, tmp_path, real_model
):
    from langchain_core.messages import AIMessage, SystemMessage, message_to_dict

    client, server, auth = env_with_provider
    first, second, client_only = tmp_path / "first", tmp_path / "second", tmp_path / "client"
    first.mkdir()
    second.mkdir()
    project_response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": "file-project-001"},
        json={"name": "Files", "directory": str(first), "client_directory": str(client_only)},
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    attachment = await upload(client, auth)
    path = attachment["workspace_path"]
    observed, _ = real_model(
        [
            read_message(path, "original"),
            AIMessage(content="Original read"),
            read_message(path, "edited"),
            AIMessage(content="Edit kept"),
            read_message(path, "restored"),
            AIMessage(content="Original restored in new workspace"),
        ]
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "file-project-first",
            "options": {"action_timeline_enabled": False},
            "project_id": project["project_id"],
            "attachment_ids": [attachment["attachment_id"]],
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "completed", done
    assert "uploaded-file-proof" in tool_result(observed[1], "original")
    (first / path).write_text("working-edit-proof", encoding="utf8")
    followup = f"/api/project-tasks/{done['task_id']}/messages"
    response = await client.post(
        followup,
        headers=auth,
        json={
            "request_id": "file-project-edited",
            "prompt": "Read it again",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, done["task_id"])
    assert done["status"] == "completed", done
    assert "working-edit-proof" in tool_result(observed[3], "edited")
    summary = AIMessage(content="The original document was uploaded and reviewed.")
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_contexts SET messages_json=? WHERE task_id=?",
            (json.dumps([message_to_dict(summary)]), done["task_id"]),
        )
    changed = await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={"expected_revision": project["revision"], "directory": str(second)},
    )
    assert changed.status_code == 200, changed.text
    response = await client.post(
        followup,
        headers=auth,
        json={
            "request_id": "file-project-restored",
            "prompt": "Read the original file",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, done["task_id"])
    assert done["status"] == "completed", done
    assert "uploaded-file-proof" in tool_result(observed[5], "restored")
    assert any(
        path in str(message.content)
        for message in observed[4]
        if isinstance(message, SystemMessage)
    )
    assert (first / path).read_text(encoding="utf8") == "working-edit-proof"
    assert (second / path).read_bytes() == b"uploaded-file-proof"
    assert not client_only.exists()
    assert done["turns"][0]["attachments"] == [attachment]
    assert done["turns"][1]["attachments"] == done["turns"][2]["attachments"] == []
    assert (
        await client.get(attachment["preview_url"], headers=auth)
    ).content == b"uploaded-file-proof"
    context = server.services.project_task_turn_repo.context(done["task_id"])
    assert context is not None and path in str(json.loads(context[1]))


async def test_routed_files_and_images_keep_vision_gate(task_env):
    client, _, auth, calls = task_env
    await text_provider(client, auth)
    document = await upload(client, auth)
    image = await upload(client, auth, "image.png", image_bytes(), "image/png")
    body = {
        "request_id": "mixed-input-001",
        "attachment_ids": [document["attachment_id"], image["attachment_id"]],
        "options": {"model_ref": "text-only/plain-chat", "routing": {"mode": "box"}},
    }
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TASK_IMAGE_MODEL_REQUIRED"
    assert not calls
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={**body, "options": {"model_ref": "openai/gpt-4o"}},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    assert done["turns"][0]["attachments"] == [document, image]
    content = input_attachments.content(
        "",
        [document["attachment_id"], image["attachment_id"]],
        [input_attachments.TaskInputAttachment.model_validate(item) for item in [document, image]],
    )
    assert [block["type"] for block in content] == ["text", "image_url"]


async def test_file_references_are_owner_and_task_scoped(task_env):
    client, server, auth, calls = task_env
    attachment = await upload(client, auth)
    foreign = await create_user(client, auth, username="file-other")
    assert (await client.get(attachment["preview_url"], headers=foreign)).status_code == 404
    body = {"request_id": "file-binding-001", "attachment_ids": [attachment["attachment_id"]]}
    response = await client.post("/api/project-tasks", headers=foreign, json=body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_ATTACHMENT_UNAVAILABLE"
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 202, response.text
    done = await settled(client, auth, response.json()["task_id"])
    response = await client.post(
        "/api/project-tasks", headers=auth, json={**body, "request_id": "file-binding-other"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_ATTACHMENT_UNAVAILABLE"
    owner = await resolve_user_id(client, auth, "admin")
    with pytest.raises(input_attachments.OctopError):
        input_attachments.download(
            server.services, owner, attachment["attachment_id"], task_id="other"
        )
    assert len(calls) == 1
    response = await client.post(
        f"/api/project-tasks/{done['task_id']}/messages",
        headers=auth,
        json={**body, "request_id": "file-binding-followup"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, done["task_id"])
    assert done["turns"][1]["attachments"] == [attachment]
    assert (await client.get(attachment["preview_url"], headers=auth)).status_code == 200
    assert (
        await client.delete(f"/api/project-tasks/{done['task_id']}", headers=auth)
    ).status_code == 204
    assert (await client.get(attachment["preview_url"], headers=auth)).status_code == 404


async def test_generic_attachment_limits_and_expiry(task_env, monkeypatch):
    client, server, auth, calls = task_env
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, max_upload_mb=1)),
    )
    response = await client.post(
        "/api/project-tasks/attachments",
        headers=auth,
        files={"file": ("large.bin", b"\0" * (1024 * 1024 + 1), "application/octet-stream")},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "TASK_ATTACHMENT_LIMIT"
    assert (
        input_attachments.max_upload_bytes(
            replace(server.services, config=replace(server.services.config, max_upload_mb=100))
        )
        == 25 * 1024 * 1024
    )
    attachment = await upload(client, auth)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "file-duplicates", "attachment_ids": [attachment["attachment_id"]] * 2},
    )
    assert response.status_code == 422
    attachments = [attachment] + [await upload(client, auth, f"file-{n}.txt") for n in range(9)]
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "file-limit",
            "attachment_ids": [a["attachment_id"] for a in attachments],
        },
    )
    assert response.status_code == 422 and not calls
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_attachments SET created_at=0 WHERE attachment_id=?",
            (attachment["attachment_id"],),
        )
    response = await client.get(attachment["preview_url"], headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_ATTACHMENT_UNAVAILABLE"
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "file-expired", "attachment_ids": [attachment["attachment_id"]]},
    )
    assert response.status_code == 404
