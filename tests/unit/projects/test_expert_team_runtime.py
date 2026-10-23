"""Persisted members use independent capabilities and task-owned accounting."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from octop_harness import HarnessAgentConfig
from octop_harness.request import ChatRequest

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import expert_team_runtime as module
from octop.infra.projects.models import ProjectFields
from octop.infra.projects.runtime import TaskActionModeMiddleware
from octop.infra.usage.model_calls import current_model_call_scope


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    services = SimpleNamespace(
        paths=SimpleNamespace(root=tmp_path),
        connector_repo=None,
        secret_repo=None,
        settings_repo=None,
        config=None,
        user_policy_repo=None,
        usage_repo=None,
        project_task_repo=SimpleNamespace(get=lambda *_: SimpleNamespace(project_id="project")),
        project_task_turn_repo=SimpleNamespace(
            get=lambda *_: SimpleNamespace(
                status="running", worker_id="worker", cancel_requested=False
            )
        ),
        user_repo=SimpleNamespace(get=lambda *_: SimpleNamespace(role="user")),
    )
    turn = SimpleNamespace(
        task_id="task",
        turn_id="turn",
        worker_id="worker",
        model_ref="provider/model",
        checkpoint_id="",
        request_id="request",
        prompt="Original task",
    )
    profile = {
        "expert_id": "member",
        "name": "Member",
        "system_prompt": "Member persona",
        "profile_files": [],
        "capabilities": {},
    }
    raw = {
        **ProjectFields(
            name="Project", instructions="Project instructions", mcp_servers=["member-mcp"]
        ).model_dump(mode="json"),
        "knowledge_base_ids": ["member-kb"],
        "knowledge_base_catalog": ["Member knowledge"],
        "action_mode": "craft",
        "execution_options": {},
    }
    monkeypatch.setattr(module, "member_configuration", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(module, "expert_configuration", lambda *_args: None)
    scope = SimpleNamespace(
        runtime_dir=tmp_path / "project-tasks" / "task",
        view=SimpleNamespace(
            capabilities=SimpleNamespace(browser=True),
            scope_revision="scope-revision",
            task_id="task",
            server_directory=str(tmp_path / "project"),
        ),
        workspace=SimpleNamespace(adownload_bytes=AsyncMock(return_value=b"Applied task skill")),
    )
    monkeypatch.setattr(module.workbench, "resolve", lambda *_args: scope)
    monkeypatch.setattr(module, "assert_token_quota_available", lambda *_args: None)
    monkeypatch.setattr(module, "runtime_plugin_configuration", lambda *_args: {})
    monkeypatch.setattr(module, "gateway_mcp_server_names", lambda **_kwargs: [])
    member_tool = SimpleNamespace(
        name="member-mcp_search", description="Search", metadata={"readOnlyHint": True}
    )
    discover = AsyncMock(return_value=[member_tool])
    monkeypatch.setattr(module, "aload_mcp_tools", discover)
    monkeypatch.setattr(module, "plugin_capabilities", lambda *_args, **_kwargs: ([], [], []))
    monkeypatch.setattr(
        module, "build_knowledge_tools", lambda *_args: [SimpleNamespace(name="search_knowledge")]
    )
    memory = SimpleNamespace(
        prompt=AsyncMock(return_value="Member recall"),
        capture=AsyncMock(),
        tools=lambda _locale: [SimpleNamespace(name="expert_memory_search")],
    )
    monkeypatch.setattr(module, "ExpertMemory", lambda *_args, **_kwargs: memory)
    root_cfg = HarnessAgentConfig(
        workspace_dir=str(tmp_path / "project"),
        system_files_path=".octop/task",
        system_prompt="Host private persona",
        tools=[SimpleNamespace(name="host-secret")],
        language="en",
    )
    runtime = module.ExpertTeamRuntime(
        services,
        1,
        turn,
        {},
        root_cfg,
        common_tools=[SimpleNamespace(name="read_task_image")],
        skill_files_factory=lambda *_args: [("member-method", [("SKILL.md", b"Member skill")])],
        action_gate_factory=lambda mode, *_args: SimpleNamespace(
            mode=mode, allows=lambda _name: True
        ),
        usage_dict={},
    )
    runtime.connectors.mcp_configs_for_user = AsyncMock(
        return_value={"member-mcp": {"url": "caller-owned"}, "host-mcp": {"url": "private"}}
    )
    return runtime, profile, memory, discover


async def test_member_assembly_excludes_host_persona_tools_and_knowledge(prepared):
    runtime, profile, memory, discover = prepared
    cfg = await runtime.configure(profile, "ephemeral")
    assert "Member persona" in cfg.system_prompt and "Member recall" in cfg.system_prompt
    assert "Project instructions" in cfg.system_prompt and "Host private" not in cfg.system_prompt
    assert {tool.name for tool in cfg.tools} == {
        "read_task_image",
        "search_knowledge",
        "expert_memory_search",
    }
    assert cfg.default_model == "provider/model" and cfg.checkpointer is False
    assert cfg.workspace_dir == runtime.root_cfg.workspace_dir
    assert isinstance(cfg.middleware[0], module.TaskFilePathMiddleware)
    assert isinstance(cfg.middleware[1], module._MemberToolEvents)
    assert all(
        any(isinstance(item, module.TaskFilePathMiddleware) for item in spec["middleware"])
        for spec in cfg.subagents
    )
    discover.assert_awaited_once_with({"member-mcp": {"url": "caller-owned"}})
    request = await runtime.enrich(
        "ephemeral",
        ChatRequest(
            messages="Delegate",
            configurable={
                "knowledge_base_ids": ["host-kb"],
                "plugin_tool_configs": {"owner-key": "secret"},
            },
        ),
    )
    assert request.skills == ["member-method"] and request.mcp_servers == ["member-mcp"]
    assert request.configurable["knowledge_base_ids"] == ["member-kb"]
    assert request.configurable["plugin_tool_configs"] == {}
    await runtime.finished(
        SimpleNamespace(to_agent_id="ephemeral", message="Delegate"),
        "peer-thread",
        {"messages": [AIMessage(content="Member answer")]},
    )
    memory.capture.assert_not_awaited()
    await runtime.flush()
    memory.capture.assert_awaited_once_with("Delegate", "Member answer", source_kind="delegation")


async def test_multiple_member_calls_survive_resume_and_flush_as_delegation(prepared):
    runtime, profile, memory, _discover = prepared
    await runtime.configure(profile, "ephemeral")
    for number in (1, 2):
        await runtime.finished(
            SimpleNamespace(to_agent_id="ephemeral", message=f"Question {number}"),
            "peer-thread",
            {"messages": [AIMessage(content=f"Answer {number}")]},
        )
    memory.capture.assert_not_awaited()
    runtime.members.clear()
    await runtime.configure(profile, "ephemeral")
    await runtime.flush()
    await runtime.flush()
    memory.capture.assert_awaited_once_with(
        "Question 1\n\nQuestion 2", "Answer 1\n\nAnswer 2", source_kind="delegation"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"usage_metadata": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}},
        {"response_metadata": {"token_usage": {"prompt_tokens": 10, "completion_tokens": 4}}},
        {"response_metadata": {"usage": {"input_tokens": 10, "output_tokens": 4}}},
    ],
)
async def test_member_accounting_uses_real_turn_and_deduplicates_message_ids(prepared, metadata):
    runtime, profile, _memory, _discover = prepared
    cfg = await runtime.configure(profile, "ephemeral")
    usage = next(item for item in cfg.middleware if isinstance(item, module._MemberModelUsage))
    message = AIMessage(
        content="Answer",
        id="same-model-call",
        **metadata,
    )

    async def handler(_request):
        scope = current_model_call_scope()
        assert (scope.purpose, scope.task_id, scope.turn_id, scope.user_id, scope.agent_id) == (
            "team_member",
            "task",
            "turn",
            1,
            "member",
        )
        return ModelResponse(result=[message])

    await usage.awrap_model_call(None, handler)
    await usage.awrap_model_call(None, handler)
    assert set(runtime.usage_dict) == {"same-model-call"}
    assert runtime.usage_dict["same-model-call"]["input_tokens"] == 10
    assert runtime.usage_dict["same-model-call"]["output_tokens"] == 4


async def test_member_shared_revocation_is_rechecked_before_invocation(prepared, monkeypatch):
    runtime, profile, _memory, _discover = prepared
    await runtime.configure(profile, "ephemeral")

    def revoked(*_args):
        raise OctopError.localized(ErrorCode.NOT_FOUND)

    monkeypatch.setattr(module, "expert_configuration", revoked)
    with pytest.raises(OctopError):
        await runtime.enrich("ephemeral", ChatRequest(messages="Delegate"))


async def test_stale_worker_cannot_replace_persisted_member_evidence(prepared):
    runtime, profile, _memory, _discover = prepared
    await runtime.configure(profile, "ephemeral")
    call = SimpleNamespace(to_agent_id="ephemeral", message="Accepted")
    await runtime.finished(call, "peer-thread", {"messages": [AIMessage(content="Saved")]})
    path = runtime.members["ephemeral"].evidence_path
    original = path.read_bytes()
    runtime.services.project_task_turn_repo.get = lambda *_: SimpleNamespace(
        status="running", worker_id="new-worker", cancel_requested=False
    )
    with pytest.raises(OctopError) as stale:
        await runtime.finished(
            SimpleNamespace(to_agent_id="ephemeral", message="Late old worker"),
            "peer-thread",
            {"messages": [AIMessage(content="Late")]},
        )
    assert stale.value.code == ErrorCode.TASK_INTERRUPTED
    assert path.read_bytes() == original


async def test_applied_task_skill_overrides_inherited_member_skill_in_current_directory(prepared):
    runtime, profile, _memory, _discover = prepared
    scope = module.workbench.resolve(runtime.services, 1, "task")
    manifest = scope.runtime_dir / "workbench" / "skills.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "member-method": {
                    "directory": scope.view.server_directory,
                    "path": "skills/member-method/SKILL.md",
                },
                "other-project": {
                    "directory": str(scope.runtime_dir / "another-project"),
                    "path": "skills/other-project/SKILL.md",
                },
            }
        ),
        encoding="utf-8",
    )
    await runtime.configure(profile, "ephemeral")
    assert runtime.members["ephemeral"].files == [
        ("member-method", [("SKILL.md", b"Applied task skill")])
    ]
    scope.workspace.adownload_bytes.assert_awaited_once_with("skills/member-method/SKILL.md")


async def test_member_resume_retains_applied_skill_snapshot_without_reading_live_task_skills(
    prepared, monkeypatch
):
    runtime, profile, _memory, _discover = prepared
    applied = AsyncMock(return_value=[("member-method", [("SKILL.md", b"Applied task skill")])])
    monkeypatch.setattr(module, "task_skill_files", applied)
    await runtime.configure(profile, "ephemeral")
    applied.assert_awaited_once()
    applied.reset_mock()
    applied.side_effect = AssertionError("A resumed member must retain its frozen task skills")
    runtime.skill_files_factory = lambda *_args: pytest.fail(
        "A resumed member must retain its frozen package skills"
    )
    runtime.members.clear()
    runtime.turn.checkpoint_id = "saved-checkpoint"
    await runtime.configure(profile, "ephemeral")
    applied.assert_not_awaited()
    assert runtime.members["ephemeral"].files == [
        ("member-method", [("SKILL.md", b"Applied task skill")])
    ]


@pytest.mark.parametrize("denied_by", ["mode", "approval"])
async def test_member_tool_events_preserve_source_when_real_gates_refuse(prepared, denied_by):
    runtime, profile, _memory, _discover = prepared
    events = []
    runtime.event_sink = events.append
    cfg = await runtime.configure(profile, "ephemeral")
    tracker = next(item for item in cfg.middleware if isinstance(item, module._MemberToolEvents))
    request = SimpleNamespace(
        tool_call={
            "id": "source-call",
            "name": "write_file",
            "args": {"file_path": "proof.txt", "content": "Change"},
        },
        state={"messages": []},
        tool=None,
    )
    if denied_by == "mode":
        gate = TaskActionModeMiddleware("ask", "en", set())
    else:
        gate = next(
            item for item in cfg.middleware if isinstance(item, module.TaskControlMiddleware)
        )
        gate._pause = AsyncMock()
        gate._authorize = lambda _call: False
    backend = AsyncMock(return_value=ToolMessage(content="Written", tool_call_id="source-call"))

    async def dispatch(actual):
        return await gate.awrap_tool_call(actual, backend)

    result = await tracker.awrap_tool_call(request, dispatch)
    backend.assert_not_awaited()
    assert result.status == "error"
    assert [event["type"] for event in events] == ["tool_start", "tool_end"]
    assert events[0]["arguments"] == request.tool_call["args"]
    assert events[0]["call_id"].endswith(":source-call")
    assert events[0]["speaker_agent_id"] == "member" and events[0]["speaker_name"] == "Member"
    assert events[1]["failed"] and events[1]["output"] == result.content


@pytest.mark.parametrize("interrupted", [False, True])
async def test_member_tool_failures_never_publish_private_error_or_complete_a_pause(
    prepared, interrupted
):
    runtime, profile, _memory, _discover = prepared
    events = []
    runtime.event_sink = events.append
    cfg = await runtime.configure(profile, "ephemeral")
    tracker = next(item for item in cfg.middleware if isinstance(item, module._MemberToolEvents))
    request = SimpleNamespace(
        tool_call={"id": "source-call", "name": "read_file", "args": {"file_path": "proof.txt"}}
    )
    error = (
        GraphInterrupt(())
        if interrupted
        else RuntimeError("private-credential-must-not-be-published")
    )
    backend = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await tracker.awrap_tool_call(request, backend)
    assert events[0]["type"] == "tool_start"
    if interrupted:
        assert len(events) == 1
    else:
        assert events[-1]["type"] == "tool_end" and events[-1]["failed"]
        assert events[-1]["output"] is None
    assert "private-credential" not in json.dumps(events)


async def test_external_member_graph_configuration_excludes_owner_user_memory_and_knowledge(
    prepared, monkeypatch
):
    runtime, profile, _memory, _discover = prepared
    private = "member-owner-private-marker"
    selected = {
        **profile,
        "profile_files": [
            {"path": "SOUL.md", "content": "Public member persona"},
            {"path": "USER.md", "content": private},
        ],
        "memory": {"enabled": True, "capture_enabled": True},
    }
    raw = {
        **ProjectFields(
            name="Project", instructions="Project rules", mcp_servers=["member-mcp"]
        ).model_dump(mode="json"),
        "expert_profile": selected,
        "knowledge_base_ids": ["private-kb"],
        "knowledge_base_catalog": [private],
        "source_origin": {
            "route_id": "external-route",
            "source": {"source_kind": "im", "authority": {"principal": "external_unverified"}},
        },
        "execution_options": {},
        "action_mode": "craft",
    }
    monkeypatch.setattr(module, "member_configuration", lambda *_args, **_kwargs: raw)
    enabled = []

    def memory(*args, **kwargs):
        enabled.append(kwargs["enabled"])
        assert kwargs["enabled"] is False
        return SimpleNamespace(
            prompt=AsyncMock(return_value=""), tools=lambda _locale: [], capture=AsyncMock()
        )

    monkeypatch.setattr(module, "ExpertMemory", memory)
    cfg = await runtime.configure(selected, "ephemeral-external")
    assert private not in cfg.system_prompt and "Public member persona" in cfg.system_prompt
    assert not any(tool.name.startswith("expert_memory_") for tool in cfg.tools)
    member = runtime.members["ephemeral-external"]
    assert member.raw["knowledge_base_ids"] == [] and member.raw["knowledge_base_catalog"] == []
    assert member.raw["expert_profile"]["memory"]["auto_extract_enabled"] is False
    assert enabled == [False]
