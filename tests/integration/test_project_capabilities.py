"""Exercise task context, memory, plugins and collaboration through real harnesses."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory
from octop_harness.plugins import LoadedPlugin, PluginManifest, PluginRegistry
from octop_harness.plugins.registry import MiddlewareRegistration, ToolRegistration

from octop.infra.projects import runtime
from tests.integration.test_project_tasks import settled


def harness_models(monkeypatch, responses, peer_responses=None):
    observed: list[tuple[str, list[Any], set[str]]] = []
    agents = []

    class Model(FakeMessagesListChatModel):
        tool_names: set[str] = set()
        role: str = "project-task"

        def bind_tools(self, tools: Any, **kwargs: Any):
            self.tool_names = {str(getattr(t, "name", "")) for t in tools}
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.append((self.role, messages, set(self.tool_names)))
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Model(responses=responses)
    peer_model = Model(responses=peer_responses or [AIMessage(content="peer-proof")])

    def create(cfg, **kwargs):
        chosen = model if cfg.name == "project-task" else peer_model
        chosen.role = cfg.name

        class Factory(ChatModelFactory):
            def get_chat_model(self, ref):
                return chosen

        agent = HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)
        agents.append(agent)
        return agent

    monkeypatch.setattr(runtime, "HarnessAgent", create)
    return observed, agents


async def create_task(client, auth, prompt="Work", project_id=None, options=None):
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": prompt,
            "project_id": project_id,
            "options": options or {},
        },
    )
    assert response.status_code == 202, response.text
    return await settled(client, auth, response.json()["task_id"])


async def follow_up(client, auth, task_id, prompt="Continue", options=None):
    response = await client.post(
        f"/api/project-tasks/{task_id}/messages",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": prompt, "options": options or {}},
    )
    assert response.status_code == 202, response.text
    return await settled(client, auth, task_id)


@pytest.fixture
def project_plugin(env_with_provider):
    _, server, _ = env_with_provider
    path = server.paths.plugins_dir / "project-proof"
    path.mkdir(parents=True)
    (path / "plugin.yaml").write_text(
        "id: project-proof\nversion: 1.0.0\nname: Project proof\nkind: tool\nentry: main.py\n",
        encoding="utf8",
    )
    skills = path / "skills" / "plugin-proof"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: plugin-proof\ndescription: plugin-skill-proof\n---\nUse proof", encoding="utf8"
    )
    calls = []

    def project_proof() -> str:
        """Return a project plugin proof."""
        calls.append("called")
        return "private-plugin-result"

    plugin = LoadedPlugin(
        manifest=PluginManifest.load(path / "plugin.yaml"),
        source_path=path,
        tools=[
            ToolRegistration("project-proof", "project_proof", project_proof, "Read project proof")
        ],
        skills_dir=skills.parent,
    )

    class ProofMiddleware(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            plugin.diagnostics.append("middleware-proof")
            return await handler(request)

    plugin.middleware.append(MiddlewareRegistration("project-proof", ProofMiddleware()))
    PluginRegistry().register(plugin)
    yield calls
    PluginRegistry().unregister("project-proof")


async def test_project_capabilities_persist_validate_and_snapshot(
    env_with_provider, project_plugin, monkeypatch
):
    client, server, auth = env_with_provider
    experts = server.services.agent_repo.list_all()
    resources = (await client.get("/api/projects/resources", headers=auth)).json()
    assert next(p for p in resources["plugins"] if p["plugin_id"] == "project-proof")["available"]
    role = resources["subagents"][0]["slug"]
    fields = {
        "name": "Capabilities",
        "plugins": ["project-proof"],
        "subagents": [role],
        "memory_enabled": True,
        "team_enabled": True,
    }
    response = await client.post(
        "/api/projects", headers={**auth, "Idempotency-Key": str(uuid4())}, json=fields
    )
    assert response.status_code == 201, response.text
    project = response.json()
    for key, value in fields.items():
        assert project[key] == value
    assert project_plugin == []
    snapshots = []

    async def run(services, owner, turn, history):
        snapshots.append(json.loads(turn.configuration_json))
        yield {"type": "token", "content": "done"}

    monkeypatch.setattr(runtime, "run_turn", run)
    task = await create_task(client, auth, project_id=project["project_id"])
    await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={
            "expected_revision": 1,
            "plugins": [],
            "memory_enabled": False,
            "team_enabled": False,
        },
    )
    task = await follow_up(client, auth, task["task_id"])
    assert snapshots[0]["plugins"] == ["project-proof"] and snapshots[0]["team_enabled"]
    assert snapshots[1]["plugins"] == [] and not snapshots[1]["memory_enabled"]
    assert task["turns"][1]["options"]["subagents"] == [role]
    for invalid in (
        {"plugins": ["unknown"]},
        {"plugins": ["project-proof", "project-proof"]},
        {"subagents": ["unknown"]},
        {"memory_enabled": "yes"},
    ):
        result = await client.patch(
            f"/api/projects/{project['project_id']}",
            headers=auth,
            json={"expected_revision": 2, **invalid},
        )
        assert result.status_code == 422, result.text
    assert server.services.agent_repo.list_all() == experts


async def test_plugin_results_survive_new_harness_and_project_directory_change(
    env_with_provider, project_plugin, monkeypatch, tmp_path
):
    client, server, auth = env_with_provider
    observed, agents = harness_models(
        monkeypatch,
        [
            AIMessage(
                content="", tool_calls=[{"id": "plugin-first", "name": "project_proof", "args": {}}]
            ),
            AIMessage(content="Plugin read"),
            AIMessage(content="Context retained"),
        ],
    )
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Context", "directory": str(first_dir), "plugins": ["project-proof"]},
        )
    ).json()
    task = await create_task(
        client, auth, project_id=project["project_id"], options={"action_timeline_enabled": False}
    )
    assert task["status"] == "completed", task
    assert project_plugin == ["called"]
    await client.patch(
        f"/api/projects/{project['project_id']}",
        headers=auth,
        json={"expected_revision": 1, "directory": str(second_dir), "plugins": []},
    )
    task = await follow_up(
        client, auth, task["task_id"], options={"action_timeline_enabled": False}
    )
    assert task["status"] == "completed", task
    assert len(agents) == 2 and agents[1].config.workspace_dir == second_dir
    messages = observed[-1][1]
    assert any(
        isinstance(m, ToolMessage) and m.content == "private-plugin-result" for m in messages
    )
    assert "project_proof" not in observed[-1][2]
    assert project_plugin == ["called"]
    tool_block = next(
        block
        for block in task["turns"][0]["content_blocks"]
        if block["tool_call_id"] == "plugin-first"
    )
    assert tool_block["output"] == "private-plugin-result"
    assert all(
        "private-plugin-result" not in turn[field]
        for turn in task["turns"]
        for field in ("reply", "reasoning")
    )
    assert PluginRegistry().get("project-proof").diagnostics == [
        "middleware-proof",
        "middleware-proof",
    ]
    assert "plugin-skill-proof" in str(observed[0][1][0].content)
    assert server.services.project_task_turn_repo.context(task["task_id"])


async def test_task_long_term_memory_reopens_and_is_isolated(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    observed, agents = harness_models(
        monkeypatch,
        [
            AIMessage(content="Remembered the ultramarine zebra"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "memory-recall",
                        "name": "memory_search",
                        "args": {"query": "ultramarine"},
                    }
                ],
            ),
            AIMessage(content="Recalled"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "memory-isolation",
                        "name": "memory_search",
                        "args": {"query": "ultramarine"},
                    }
                ],
            ),
            AIMessage(content="Separate task"),
        ],
    )
    task = await create_task(
        client, auth, "Remember ultramarine zebra", options={"action_timeline_enabled": False}
    )
    assert task["status"] == "completed", task
    task = await follow_up(
        client,
        auth,
        task["task_id"],
        "Recall my preference",
        options={"action_timeline_enabled": False},
    )
    assert task["status"] == "completed", task
    recall = [str(m.content) for m in observed[-1][1] if isinstance(m, ToolMessage)]
    assert any("ultramarine" in text for text in recall), recall
    other = await create_task(
        client, auth, "Recall previous preferences", options={"action_timeline_enabled": False}
    )
    assert other["status"] == "completed", other
    results = [str(m.content) for m in observed[-1][1] if isinstance(m, ToolMessage)]
    assert not any("ultramarine zebra" in text for text in results), results
    assert agents[0].config.memory_namespace == agents[1].config.memory_namespace
    assert agents[2].config.memory_namespace != agents[0].config.memory_namespace
    assert Path(agents[0].config.memory_backend_config["db_path"]).is_file()


async def test_task_can_delegate_to_selected_subagent(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    before = server.services.agent_repo.list_all()
    observed, _ = harness_models(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate",
                        "name": "task",
                        "args": {
                            "description": "Review the design",
                            "subagent_type": "engineering-code-reviewer",
                        },
                    }
                ],
            ),
            AIMessage(content="subagent-proof"),
            AIMessage(content="Delegation complete"),
        ],
    )
    task = await create_task(
        client,
        auth,
        options={
            "subagents": ["engineering-code-reviewer"],
            "memory_enabled": False,
            "action_timeline_enabled": False,
        },
    )
    assert task["status"] == "completed", task
    assert task["turns"][0]["reply"] == "Delegation complete"
    assert not {"browser_use", "desktop_screenshot", "send_file_to_user"} & observed[1][2]
    assert any(
        isinstance(m, ToolMessage) and "subagent-proof" in str(m.content) for m in observed[-1][1]
    )
    assert server.services.agent_repo.list_all() == before


async def test_task_team_calls_only_task_members(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    before = server.services.agent_repo.list_all()
    observed, agents = harness_models(
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[{"id": "roster", "name": "agent_list", "args": {}}]),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "peer",
                        "name": "ask_agent",
                        "args": {
                            "expert": "engineering-code-reviewer",
                            "message": "Review the design",
                        },
                    }
                ],
            ),
            AIMessage(
                content="Team complete",
                usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            ),
        ],
        [
            AIMessage(
                content="team-proof",
                additional_kwargs={"reasoning_content": "成员的思考不混入主回复"},
                usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            )
        ],
    )
    task = await create_task(
        client,
        auth,
        options={
            "subagents": ["engineering-code-reviewer"],
            "team_enabled": True,
            "action_timeline_enabled": False,
        },
    )
    assert task["status"] == "completed", task
    assert task["turns"][0]["reply"] == "Team complete"
    assert task["turns"][0]["reasoning"] == ""
    root = [messages for role, messages, _ in observed if role == "project-task"][-1]
    tools = [m for m in root if isinstance(m, ToolMessage)]
    assert any("team-proof" in str(m.content) for m in tools), tools
    roster = json.loads(next(m.content for m in tools if m.name == "agent_list"))
    assert len(roster) == 1 and roster[0]["agent_id"].startswith(task["task_id"] + ":")
    assert len(agents) == 2 and not agents[1].config.team_enabled
    assert task["turns"][0]["input_tokens"] == 12
    assert task["turns"][0]["output_tokens"] == 5
    assert server.services.agent_repo.list_all() == before


async def test_cancelled_tool_context_is_closed_and_not_replayed(
    env_with_provider, project_plugin, monkeypatch
):
    client, server, auth = env_with_provider
    started = asyncio.Event()
    calls = []

    async def blocked_proof() -> str:
        """Wait until the task is cancelled."""
        calls.append("started")
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    plugin = PluginRegistry().get("project-proof")
    plugin.tools.append(
        ToolRegistration("project-proof", "blocked_proof", blocked_proof, "Wait for cancellation")
    )
    observed, _ = harness_models(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "pending-proof", "name": "blocked_proof", "args": {}}],
            ),
            AIMessage(content="Continued without replay"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "Begin",
            "options": {"plugins": ["project-proof"], "action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202
    task = response.json()
    await asyncio.wait_for(started.wait(), 3)
    await client.post(
        f"/api/project-tasks/{task['task_id']}/cancel",
        headers=auth,
        json={"turn_id": task["active_turn_id"]},
    )
    done = await settled(client, auth, task["task_id"])
    assert done["status"] == "cancelled", done
    done = await follow_up(
        client, auth, task["task_id"], options={"action_timeline_enabled": False}
    )
    assert done["status"] == "completed", done
    result = next(
        m
        for m in observed[-1][1]
        if isinstance(m, ToolMessage) and m.tool_call_id == "pending-proof"
    )
    assert result.status == "error"
    assert calls == ["started"]


async def test_context_rejects_lost_worker_lease(env_with_provider):
    from octop.infra.projects.tasks import TaskMessage, now_ms, send
    from tests.support.auth import resolve_user_id

    client, server, auth = env_with_provider
    await server.project_task_worker.close()
    services = server.services
    owner = await resolve_user_id(client, auth, "admin")
    task = send(services, owner, TaskMessage(request_id="lease-context", prompt="Work"))
    turn = services.project_task_turn_repo.claim("current-worker", now_ms())
    assert not services.project_task_turn_repo.save_context(turn.turn_id, "old-worker", "[]")
    assert services.project_task_turn_repo.context(task.task_id) is None
    assert services.project_task_turn_repo.save_context(turn.turn_id, "current-worker", "[]")
    with services.db.transaction() as conn:
        services.project_task_turn_repo.finish(
            conn, turn.turn_id, "current-worker", "failed", "", "TASK_INTERRUPTED", now_ms()
        )
        services.project_task_repo.finish(conn, task.task_id, turn.turn_id, now_ms())
    assert not services.project_task_turn_repo.save_context(
        turn.turn_id, "current-worker", '[{"old":"context"}]'
    )
    assert services.project_task_turn_repo.context(task.task_id)[1] == "[]"
