"""Composer file references are scoped reads, including before the first Task."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from octop.infra.projects.expert_profiles import _workspace
from tests.integration.test_project_tasks import task_env as task_env
from tests.support.auth import create_user


@pytest.fixture
async def composer_env(task_env, tmp_path):
    client, server, auth, calls = task_env
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "docs").mkdir()
    (directory / "docs" / "guide.md").write_text("Project document")
    (directory / ".octop").mkdir()
    (directory / ".octop" / "secret").write_text("internal")
    response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={
            "name": "Composer",
            "directory": str(directory),
            "client_directory": "Z:\\client-only",
        },
    )
    assert response.status_code == 201, response.text
    project = response.json()
    return client, server, auth, calls, directory, project


async def test_project_draft_files_do_not_create_task_thread_or_model_call(composer_env):
    client, server, auth, calls, _, project = composer_env
    services = server.services
    experts = services.agent_repo.list_all()
    with services.db.read_snapshot() as conn:
        before_threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    before_runtime = set((services.paths.root / "project-tasks").glob("*"))
    base = f"/api/projects/{project['project_id']}"
    response = await client.get(
        base + "/workspace/tree-children",
        headers=auth,
        params={"path": "docs", "expected_project_revision": project["revision"]},
    )
    assert response.status_code == 200, response.text
    assert [entry["path"] for entry in response.json()] == ["docs/guide.md"]
    assert (await client.get(base + "/tasks", headers=auth)).json()["items"] == []
    assert services.agent_repo.list_all() == experts and calls == []
    with services.db.read_snapshot() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_task_turns").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == before_threads
    assert set((services.paths.root / "project-tasks").glob("*")) == before_runtime


async def test_project_draft_files_validate_owner_version_and_archive(composer_env):
    client, server, auth, _, _, project = composer_env
    base = f"/api/projects/{project['project_id']}"
    path = base + "/workspace/tree-children"
    params = {"expected_project_revision": project["revision"]}
    other = await create_user(client, auth, username="composer-other")
    assert (await client.get(path, headers=other, params=params)).status_code == 404
    stale = await client.get(path, headers=auth, params={"expected_project_revision": 99})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROJECT_REVISION_CONFLICT"
    with server.services.db.transaction() as conn:
        server.services.project_repo.mark_deleting(conn, 1, project["project_id"])
    assert (await client.get(path, headers=auth, params=params)).status_code == 409
    with server.services.db.transaction() as conn:
        server.services.project_repo.clear_deleting(conn, 1, project["project_id"])
    archived = await client.patch(
        base + "/archive",
        headers=auth,
        json={"archived": True, "expected_revision": project["revision"]},
    )
    assert archived.status_code == 200, archived.text
    assert (await client.get(path, headers=auth, params=params)).status_code == 409


@pytest.mark.parametrize("path", ["../", ".octop", "docs/../../outside"])
async def test_project_draft_files_reject_scope_escape(composer_env, path):
    client, _, auth, _, _, project = composer_env
    response = await client.get(
        f"/api/projects/{project['project_id']}/workspace/tree-children",
        headers=auth,
        params={"path": path, "expected_project_revision": project["revision"]},
    )
    assert response.status_code == 403, response.text


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink containment")
async def test_project_draft_does_not_suggest_outside_symlinks(composer_env, tmp_path):
    client, _, auth, _, directory, project = composer_env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private")
    (directory / "escape").symlink_to(outside, target_is_directory=True)
    base = f"/api/projects/{project['project_id']}/workspace/tree-children"
    params = {"expected_project_revision": project["revision"]}
    response = await client.get(base, headers=auth, params=params)
    assert response.status_code == 200, response.text
    assert "escape" not in {entry["name"] for entry in response.json()}
    assert (
        await client.get(base, headers=auth, params={**params, "path": "escape"})
    ).status_code == 403


async def test_project_without_server_directory_does_not_probe_client_or_create_workspace(task_env):
    client, server, auth, calls = task_env
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Client only", "client_directory": "Z:\\must-not-probe"},
        )
    ).json()
    response = await client.get(
        f"/api/projects/{project['project_id']}/workspace/tree-children",
        headers=auth,
        params={"expected_project_revision": project["revision"]},
    )
    assert response.status_code == 422, response.text
    assert not (server.services.paths.root / "project-tasks").exists() and calls == []


async def test_expert_resource_defaults_use_metadata_without_reading_persona(task_env, monkeypatch):
    client, server, auth, _ = task_env
    services = server.services
    services.agent_repo.create(agent_id="composer-expert", user_id=1, name="Composer expert")
    row = services.agent_repo.get("composer-expert")
    workspace = _workspace(services, row, json.loads(row.config_json or "{}"))
    workspace.upload_bytes("USER.md", b"PRIVATE PERSONA MUST NOT APPEAR")
    workspace.upload_bytes("skills/private-method/SKILL.md", b"SKILL BODY MUST NOT APPEAR")
    monkeypatch.setattr(
        type(workspace),
        "read_text",
        lambda *args, **kwargs: pytest.fail("resource catalog must not read persona or Skill body"),
    )
    response = await client.get("/api/project-tasks/resources", headers=auth)
    assert response.status_code == 200, response.text
    expert = next(
        entry for entry in response.json()["experts"] if entry["expert_id"] == row.agent_id
    )
    assert expert["default_skills"] == [] and expert["has_workspace_skills"] is True
    assert "PRIVATE PERSONA" not in response.text and "SKILL BODY" not in response.text
