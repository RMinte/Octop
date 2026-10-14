"""Owned Project documents share the model tool's bounded workspace contract."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user

PREFIX = "/api/agentcomputer/v1"


def project_command() -> dict[str, object]:
    return {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "projectID": str(uuid4()),
        "name": "Project document test",
        "configuration": {
            "schemaVersion": 1,
            "modelRef": "not-configured/no-model",
            "instructions": "Use project documents when needed.",
        },
    }


async def _create_project(client, auth: dict[str, str]) -> str:
    body = project_command()
    response = await client.post(f"{PREFIX}/projects", headers=auth, json=body)
    assert response.status_code == 201, response.text
    return str(body["projectID"])


async def test_import_list_and_read_document_without_provider(env) -> None:
    client, server, auth = env
    project_id = await _create_project(client, auth)
    base = f"{PREFIX}/projects/{project_id}/documents"
    content = "项目背景：离线盒子。\n下一步：验证模型读取。"
    created = await client.put(f"{base}/brief.md", headers=auth, json={"content": content})
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    assert created.json() == {"filename": "brief.md", "content": content}
    assert (await client.get(base, headers=auth)).json() == {"items": ["brief.md"]}
    read = await client.get(f"{base}/brief.md", headers=auth)
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"
    assert read.json() == created.json()
    second = await client.put(f"{base}/notes.txt", headers=auth, json={"content": "next"})
    assert second.status_code == 201
    assert (await client.get(base, headers=auth)).json() == {"items": ["brief.md", "notes.txt"]}
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 0


async def test_import_is_create_only_even_with_parallel_requests(env) -> None:
    client, _server, auth = env
    project_id = await _create_project(client, auth)
    url = f"{PREFIX}/projects/{project_id}/documents/notes.txt"
    responses = await asyncio.gather(
        client.put(url, headers=auth, json={"content": "first"}),
        client.put(url, headers=auth, json={"content": "second"}),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(
        response.json()["content"] for response in responses if response.status_code == 201
    )
    assert (await client.get(url, headers=auth)).json()["content"] == winner
    assert (await client.put(url, headers=auth, json={"content": "third"})).status_code == 409
    assert (await client.get(url, headers=auth)).json()["content"] == winner


async def test_virtual_root_shadow_is_neither_read_nor_an_import_conflict(env) -> None:
    client, server, auth = env
    project_id = await _create_project(client, auth)
    assert server.services is not None and server.app_runtime is not None
    with server.services.db.connect() as conn:
        row = conn.execute(
            "SELECT default_agent_id FROM ac_projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    assert row is not None
    workspace = server.app_runtime.agent_registry.workspace_for_project_preparation(
        row["default_agent_id"], project_id
    )
    shadow = Path(workspace.resolve_path("/")) / "documents" / "brief.md"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("outside managed workspace", encoding="utf-8")
    url = f"{PREFIX}/projects/{project_id}/documents/brief.md"
    assert (await client.get(url, headers=auth)).status_code == 404
    created = await client.put(url, headers=auth, json={"content": "inside managed workspace"})
    assert created.status_code == 201, created.text
    assert (await client.get(url, headers=auth)).json()["content"] == "inside managed workspace"
    assert shadow.read_text(encoding="utf-8") == "outside managed workspace"


async def test_document_owner_scope_and_missing_are_indistinguishable(env) -> None:
    client, _server, admin = env
    alice = await create_user(client, admin, username="project-doc-alice")
    project_id = await _create_project(client, alice)
    base = f"{PREFIX}/projects/{project_id}/documents"
    for auth in (admin, None):
        expected = 404 if auth is admin else 401
        assert (await client.get(base, headers=auth)).status_code == expected
        assert (await client.get(f"{base}/brief.md", headers=auth)).status_code == expected
        assert (
            await client.put(f"{base}/brief.md", headers=auth, json={"content": "secret"})
        ).status_code == expected
    assert (await client.get(base, headers=alice)).json() == {"items": []}
    assert (
        await client.get(f"{PREFIX}/projects/{uuid4()}/documents", headers=alice)
    ).status_code == 404


@pytest.mark.parametrize("filename", [".secret.md", "bad.pdf", "a:b.txt", " space.md"])
async def test_document_filename_and_size_limits(env, filename: str) -> None:
    client, _server, auth = env
    project_id = await _create_project(client, auth)
    base = f"{PREFIX}/projects/{project_id}/documents"
    assert (
        await client.put(f"{base}/{filename}", headers=auth, json={"content": "x"})
    ).status_code == 422
    assert (await client.get(f"{base}/{filename}", headers=auth)).status_code == 422
    too_large = "中" * 5_462  # 16386 UTF-8 bytes
    assert (
        await client.put(f"{base}/large.md", headers=auth, json={"content": too_large})
    ).status_code == 413
    assert (await client.get(base, headers=auth)).json() == {"items": []}
    at_limit = "x" * 16_384
    assert (
        await client.put(f"{base}/bound.txt", headers=auth, json={"content": at_limit})
    ).status_code == 201
    assert (await client.get(f"{base}/bound.txt", headers=auth)).json()["content"] == at_limit


@pytest.mark.skipif(os.name != "posix", reason="symlink protection is POSIX-only")
async def test_document_symlink_cannot_expose_or_overwrite_outside_file(env, tmp_path) -> None:
    client, server, auth = env
    project_id = await _create_project(client, auth)
    assert server.services is not None and server.app_runtime is not None
    with server.services.db.connect() as conn:
        row = conn.execute(
            "SELECT default_agent_id FROM ac_projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    assert row is not None
    workspace = server.app_runtime.agent_registry.workspace_for_project_preparation(
        row["default_agent_id"], project_id
    )
    workspace.mkdir("documents")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (workspace.workspace_dir / "documents" / "escape.txt").symlink_to(outside)
    url = f"{PREFIX}/projects/{project_id}/documents/escape.txt"
    assert (await client.get(url, headers=auth)).status_code == 403
    assert (await client.put(url, headers=auth, json={"content": "changed"})).status_code == 403
    assert outside.read_text(encoding="utf-8") == "private"
    assert (await client.get(f"{PREFIX}/projects/{project_id}/documents", headers=auth)).json() == {
        "items": []
    }


@pytest.mark.skipif(os.name != "posix", reason="symlink protection is POSIX-only")
async def test_document_directory_symlink_is_rejected_before_listing(env, tmp_path) -> None:
    client, server, auth = env
    project_id = await _create_project(client, auth)
    assert server.services is not None and server.app_runtime is not None
    with server.services.db.connect() as conn:
        row = conn.execute(
            "SELECT default_agent_id FROM ac_projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    assert row is not None
    workspace = server.app_runtime.agent_registry.workspace_for_project_preparation(
        row["default_agent_id"], project_id
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("private", encoding="utf-8")
    (workspace.workspace_dir / "documents").symlink_to(outside, target_is_directory=True)
    base = f"{PREFIX}/projects/{project_id}/documents"
    assert (await client.get(base, headers=auth)).status_code == 403
    assert (await client.get(f"{base}/secret.md", headers=auth)).status_code == 403
    assert (
        await client.put(f"{base}/secret.md", headers=auth, json={"content": "changed"})
    ).status_code == 403
    assert (outside / "secret.md").read_text(encoding="utf-8") == "private"


async def test_document_routes_have_typed_openapi_schema(tmp_path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema_response = await client.get("/api/openapi.json")
        assert schema_response.status_code == 200
        paths = schema_response.json()["paths"]
        collection = paths[f"{PREFIX}/projects/{{projectID}}/documents"]
        resource = paths[f"{PREFIX}/projects/{{projectID}}/documents/{{filename}}"]
        assert collection["get"]["summary"] == "List an owned Project's text documents"
        assert resource["put"]["requestBody"]["content"]["application/json"]["schema"]
        assert resource["get"]["responses"]["200"]["content"]["application/json"]["schema"]
