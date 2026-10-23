"""Permanent deletion is scoped, atomic, and visible to cached lists."""

import asyncio
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
async def test_delete_project_cascades_tasks_and_keeps_files(
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
    unrelated_records = task_records(services, unrelated.task_id)
    if archived_project:
        project = service.archive_project(
            services, owner, project.project_id, project.revision, True
        )
    base = f"/api/projects/{project.project_id}"
    baseline = (await client.get("/api/projects/sync", headers=auth)).json()["next_sync_cursor"]
    cursors = {
        value: (
            await client.get(
                "/api/project-tasks", headers=auth, params={"scope": "all", "archived": value}
            )
        ).json()["sync_cursor"]
        for value in (False, True)
    }
    response = await client.delete(base, headers=auth)
    assert response.status_code == 204, response.text
    assert response.content == b"" and response.headers["cache-control"] == "no-store"
    for task in (first, archived):
        assert_task_deleted(services, task.task_id)
        assert (
            await client.get(f"/api/project-tasks/{task.task_id}", headers=auth)
        ).status_code == 404
    assert file.read_text() == "keep me"
    assert (await client.get(base, headers=auth)).status_code == 404
    assert (await client.delete(base, headers=auth)).status_code == 404
    assert (await client.get("/api/projects", headers=auth)).json() == []
    assert task_records(services, unrelated.task_id) == unrelated_records
    for archived_filter, cursor in cursors.items():
        delta = (
            await client.get(
                "/api/project-tasks/sync",
                headers=auth,
                params={"scope": "all", "archived": archived_filter, "sync_cursor": cursor},
            )
        ).json()
        deleted = {
            change["task_id"] for change in delta["changes"] if change["operation"] == "delete"
        }
        assert deleted == {first.task_id, archived.task_id}
    with services.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM project_create_requests").fetchone()[0] == 0
        assert not services.project_repo.is_deleting(conn, owner, project.project_id)
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    delta = (
        await client.get("/api/projects/sync", headers=auth, params={"sync_cursor": baseline})
    ).json()
    assert delta["changes"] == [
        {"operation": "delete", "project_id": project.project_id, "data": None}
    ]
    assert (
        service.create_project(
            services, owner, ProjectFields(name=project.name), str(uuid4())
        ).project_id
        != project.project_id
    )


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_project_deletion_stops_queue_but_retains_unconfirmed_running_turn(
    deletion_env, env_with_provider, monkeypatch, status, locale
):
    client, services, auth, owner = deletion_env
    worker = env_with_provider[1].project_task_worker
    # Keep HTTP admission available without restarting this fixture's worker loop.
    monkeypatch.setattr(worker, "closing", False)
    stop = worker.stop_for_deletion

    async def short_stop(owner_id, task_ids):
        return await stop(owner_id, task_ids, timeout=0.05)

    monkeypatch.setattr(worker, "stop_for_deletion", short_stop)
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
    before_saved = task_records(services, saved.task_id)
    alice = await create_user(client, auth, username="deletion-alice")
    project_path = f"/api/projects/{project.project_id}"
    for path in (project_path, f"/api/project-tasks/{saved.task_id}"):
        assert (await client.delete(path, headers=alice)).status_code == 404
    with services.db.connect() as conn:
        assert not services.project_repo.is_deleting(conn, owner, project.project_id)
    single = await client.delete(f"/api/project-tasks/{queued.task_id}", headers=auth)
    assert single.status_code == 409 and single.json()["error"]["code"] == "TASK_BUSY"
    response = await client.delete(project_path, headers=auth)
    if status == "running":
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "PROJECT_TASKS_BUSY"
        assert response.json()["error"]["message"] == error_message("PROJECT_TASKS_BUSY", locale)
        assert task_records(services, saved.task_id) == before_saved
        assert (await client.get(project_path, headers=auth)).status_code == 200
        blocked = await client.post(
            project_path + "/tasks",
            headers=auth,
            json={"request_id": "must-not-start", "prompt": "new"},
        )
        assert (
            blocked.status_code == 409 and blocked.json()["error"]["code"] == "PROJECT_TASKS_BUSY"
        )
        with services.db.transaction() as conn:
            assert services.project_task_turn_repo.finish(
                conn, queued.active_turn_id, "deletion-worker", "cancelled", "", "", 3
            )
            services.project_task_repo.finish(conn, queued.task_id, queued.active_turn_id, 3)
        response = await client.delete(project_path, headers=auth)
    assert response.status_code == 204, response.text
    for task in (saved, queued):
        assert_task_deleted(services, task.task_id)


async def test_project_delete_waits_for_execution_cleanup_and_closes_workbench(
    deletion_env, env_with_provider, monkeypatch
):
    from octop.api.routers import project_task_workbench

    client, services, auth, owner = deletion_env
    worker = env_with_provider[1].project_task_worker
    project = service.create_project(
        services, owner, ProjectFields(name="Drain execution"), str(uuid4())
    )
    task = tasks.send(
        services,
        owner,
        tasks.TaskMessage(request_id="running-to-delete", prompt="Running"),
        project_id=project.project_id,
    )
    turn = services.project_task_turn_repo.claim("execution-worker", 2)
    assert turn.turn_id == task.active_turn_id
    started, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = []

    async def execute():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            await release.wait()
            await asyncio.to_thread(worker._finish, turn, "cancelled", "", "")

    async def close_session(task_id, user_id):
        assert release.is_set()
        assert user_id == owner
        closed.append(task_id)

    monkeypatch.setattr(project_task_workbench, "close_task_sessions", close_session)
    execution = asyncio.create_task(execute())
    worker._running[turn.turn_id] = execution
    await started.wait()
    request = asyncio.create_task(
        client.delete(f"/api/projects/{project.project_id}", headers=auth)
    )
    try:
        await asyncio.wait_for(stopping.wait(), 2)
        assert not request.done()
        assert services.project_repo.get(project.project_id, owner) is not None
        assert services.project_task_repo.get(task.task_id, owner) is not None
        release.set()
        response = await asyncio.wait_for(request, 2)
        assert response.status_code == 204, response.text
        assert execution.done()
        assert closed == [task.task_id]
        assert_task_deleted(services, task.task_id)
    finally:
        release.set()
        await asyncio.gather(execution, request, return_exceptions=True)
        worker._running.pop(turn.turn_id, None)


@pytest.mark.parametrize("wait_kind", ["approval", "answer", "client_tool", "user", "interrupted"])
async def test_project_delete_cancels_durable_waits(deletion_env, wait_kind):
    client, services, auth, owner = deletion_env
    project = service.create_project(
        services, owner, ProjectFields(name="Waiting project"), str(uuid4())
    )
    task = tasks.send(
        services,
        owner,
        tasks.TaskMessage(request_id="waiting-to-delete", prompt="Waiting"),
        project_id=project.project_id,
    )
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET status='running',worker_id='',"
            "checkpoint_id='saved-checkpoint',wait_kind=? WHERE turn_id=?",
            (wait_kind, task.active_turn_id),
        )
        services.project_task_repo.wait(conn, task.task_id, task.active_turn_id, wait_kind, 2)
    response = await client.delete(f"/api/projects/{project.project_id}", headers=auth)
    assert response.status_code == 204, response.text
    assert_task_deleted(services, task.task_id)


async def test_concurrent_project_delete_has_no_partial_failure(deletion_env):
    client, services, auth, owner = deletion_env
    project = service.create_project(
        services, owner, ProjectFields(name="Concurrent delete"), str(uuid4())
    )
    task = seed_task(services, owner, project.project_id)
    replies = await asyncio.gather(
        client.delete(f"/api/projects/{project.project_id}", headers=auth),
        client.delete(f"/api/projects/{project.project_id}", headers=auth),
    )
    assert sorted(reply.status_code for reply in replies) == [204, 404]
    assert_task_deleted(services, task.task_id)


async def test_http_project_delete_stops_the_running_worker(env_with_provider, monkeypatch):
    from octop.infra.projects import runtime
    from octop.infra.projects.title import TitleResult

    client, server, auth = env_with_provider
    owner = await resolve_user_id(client, auth, "admin")
    started, stopped = asyncio.Event(), asyncio.Event()

    async def stream(services, owner_id, turn, history):
        started.set()
        try:
            yield {"type": "token", "content": "Task has started."}
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def title(job):
        return TitleResult()

    monkeypatch.setattr(runtime, "run_turn", stream)
    monkeypatch.setattr(server.project_task_worker.title_worker.agent, "generate", title)
    project = service.create_project(
        server.services, owner, ProjectFields(name="HTTP running delete"), str(uuid4())
    )
    created = await client.post(
        f"/api/projects/{project.project_id}/tasks",
        headers=auth,
        json={
            "request_id": "delete-live-worker",
            "prompt": "Run until cancelled",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    await asyncio.wait_for(started.wait(), 2)
    removed = await client.delete(f"/api/projects/{project.project_id}", headers=auth)
    assert removed.status_code == 204, removed.text
    assert stopped.is_set()
    assert_task_deleted(server.services, created.json()["task_id"])
