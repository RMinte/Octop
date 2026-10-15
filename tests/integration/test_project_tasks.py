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
    assert (
        await client.get(f"/api/projects/{project['project_id']}/tasks", headers=auth)
    ).json() == []
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
    assert len((await client.get(base, headers=auth)).json()) == 1
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
        t["task_id"] for t in (await client.get("/api/project-tasks", headers=auth)).json()
    ] == [task["task_id"]]
    alice = await create_user(client, auth, username="alice")
    base = f"/api/project-tasks/{task['task_id']}"
    assert (await client.get("/api/project-tasks", headers=alice)).json() == []
    assert (await client.get(base, headers=alice)).status_code == 404
    assert (
        await client.post(
            base + "/messages", headers=alice, json={"request_id": "other-user", "prompt": "Steal"}
        )
    ).status_code == 404
    assert (await client.post(base + "/cancel", headers=alice)).status_code == 404
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
    assert (await client.post(base + "/cancel", headers=auth)).status_code == 200
    done = await settled(client, auth, task["task_id"])
    assert done["turns"][0]["status"] == "cancelled"
    assert done["turns"][0]["reply"] == "Partial reply"

    async def next_run(services, owner, turn, history):
        assert history == []  # Incomplete actions are never replayed.
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
    assert done["turns"][0]["error_code"] == "TASK_EXECUTION_FAILED"
    assert "private-test-key" not in json.dumps(done)


async def test_queued_cancel_and_abandoned_worker_recovery(task_env):
    client, server, auth, calls = task_env
    worker, services = server.project_task_worker, server.services
    await worker.close()  # Direct domain submission below tests restart recovery without a race.
    owner = await resolve_user_id(client, auth, "admin")
    task = send(services, owner, TaskMessage(request_id="queued-request", prompt="Work"))
    await client.post(f"/api/project-tasks/{task.task_id}/cancel", headers=auth)
    assert (await client.get(f"/api/project-tasks/{task.task_id}", headers=auth)).json()["turns"][
        0
    ]["status"] == "cancelled"
    task = send(services, owner, TaskMessage(request_id="lost-worker", prompt="Work"))
    claimed = services.project_task_turn_repo.claim("dead-process", now_ms() - 100_000)
    assert claimed is not None
    worker._recover()
    done = (await client.get(f"/api/project-tasks/{task.task_id}", headers=auth)).json()
    assert done["active_turn_id"] is None and done["turns"][0]["error_code"] == "TASK_INTERRUPTED"
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

    from octop.infra.projects.tasks import _configuration

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
    assert _configuration(configured, owner, None)[1] == "preferred/glm"
    services.settings_repo.set_active_model("openai", "gpt-4o")
    assert _configuration(configured, owner, None)[1] == "openai/gpt-4o"
    services.settings_repo.set_active_model("missing", "unavailable")
    assert _configuration(configured, owner, None)[1] == "preferred/glm"
