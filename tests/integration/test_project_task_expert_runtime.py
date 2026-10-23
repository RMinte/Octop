"""Run expert-backed tasks through real graphs with deterministic local models."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory
from octop_harness.plugins import LoadedPlugin, PluginManifest, PluginRegistry
from octop_harness.plugins.registry import ToolRegistration
from octop_harness.plugins.tools import get_tool_config
from octop_memory.core import Memory

from octop.infra.projects import runtime
from octop.infra.projects.expert_memory import memory_namespace
from octop.infra.projects.expert_profiles import _workspace
from tests.integration.test_project_tasks import settled
from tests.support.auth import resolve_user_id


@pytest.mark.parametrize("mode", ["craft", "ask", "plan"])
async def test_real_expert_graph_keeps_project_directory_memory_and_mode_boundaries(
    env_with_provider, tmp_path, monkeypatch, mode
):
    client, server, auth = env_with_provider
    owner = await resolve_user_id(client, auth, "admin")
    services = server.services
    expert_directory = tmp_path / "expert-home"
    plugin_path = services.paths.plugins_dir / "runtime-proof"
    plugin_path.mkdir(parents=True, exist_ok=True)
    manifest = plugin_path / "plugin.yaml"
    manifest.write_text(
        "id: runtime-proof\nversion: 1.0.0\nname: Runtime proof\nkind: tool\nentry: main.py\n",
        encoding="utf-8",
    )

    def expert_parameter() -> str:
        """Return a configured expert plugin parameter."""
        return (get_tool_config("expert_parameter") or {}).get(
            "prefix", "missing expert configuration"
        )

    plugin = LoadedPlugin(
        manifest=PluginManifest.load(manifest),
        source_path=plugin_path,
        tools=[
            ToolRegistration(
                "runtime-proof", "expert_parameter", expert_parameter, "Read the expert parameter"
            ),
            ToolRegistration(
                "runtime-proof", "hidden_parameter", expert_parameter, "Hidden parameter"
            ),
        ],
    )
    monkeypatch.setitem(PluginRegistry()._plugins, "runtime-proof", plugin)
    config = {
        "workspace_dir": str(expert_directory),
        "plugins": {
            "runtime-proof": {
                "enabled": True,
                "tools": {
                    "expert_parameter": {
                        "config": {
                            "prefix": "expert-parameter-proof",
                            "api_key": "fixture-plugin-secret",
                        }
                    },
                    "hidden_parameter": {"enabled": False},
                },
            }
        },
    }
    services.agent_repo.create(
        agent_id="runtime-expert",
        user_id=owner,
        name="Expert persona",
        system_prompt="expert-professional-method-proof",
        config_json=json.dumps(config),
    )
    workspace = _workspace(services, services.agent_repo.get("runtime-expert"), config)
    workspace.upload_bytes("SOUL.md", b"expert-soul-proof")
    general = Memory(
        memory_namespace(owner, "runtime-expert", "general"),
        backend_config={"db_path": str(expert_directory / "memory.sqlite")},
    )
    general.add_raw(
        "Please remember that blueberry is the preferred financial analysis example.",
        event_type="user_message",
    )
    general.backend.close()
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "proof.md").write_text("project-directory-proof", encoding="utf-8")
    observed, agents, names = [], [], set()
    mutated = []

    @tool
    def external_write() -> str:
        """A deliberately registered write tool that read-only modes must deny."""
        mutated.append(True)
        return "unexpected external effect"

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            names.update(
                t.name if hasattr(t, "name") else t.get("name") or t.get("function", {}).get("name")
                for t in tools
            )
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.append(messages)
            return super()._generate(messages, stop, run_manager, **kwargs)

    calls = [
        {"id": "read-proof", "name": "read_file", "args": {"file_path": "/proof.md"}},
        {
            "id": "memory-proof",
            "name": "expert_memory_search",
            "args": {"query": "blueberry", "source": "general"},
        },
    ]
    if mode != "craft":
        calls.append({"id": "forbidden-write", "name": "external_write", "args": {}})
    else:
        calls.append({"id": "plugin-parameter", "name": "expert_parameter", "args": {}})
    model = Model(
        responses=[
            AIMessage(content="", tool_calls=calls),
            AIMessage(content="Expert result with project context and memory."),
        ]
    )

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return model

    def real_agent(cfg, **kwargs):
        # Simulate a late plugin registration. Dispatch must still enforce mode
        # even if a tool reaches the graph after initial capability filtering.
        if mode != "craft":
            cfg.tools = [*(cfg.tools or []), external_write]
        result = HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)
        agents.append(result)
        return result

    monkeypatch.setattr(runtime, "HarnessAgent", real_agent)
    project = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={
            "name": "Expert project",
            "directory": str(directory),
            "instructions": "project-rule-proof",
        },
    )
    assert project.status_code == 201, project.text
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "project_id": project.json()["project_id"],
            "selected_expert_id": "runtime-expert",
            "action_mode": mode,
            "request_id": "real-expert-runtime",
            "prompt": "Please remember the blueberry project and read its proof.",
            "web_search_enabled": False,
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    done = await settled(client, auth, task_id)
    turn = done["turns"][0]
    assert turn["status"] == "completed", done
    assert turn["reply"] == "Expert result with project context and memory."
    assert agents[0].config.workspace_dir == directory
    assert isinstance(agents[0].checkpointer.delegate, AsyncSqliteSaver)
    assert agents[0].checkpointer.delegate is not agents[0].memory
    assert "expert_memory_search" in names and "expert_memory_get" in names
    system = str(observed[0][0].content)
    assert "expert-soul-proof" in system
    assert "expert-professional-method-proof" in system
    assert "project-rule-proof" in system
    results = [str(message.content) for message in observed[-1] if isinstance(message, ToolMessage)]
    assert any("project-directory-proof" in result for result in results), results
    assert any("blueberry" in result for result in results), results
    assert "hidden_parameter" not in names
    if mode == "craft":
        assert any("expert-parameter-proof" in result for result in results), results
    stored = services.project_task_turn_repo.get(turn["turn_id"])
    assert "fixture-plugin-secret" not in stored.configuration_json
    if mode != "craft":
        assert "external_write" not in names
        assert "write_file" not in names and "execute" not in names
        blocked = [
            message
            for message in observed[-1]
            if isinstance(message, ToolMessage) and message.tool_call_id == "forbidden-write"
        ]
        assert blocked and blocked[0].status == "error"
        assert mutated == []
    capture = (
        services.paths.root
        / "project-tasks"
        / task_id
        / turn["turn_id"]
        / "expert-memory"
        / hashlib.sha256(b"runtime-expert").hexdigest()
        / "capture.json"
    )
    assert json.loads(capture.read_text())["status"] == "completed"
    assert (
        services.paths.root / "project-tasks" / task_id / turn["turn_id"] / "checkpoints.sqlite"
    ).exists()
