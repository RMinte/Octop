"""Permanent deletion is scoped, atomic, and visible to cached lists."""

from uuid import uuid4

import pytest

from octop.infra.projects import service, tasks
from octop.infra.projects.models import ProjectFields
from tests.support.auth import create_user, resolve_user_id


@pytest.fixture
async def deletion_env(env_with_provider):
    client, server, auth = env_with_provider
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    return client, server.services, auth, owner


def seed_task(services, owner, project_id=None, *, previously_archived=False):
    task = tasks.send(
        services,
        owner,
        tasks.TaskMessage(request_id=str(uuid4()), prompt="Keep the saved conversation"),
        project_id=project_id,
    )
    task = tasks.cancel(services, owner, task.task_id, task.active_turn_id)
    turn = task.turns[0]
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO project_task_contexts(task_id, turn_id, messages_json) VALUES (?, ?, '[]')",
            (task.task_id, turn.turn_id),
        )
        conn.execute(
            "INSERT INTO project_task_tool_calls(call_id,task_id,turn_id,owner_id,client_id,name,"
            "arguments_json,status,created_at,updated_at,expires_at) "
            "VALUES(?,?,?,?, 'client', 'read_file', '{}', 'completed', 1, 1, 2)",
            ("call_" + task.task_id, task.task_id, turn.turn_id, owner),
        )
        conn.execute(
            "INSERT INTO project_task_artifacts(artifact_id,task_id,turn_id,tool_call_id,"
            "request_hash,name,media_type,byte_count,sha256,content,created_at) "
            "VALUES(?,?,?, 'publish', 'hash', 'report.txt', 'text/plain', 6, 'hash', ?, 1)",
            ("artifact_" + task.task_id, task.task_id, turn.turn_id, b"report"),
        )
    if previously_archived:
        with services.db.transaction() as conn:
            conn.execute("UPDATE project_tasks SET archived=1 WHERE task_id=?", (task.task_id,))
    return task


def assert_task_deleted(services, task_id):
    with services.db.connect() as conn:
        for table in (
            "project_tasks",
            "project_task_turns",
            "project_task_artifacts",
            "project_task_tool_calls",
            "project_task_contexts",
            "project_task_titles",
        ):
            assert (
                conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE task_id=?", (task_id,)
                ).fetchone()["n"]
                == 0
            )


@pytest.mark.parametrize("in_project", [False, True])
@pytest.mark.parametrize("previously_archived", [False, True])
async def test_delete_task_cleans_children_and_sync_without_deleting_project(
    deletion_env, in_project, previously_archived
):
    client, services, auth, owner = deletion_env
    project = service.create_project(
        services, owner, ProjectFields(name="Keep project"), str(uuid4())
    )
    project_id = project.project_id if in_project else None
    task = seed_task(services, owner, project_id, previously_archived=previously_archived)
    other = seed_task(services, owner, project_id)
    base = f"/api/project-tasks/{task.task_id}"
    list_path = f"/api/projects/{project_id}/tasks" if project_id else "/api/project-tasks"
    page = (await client.get(list_path, headers=auth)).json()
    response = await client.delete(
        base, headers=auth, params={"expected_revision": task.list_revision}
    )
    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert_task_deleted(services, task.task_id)
    assert (await client.get(base, headers=auth)).status_code == 404
    assert (
        await client.get(base + f"/artifacts/artifact_{task.task_id}/download", headers=auth)
    ).status_code == 404
    assert (
        await client.get(f"/api/project-tasks/{other.task_id}", headers=auth)
    ).status_code == 200
    assert (
        await client.get(f"/api/projects/{project.project_id}", headers=auth)
    ).status_code == 200
    delta = (
        await client.get(
            list_path + "/sync", headers=auth, params={"sync_cursor": page["sync_cursor"]}
        )
    ).json()
    assert delta["changes"] == [
        {
            "operation": "delete",
            "task_id": task.task_id,
            "list_revision": task.list_revision + 1,
            "data": None,
        }
    ]


@pytest.mark.parametrize("archived_project", [False, True])
async def test_delete_project_cascades_active_and_archived_tasks_but_keeps_files(
    deletion_env, tmp_path, archived_project
):
    client, services, auth, owner = deletion_env
    directory = tmp_path / "work"
    directory.mkdir()
    file = directory / "keep.txt"
    file.write_text("keep me")
    project = service.create_project(
        services,
        owner,
        ProjectFields(
            name="Delete project", directory=str(directory), client_directory=str(tmp_path)
        ),
        str(uuid4()),
    )
    first = seed_task(services, owner, project.project_id)
    archived = seed_task(services, owner, project.project_id, previously_archived=True)
    unrelated = seed_task(services, owner)
    baseline = (await client.get("/api/projects/sync", headers=auth)).json()["next_sync_cursor"]
    if archived_project:
        project = service.archive_project(
            services, owner, project.project_id, project.revision, True
        )
    base = f"/api/projects/{project.project_id}"
    response = await client.delete(
        base, headers=auth, params={"expected_revision": project.revision}
    )
    assert response.status_code == 204, response.text
    assert_task_deleted(services, first.task_id)
    assert_task_deleted(services, archived.task_id)
    assert file.read_text() == "keep me"
    assert (await client.get(base, headers=auth)).status_code == 404
    assert (await client.get("/api/projects", headers=auth)).json() == []
    assert (
        await client.get(f"/api/project-tasks/{unrelated.task_id}", headers=auth)
    ).status_code == 200
    with services.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM project_create_requests").fetchone()["n"] == 0
        )
    delta = (
        await client.get("/api/projects/sync", headers=auth, params={"sync_cursor": baseline})
    ).json()
    assert delta["changes"] == [
        {"operation": "delete", "project_id": project.project_id, "data": None}
    ]
    assert (await client.get("/api/projects/sync", headers=auth)).json()["changes"] == []
    # Deletion releases the name for a subsequent project.
    assert (
        service.create_project(
            services, owner, ProjectFields(name=project.name), str(uuid4())
        ).project_id
        != project.project_id
    )


async def test_deletion_checks_ownership_revisions_and_busy_tasks_before_any_removal(deletion_env):
    client, services, auth, owner = deletion_env
    project = service.create_project(
        services, owner, ProjectFields(name="Busy project"), str(uuid4())
    )
    saved = seed_task(services, owner, project.project_id)
    queued = tasks.send(
        services,
        owner,
        tasks.TaskMessage(request_id="queued-deletion", prompt="Still queued"),
        project_id=project.project_id,
    )
    alice = await create_user(client, auth, username="deletion-alice")
    project_path = f"/api/projects/{project.project_id}"
    task_path = f"/api/project-tasks/{saved.task_id}"
    for path in (project_path, task_path):
        assert (await client.delete(path, headers=auth)).status_code == 422
        assert (
            await client.delete(path, headers=alice, params={"expected_revision": 1})
        ).status_code == 404
        stale = await client.delete(path, headers=auth, params={"expected_revision": 999})
        assert stale.status_code == 409 and stale.json()["error"]["code"].endswith(
            "REVISION_CONFLICT"
        )
    busy_project = await client.delete(project_path, headers=auth, params={"expected_revision": 1})
    assert busy_project.status_code == 409
    assert busy_project.json()["error"]["code"] == "PROJECT_TASKS_BUSY"
    busy_task = await client.delete(
        f"/api/project-tasks/{queued.task_id}",
        headers=auth,
        params={"expected_revision": queued.list_revision},
    )
    assert busy_task.status_code == 409 and busy_task.json()["error"]["code"] == "TASK_BUSY"
    assert (await client.get(task_path, headers=auth)).status_code == 200
    assert (await client.get(project_path, headers=auth)).status_code == 200
    tasks.cancel(services, owner, queued.task_id, queued.active_turn_id)
    assert (
        await client.delete(project_path, headers=auth, params={"expected_revision": 1})
    ).status_code == 204
