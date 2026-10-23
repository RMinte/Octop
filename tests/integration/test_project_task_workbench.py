"""Task workbench HTTP/WS boundaries with real files and no browser, shell or LLM calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from octop.api.app import build_app
from octop.api.routers import project_task_workbench as transport
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks import task_env as task_env
from tests.support.auth import create_user
from tests.support.http import ws_connect, ws_token


@pytest.fixture
async def workbench_env(task_env, tmp_path):
    client, server, auth, calls = task_env
    directory = tmp_path / "project-files"
    directory.mkdir()
    created = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={
            "name": "Task workbench",
            "directory": str(directory),
            "client_directory": r"Z:\user-computer\unavailable",
        },
    )
    assert created.status_code == 201, created.text
    project = created.json()
    response = await client.post(
        f"/api/projects/{project['project_id']}/tasks",
        headers=auth,
        json={"request_id": "workbench-first", "prompt": "Ready"},
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    base = f"/api/project-tasks/{task['task_id']}"
    view = await client.get(base + "/workbench", headers=auth)
    assert view.status_code == 200, view.text
    scope = view.json()
    assert Path(scope["server_directory"]) == directory
    assert scope["client_directory"] == r"Z:\user-computer\unavailable"
    return client, server, auth, task, project, directory, base, scope


async def test_workbench_http_crud_and_content_hash_conflict(workbench_env):
    client, _, auth, _, _, directory, base, scope = workbench_env
    revision = scope["scope_revision"]
    body = {"path": "notes.txt", "content": "first", "expected_scope_revision": revision}
    created = await client.put(
        base + "/workspace/file", headers=auth, json={**body, "expected_revision": "missing"}
    )
    assert created.status_code == 200, created.text
    assert created.json()["revision"] == hashlib.sha256(b"first").hexdigest()
    assert (directory / "notes.txt").read_text() == "first"
    read = await client.get(
        base + "/workspace/file",
        headers=auth,
        params={"path": "notes.txt", "expected_scope_revision": revision},
    )
    assert read.status_code == 200
    assert read.json()["content"] == "first" and read.json()["size"] == 5
    # A model or editor changes the content after this client read its hash.
    (directory / "notes.txt").write_text("model update")
    stale = await client.put(
        base + "/workspace/file",
        headers=auth,
        json={**body, "content": "stale draft", "expected_revision": read.json()["revision"]},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "TASK_REVISION_CONFLICT"
    assert (directory / "notes.txt").read_text() == "model update"
    assert (
        await client.post(
            base + "/workspace/mkdir",
            headers=auth,
            json={"path": "docs", "expected_scope_revision": revision},
        )
    ).status_code == 200
    moved = await client.post(
        base + "/workspace/move",
        headers=auth,
        json={
            "path": "notes.txt",
            "destination": "docs/notes.txt",
            "expected_scope_revision": revision,
        },
    )
    assert moved.status_code == 200, moved.text
    assert not (directory / "notes.txt").exists()
    listing = await client.get(
        base + "/workspace/tree-children", headers=auth, params={"path": "docs"}
    )
    assert listing.status_code == 200
    assert listing.json()[0]["path"] == "docs/notes.txt"
    assert listing.json()[0]["name"] == "notes.txt"
    uploaded = await client.post(
        base + "/workspace/upload",
        headers=auth,
        params={"path": "docs/upload.bin"},
        data={"expected_scope_revision": revision},
        files={"file": ("original.bin", b"abcdefgh", "application/octet-stream")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert (directory / "docs" / "upload.bin").read_bytes() == b"abcdefgh"
    deleted = await client.delete(
        base + "/workspace/file",
        headers=auth,
        params={"path": "docs/notes.txt", "expected_scope_revision": revision},
    )
    assert deleted.status_code == 204, deleted.text
    assert not (directory / "docs" / "notes.txt").exists()


async def test_media_range_and_access_token_authentication(workbench_env):
    client, _, auth, _, _, directory, base, scope = workbench_env
    (directory / "clip.bin").write_bytes(b"abcdefgh")
    params = {
        "path": "clip.bin",
        "expected_scope_revision": scope["scope_revision"],
        "access_token": ws_token(auth),
    }
    response = await client.get(
        base + "/workspace/media", params=params, headers={"Range": "bytes=2-4"}
    )
    assert response.status_code == 206, response.text
    assert response.content == b"cde"
    assert response.headers["content-range"] == "bytes 2-4/8"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "no-store"
    suffix = await client.get(
        base + "/workspace/media", params=params, headers={"Range": "bytes=-2"}
    )
    assert suffix.status_code == 206 and suffix.content == b"gh"
    invalid = await client.get(
        base + "/workspace/media", params=params, headers={"Range": "bytes=99-"}
    )
    assert invalid.status_code == 416 and invalid.headers["content-range"] == "bytes */8"
    download = await client.get(base + "/workspace/download-file", params=params)
    assert download.status_code == 200 and download.content == b"abcdefgh"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert (
        await client.get(base + "/workspace/media", params={"path": "clip.bin"})
    ).status_code == 401


async def test_directory_change_invalidates_old_scope_and_uses_current_project(
    workbench_env, tmp_path
):
    client, _, auth, _, project, directory, base, scope = workbench_env
    (directory / "location.txt").write_text("old directory")
    new_directory = tmp_path / "replacement"
    new_directory.mkdir()
    (new_directory / "location.txt").write_text("new directory")
    patched = await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={"directory": str(new_directory), "expected_revision": project["revision"]},
    )
    assert patched.status_code == 200, patched.text
    stale = await client.put(
        base + "/workspace/file",
        headers=auth,
        json={
            "path": "location.txt",
            "content": "stale",
            "expected_scope_revision": scope["scope_revision"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "TASK_REVISION_CONFLICT"
    assert (directory / "location.txt").read_text() == "old directory"
    assert (new_directory / "location.txt").read_text() == "new directory"
    assert (
        await client.get(
            base + "/workspace/file",
            headers=auth,
            params={"path": "location.txt", "expected_scope_revision": scope["scope_revision"]},
        )
    ).status_code == 409
    fresh = (await client.get(base + "/workbench", headers=auth)).json()
    assert fresh["scope_revision"] != scope["scope_revision"]
    assert Path(fresh["server_directory"]) == new_directory
    read = await client.get(
        base + "/workspace/file",
        headers=auth,
        params={"path": "location.txt", "expected_scope_revision": fresh["scope_revision"]},
    )
    assert read.json()["content"] == "new directory"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        ".octop/secret.txt",
        ".OCTOP/secret.txt",
        "/.octop/secret.txt",
        r"..\outside.txt",
        "C:\\private.txt",
    ],
)
async def test_reserved_and_traversal_paths_cannot_be_read_or_written(workbench_env, path):
    client, _, auth, _, _, _, base, scope = workbench_env
    assert (
        await client.get(base + "/workspace/file", headers=auth, params={"path": path})
    ).status_code == 403
    result = await client.put(
        base + "/workspace/file",
        headers=auth,
        json={"path": path, "content": "bad", "expected_scope_revision": scope["scope_revision"]},
    )
    assert result.status_code == 403, result.text


@pytest.mark.skipif(os.name != "posix", reason="symlink boundary is POSIX-only")
async def test_symlink_cannot_escape_project_directory(workbench_env, tmp_path):
    client, _, auth, _, _, directory, base, scope = workbench_env
    outside = tmp_path / "private-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private")
    (directory / "escape").symlink_to(outside, target_is_directory=True)
    read = await client.get(
        base + "/workspace/file", headers=auth, params={"path": "escape/secret.txt"}
    )
    assert read.status_code == 403, read.text
    write = await client.put(
        base + "/workspace/file",
        headers=auth,
        json={
            "path": "escape/stolen.txt",
            "content": "bad",
            "expected_scope_revision": scope["scope_revision"],
        },
    )
    assert write.status_code == 403, write.text
    assert not (outside / "stolen.txt").exists()
    system = directory / ".octop"
    system.mkdir(exist_ok=True)
    (system / "private.txt").write_text("private system content")
    (directory / "system-alias").symlink_to(system, target_is_directory=True)
    hidden = await client.get(
        base + "/workspace/file", headers=auth, params={"path": "system-alias/private.txt"}
    )
    assert hidden.status_code == 403
    assert (
        await client.get(
            base + "/workspace/tree-children", headers=auth, params={"path": "system-alias"}
        )
    ).status_code == 403


async def test_other_users_cannot_inspect_or_mutate_task_workbench(workbench_env):
    client, _, auth, _, _, directory, base, scope = workbench_env
    other = await create_user(client, auth, username="workbench-other")
    (directory / "secret.txt").write_text("owner-only")
    for suffix in [
        "/workbench",
        "/workspace/tree-children",
        "/workspace/file?path=secret.txt",
        "/workspace/media?path=secret.txt",
        "/browser/sessions",
    ]:
        response = await client.get(base + suffix, headers=other)
        assert response.status_code == 404, response.text
    denied = await client.put(
        base + "/workspace/file",
        headers=other,
        json={
            "path": "secret.txt",
            "content": "stolen",
            "expected_scope_revision": scope["scope_revision"],
        },
    )
    assert denied.status_code == 404
    assert (directory / "secret.txt").read_text() == "owner-only"
    assert (
        await client.delete(base + "/terminal/sessions/shell-a", headers=other)
    ).status_code == 404
    for suffix in ["browser-stream/ws", "terminal/ws"]:
        with pytest.raises(WebSocketDisconnect) as closed:
            async with ws_connect(
                client._octop_app,
                base
                + "/"
                + suffix
                + "?"
                + urlencode(
                    {"token": ws_token(other), "expected_scope_revision": scope["scope_revision"]}
                ),
            ):
                pytest.fail("another user connected to Task workbench")
        assert closed.value.code == 4003


async def test_archived_task_keeps_files_readable_but_blocks_writes(workbench_env):
    client, _, auth, task, _, directory, base, scope = workbench_env
    (directory / "history.txt").write_text("preserved")
    archived = await client.patch(
        base, headers=auth, json={"expected_revision": task["list_revision"], "archived": True}
    )
    assert archived.status_code == 200, archived.text
    assert (await client.get(base + "/workbench", headers=auth)).json()["read_only"] is True
    assert (
        await client.get(base + "/workspace/file", headers=auth, params={"path": "history.txt"})
    ).json()["content"] == "preserved"
    result = await client.put(
        base + "/workspace/file",
        headers=auth,
        json={
            "path": "history.txt",
            "content": "changed",
            "expected_scope_revision": scope["scope_revision"],
        },
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "TASK_ARCHIVED"


async def test_browser_transport_receives_task_profile_and_project_screenshots(
    workbench_env, monkeypatch
):
    client, _, auth, task, _, directory, base, scope = workbench_env
    received = []

    async def fake_browser(**options):
        body = json.loads(await options["receive_text"]())
        received.append(options)
        await options["send_json"](
            {"type": "attached", "profile": options["profile_name"], "url": body.get("url", "")}
        )

    monkeypatch.setattr(transport, "run_browser_stream_session", fake_browser)
    query = urlencode({"token": ws_token(auth), "expected_scope_revision": scope["scope_revision"]})
    async with ws_connect(client._octop_app, base + "/browser-stream/ws?" + query) as ws:
        await ws.send_json({"type": "start", "url": "", "session_id": "foreign-agent-profile"})
        frame = await ws.receive_json()
    expected = (
        "task-"
        + hashlib.sha256(task["task_id"].encode()).hexdigest()[:16]
        + "-"
        + scope["scope_revision"][:24]
    )
    assert frame["profile"] == expected
    assert received[0]["profile_name"] != "foreign-agent-profile"
    assert received[0]["screenshots_dir"] == directory / "outbound" / "screenshots"


async def test_terminal_transport_receives_task_scope_and_cwd(workbench_env, monkeypatch):
    client, _, auth, task, _, directory, base, scope = workbench_env
    received = []

    async def fake_terminal(websocket, key, user, cwd, session_id, cols, rows, *, validate):
        await websocket.accept()
        await websocket.receive_text()
        await validate()
        received.append((key, cwd, session_id, cols, rows))
        await websocket.send_json({"type": "output", "data": "attached"})
        await websocket.close()

    monkeypatch.setattr(transport.terminal, "run_terminal_session", fake_terminal)
    query = urlencode(
        {
            "token": ws_token(auth),
            "expected_scope_revision": scope["scope_revision"],
            "session_id": "shell-a",
            "cols": 0,
            "rows": 9999,
        }
    )
    async with ws_connect(client._octop_app, base + "/terminal/ws?" + query) as ws:
        await ws.send_json({"type": "input", "data": "pwd\n"})
        assert (await ws.receive_json())["data"] == "attached"
    assert received == [
        (f"task:{task['task_id']}:{scope['scope_revision']}", str(directory), "shell-a", 1, 1000)
    ]


@pytest.mark.parametrize("kind", ["browser", "terminal"])
async def test_live_transports_revalidate_scope_before_accepting_input(
    workbench_env, tmp_path, monkeypatch, kind
):
    client, _, auth, _, project, _, base, scope = workbench_env
    applied = []

    async def fake_browser(**options):
        await options["receive_text"]()
        applied.append("browser-action")

    async def fake_terminal(websocket, *args, validate, **kwargs):
        await websocket.accept()
        await websocket.receive_text()
        await validate()
        applied.append("shell-input")

    monkeypatch.setattr(transport, "run_browser_stream_session", fake_browser)
    monkeypatch.setattr(transport.terminal, "run_terminal_session", fake_terminal)
    suffix = "/browser-stream/ws" if kind == "browser" else "/terminal/ws"
    query = urlencode({"token": ws_token(auth), "expected_scope_revision": scope["scope_revision"]})
    replacement = tmp_path / "next-directory"
    replacement.mkdir()
    async with ws_connect(client._octop_app, base + suffix + "?" + query) as ws:
        changed = await client.patch(
            f"/api/projects/{project['project_id']}",
            headers=auth,
            json={"directory": str(replacement), "expected_revision": project["revision"]},
        )
        assert changed.status_code == 200, changed.text
        await ws.send_json({"type": "input", "data": "write old scope"})
        with pytest.raises(WebSocketDisconnect):
            await ws.receive_json()
    assert applied == []


async def test_workbench_docs_and_openapi_use_readable_task_scoped_models(
    workbench_env, monkeypatch
):
    _, server, _, _, _, _, _, _ = workbench_env
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, enable_api_docs=True)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(server)), base_url="http://testserver"
    ) as client:
        docs = await client.get("/api/docs")
        assert docs.status_code == 200 and "/api/openapi.json" in docs.text
        response = await client.get("/api/openapi.json")
        assert response.status_code == 200, response.text
        schema = response.json()
    paths = schema["paths"]
    base = "/api/project-tasks/{task_id}"
    view = paths[base + "/workbench"]["get"]
    assert view["summary"]
    assert view["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/WorkbenchView"
    )
    file = paths[base + "/workspace/file"]
    assert file["put"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/FileWrite"
    )
    assert file["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/WorkspaceFile")
    assert any(
        parameter["name"] == "expected_scope_revision" and parameter["in"] == "query"
        for parameter in file["get"]["parameters"]
    )
    write_schema = schema["components"]["schemas"]["FileWrite"]
    assert "expected_scope_revision" in write_schema["required"]
    assert write_schema["properties"]["expected_scope_revision"]["type"] == "string"
    assert "expected_revision" in write_schema["properties"]


async def test_passive_browser_frames_are_rejected_after_directory_scope_changes(
    workbench_env, tmp_path, monkeypatch
):
    client, _, auth, _, project, _, base, scope = workbench_env
    emit = asyncio.Event()
    attempted = []

    async def fake_browser(**options):
        await options["send_json"]({"type": "ready"})
        await emit.wait()
        attempted.append("old-frame")
        await options["send_json"]({"type": "frame", "data": "old-directory-image"})

    monkeypatch.setattr(transport, "run_browser_stream_session", fake_browser)
    replacement = tmp_path / "passive-browser-next"
    replacement.mkdir()
    query = urlencode({"token": ws_token(auth), "expected_scope_revision": scope["scope_revision"]})
    async with ws_connect(client._octop_app, base + "/browser-stream/ws?" + query) as ws:
        assert await ws.receive_json() == {"type": "ready"}
        changed = await client.patch(
            f"/api/projects/{project['project_id']}",
            headers=auth,
            json={"directory": str(replacement), "expected_revision": project["revision"]},
        )
        assert changed.status_code == 200, changed.text
        emit.set()
        with pytest.raises(WebSocketDisconnect):
            await ws.receive_json()
    assert attempted == ["old-frame"]


async def test_idle_terminal_watcher_closes_only_invalidated_task_scope(
    workbench_env, tmp_path, monkeypatch
):
    client, _, auth, task, project, _, base, scope = workbench_env
    tick = asyncio.Event()
    destroyed = []
    owned = SimpleNamespace(user_id=1)
    unrelated = SimpleNamespace(user_id=1)
    key = f"task:{task['task_id']}:{scope['scope_revision']}"

    async def clock(seconds):
        assert seconds == 2
        await tick.wait()
        tick.clear()

    async def fake_destroy(session, *, cancel_pump):
        assert cancel_pump
        destroyed.append(session)

    async def fake_terminal(websocket, *args, **kwargs):
        await websocket.accept()
        await websocket.receive_text()

    monkeypatch.setattr(
        transport,
        "asyncio",
        SimpleNamespace(
            sleep=clock,
            create_task=asyncio.create_task,
            to_thread=asyncio.to_thread,
            CancelledError=asyncio.CancelledError,
        ),
    )
    monkeypatch.setattr(transport.terminal, "run_terminal_session", fake_terminal)
    monkeypatch.setattr(transport.terminal, "_destroy_session", fake_destroy)
    monkeypatch.setattr(
        transport.terminal,
        "_sessions",
        {(key, "shell-a"): owned, ("task:another-task:scope", "shell-a"): unrelated},
    )
    replacement = tmp_path / "idle-terminal-next"
    replacement.mkdir()
    query = urlencode({"token": ws_token(auth), "expected_scope_revision": scope["scope_revision"]})
    async with ws_connect(client._octop_app, base + "/terminal/ws?" + query) as ws:
        changed = await client.patch(
            f"/api/projects/{project['project_id']}",
            headers=auth,
            json={"directory": str(replacement), "expected_revision": project["revision"]},
        )
        assert changed.status_code == 200, changed.text
        tick.set()
        with pytest.raises(WebSocketDisconnect) as closed:
            await ws.receive_json()
        assert closed.value.code == 4003
    assert destroyed == [owned]
