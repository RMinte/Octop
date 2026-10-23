"""Permanent deletion is scoped, atomic, and visible to cached lists."""

from uuid import uuid4

import pytest

from octop.i18n import error_message
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


def task_records(services, task_id):
    with services.db.connect() as conn:
        return {
            table: [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} WHERE task_id=?", (task_id,)
                ).fetchall()
            ]
            for table in (
                "project_tasks",
                "project_task_turns",
                "project_task_artifacts",
                "project_task_tool_calls",
                "project_task_contexts",
                "project_task_titles",
            )
        }


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
    response = await client.delete(base, headers=auth)
    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert_task_deleted(services, task.task_id)
    assert (await client.get(base, headers=auth)).status_code == 404
    assert (await client.delete(base, headers=auth)).status_code == 404
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
async def test_delete_project_preserves_tasks_until_explicitly_deleted_and_keeps_files(
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
    if archived_project:
        project = service.archive_project(
            services, owner, project.project_id, project.revision, True
        )
    base = f"/api/projects/{project.project_id}"
    baseline = (await client.get("/api/projects/sync", headers=auth)).json()["next_sync_cursor"]
    task_cursors = {
        archived_filter: (
            await client.get(base + "/tasks", headers=auth, params={"archived": archived_filter})
        ).json()["sync_cursor"]
        for archived_filter in (False, True)
    }
    before_project = (await client.get(base, headers=auth)).json()
    before_tasks = {
        task.task_id: task_records(services, task.task_id) for task in (first, archived, unrelated)
    }
    response = await client.delete(base, headers=auth)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROJECT_HAS_TASKS"
    assert (await client.get(base, headers=auth)).json() == before_project
    for task_id, records in before_tasks.items():
        assert task_records(services, task_id) == records
        assert (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).status_code == 200
    assert file.read_text() == "keep me"
    assert (
        await client.get("/api/projects/sync", headers=auth, params={"sync_cursor": baseline})
    ).json()["changes"] == []
    for archived_filter, cursor in task_cursors.items():
        assert (
            await client.get(
                base + "/tasks/sync",
                headers=auth,
                params={"archived": archived_filter, "sync_cursor": cursor},
            )
        ).json()["changes"] == []

    assert (
        await client.delete(f"/api/project-tasks/{first.task_id}", headers=auth)
    ).status_code == 204
    assert_task_deleted(services, first.task_id)
    retained = await client.delete(base, headers=auth)
    assert retained.status_code == 409 and retained.json()["error"]["code"] == "PROJECT_HAS_TASKS"
    assert task_records(services, archived.task_id) == before_tasks[archived.task_id]
    assert (
        await client.delete(f"/api/project-tasks/{archived.task_id}", headers=auth)
    ).status_code == 204
    assert_task_deleted(services, archived.task_id)
    for archived_filter, cursor in task_cursors.items():
        changes = (
            await client.get(
                base + "/tasks/sync",
                headers=auth,
                params={"archived": archived_filter, "sync_cursor": cursor},
            )
        ).json()["changes"]
        assert {change["task_id"] for change in changes} == {first.task_id, archived.task_id}
        assert all(change["operation"] == "delete" and change["data"] is None for change in changes)
    response = await client.delete(base, headers=auth)
    assert response.status_code == 204 and response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert file.read_text() == "keep me"
    assert (await client.get(base, headers=auth)).status_code == 404
    assert (await client.delete(base, headers=auth)).status_code == 404
    assert (await client.get("/api/projects", headers=auth)).json() == []
    assert (
        await client.get(f"/api/project-tasks/{unrelated.task_id}", headers=auth)
    ).status_code == 200
    assert task_records(services, unrelated.task_id) == before_tasks[unrelated.task_id]
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


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_deletion_checks_ownership_and_reports_busy_reason_before_any_removal(
    deletion_env, status, locale
):
    client, services, auth, owner = deletion_env
    auth = {**auth, "Accept-Language": locale}
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
    if status == "running":
        claimed = services.project_task_turn_repo.claim("deletion-worker", 2)
        assert claimed.turn_id == queued.active_turn_id
    before_tasks = {task.task_id: task_records(services, task.task_id) for task in (saved, queued)}
    alice = await create_user(client, auth, username="deletion-alice")
    project_path = f"/api/projects/{project.project_id}"
    task_path = f"/api/project-tasks/{saved.task_id}"
    for path in (project_path, task_path):
        assert (await client.delete(path, headers=alice)).status_code == 404
    busy_project = await client.delete(project_path, headers=auth)
    assert busy_project.status_code == 409
    assert busy_project.json()["error"]["code"] == "PROJECT_TASKS_BUSY"
    assert busy_project.json()["error"]["message"] == error_message("PROJECT_TASKS_BUSY", locale)
    busy_task = await client.delete(f"/api/project-tasks/{queued.task_id}", headers=auth)
    assert busy_task.status_code == 409 and busy_task.json()["error"]["code"] == "TASK_BUSY"
    assert busy_task.json()["error"]["message"] == error_message("TASK_BUSY", locale)
    for task_id, records in before_tasks.items():
        assert task_records(services, task_id) == records
    with services.db.connect() as conn:
        for task_id in (saved.task_id, queued.task_id):
            assert conn.execute(
                "SELECT 1 FROM project_task_turns WHERE task_id=?", (task_id,)
            ).fetchone()
    assert (await client.get(task_path, headers=auth)).status_code == 200
    assert (await client.get(project_path, headers=auth)).status_code == 200
    cancelled = tasks.cancel(services, owner, queued.task_id, queued.active_turn_id)
    if status == "running":
        assert cancelled.active_turn_id == queued.active_turn_id
        pending = await client.delete(project_path, headers=auth)
        assert (
            pending.status_code == 409 and pending.json()["error"]["code"] == "PROJECT_TASKS_BUSY"
        )
        # This fixture has no worker loop; acknowledge the exact cancellation
        # through the same fenced terminal transition used by the worker.
        with services.db.transaction() as conn:
            assert services.project_task_turn_repo.finish(
                conn, queued.active_turn_id, "deletion-worker", "cancelled", "", "", 3
            )
            services.project_task_repo.finish(conn, queued.task_id, queued.active_turn_id, 3)
    retained = await client.delete(project_path, headers=auth)
    assert retained.status_code == 409 and retained.json()["error"]["code"] == "PROJECT_HAS_TASKS"
    assert retained.json()["error"]["message"] == error_message("PROJECT_HAS_TASKS", locale)
    for task in (saved, queued):
        assert (
            await client.delete(f"/api/project-tasks/{task.task_id}", headers=auth)
        ).status_code == 204
        assert_task_deleted(services, task.task_id)
    assert (await client.delete(project_path, headers=auth)).status_code == 204
