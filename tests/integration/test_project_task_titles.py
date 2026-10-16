"""Task naming through the real HTTP, journal, worker and sync paths, without LLM I/O."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from octop.infra.projects import runtime, tasks, title
from tests.support.auth import create_user, resolve_user_id


@pytest.fixture
async def title_env(env_with_provider, monkeypatch):
    async def run(services, owner, turn, history):
        yield {"type": "token", "content": "Main answer"}

    monkeypatch.setattr(runtime, "run_turn", run)
    return env_with_provider


async def wait_for_detail(client, auth, task_id, predicate):
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
            assert response.status_code == 200, response.text
            data = response.json()
            if predicate(data):
                return data
            await asyncio.sleep(0.01)


def saved_job(server, task_id):
    with server.services.db.connect() as conn:
        return dict(
            conn.execute("SELECT * FROM project_task_titles WHERE task_id=?", (task_id,)).fetchone()
        )


@pytest.mark.parametrize("in_project", [False, True])
async def test_title_is_async_synced_accounted_and_generated_once(
    title_env, monkeypatch, in_project
):
    from tests.integration.test_project_tasks_ws import connected, until

    client, server, auth = title_env
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def generate(self, job):
        calls.append(job)
        entered.set()
        await release.wait()
        return title.TitleResult("修复登录失效问题", 9, 3)

    monkeypatch.setattr(title.TaskTitleAgent, "generate", generate)
    project_id = None
    if in_project:
        project = await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Naming project"},
        )
        project_id = project.json()["project_id"]
    base = f"/api/projects/{project_id}/tasks" if project_id else "/api/project-tasks"
    experts = server.services.agent_repo.list_all()
    request = {"request_id": "semantic-title-first", "prompt": "  请修复登录失效的问题。\n附加说明"}
    response = await client.post(base, headers=auth, json=request)
    assert response.status_code == 202
    created = response.json()
    assert created["title"] == "请修复登录失效的问题。"
    await asyncio.wait_for(entered.wait(), 5)
    done = await wait_for_detail(
        client, auth, created["task_id"], lambda d: d["status"] == "completed"
    )
    page = (await client.get(base, headers=auth)).json()
    owner = await resolve_user_id(client, auth, "admin")
    session, ws = await connected(client, auth)
    try:
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "watch-title",
                "task_id": done["task_id"],
                "cursor": done["event_cursor"],
            }
        )
        await until(ws, lambda frame: frame.get("id") == "watch-title")
        release.set()
        updated = await wait_for_detail(
            client, auth, done["task_id"], lambda d: d["title"] == "修复登录失效问题"
        )
        event = await until(
            ws,
            lambda frame: (
                frame["type"] == "turn" and frame["data"]["task"]["title"] == updated["title"]
            ),
        )
        assert event["data"]["task"]["list_revision"] == updated["list_revision"]
    finally:
        release.set()
        await session.__aexit__(None, None, None)
    assert updated["updated_at"] == done["updated_at"]
    assert updated["turns"] == done["turns"]
    assert updated["list_revision"] == done["list_revision"] + 1
    changes = (
        await client.get(base + "/sync", headers=auth, params={"sync_cursor": page["sync_cursor"]})
    ).json()
    assert changes["changes"][0]["data"]["title"] == updated["title"]
    assert (
        tasks.updates(server.services, owner, done["task_id"], done["event_cursor"])[0].task.title
        == updated["title"]
    )
    assert saved_job(server, done["task_id"])["input_tokens"] == 9
    assert server.services.usage_repo.total_tokens_for_user(owner) == 12
    again = await client.post(base, headers=auth, json=request)
    assert again.json()["task_id"] == done["task_id"]
    await client.post(
        f"/api/project-tasks/{done['task_id']}/messages",
        headers=auth,
        json={"request_id": "semantic-title-followup", "prompt": "再补上验证"},
    )
    await wait_for_detail(client, auth, done["task_id"], lambda d: d["status"] == "completed")
    assert len(calls) == 1 and calls[0].model_ref == done["turns"][0]["model_ref"]
    assert saved_job(server, done["task_id"])["status"] == "completed"
    assert server.services.agent_repo.list_all() == experts


@pytest.mark.parametrize("same_text", [False, True])
async def test_user_rename_wins_over_inflight_title_even_with_same_text(
    title_env, monkeypatch, same_text
):
    client, server, auth = title_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(self, job):
        entered.set()
        await release.wait()
        return title.TitleResult("Generated title", 4, 2)

    monkeypatch.setattr(title.TaskTitleAgent, "generate", generate)
    created = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "manual-title-test", "prompt": "Original title"},
        )
    ).json()
    await asyncio.wait_for(entered.wait(), 5)
    done = await wait_for_detail(
        client, auth, created["task_id"], lambda d: d["status"] == "completed"
    )
    manual = done["title"] if same_text else "My title"
    response = await client.patch(
        f"/api/project-tasks/{done['task_id']}",
        headers=auth,
        json={"expected_revision": done["list_revision"], "title": manual},
    )
    assert response.status_code == 200
    release.set()
    async with asyncio.timeout(5):
        while saved_job(server, done["task_id"])["status"] == "running":
            await asyncio.sleep(0.01)
    current = (await client.get(f"/api/project-tasks/{done['task_id']}", headers=auth)).json()
    assert current["title"] == manual
    assert current["list_revision"] == response.json()["list_revision"]
    assert saved_job(server, done["task_id"])["status"] == "skipped"
    assert saved_job(server, done["task_id"])["output_tokens"] == 2


async def test_title_timeout_does_not_fail_main_task_and_stops_after_two_attempts(
    title_env, monkeypatch
):
    client, server, auth = title_env
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(self, job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(title.TaskTitleAgent, "generate", generate)
    monkeypatch.setattr(title, "_GENERATION_TIMEOUT", 0.01)
    created = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "title-timeout-test", "prompt": "Keep fallback"},
        )
    ).json()
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(cancelled.wait(), 5)
    async with asyncio.timeout(5):
        while saved_job(server, created["task_id"])["status"] == "running":
            await asyncio.sleep(0.01)
    await server.project_task_worker.title_worker.close()
    done = await wait_for_detail(
        client, auth, created["task_id"], lambda d: d["status"] == "completed"
    )
    assert done["title"] == "Keep fallback" and done["turns"][0]["reply"] == "Main answer"
    repo = server.services.project_task_title_repo
    retry = repo.claim("retry-worker", tasks.now_ms() + 6_000)
    assert retry is not None and retry.attempts == 2
    await server.project_task_worker.title_worker._execute(retry)
    assert saved_job(server, done["task_id"])["status"] == "failed"
    assert repo.claim("third-worker", tasks.now_ms() + 120_000) is None


async def test_title_lease_recovery_and_late_worker_result_are_isolated(title_env):
    client, server, auth = title_env
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    repo = server.services.project_task_title_repo
    created = tasks.send(
        server.services, owner, tasks.TaskMessage(request_id="lease-title-test", prompt="Name me")
    )
    start = tasks.now_ms()
    assert repo.claim("worker-a", start) is None  # The primary request has priority.
    turn = server.services.project_task_turn_repo.claim("main-worker", start)
    assert turn is not None
    assert repo.claim("worker-a", start) is None  # Wait for the first primary token.
    server.services.project_task_turn_repo.progress(
        turn.turn_id, "main-worker", "First token", "", start
    )
    first = repo.claim("worker-a", start)
    assert first is not None and first.attempts == 1
    assert repo.claim("worker-b", start + 59_999) is None
    replacement = repo.claim("worker-b", start + 60_001)
    assert replacement is not None and replacement.attempts == 2
    repo.finish(first, "Stale result", 100, 100, start + 60_002)
    assert tasks.detail(server.services, owner, created.task_id).title == "Name me"
    repo.finish(replacement, "Recovered title", 3, 2, start + 60_003)
    repo.finish(replacement, "Duplicate result", 3, 2, start + 60_004)
    updated = tasks.detail(server.services, owner, created.task_id)
    assert updated.title == "Recovered title" and updated.status == "running"
    assert server.services.usage_repo.total_tokens_for_user(owner) == 5


async def test_pending_title_is_skipped_when_user_renames_before_execution(title_env):
    client, server, auth = title_env
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    created = tasks.send(
        server.services,
        owner,
        tasks.TaskMessage(request_id="pending-rename-test", prompt="Original"),
    )
    tasks.patch(
        server.services,
        owner,
        created.task_id,
        tasks.TaskPatch(expected_revision=1, title="Manual"),
    )
    assert saved_job(server, created.task_id)["status"] == "skipped"
    assert server.services.project_task_title_repo.claim("namer", tasks.now_ms() + 120_000) is None


def historical_task(server, owner, request_id, *, legacy=True):
    prompt = "修复项目登录失败的问题，保留现有配置" + "，核对历史消息和权限配置" * 8
    created = tasks.send(
        server.services,
        owner,
        tasks.TaskMessage(request_id=request_id, prompt=prompt),
    )
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET status='completed',reply='Existing answer' "
            "WHERE task_id=?",
            (created.task_id,),
        )
        conn.execute(
            "UPDATE project_tasks SET active_turn_id=NULL,status='completed' WHERE task_id=?",
            (created.task_id,),
        )
        if legacy:
            conn.execute("DELETE FROM project_task_titles WHERE task_id=?", (created.task_id,))
            conn.execute(
                "UPDATE project_tasks SET title=?,title_source='manual',title_revision=1 WHERE task_id=?",
                (prompt[:80], created.task_id),
            )
    return tasks.detail(server.services, owner, created.task_id)


async def test_only_explicit_open_admits_legacy_title_and_pushes_the_result(title_env, monkeypatch):
    from tests.integration.test_project_tasks_ws import command, connected, until

    client, server, auth = title_env
    await server.project_task_worker.close()
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    owner = await resolve_user_id(client, auth, "admin")
    opened = historical_task(server, owner, "legacy-open-first")
    untouched = historical_task(server, owner, "legacy-unopened-second")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def generate(self, job):
        calls.append(job)
        entered.set()
        await release.wait()
        return title.TitleResult("修复项目登录失败", 6, 2)

    monkeypatch.setattr(title.TaskTitleAgent, "generate", generate)
    assert (await client.get("/api/project-tasks", headers=auth)).status_code == 200
    assert (
        await client.get(f"/api/project-tasks/{opened.task_id}", headers=auth)
    ).status_code == 200
    session, ws = await connected(client, auth)
    try:
        await command(ws, "task.subscribe", "subscribe-readonly", task_id=opened.task_id)
        with server.services.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM project_task_titles").fetchone()[0] == 0
        first = await command(ws, "task.open", "explicit-open-first", task_id=opened.task_id)
        assert first["data"]["task_id"] == opened.task_id
        await command(ws, "task.open", "duplicate-open-first", task_id=opened.task_id)
        job = saved_job(server, opened.task_id)
        assert job["status"] == "pending" and job["attempt_limit"] == 1
        await server.project_task_worker.title_worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        await command(ws, "task.open", "inflight-open-first", task_id=opened.task_id)
        assert saved_job(server, opened.task_id)["attempts"] == 1
        release.set()
        frame = await until(
            ws,
            lambda f: f["type"] == "turn" and f["data"]["task"]["title"] == "修复项目登录失败",
        )
        assert frame["data"]["task"]["list_revision"] == opened.list_revision + 1
        await command(ws, "task.open", "completed-open-first", task_id=opened.task_id)
        assert len(calls) == 1
        after = tasks.detail(server.services, owner, opened.task_id)
        assert after.turns == opened.turns and after.updated_at == opened.updated_at
        with server.services.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM project_task_titles").fetchone()[0] == 1
        assert tasks.detail(server.services, owner, untouched.task_id).title == untouched.title
    finally:
        release.set()
        await session.__aexit__(None, None, None)


async def test_open_checks_ownership_and_does_not_accept_subscription_fields(title_env):
    from tests.integration.test_project_tasks_ws import command, connected

    client, server, auth = title_env
    await server.project_task_worker.close()
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    owner = await resolve_user_id(client, auth, "admin")
    task = historical_task(server, owner, "private-open-task")
    alice = await create_user(client, auth, username="title-reader")
    session, ws = await connected(client, alice)
    try:
        response = await command(ws, "task.open", "forbidden-task-open", task_id=task.task_id)
        assert response["status"] == 404
    finally:
        await session.__aexit__(None, None, None)
    session, ws = await connected(client, auth)
    try:
        response = await command(
            ws, "task.open", "invalid-open-frame", task_id=task.task_id, cursor="not-an-open-field"
        )
        assert response["error"]["code"] == "TASK_FRAME_INVALID"
    finally:
        await session.__aexit__(None, None, None)
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_task_titles").fetchone()[0] == 0


async def test_two_clients_opening_the_same_task_admit_one_job(title_env):
    from tests.integration.test_project_tasks_ws import command, connected

    client, server, auth = title_env
    await server.project_task_worker.close()
    server.project_task_worker = runtime.ProjectTaskWorker(server.services)
    owner = await resolve_user_id(client, auth, "admin")
    task = historical_task(server, owner, "concurrent-open-task")
    first_session, first_ws = await connected(client, auth)
    second_session, second_ws = await connected(client, auth)
    try:
        results = await asyncio.gather(
            command(first_ws, "task.open", "first-client-open", task_id=task.task_id),
            command(second_ws, "task.open", "second-client-open", task_id=task.task_id),
        )
        assert all(result["data"]["task_id"] == task.task_id for result in results)
        with server.services.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM project_task_titles").fetchone()[0] == 1
        job = saved_job(server, task.task_id)
        assert job["attempts"] == 0 and job["attempt_limit"] == 1
    finally:
        await first_session.__aexit__(None, None, None)
        await second_session.__aexit__(None, None, None)


@pytest.mark.parametrize("manual_kind", ["old_custom", "new_same_text", "generated"])
async def test_open_preserves_manual_and_generated_titles(title_env, manual_kind):
    client, server, auth = title_env
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    task = historical_task(server, owner, "protected-open-task")
    if manual_kind == "new_same_text":
        task = tasks.patch(
            server.services,
            owner,
            task.task_id,
            tasks.TaskPatch(expected_revision=task.list_revision, title=task.title),
        )
    else:
        with server.services.db.transaction() as conn:
            conn.execute(
                "UPDATE project_tasks SET title=?,title_source=? WHERE task_id=?",
                (
                    "User's custom title",
                    "generated" if manual_kind == "generated" else "manual",
                    task.task_id,
                ),
            )
        task = tasks.detail(server.services, owner, task.task_id)
    opened = title.open_task(server.services, owner, task.task_id)
    assert opened.title == task.title and opened.list_revision == task.list_revision
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_task_titles").fetchone()[0] == 0


@pytest.mark.parametrize("legacy", [False, True])
async def test_failed_title_requires_cooldown_and_new_open_without_resetting_attempts(
    title_env, monkeypatch, legacy
):
    client, server, auth = title_env
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    task = historical_task(server, owner, "retry-on-open-task", legacy=legacy)
    clock = [tasks.now_ms()]
    monkeypatch.setattr(title, "now_ms", lambda: clock[0])
    repo = server.services.project_task_title_repo
    title.open_task(server.services, owner, task.task_id)
    attempt = repo.claim("same-namer", clock[0])
    assert attempt is not None
    repo.finish(attempt, None, 4, 1, clock[0])
    if not legacy:
        clock[0] += 5_000
        attempt = repo.claim("same-namer", clock[0])
        assert attempt is not None and attempt.attempts == 2
        repo.finish(attempt, None, 4, 1, clock[0])
    assert saved_job(server, task.task_id)["status"] == "failed"
    cooldown_ends = saved_job(server, task.task_id)["lease_until"]
    assert cooldown_ends == clock[0] + 600_000
    clock[0] = cooldown_ends - 1
    title.open_task(server.services, owner, task.task_id)
    assert saved_job(server, task.task_id)["status"] == "failed"
    clock[0] = cooldown_ends
    assert repo.claim("same-namer", clock[0]) is None  # Time alone must not trigger a request.
    title.open_task(server.services, owner, task.task_id)
    title.open_task(server.services, owner, task.task_id)
    replacement = repo.claim("same-namer", clock[0])
    assert replacement is not None and replacement.attempts == attempt.attempts + 1
    assert replacement.attempt_limit == replacement.attempts
    repo.finish(attempt, "Stale title", 100, 100, clock[0])
    assert tasks.detail(server.services, owner, task.task_id).title == task.title
    repo.finish(replacement, "Recovered title", 3, 1, clock[0])
    title.open_task(server.services, owner, task.task_id)
    assert tasks.detail(server.services, owner, task.task_id).title == "Recovered title"
    assert repo.claim("same-namer", clock[0] + 1_000_000) is None
    assert server.services.usage_repo.total_tokens_for_user(owner) == (9 if legacy else 14)


async def test_interrupted_lazy_title_waits_for_cooldown_and_new_open(title_env, monkeypatch):
    client, server, auth = title_env
    await server.project_task_worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    task = historical_task(server, owner, "interrupted-lazy-title")
    clock = [tasks.now_ms()]
    monkeypatch.setattr(title, "now_ms", lambda: clock[0])
    repo = server.services.project_task_title_repo
    title.open_task(server.services, owner, task.task_id)
    first = repo.claim("dead-worker", clock[0])
    assert first is not None and first.attempts == 1
    clock[0] += 60_001
    assert repo.claim("replacement-worker", clock[0]) is None
    assert saved_job(server, task.task_id)["status"] == "failed"
    title.open_task(server.services, owner, task.task_id)
    assert saved_job(server, task.task_id)["status"] == "failed"
    clock[0] += 600_000
    assert repo.claim("replacement-worker", clock[0]) is None
    title.open_task(server.services, owner, task.task_id)
    replacement = repo.claim("replacement-worker", clock[0])
    assert replacement is not None and replacement.attempts == 2
    repo.finish(first, "Late stale title", 99, 99, clock[0])
    repo.finish(replacement, "Recovered title", 2, 1, clock[0])
    assert tasks.detail(server.services, owner, task.task_id).title == "Recovered title"
    assert server.services.usage_repo.total_tokens_for_user(owner) == 3
