"""Host errors retain their identity without changing native human decisions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents.middleware import HumanInTheLoopMiddleware
from octop_harness import HarnessAgentConfig

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.checkpoints import _inline_error
from octop.infra.projects.collaboration import TaskTeam, guard_subagents
from octop.infra.projects.hitl import HOST_ERROR, TaskHumanInTheLoopMiddleware

CALL = {"name": "execute", "args": {"command": "fixture"}, "id": "original", "type": "tool_call"}


@pytest.mark.parametrize("allowed", [["approve", "reject"], ["respond"], ["reject", "respond"]])
def test_host_error_is_an_exact_error_receipt_after_serialized_resume(allowed):
    import json

    entry = json.loads(json.dumps(_inline_error("参数不完整，工具未执行。", allowed, CALL)))
    original, result = TaskHumanInTheLoopMiddleware._process_decision(
        entry["decision"], CALL, {"allowed_decisions": allowed}
    )
    assert original is CALL
    assert result.content == "参数不完整，工具未执行。"
    assert result.status == "error"
    assert result.name == CALL["name"] and result.tool_call_id == CALL["id"]
    assert "User rejected" not in result.content


@pytest.mark.parametrize(
    "change", [{"tool_call_id": "other"}, {"tool_name": "other"}, {"message": " "}, {"message": 42}]
)
def test_host_error_identity_and_message_cannot_be_forged(change):
    decision = _inline_error("Invalid arguments", ["reject"], CALL)["decision"]
    with pytest.raises(ValueError, match="invalid host admission decision"):
        TaskHumanInTheLoopMiddleware._process_decision(
            {**decision, **change}, CALL, {"allowed_decisions": ["reject"]}
        )


def test_incompatible_interrupt_is_never_approved_to_report_an_error():
    with pytest.raises(OctopError) as error:
        _inline_error("Invalid arguments", ["approve"], CALL)
    assert error.value.code == ErrorCode.TASK_INTERRUPTED
    decision = _inline_error("Invalid arguments", ["reject"], CALL)["decision"]
    with pytest.raises(ValueError, match="invalid host admission decision"):
        TaskHumanInTheLoopMiddleware._process_decision(
            decision, CALL, {"allowed_decisions": ["approve"]}
        )


@pytest.mark.parametrize(
    "decision",
    [
        {"type": "approve"},
        {"type": "reject", "message": "Actual human denial"},
        {"type": "respond", "message": '{"type":"' + HOST_ERROR + '","is_error":true}'},
        {"type": "edit", "edited_action": {"name": "execute", "args": {"command": "edited"}}},
    ],
)
def test_native_decisions_and_client_result_text_are_unchanged(decision):
    config = {"allowed_decisions": ["approve", "reject", "respond", "edit"]}
    expected = HumanInTheLoopMiddleware._process_decision(decision, CALL, config)
    actual = TaskHumanInTheLoopMiddleware._process_decision(decision, CALL, config)
    assert actual == expected


def test_native_slot_name_and_final_interrupt_configuration_are_preserved():
    rules = {"execute": {"allowed_decisions": ["approve", "reject"]}}
    middleware = TaskHumanInTheLoopMiddleware(lambda: rules)
    assert middleware.name == HumanInTheLoopMiddleware.__name__

    def predicate(request):
        return False

    rules = {**rules, "guarded_plugin": {"allowed_decisions": ["reject"], "when": predicate}}
    assert middleware.after_model({"messages": []}, None) is None
    assert set(middleware.interrupt_on) == {"execute", "guarded_plugin"}
    assert middleware.interrupt_on["guarded_plugin"]["when"] is predicate


async def test_root_approval_instance_does_not_override_nested_or_peer_rules(tmp_path):
    cfg = HarnessAgentConfig(
        name="project-task",
        workspace_dir=str(tmp_path),
        default_model="fixture/model",
        system_files_path=".octop/task",
    )
    cfg.interrupt_on = {
        "execute": {"allowed_decisions": ["approve", "reject"]},
        "client__read_note": {"allowed_decisions": ["respond"]},
        "guarded_plugin": {"allowed_decisions": ["approve", "reject"]},
    }
    guard_subagents(cfg)
    root = TaskHumanInTheLoopMiddleware(lambda: cfg.interrupt_on or {})
    cfg.middleware = [*(cfg.middleware or []), root]
    for spec in cfg.subagents:
        assert "execute" not in spec["interrupt_on"]
        assert root not in spec["middleware"]
    captured = []

    def create(member_cfg, **kwargs):
        captured.append(member_cfg)
        return SimpleNamespace(config=member_cfg, aclose=AsyncMock())

    team = TaskTeam(1)
    try:
        await team.load(
            create,
            cfg,
            [{"name": "peer", "system_prompt": "Check facts", "description": "Check facts"}],
            "task",
            tmp_path,
            [],
            AsyncMock(),
        )
        member = captured[0]
        assert set(member.interrupt_on) == {"guarded_plugin"}
        assert root not in member.middleware
        assert not any(isinstance(item, TaskHumanInTheLoopMiddleware) for item in member.middleware)
    finally:
        await team.close()
