"""A selected persisted team delegates through real task-scoped harnesses."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.projects import runtime
from octop.infra.projects.expert_profiles import _workspace
from tests.integration.test_project_tasks import settled
from tests.support.auth import resolve_user_id


async def test_real_team_keeps_members_and_project_scope(env_with_provider, tmp_path, monkeypatch):
    client, server, auth = env_with_provider
    owner = await resolve_user_id(client, auth, "admin")
    services = server.services
    with services.db.transaction() as conn:
        shared_owner = conn.execute(
            "INSERT INTO users(username,role,created_at) VALUES('team-sharer','user',1) RETURNING id"
        ).fetchone()[0]
    for expert_id, name, user_id in (
        ("member-one", "Member One", owner),
        ("member-two", "Member Two", shared_owner),
        ("team-host", "Team Host", owner),
    ):
        config = {"workspace_dir": str(tmp_path / expert_id), "memory": {"memory_enabled": False}}
        services.agent_repo.create(
            agent_id=expert_id,
            user_id=user_id,
            name=name,
            kind="team" if expert_id == "team-host" else "expert",
            system_prompt=f"persona-{expert_id}",
            config_json=json.dumps(config),
        )
        workspace = _workspace(services, services.agent_repo.get(expert_id), config)
        workspace.upload_bytes("SOUL.md", f"public-{expert_id}".encode())
        workspace.upload_bytes("USER.md", f"private-{expert_id}".encode())
        if expert_id == "team-host":
            workspace.upload_bytes(
                ".octop/manifest.json", b'{"members":["member-one","member-two"]}'
            )
    services.agent_repo.set_shared("member-two", True)
    original_experts = {
        expert_id: services.agent_repo.get(expert_id)
        for expert_id in ("member-one", "member-two", "team-host")
    }
    captured, observed, errors = {}, {}, []
    original = runtime.run_turn

    async def diagnose(*args):
        try:
            async for event in original(*args):
                yield event
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(runtime, "run_turn", diagnose)

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.setdefault(self.name, []).append(messages)
            return super()._generate(messages, stop, run_manager, **kwargs)

    def real_agent(cfg, **kwargs):
        captured[cfg.name] = cfg
        root = cfg.name == "project-task"
        responses = (
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "delegate-one",
                            "name": "ask_agent",
                            "args": {
                                "expert": "Member One",
                                "message": "Use the first member's method",
                            },
                        },
                        {
                            "id": "delegate-two",
                            "name": "ask_agent",
                            "args": {
                                "expert": "Member Two",
                                "message": "Use the second member's method",
                            },
                        },
                    ],
                ),
                AIMessage(content="Team synthesis."),
            ]
            if root
            else [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "same-member-read-call",
                            "name": "read_file",
                            "args": {"file_path": "/proof.txt"},
                        }
                    ],
                ),
                AIMessage(
                    content=f"Result from {cfg.name}.",
                    response_metadata={"token_usage": {"prompt_tokens": 5, "completion_tokens": 3}},
                ),
            ]
        )
        model = Model(name=cfg.name, responses=responses)

        class Factory(ChatModelFactory):
            def get_chat_model(self, ref):
                return model

        return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

    monkeypatch.setattr(runtime, "HarnessAgent", real_agent)
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "proof.txt").write_text("Project file read by real team members", encoding="utf-8")
    project = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={
            "name": "Team project",
            "directory": str(directory),
            "instructions": "Common project instruction",
        },
    )
    assert project.status_code == 201, project.text
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "project_id": project.json()["project_id"],
            "selected_expert_id": "team-host",
            "request_id": "real-team-runtime",
            "prompt": "Work with both members",
            "web_search_enabled": False,
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    done = await settled(client, auth, created.json()["task_id"])
    if errors:
        raise errors[0]
    assert done["status"] == "completed", done
    assert set(captured) == {"project-task", "Member One", "Member Two"}
    for name, expert_id, other in (
        ("Member One", "member-one", "member-two"),
        ("Member Two", "member-two", "member-one"),
    ):
        cfg = captured[name]
        assert Path(cfg.workspace_dir) == directory
        assert cfg.checkpointer is False and not cfg.team_enabled
        system = str(observed[name][0][0].content)
        assert f"persona-{expert_id}" in system and f"public-{expert_id}" in system
        assert f"persona-{other}" not in system and "persona-team-host" not in system
        assert "Common project instruction" in system
    assert "private-member-two" not in str(observed["Member Two"])
    speakers = {
        block["speaker_agent_id"]
        for block in done["turns"][0]["content_blocks"]
        if block.get("speaker_agent_id")
    }
    assert speakers == {"member-one", "member-two"}
    member_tools = [
        block
        for block in done["turns"][0]["content_blocks"]
        if block["type"] == "tool" and block.get("speaker_agent_id")
    ]
    assert {block["speaker_agent_id"] for block in member_tools} == {"member-one", "member-two"}
    assert len({block["tool_call_id"] for block in member_tools}) == 2
    assert all(
        block["tool_name"] == "read_file" and block["status"] == "completed"
        for block in member_tools
    )
    assert all(
        "Project file read by real team members" in str(block["output"]) for block in member_tools
    )
    assert done["turns"][0]["input_tokens"] == 10
    assert done["turns"][0]["output_tokens"] == 6
    assert {
        expert_id: services.agent_repo.get(expert_id) for expert_id in original_experts
    } == original_experts
