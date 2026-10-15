"""Exercise the real task HTTP, SQL journal and worker without external LLM calls."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from octop.infra.projects import runtime
from octop.infra.projects.tasks import TaskMessage, now_ms, send
from tests.support.auth import create_user, resolve_user_id


async def settled(client, auth, task_id):
    for _ in range(300):
        result = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
        assert result.status_code == 200, result.text
        data = result.json()
        if data["active_turn_id"] is None:
            return data
        await asyncio.sleep(0.01)
    pytest.fail("task did not finish")


@pytest.fixture
async def task_env(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    calls = []

    async def fake_run(services, owner, turn, history):
        calls.append((turn, history))
        yield {"type": "activity", "name": "read_file"}
        yield {"type": "token", "content": f"Reply {len(history) // 2 + 1}"}

    monkeypatch.setattr(runtime, "run_turn", fake_run)
    return client, server, auth, calls


async def test_tasks_require_model_without_creating_empty_tasks_or_experts(env):
    client, server, auth = env
    before = server.services.agent_repo.list_all()
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Independent"},
        )
    ).json()
    result = await client.post(
        f"/api/projects/{project['project_id']}/tasks",
        headers=auth,
        json={"request_id": "first-request", "prompt": "Hello"},
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "TASK_MODEL_UNAVAILABLE"
    assert (await client.get(f"/api/projects/{project['project_id']}/tasks", headers=auth)).json()[
        "items"
    ] == []
    assert server.services.agent_repo.list_all() == before


async def test_task_multi_turn_idempotency_and_latest_project_configuration(task_env, tmp_path):
    client, server, auth, calls = task_env
    before = server.services.agent_repo.list_all()
    directory = tmp_path / "work"
    directory.mkdir()
    fields = {
        "name": "Independent",
        "directory": str(directory),
        "client_directory": "C:\\client",
        "instructions": "First instructions",
    }
    before = server.services.agent_repo.list_all()
    project = (
        await client.post(
            "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=fields
        )
    ).json()
    base = f"/api/projects/{project['project_id']}/tasks"
    first = {"request_id": "first-request", "prompt": "Remember the blue bird"}
    response = await client.post(base, headers=auth, json=first)
    assert response.status_code == 202, response.text
    task = response.json()
    again = await client.post(base, headers=auth, json=first)
    assert again.json()["task_id"] == task["task_id"]
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][0]["reply"] == "Reply 1"
    assert done["turns"][0]["status"] == "completed"
    assert done["status"] == "completed"
    conflict = await client.post(base, headers=auth, json={**first, "prompt": "different"})
    assert conflict.status_code == 409
    assert len(calls) == 1
    saved = json.loads(calls[0][0].configuration_json)
    assert saved["directory"] == str(directory) and saved["client_directory"] == "C:\\client"
    await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={**fields, "instructions": "Updated instructions", "expected_revision": 1},
    )
    second = {"request_id": "second-request", "prompt": "Which bird?"}
    sent = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages", headers=auth, json=second
    )
    assert sent.status_code == 202, sent.text
    done = await settled(client, auth, task["task_id"])
    assert len(done["turns"]) == 2 and done["turns"][1]["project_revision"] == 2
    assert done["turns"][1]["reply"] == "Reply 2"
    assert calls[1][1] == [
        {"role": "user", "content": first["prompt"]},
        {"role": "assistant", "content": "Reply 1"},
    ]
    assert json.loads(calls[1][0].configuration_json)["instructions"] == "Updated instructions"
    assert (
        await client.post(
            f"/api/project-tasks/{task['task_id']}/messages", headers=auth, json=second
        )
    ).status_code == 202
    assert len(calls) == 2
    assert len((await client.get(base, headers=auth)).json()["items"]) == 1
    assert server.services.agent_repo.list_all() == before
    assert done == (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()


async def test_standalone_tasks_and_owner_isolation(task_env):
    client, server, auth, calls = task_env
    before = server.services.agent_repo.list_all()
    task = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "standalone-first", "prompt": "Hi"},
        )
    ).json()
    assert task["project_id"] is None
    await settled(client, auth, task["task_id"])
    assert [
        t["task_id"] for t in (await client.get("/api/project-tasks", headers=auth)).json()["items"]
    ] == [task["task_id"]]
    alice = await create_user(client, auth, username="alice")
    base = f"/api/project-tasks/{task['task_id']}"
    assert (await client.get("/api/project-tasks", headers=alice)).json()["items"] == []
    assert (await client.get(base, headers=alice)).status_code == 404
    assert (
        await client.post(
            base + "/messages", headers=alice, json={"request_id": "other-user", "prompt": "Steal"}
        )
    ).status_code == 404
    assert (
        await client.post(
            base + "/cancel", headers=alice, json={"turn_id": task["turns"][0]["turn_id"]}
        )
    ).status_code == 404
    assert len(calls) == 1 and server.services.agent_repo.list_all() == before


async def test_busy_cancel_and_continue_preserve_history(task_env, monkeypatch):
    client, server, auth, calls = task_env
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        yield {"type": "token", "content": "Partial reply"}
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "run_turn", blocked)
    task = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "cancel-first", "prompt": "Work"},
        )
    ).json()
    await asyncio.wait_for(entered.wait(), 2)
    base = f"/api/project-tasks/{task['task_id']}"
    result = await client.post(
        base + "/messages", headers=auth, json={"request_id": "while-busy", "prompt": "another"}
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "TASK_BUSY"
    assert (
        await client.post(
            base + "/cancel", headers=auth, json={"turn_id": task["turns"][0]["turn_id"]}
        )
    ).status_code == 200
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][0]["status"] == "cancelled"
    assert done["status"] == "cancelled"
    assert done["turns"][0]["reply"] == "Partial reply"

    async def next_run(services, owner, turn, history):
        assert history[0] == {"role": "user", "content": "Work"}
        assert history[1]["role"] == "assistant"
        assert history[1]["content"].startswith("Partial reply")
        assert "未完成" in history[1]["content"] or "did not complete" in history[1]["content"]
        yield {"type": "token", "content": "Continued"}

    monkeypatch.setattr(runtime, "run_turn", next_run)
    assert (
        await client.post(
            base + "/messages",
            headers=auth,
            json={"request_id": "after-cancel", "prompt": "Continue"},
        )
    ).status_code == 202
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][1]["reply"] == "Continued"


async def test_failure_hides_raw_provider_secrets(task_env, monkeypatch):
    client, server, auth, calls = task_env

    async def failing(*args):
        raise RuntimeError("Authorization: Bearer private-test-key")
        yield

    monkeypatch.setattr(runtime, "run_turn", failing)
    task = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "failure-first", "prompt": "Work"},
        )
    ).json()
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][0]["status"] == "failed"
    assert done["status"] == "failed"
    assert done["turns"][0]["error_code"] == "TASK_EXECUTION_FAILED"
    assert "private-test-key" not in json.dumps(done)


async def test_queued_cancel_and_abandoned_worker_recovery(task_env):
    client, server, auth, calls = task_env
    worker, services = server.project_task_worker, server.services
    await worker.close()  # Direct domain submission below tests restart recovery without a race.
    owner = await resolve_user_id(client, auth, "admin")
    task = send(services, owner, TaskMessage(request_id="queued-request", prompt="Work"))
    await client.post(
        f"/api/project-tasks/{task.task_id}/cancel",
        headers=auth,
        json={"turn_id": task.active_turn_id},
    )
    cancelled = (await client.get(f"/api/project-tasks/{task.task_id}", headers=auth)).json()
    assert cancelled["turns"][0]["status"] == cancelled["status"] == "cancelled"
    task = send(services, owner, TaskMessage(request_id="lost-worker", prompt="Work"))
    claimed = services.project_task_turn_repo.claim("dead-process", now_ms() - 100_000)
    assert claimed is not None
    worker._recover()
    done = (await client.get(f"/api/project-tasks/{task.task_id}", headers=auth)).json()
    assert done["active_turn_id"] is None and done["turns"][0]["error_code"] == "TASK_INTERRUPTED"
    assert done["status"] == "failed"
    assert calls == []  # Recovery does not rerun possibly effective tools.


async def test_real_harness_reads_project_files_and_loads_selected_skills_mcp(
    env_with_provider, tmp_path, monkeypatch
):
    from typing import Any

    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool
    from octop_harness import HarnessAgent
    from octop_harness.llm.factory import ChatModelFactory

    from tests.integration.test_projects import resource_fixture

    client, server, auth = env_with_provider
    before = server.services.agent_repo.list_all()
    owner = await resolve_user_id(client, auth, "admin")
    package = resource_fixture(server, owner)
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "proof.md").write_text("project-file-proof", encoding="utf8")
    observed = []

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.append(messages)
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "read-proof", "name": "read_file", "args": {"file_path": "/proof.md"}},
                    {"id": "mcp-proof", "name": "notes__remember", "args": {}},
                ],
            ),
            AIMessage(
                content="Project files and MCP read successfully",
                usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            ),
        ]
    )

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    agents = []

    def real_agent(cfg, **kwargs):
        agent = HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)
        agents.append(agent)
        return agent

    @tool
    def notes__remember() -> str:
        """Read a fixture note."""
        return "mcp-note-proof"

    loaded = []

    async def load_mcp(configs):
        loaded.append(configs)
        return [notes__remember]

    monkeypatch.setattr(runtime, "HarnessAgent", real_agent)
    monkeypatch.setattr(runtime, "aload_mcp_tools", load_mcp)
    monkeypatch.setattr(
        runtime,
        "download_skillhub_package_files",
        lambda slug: [
            (
                "SKILL.md",
                b"---\nname: hub-proof\ndescription: hub-skill-proof-description\n---\nFollow the project guide",
            )
        ],
    )
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={
                "name": "Runtime inheritance",
                "directory": str(workspace),
                "client_directory": "Z:\\unavailable-client",
                "instructions": "project-instructions-proof",
                "skills": [
                    {"package_id": package, "slug": "review"},
                    {"source": "skillhub", "slug": "hub-proof", "name": "Hub Proof"},
                ],
                "mcp_servers": ["notes"],
            },
        )
    ).json()
    created = await client.post(
        f"/api/projects/{project['project_id']}/tasks",
        headers=auth,
        json={"request_id": "actual-harness", "prompt": "Read proof.md and the note"},
    )
    assert created.status_code == 202, created.text
    done = await settled(client, auth, created.json()["task_id"])
    assert done["turns"][0]["status"] == "completed", done
    assert done["turns"][0]["reply"] == "Project files and MCP read successfully"
    assert len(loaded) == 1 and set(loaded[0]) == {"notes"}
    tool_results = [str(m.content) for m in observed[-1] if isinstance(m, ToolMessage)]
    assert any("project-file-proof" in r for r in tool_results), tool_results
    assert any("mcp-note-proof" in r for r in tool_results), tool_results
    system = str(observed[0][0].content)
    assert "project-instructions-proof" in system
    assert "hub-skill-proof-description" in system and "review" in system, system
    assert server.services.agent_repo.list_all() == before
    assert done["turns"][0]["input_tokens"] == 5
    assert done["turns"][0]["output_tokens"] == 3
    assert server.services.usage_repo.total_tokens_for_user(owner) == 8
    assert agents[0].config.workspace_dir == workspace
    assert "fixture-private-secret" not in json.dumps(done)


async def test_model_selection_prefers_global_pin_then_existing_text_model(task_env):
    from dataclasses import replace

    from octop.infra.projects.tasks import TaskExecutionOptions, _configuration

    client, server, auth, calls = task_env
    services = server.services
    owner = await resolve_user_id(client, auth, "admin")
    services.provider_repo.create(
        name="preferred",
        kind="openai",
        base_url="https://fixture.invalid/v1",
        api_key="fixture",
        models_json=json.dumps([{"id": "glm", "enabled": True}]),
    )
    configured = replace(
        services, config=replace(services.config, agentcomputer_text_model_ref="preferred/glm")
    )
    assert _configuration(configured, owner, None, TaskExecutionOptions())[1] == "preferred/glm"
    services.settings_repo.set_active_model("openai", "gpt-4o")
    assert _configuration(configured, owner, None, TaskExecutionOptions())[1] == "openai/gpt-4o"
    services.settings_repo.set_active_model("missing", "unavailable")
    assert _configuration(configured, owner, None, TaskExecutionOptions())[1] == "preferred/glm"


@pytest.mark.parametrize("in_project", [False, True])
@pytest.mark.parametrize("result_status", ["completed", "failed", "cancelled"])
async def test_task_status_is_in_list_and_deltas_through_follow_up(
    task_env, monkeypatch, in_project, result_status
):
    from octop.infra.projects import tasks

    client, server, auth, _ = task_env
    worker, services = server.project_task_worker, server.services
    await worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    project_id = None
    if in_project:
        project_id = (
            await client.post(
                "/api/projects",
                headers={**auth, "Idempotency-Key": str(uuid4())},
                json={"name": "Status summaries"},
            )
        ).json()["project_id"]
    base = f"/api/projects/{project_id}/tasks" if project_id else "/api/project-tasks"
    task = send(
        services,
        owner,
        TaskMessage(request_id="status-first", prompt="Work"),
        project_id=project_id,
    )
    assert task.status == "queued" and task.list_revision == 1
    cursor = None

    def forbid_conversation_reads(*args, **kwargs):
        pytest.fail("List status must not read per-task conversation details")

    async def check_list(status, revision):
        nonlocal cursor
        with monkeypatch.context() as patch:
            patch.setattr(tasks, "detail", forbid_conversation_reads)
            for method in ("page", "list", "list_in_transaction", "get", "get_in_transaction"):
                patch.setattr(services.project_task_turn_repo, method, forbid_conversation_reads)
            response = await client.get(base, headers=auth)
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page["items"]) == 1
            item = page["items"][0]
            assert item["task_id"] == task.task_id
            assert item["status"] == status and item["list_revision"] == revision
            assert "turns" not in item and "reply" not in item
            if cursor:
                response = await client.get(
                    base + "/sync", headers=auth, params={"sync_cursor": cursor}
                )
                assert response.status_code == 200, response.text
                changes = response.json()["changes"]
                assert len(changes) == 1 and changes[0]["data"] == item
                assert changes[0]["list_revision"] == revision
            cursor = page["sync_cursor"]
        return item

    await check_list("queued", 1)
    claimed = services.project_task_turn_repo.claim(worker.worker_id, now_ms())
    assert claimed is not None
    await check_list("running", 2)
    # Text, tool progress and lease heartbeats stay out of the list change stream.
    services.project_task_turn_repo.progress(
        claimed.turn_id, worker.worker_id, "Partial", "read_file", now_ms()
    )
    unchanged = await client.get(base + "/sync", headers=auth, params={"sync_cursor": cursor})
    assert unchanged.json()["changes"] == []
    if result_status == "cancelled":
        tasks.cancel(services, owner, task.task_id, claimed.turn_id)
    worker._finish(claimed, "failed" if result_status == "failed" else "completed", "Result", "")
    item = await check_list(result_status, 3)
    assert item["active_turn_id"] is None
    follow_up = send(
        services,
        owner,
        TaskMessage(request_id="status-follow-up", prompt="Continue"),
        task_id=task.task_id,
    )
    assert follow_up.status == "queued" and follow_up.task_id == task.task_id
    await check_list("queued", 4)
    claimed = services.project_task_turn_repo.claim(worker.worker_id, now_ms())
    await check_list("running", 5)
    worker._finish(claimed, "completed", "Continued", "")
    await check_list("completed", 6)


async def test_task_status_delta_pagination_preserves_cutoff(task_env):
    client, server, auth, _ = task_env
    worker, services = server.project_task_worker, server.services
    await worker.close()
    owner = await resolve_user_id(client, auth, "admin")
    baseline = (await client.get("/api/project-tasks", headers=auth)).json()["sync_cursor"]
    first = send(services, owner, TaskMessage(request_id="cutoff-first", prompt="First"))
    second = send(services, owner, TaskMessage(request_id="cutoff-second", prompt="Second"))
    params = {"sync_cursor": baseline, "limit": 1}
    response = await client.get("/api/project-tasks/sync", headers=auth, params=params)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["changes"][0]["data"]["status"] == "queued"
    assert page["next_page_cursor"] and page["next_sync_cursor"] is None
    for _ in range(2):
        assert services.project_task_turn_repo.claim(worker.worker_id, now_ms()) is not None
    response = await client.get(
        "/api/project-tasks/sync",
        headers=auth,
        params={**params, "page_cursor": page["next_page_cursor"]},
    )
    assert response.status_code == 200, response.text
    last = response.json()
    assert last["changes"][0]["data"]["status"] == "queued"
    assert last["changes"][0]["list_revision"] == 1
    assert last["next_page_cursor"] is None and last["next_sync_cursor"]
    response = await client.get(
        "/api/project-tasks/sync",
        headers=auth,
        params={"sync_cursor": last["next_sync_cursor"]},
    )
    assert response.status_code == 200, response.text
    changes = response.json()["changes"]
    assert {c["task_id"] for c in changes} == {first.task_id, second.task_id}
    assert all(c["data"]["status"] == "running" and c["list_revision"] == 2 for c in changes)


async def test_task_cursor_pages_are_bounded_and_sync_promotes_unloaded_tasks(env):
    from octop.infra.db.repos.project_tasks import ProjectTaskRow

    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    repo = server.services.project_task_repo
    with server.services.db.transaction() as conn:
        for index in range(125):
            repo.insert(
                conn,
                ProjectTaskRow(
                    f"task_seed_{index:03d}",
                    owner,
                    None,
                    f"Task {index}",
                    f"request-{index}",
                    None,
                    1000,
                    1000,
                ),
            )
    response = await client.get("/api/project-tasks", headers=auth)
    assert response.status_code == 200, response.text
    first = response.json()
    assert len(first["items"]) == 50
    assert first["items"][0]["task_id"] == "task_seed_124"
    assert first["next_page_cursor"] and first["sync_cursor"]
    assert all("turns" not in row and "prompt" not in row for row in first["items"])
    with server.services.db.transaction() as conn:
        assert repo.activate(conn, "task_seed_000", "turn_test", 2000)
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,created_at,updated_at) "
            "VALUES('turn_test','task_seed_000','test-request','Test','completed','{}','test/model',2000,2001)"
        )
        repo.finish(conn, "task_seed_000", "turn_test", 2001)
    delta = await client.get(
        "/api/project-tasks/sync", headers=auth, params={"sync_cursor": first["sync_cursor"]}
    )
    assert delta.status_code == 200, delta.text
    changes = delta.json()["changes"]
    assert len(changes) == 1
    assert changes[0]["task_id"] == "task_seed_000"
    assert changes[0]["data"]["active_turn_id"] is None
    assert changes[0]["data"]["status"] == "completed"
    assert changes[0]["list_revision"] == 3
    empty = await client.get(
        "/api/project-tasks/sync",
        headers=auth,
        params={"sync_cursor": delta.json()["next_sync_cursor"]},
    )
    assert empty.json()["changes"] == []
    ids = {row["task_id"] for row in first["items"]} | {"task_seed_000"}
    page_cursor = first["next_page_cursor"]
    while page_cursor:
        page = await client.get(
            "/api/project-tasks", headers=auth, params={"page_cursor": page_cursor}
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["sync_cursor"] is None
        assert len(body["items"]) <= 50
        ids.update(row["task_id"] for row in body["items"])
        page_cursor = body["next_page_cursor"]
    assert len(ids) == 125
    with server.services.db.connect() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM project_tasks WHERE owner_id=? AND project_id IS NULL ORDER BY updated_at DESC, task_id DESC LIMIT 51",
            (owner,),
        ).fetchall()
    assert any("idx_project_tasks_scope_page" in str(tuple(row)) for row in plan)


async def test_task_cursor_scope_tamper_and_transaction_rollback(env):
    from octop.infra.db.repos.project_tasks import ProjectTaskRow
    from octop.infra.projects.cursors import cursor_codec

    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    first = (await client.get("/api/project-tasks", headers=auth)).json()
    assert first["items"] == [] and first["sync_cursor"]
    assert len(first["sync_cursor"]) <= 16
    legacy = cursor_codec(server.services, owner, "tasks::archive-v1:False")
    for path, params in (
        ("/api/project-tasks/sync", {"sync_cursor": legacy.encode("sync", position=0)}),
        (
            "/api/project-tasks",
            {"page_cursor": legacy.encode("history", updated_at=1000, after="task_old")},
        ),
    ):
        outdated = await client.get(path, headers=auth, params=params)
        assert outdated.status_code == 409
        assert outdated.json()["error"]["code"] == "PROJECT_SYNC_RESET"
    with pytest.raises(RuntimeError), server.services.db.transaction() as conn:
        server.services.project_task_repo.insert(
            conn,
            ProjectTaskRow(
                "task_rollback",
                owner,
                None,
                "Rollback",
                "rollback-request",
                None,
                1000,
                1000,
            ),
        )
        raise RuntimeError("abort")
    delta = await client.get(
        "/api/project-tasks/sync", headers=auth, params={"sync_cursor": first["sync_cursor"]}
    )
    assert delta.json()["changes"] == []
    assert delta.json()["next_sync_cursor"] == first["sync_cursor"]
    other = await create_user(client, auth, username="sync-other")
    for token in (first["sync_cursor"] + "x", "bad.游标"):
        result = await client.get(
            "/api/project-tasks/sync", headers=auth, params={"sync_cursor": token}
        )
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "PROJECT_SYNC_RESET"
    cross_user = await client.get(
        "/api/project-tasks/sync", headers=other, params={"sync_cursor": first["sync_cursor"]}
    )
    assert cross_user.status_code == 409
    project_cursor = (await client.get("/api/projects/sync", headers=auth)).json()[
        "next_sync_cursor"
    ]
    wrong_kind = await client.get(
        "/api/project-tasks/sync", headers=auth, params={"sync_cursor": project_cursor}
    )
    assert wrong_kind.status_code == 409
    wrong_use = await client.get(
        "/api/project-tasks", headers=auth, params={"page_cursor": first["sync_cursor"]}
    )
    assert wrong_use.status_code == 409
    assert (await client.get("/api/project-tasks?limit=101", headers=auth)).status_code == 422


async def test_task_list_sync_is_scoped_per_project_and_restore_epoch(env):
    from octop.infra.agentcomputer.identity import get_or_create_identity
    from octop.infra.db.repos.project_tasks import ProjectTaskRow

    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    project_ids = []
    for name in ("Scoped A", "Scoped B"):
        response = await client.post(
            "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json={"name": name}
        )
        project_ids.append(response.json()["project_id"])
    bases = [f"/api/projects/{project_id}/tasks" for project_id in project_ids]
    first = [(await client.get(base, headers=auth)).json() for base in bases]
    with server.services.db.transaction() as conn:
        server.services.project_task_repo.insert(
            conn,
            ProjectTaskRow(
                "task_scoped",
                owner,
                project_ids[0],
                "Scoped task",
                "scope-request",
                None,
                1000,
                1000,
            ),
        )
    for index, base in enumerate(bases):
        response = await client.get(
            base + "/sync", headers=auth, params={"sync_cursor": first[index]["sync_cursor"]}
        )
        assert response.status_code == 200
        assert len(response.json()["changes"]) == (1 if index == 0 else 0)
    cross_scope = await client.get(
        bases[1] + "/sync", headers=auth, params={"sync_cursor": first[0]["sync_cursor"]}
    )
    assert cross_scope.status_code == 409
    path = server.paths.agentcomputer_identity
    identity = get_or_create_identity(path)
    path.write_text(
        identity.model_copy(update={"restoreEpoch": identity.restoreEpoch + 1}).model_dump_json()
    )
    old_epoch = await client.get(
        bases[0] + "/sync", headers=auth, params={"sync_cursor": first[0]["sync_cursor"]}
    )
    assert old_epoch.status_code == 409
    rebuilt = await client.get(bases[0], headers=auth)
    assert rebuilt.status_code == 200 and rebuilt.json()["items"][0]["task_id"] == "task_scoped"


async def test_conversation_history_pages_are_bounded_and_scoped(task_env, monkeypatch):
    from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
    from octop.infra.projects import tasks

    client, server, auth, _ = task_env
    await server.project_task_worker.close()
    services = server.services
    owner = await resolve_user_id(client, auth, "admin")
    task = send(services, owner, TaskMessage(request_id="history-first", prompt="First"))
    with services.db.transaction() as conn:
        first = services.project_task_turn_repo.get_in_transaction(conn, task.active_turn_id)
        for number in range(1, 125):
            services.project_task_turn_repo.insert(
                conn,
                ProjectTaskTurnRow(
                    f"turn_history_{number:04d}",
                    task.task_id,
                    f"history-{number}",
                    str(number),
                    "",
                    "queued",
                    "",
                    "",
                    first.configuration_json,
                    first.model_ref,
                    "",
                    False,
                    first.created_at,
                    first.created_at,
                ),
            )
        conn.execute(
            "UPDATE project_task_turns SET status='completed', reply=prompt WHERE task_id=?",
            (task.task_id,),
        )
        services.project_task_repo.finish(conn, task.task_id, task.active_turn_id, now_ms())

    def forbid_full_scan(*args):
        pytest.fail("Conversation reads must not load the complete history")

    monkeypatch.setattr(services.project_task_turn_repo, "list_in_transaction", forbid_full_scan)
    base = f"/api/project-tasks/{task.task_id}"
    page = (await client.get(base, headers=auth)).json()
    assert len(page["turns"]) == 50 and page["next_page_cursor"] and page["event_cursor"]
    assert page["turns"][0]["prompt"] == "75" and page["turns"][-1]["prompt"] == "124"
    cursor = page["next_page_cursor"]
    seen = {t["turn_id"] for t in page["turns"]}
    while cursor:
        page = (await client.get(base, headers=auth, params={"page_cursor": cursor})).json()
        assert len(page["turns"]) <= 50 and page["event_cursor"] is None
        assert not seen.intersection(t["turn_id"] for t in page["turns"])
        seen.update(t["turn_id"] for t in page["turns"])
        cursor = page["next_page_cursor"]
    assert len(seen) == 125
    # Catch up a disconnected reader over multiple bounded stream batches.
    event_cursor = task.event_cursor
    replayed = []
    while batch := tasks.updates(services, owner, task.task_id, event_cursor):
        replayed.extend(update.turn.turn_id for update in batch)
        event_cursor = batch[-1].cursor
    assert len(replayed) == len(set(replayed)) == 125
    for params in ({"limit": 101}, {"limit": 0}, {"page_cursor": ""}):
        assert (await client.get(base, headers=auth, params=params)).status_code == 422
    other = send(services, owner, TaskMessage(request_id="history-other", prompt="Other"))
    other_cursor = tasks.detail(services, owner, other.task_id).event_cursor
    for token in (other_cursor, "tampered"):
        response = await client.get(base, headers=auth, params={"page_cursor": token})
        assert response.status_code == 409
    assert (
        tasks.updates(
            services, owner, task.task_id, tasks.detail(services, owner, task.task_id).event_cursor
        )
        == []
    )


async def test_precise_cancel_does_not_stop_the_next_turn_or_other_tasks(task_env):
    client, server, auth, _ = task_env
    await server.project_task_worker.close()
    services = server.services
    server.project_task_worker = runtime.ProjectTaskWorker(services)
    owner = await resolve_user_id(client, auth, "admin")
    first = send(services, owner, TaskMessage(request_id="precise-first", prompt="First"))
    base = f"/api/project-tasks/{first.task_id}"
    body = {"turn_id": first.active_turn_id}
    assert (await client.post(base + "/cancel", headers=auth)).status_code == 422
    for _ in range(2):
        assert (await client.post(base + "/cancel", headers=auth, json=body)).status_code == 200
    next_turn = (
        await client.post(
            base + "/messages", headers=auth, json={"request_id": "precise-next", "prompt": "Next"}
        )
    ).json()
    delayed = (await client.post(base + "/cancel", headers=auth, json=body)).json()
    assert delayed["active_turn_id"] == next_turn["active_turn_id"]
    assert delayed["turns"][-1]["status"] == "queued"
    assert delayed["turns"][-1]["cancel_requested"] is False
    other = send(services, owner, TaskMessage(request_id="precise-other", prompt="Other"))
    assert (
        await client.post(base + "/cancel", headers=auth, json={"turn_id": other.active_turn_id})
    ).status_code == 404


async def test_retry_preserves_failed_text_uses_current_configuration_and_is_idempotent(
    task_env, monkeypatch
):
    client, server, auth, calls = task_env

    async def failing(*args):
        yield {"type": "token", "content": "Saved partial result"}
        raise RuntimeError("do not expose this")

    monkeypatch.setattr(runtime, "run_turn", failing)
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Retry project", "instructions": "Initial"},
        )
    ).json()
    first = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={
                "project_id": project["project_id"],
                "request_id": "retry-original",
                "prompt": "Original prompt",
            },
        )
    ).json()
    failed = await settled(client, auth, first["task_id"])
    original = failed["turns"][0]
    assert original["status"] == "failed" and original["reply"] == "Saved partial result"
    await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={"expected_revision": 1, "instructions": "Updated"},
    )
    observed = []

    async def succeeding(services, owner, turn, history):
        observed.append((turn, history))
        yield {"type": "token", "content": "Recovered"}

    monkeypatch.setattr(runtime, "run_turn", succeeding)
    base = f"/api/project-tasks/{first['task_id']}"
    url = base + f"/turns/{original['turn_id']}/retry"
    request = {
        "request_id": "retry-new-round",
        "options": {"model_ref": "openai/gpt-4o", "skills": [], "mcp_servers": []},
    }
    response = await client.post(url, headers=auth, json=request)
    assert response.status_code == 202, response.text
    done = await settled(client, auth, first["task_id"])
    assert len(done["turns"]) == 2 and done["turns"][0] == original
    retried = done["turns"][1]
    assert retried["options"] == request["options"]
    assert retried["retry_of_turn_id"] == original["turn_id"]
    assert retried["prompt"] == original["prompt"] and retried["project_revision"] == 2
    assert json.loads(observed[0][0].configuration_json)["instructions"] == "Updated"
    assert observed[0][1][0] == {"role": "user", "content": "Original prompt"}
    assert observed[0][1][1]["content"].startswith("Saved partial result")
    assert "do not expose this" not in json.dumps(observed[0][1])
    assert (await client.post(url, headers=auth, json=request)).status_code == 202
    assert len(observed) == 1
    response = await client.post(url, headers=auth, json={"request_id": "retry-too-old"})
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "TASK_RETRY_UNAVAILABLE"
    )
    response = await client.post(
        base + "/messages",
        headers=auth,
        json={"request_id": request["request_id"], "prompt": "Original prompt"},
    )
    assert response.status_code == 409


async def test_task_sse_resumes_persisted_state_and_rejects_foreign_cursors(task_env, monkeypatch):
    from octop.api.routers import projects as routes
    from octop.infra.projects import tasks

    client, server, auth, _ = task_env
    await server.project_task_worker.close()
    services = server.services
    owner = await resolve_user_id(client, auth, "admin")
    first = send(services, owner, TaskMessage(request_id="stream-first", prompt="Stream"))
    cursor = first.event_cursor
    claimed = services.project_task_turn_repo.claim("stream-worker", now_ms())
    services.project_task_turn_repo.progress(
        claimed.turn_id, "stream-worker", "Live partial", "read_file", now_ms()
    )
    # The route still uses its real auth, cursor validation and SSE generator.
    monkeypatch.setattr(routes, "_TASK_STREAM_SECONDS", 0.01)
    base = f"/api/project-tasks/{first.task_id}"
    response = await client.get(base + "/events", headers=auth, params={"cursor": cursor})
    assert response.status_code == 200 and response.headers["content-type"].startswith(
        "text/event-stream"
    )
    assert response.headers["x-accel-buffering"] == "no"
    assert "event: turn" in response.text
    data = json.loads(
        next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
    )
    assert data["turn"]["reply"] == "Live partial" and data["turn"]["activity"] == "read_file"
    cursor = data["cursor"]
    services.project_task_turn_repo.progress(
        claimed.turn_id, "stream-worker", "Live partial", "read_file", now_ms() + 1
    )
    assert (
        tasks.updates(services, owner, first.task_id, cursor) == []
    )  # Lease heartbeat is not an event.
    with services.db.transaction() as conn:
        services.project_task_turn_repo.finish(
            conn, claimed.turn_id, "stream-worker", "completed", "Live final", "", now_ms()
        )
        services.project_task_repo.finish(conn, first.task_id, claimed.turn_id, now_ms())
    # Resume after a disconnected completion, including a subsequently queued round.
    send(
        services,
        owner,
        TaskMessage(request_id="stream-second", prompt="Follow up"),
        task_id=first.task_id,
    )
    response = await client.get(base + "/events", headers={**auth, "Last-Event-ID": cursor})
    payloads = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [p["turn"]["status"] for p in payloads] == ["completed", "queued"]
    assert payloads[0]["turn"]["reply"] == "Live final"
    assert tasks.updates(services, owner, first.task_id, payloads[-1]["cursor"]) == []
    snapshot = await client.get(base + "/events", headers=auth)
    assert "event: snapshot" in snapshot.text
    other = send(services, owner, TaskMessage(request_id="stream-other", prompt="Other"))
    for token in (other.event_cursor, first.next_page_cursor or "bad", cursor + "x"):
        invalid = await client.get(base + "/events", headers=auth, params={"cursor": token})
        assert invalid.status_code == 409
    alice = await create_user(client, auth, username="stream-alice")
    assert (await client.get(base + "/events", headers=alice)).status_code == 404


async def test_task_rename_archive_restore_and_filtered_sync(task_env):
    from octop.infra.projects import tasks

    client, server, auth, _ = task_env
    created = (
        await client.post(
            "/api/project-tasks",
            headers=auth,
            json={"request_id": "managed-task", "prompt": "Keep this conversation"},
        )
    ).json()
    base = f"/api/project-tasks/{created['task_id']}"
    done = await settled(client, auth, created["task_id"])
    owner = await resolve_user_id(client, auth, "admin")
    active_page = (await client.get("/api/project-tasks", headers=auth)).json()
    archived_page = (await client.get("/api/project-tasks?archived=true", headers=auth)).json()
    assert archived_page["items"] == []
    renamed = (
        await client.patch(
            base,
            headers=auth,
            json={"expected_revision": done["list_revision"], "title": "  A better title  "},
        )
    ).json()
    assert renamed["title"] == "A better title" and renamed["turns"] == done["turns"]
    changes = tasks.updates(server.services, owner, created["task_id"], done["event_cursor"])
    assert len(changes) == 1 and changes[0].task.title == "A better title"
    response = await client.patch(
        base, headers=auth, json={"expected_revision": done["list_revision"], "title": "Stale edit"}
    )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "TASK_REVISION_CONFLICT"
    )
    archived = (
        await client.patch(
            base,
            headers=auth,
            json={"expected_revision": renamed["list_revision"], "archived": True},
        )
    ).json()
    assert archived["archived"] is True and archived["turns"] == done["turns"]
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []
    assert (await client.get("/api/project-tasks?archived=true", headers=auth)).json()["items"][0][
        "task_id"
    ] == created["task_id"]
    for page, query in ((active_page, {}), (archived_page, {"archived": True})):
        delta = (
            await client.get(
                "/api/project-tasks/sync",
                headers=auth,
                params={"sync_cursor": page["sync_cursor"], **query},
            )
        ).json()
        assert len(delta["changes"]) == 1 and delta["changes"][0]["data"]["archived"] is True
    assert (
        await client.get(
            "/api/project-tasks/sync",
            headers=auth,
            params={"archived": True, "sync_cursor": active_page["sync_cursor"]},
        )
    ).status_code == 409
    response = await client.post(
        base + "/messages",
        headers=auth,
        json={"request_id": "archived-new-message", "prompt": "Continue"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "TASK_ARCHIVED"
    restored = (
        await client.patch(
            base,
            headers=auth,
            json={"expected_revision": archived["list_revision"], "archived": False},
        )
    ).json()
    assert restored["archived"] is False and restored["turns"] == done["turns"]
    assert (
        await client.post(
            base + "/messages",
            headers=auth,
            json={"request_id": "restored-new-message", "prompt": "Continue"},
        )
    ).status_code == 202
    await settled(client, auth, created["task_id"])
    alice = await create_user(client, auth, username="task-editor-alice")
    assert (
        await client.patch(
            base,
            headers=alice,
            json={"expected_revision": restored["list_revision"], "title": "Steal"},
        )
    ).status_code == 404
    for patch in ({}, {"title": None}, {"title": "  "}, {"archived": None}, {"archived": 1}):
        assert (
            await client.patch(
                base, headers=auth, json={"expected_revision": restored["list_revision"], **patch}
            )
        ).status_code == 422


async def test_active_task_can_be_renamed_but_not_archived(task_env):
    client, server, auth, _ = task_env
    await server.project_task_worker.close()
    services = server.services
    owner = await resolve_user_id(client, auth, "admin")
    task = send(services, owner, TaskMessage(request_id="managed-busy", prompt="Still queued"))
    base = f"/api/project-tasks/{task.task_id}"
    response = await client.patch(
        base, headers=auth, json={"expected_revision": task.list_revision, "archived": True}
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "TASK_BUSY"
    renamed = (
        await client.patch(
            base,
            headers=auth,
            json={"expected_revision": task.list_revision, "title": "Queued work"},
        )
    ).json()
    assert renamed["active_turn_id"] == task.active_turn_id and not renamed["archived"]


async def test_turn_execution_options_override_without_mutating_project_and_bind_replays(task_env):
    from tests.integration.test_projects import resource_fixture
    from tests.support.auth import seed_openai_provider

    client, server, auth, calls = task_env
    owner = await resolve_user_id(client, auth, "admin")
    package = resource_fixture(server, owner)
    await seed_openai_provider(client, auth, name="alternate")
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={
                "name": "Per-turn choices",
                "skills": [{"package_id": package, "slug": "review"}],
                "mcp_servers": ["notes"],
            },
        )
    ).json()
    choice = {"model_ref": "alternate/gpt-4o", "skills": [], "mcp_servers": []}
    body = {
        "request_id": "chosen-first",
        "project_id": project["project_id"],
        "prompt": "Use my choices",
        "options": choice,
    }
    response = await client.post("/api/project-tasks", headers=auth, json=body)
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    assert (
        task["turns"][0]["options"] == choice
        and task["turns"][0]["model_ref"] == choice["model_ref"]
    )
    assert task["turns"][0]["project_revision"] == 1
    assert (
        await client.get(f"/api/projects/{project['project_id']}", headers=auth)
    ).json() == project
    assert (await client.post("/api/project-tasks", headers=auth, json=body)).status_code == 202
    conflict = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={**body, "options": {**choice, "model_ref": "openai/gpt-4o"}},
    )
    assert conflict.status_code == 409 and len(calls) == 1
    base = f"/api/project-tasks/{task['task_id']}"
    inherited = await client.post(
        base + "/messages",
        headers=auth,
        json={"request_id": "inherited-next", "prompt": "Use project defaults"},
    )
    assert inherited.status_code == 202
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][-1]["options"]["skills"] == project["skills"]
    assert done["turns"][-1]["options"]["mcp_servers"] == ["notes"]
    for options, status in (
        ({"model_ref": "missing/model"}, 409),
        ({"skills": [{"package_id": "missing", "slug": "review"}]}, 422),
        ({"mcp_servers": ["not-owned"]}, 422),
        ({"mcp_servers": ["notes", "notes"]}, 422),
        ({"permission_mode": "bypass"}, 422),
    ):
        response = await client.post(
            base + "/messages",
            headers=auth,
            json={
                "request_id": "invalid-options",
                "prompt": "Do not accept this",
                "options": options,
            },
        )
        assert response.status_code == status, response.text
    assert len((await client.get(base, headers=auth)).json()["turns"]) == 2
    resources = (await client.get("/api/project-tasks/resources", headers=auth)).json()
    assert {model["model_ref"] for model in resources["models"]} == {
        "openai/gpt-4o",
        "alternate/gpt-4o",
    }
    assert "fixture-private-secret" not in json.dumps(resources) and "api_key" not in json.dumps(
        resources
    )
