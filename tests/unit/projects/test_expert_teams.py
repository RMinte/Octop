"""Team members keep their persona and capabilities inside the task boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from octop_harness import HarnessAgentConfig

from octop.infra.projects import expert_profiles, expert_teams
from octop.infra.projects.collaboration import TaskTeam
from octop.infra.projects.content import TaskContent
from octop.infra.projects.models import ProjectFields


def test_member_configuration_uses_project_and_member_without_host_abilities(monkeypatch):
    monkeypatch.setattr(expert_profiles, "expert_configuration", lambda *_args: None)
    monkeypatch.setattr(expert_teams, "_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        expert_teams,
        "selection_snapshot",
        lambda _s, _o, ids: {"knowledge_base_ids": ids or [], "knowledge_base_catalog": "catalog"},
    )
    root = {
        "project_defaults": ProjectFields(
            name="Project", plugins=["project-plugin"], instructions="Project rules"
        ).model_dump(mode="json"),
        "plugins": ["host-private"],
        "knowledge_base_ids": ["host-private"],
        "execution_options": {},
        "action_mode": "plan",
    }
    profile = {
        "expert_id": "member",
        "capabilities": {"plugins": ["member-plugin"], "knowledge_base_ids": ["member-kb"]},
    }
    result = expert_teams.member_configuration(None, 1, root, profile)
    assert result["plugins"] == ["member-plugin", "project-plugin"]
    assert result["knowledge_base_ids"] == ["member-kb"]
    assert result["instructions"] == "Project rules" and result["action_mode"] == "plan"
    assert result["expert_profile"] == profile and result["team_enabled"] is False


async def test_members_have_project_directory_and_independent_runtime_without_host_tools(tmp_path):
    team = TaskTeam(1)
    root = HarnessAgentConfig(
        workspace_dir=str(tmp_path / "project"),
        backend={"type": "filesystem"},
        system_files_path=".octop/task",
        permissions=[],
        tools=[],
    )
    profile = {"expert_id": "member", "name": "Researcher"}
    configured = HarnessAgentConfig(
        workspace_dir=str(tmp_path / "private-owner-path"),
        backend={"private": "backend"},
        checkpointer=object(),
        tools=["member-tool"],
        system_prompt="Member identity",
    )
    prepare = AsyncMock()
    seen = []

    async def configure(_profile, _agent_id):
        return configured

    def factory(cfg, *, agent_id):
        seen.append(cfg)
        return SimpleNamespace(config=cfg, agent_id=agent_id, aclose=AsyncMock())

    await team.load_experts(
        factory, root, [profile], "task", tmp_path / "runtime", configure=configure, prepare=prepare
    )
    cfg = seen[0]
    assert cfg.workspace_dir == root.workspace_dir and cfg.backend == root.backend
    assert cfg.tools == ["member-tool"] and cfg.system_prompt == "Member identity"
    assert cfg.system_files_path.startswith(".octop/task/team/")
    assert cfg.checkpointer is False and not cfg.team_enabled and not cfg.memory_capture_enabled
    assert cfg.memory_backend_config != root.memory_backend_config
    entry = next(iter(team.registry.values()))
    assert team.speaker(entry.agent_id) == ("member", "Researcher")
    prepare.assert_awaited_once_with(entry.agent, profile)
    await team.close()


def test_speaker_change_splits_text_and_preserves_tool_origin_after_restore():
    content = TaskContent("turn")
    content.append("text", "First", speaker_agent_id="a", speaker_name="Alice")
    content.append("text", "Second", speaker_agent_id="b", speaker_name="Bob")
    content.start_tool("call", "read_file", speaker_agent_id="b", speaker_name="Bob")
    content.start_tool("call", "read_file", arguments={"file_path": "file"})
    content.finish(completed=True)
    restored = TaskContent.restore("turn", content.dump(), content.revision)
    assert [(block.text, block.speaker_agent_id) for block in restored.blocks[:2]] == [
        ("First", "a"),
        ("Second", "b"),
    ]
    assert restored.blocks[2].speaker_agent_id == "b" and restored.blocks[2].speaker_name == "Bob"
