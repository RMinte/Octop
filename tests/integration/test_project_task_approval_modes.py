"""Real Harness interrupts use task policy and a separate durable reviewer slot."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from octop_harness import HarnessAgent
from octop_harness.backends.bwrap_shell import BubbledLocalShellBackend
from octop_harness.backends.local_shell import HarnessLocalShellBackend
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.agents.security.policy_store import SecuritySettingsStore
from octop.infra.projects import approval_reviewer, runtime
from octop.infra.projects.approval_reviewer import ApprovalReviewResult, TaskApprovalReviewer
from tests.integration.test_project_task_interactions import approval, respond
from tests.integration.test_project_tasks import settled
from tests.support.auth import create_user

SETTINGS_URL = "/api/admin/security/task-approvals"
ALL_MODES = ["ask_for_approval", "approve_for_me", "full_access"]


@pytest.fixture
async def modes_env(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    effects, generations, reviews = [], [], []
    entered, released = asyncio.Event(), asyncio.Event()
    scenario = {"decision": "allow_once", "block": False}

    def execute(self, command, *, timeout=None):
        effects.append(command)
        return ExecuteResponse(
            output="Executed safely by the test fake", exit_code=0, truncated=False
        )

    monkeypatch.setattr(HarnessLocalShellBackend, "execute", execute)
    monkeypatch.setattr(BubbledLocalShellBackend, "execute", execute)

    async def review(self, job):
        reviews.append(job)
        entered.set()
        if scenario["block"]:
            await released.wait()
        return ApprovalReviewResult(
            decision=scenario["decision"],
            reason_code="review_" + scenario["decision"],
            input_tokens=19,
            output_tokens=5,
        )

    monkeypatch.setattr(TaskApprovalReviewer, "generate", review)

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            generations.append(messages)
            if isinstance(messages[-1], HumanMessage):
                prompt = messages[-1].content
                if prompt == "ordinary":
                    message = AIMessage(content="Ordinary complete")
                elif prompt in {"delegate", "delegate-read"}:
                    message = AIMessage(
                        content="Before delegation. ",
                        tool_calls=[
                            {
                                "id": "delegate-once",
                                "name": "task",
                                "args": {
                                    "description": "List workspace"
                                    if prompt == "delegate-read"
                                    else "Run a command for the task",
                                    "subagent_type": "general-purpose",
                                },
                            }
                        ],
                    )
                elif prompt == "List workspace":
                    message = AIMessage(
                        content="Before listing. ",
                        tool_calls=[
                            {
                                "id": "read-once",
                                "name": "ls",
                                "args": {"path": "."},
                            }
                        ],
                    )
                else:
                    message = AIMessage(
                        content="Before command. ",
                        tool_calls=[
                            {
                                "id": "execute-once",
                                "name": "execute",
                                "args": {"command": "echo approval-check"},
                            }
                        ],
                    )
            else:
                assert isinstance(messages[-1], ToolMessage)
                if (
                    scenario.get("repeat")
                    and sum(isinstance(m, ToolMessage) for m in messages) == 1
                ):
                    message = AIMessage(
                        content="Repeat command. ",
                        tool_calls=[
                            {
                                "id": "execute-again",
                                "name": "execute",
                                "args": {"command": "echo approval-check"},
                            }
                        ],
                    )
                else:
                    message = AIMessage(content="After command.")
            message.usage_metadata = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
            return ChatResult(generations=[ChatGeneration(message=message)])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return Model(responses=[])

    def create(cfg, **kwargs):
        return HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs)

    monkeypatch.setattr(runtime, "HarnessAgent", create)
    enabled = await client.put(SETTINGS_URL, headers=auth, json={"allowed_modes": ALL_MODES})
    assert enabled.status_code == 200, enabled.text
    yield client, server, auth, effects, generations, reviews, scenario, entered, released
    released.set()


async def create(env, mode="approve_for_me", prompt="command"):
    client, _, auth, *_ = env
    result = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": prompt,
            "approval_mode": mode,
        },
    )
    assert result.status_code == 202, result.text
    return result.json()


async def interaction(env, task_id, status):
    client, _, auth, *_ = env
    for _ in range(400):
        detail = (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json()
        items = detail["turns"][0]["interactions"]
        if items and items[0]["status"] == status:
            return detail, items[0]
        if detail["status"] in {"failed", "cancelled", "completed"}:
            pytest.fail(f"Task ended before interaction status {status}: {detail}")
        await asyncio.sleep(0.01)
    pytest.fail(f"Interaction did not reach {status}")


async def review_settled(env, review_id):
    _, server, *_ = env
    for _ in range(300):
        with server.services.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_task_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            if row is not None and not row["worker_id"]:
                return dict(row)
        await asyncio.sleep(0.01)
    pytest.fail("Review usage did not settle")


async def test_auto_allow_resumes_original_turn_once_and_bills_separately(modes_env):
    env = modes_env
    client, server, auth, effects, generations, reviews, *_ = env
    created = await create(env)
    final = await settled(client, auth, created["task_id"])
    assert final["status"] == "completed", final
    turn = final["turns"][0]
    assert len(final["turns"]) == 1 and turn["turn_id"] == created["active_turn_id"]
    assert turn["reply"] == "Before command. After command."
    assert effects == ["echo approval-check"] and len(reviews) == 1 and len(generations) == 2
    item = turn["interactions"][0]
    assert item["resolution"]["outcome"] == "allow_once"
    assert item["resolution"]["source"] == "auto_reviewer" and item["dispatch_started"]
    assert item["review"]["decision"] == "allow_once"
    assert (turn["input_tokens"], turn["output_tokens"]) == (14, 6)
    row = await review_settled(env, reviews[0].review_id)
    assert (row["input_tokens"], row["output_tokens"]) == (19, 5)
    assert row["status"] == "completed"
    proposal = json.loads(row["proposal_json"])
    assert (
        proposal["execution_host"] == "server" and proposal["execution_profile"] == "host_process"
    )
    assert proposal["action_digest"] == item["payload"]["action_digest"]
    assert not server.services.project_task_review_repo.finish(
        reviews[0], "allow_once", "late_duplicate", 19, 5, runtime.now_ms()
    )
    duplicate = await review_settled(env, reviews[0].review_id)
    assert (duplicate["input_tokens"], duplicate["output_tokens"]) == (19, 5)
    assert effects == ["echo approval-check"]


async def test_auto_deny_never_dispatches_command(modes_env):
    client, _, auth, effects, _, reviews, scenario, *_ = modes_env
    scenario["decision"] = "deny"
    created = await create(modes_env)
    final = await settled(client, auth, created["task_id"])
    assert final["status"] == "completed", final
    item = final["turns"][0]["interactions"][0]
    assert item["resolution"]["outcome"] == "deny" and not item["dispatch_started"]
    assert item["resolution"]["source"] == "auto_reviewer"
    assert effects == [] and len(reviews) == 1


@pytest.mark.parametrize("reason", ["requires_user", "timeout"])
async def test_unavailable_auto_review_falls_back_to_human(modes_env, monkeypatch, reason):
    env = modes_env
    client, _, auth, effects, _, reviews, scenario, *_ = env
    if reason == "timeout":
        scenario["block"] = True
        monkeypatch.setattr(approval_reviewer, "_GENERATION_TIMEOUT", 0.05)
    else:
        scenario["decision"] = "requires_user"
    created = await create(env)
    detail, item = await interaction(env, created["task_id"], "pending")
    assert detail["status"] == "waiting_approval" and effects == [] and len(reviews) == 1
    assert item["review"]["decision"] == "requires_user"
    if reason == "timeout":
        assert item["review"]["reason_code"] == "review_timeout"
    accepted = await respond(env, detail, item, approval(item))
    assert accepted.status_code == 200, accepted.text
    final = await settled(client, auth, created["task_id"])
    assert final["status"] == "completed" and effects == ["echo approval-check"]
    assert final["turns"][0]["interactions"][0]["resolution"]["source"] == "user"
    assert len(reviews) == 1


async def test_full_access_skips_reviewer_but_keeps_exact_action_receipt(modes_env):
    client, _, auth, effects, _, reviews, *_ = modes_env
    created = await create(modes_env, mode="full_access")
    final = await settled(client, auth, created["task_id"])
    assert final["status"] == "completed", final
    assert reviews == [] and effects == ["echo approval-check"]
    item = final["turns"][0]["interactions"][0]
    assert item["resolution"]["source"] == "full_access_mode" and item["dispatch_started"]


async def test_switching_mode_does_not_approve_existing_human_card(modes_env):
    env = modes_env
    client, _, auth, effects, _, reviews, *_ = env
    created = await create(env, mode="ask_for_approval")
    detail, item = await interaction(env, created["task_id"], "pending")
    assert reviews == [] and effects == []
    changed = await client.put(
        f"/api/project-tasks/{created['task_id']}/approval-policy",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "expected_revision": 1,
            "mode": "full_access",
        },
    )
    assert changed.status_code == 200, changed.text
    await asyncio.sleep(0.1)
    after = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    assert after["turns"][0]["interactions"][0] == item
    assert after["status"] == "waiting_approval" and reviews == [] and effects == []
    accepted = await respond(env, detail, item, approval(item))
    assert accepted.status_code == 200, accepted.text
    assert (await settled(client, auth, created["task_id"]))["status"] == "completed"
    assert effects == ["echo approval-check"] and reviews == []


async def test_revoking_auto_mode_while_reviewing_requires_human(modes_env):
    env = modes_env
    client, _, auth, effects, _, _, scenario, entered, released = env
    scenario["block"] = True
    created = await create(env)
    await asyncio.wait_for(entered.wait(), 5)
    revoked = await client.put(
        SETTINGS_URL, headers=auth, json={"allowed_modes": ["ask_for_approval"]}
    )
    assert revoked.status_code == 200
    released.set()
    detail, item = await interaction(env, created["task_id"], "pending")
    assert detail["status"] == "waiting_approval" and effects == []
    assert item["review"]["decision"] == "allow_once" and item["resolution"] is None
    assert (await respond(env, detail, item, approval(item, "deny"))).status_code == 200
    assert (await settled(client, auth, created["task_id"]))["status"] == "completed"
    assert effects == []


async def test_review_wait_releases_task_slot_and_cancel_rejects_late_allow(modes_env):
    env = modes_env
    client, server, auth, effects, _, reviews, scenario, entered, released = env
    scenario["block"] = True
    created = await create(env)
    await asyncio.wait_for(entered.wait(), 5)
    detail, item = await interaction(env, created["task_id"], "reviewing")
    turn = await asyncio.to_thread(
        server.services.project_task_turn_repo.get, created["active_turn_id"]
    )
    assert turn.is_waiting and turn.checkpoint_id and not turn.worker_id
    ordinary = await create(env, prompt="ordinary")
    assert (await settled(client, auth, ordinary["task_id"]))["status"] == "completed"
    stopped = await client.post(
        f"/api/project-tasks/{created['task_id']}/cancel",
        headers=auth,
        json={
            "turn_id": created["active_turn_id"],
        },
    )
    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    released.set()
    row = await review_settled(env, reviews[0].review_id)
    assert row["status"] == "cancelled" and row["decision"] == ""
    assert (row["input_tokens"], row["output_tokens"]) == (19, 5)
    late = await respond(env, detail, item, approval(item))
    assert late.status_code == 409
    final = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
    assert final["status"] == "cancelled" and effects == [] and len(reviews) == 1
    assert (final["turns"][0]["input_tokens"], final["turns"][0]["output_tokens"]) == (7, 3)


async def test_approval_settings_are_admin_only_and_reject_unsafe_shape(env_with_provider):
    client, _, auth = env_with_provider
    user = await create_user(client, auth, username="approval-settings-user")
    for method in ("GET", "PUT"):
        response = await client.request(
            method,
            SETTINGS_URL,
            headers=user,
            json={"allowed_modes": ALL_MODES} if method == "PUT" else None,
        )
        assert response.status_code == 403
    invalid = await client.put(SETTINGS_URL, headers=auth, json={"allowed_modes": ["full_access"]})
    assert invalid.status_code == 422
    valid = await client.put(
        SETTINGS_URL,
        headers=auth,
        json={
            "allowed_modes": ALL_MODES,
            "require_human_tools": ["execute"],
        },
    )
    assert valid.status_code == 200
    read = await client.get(SETTINGS_URL, headers=auth)
    assert read.json() == valid.json()


@pytest.mark.parametrize("mode", ["approve_for_me", "full_access"])
async def test_required_human_tool_cannot_be_bypassed_by_task_mode(modes_env, mode):
    env = modes_env
    client, _, auth, effects, _, reviews, *_ = env
    saved = await client.put(
        SETTINGS_URL,
        headers=auth,
        json={
            "allowed_modes": ALL_MODES,
            "require_human_tools": ["execute"],
        },
    )
    assert saved.status_code == 200
    created = await create(env, mode=mode)
    detail, item = await interaction(env, created["task_id"], "pending")
    assert item["payload"]["approval_mode"] == mode
    assert item["review"] is None and item["resolution"] is None
    assert reviews == [] and effects == []
    rejected = await respond(env, detail, item, approval(item, "deny"))
    assert rejected.status_code == 200
    assert (await settled(client, auth, created["task_id"]))["status"] == "completed"
    assert reviews == [] and effects == []


async def test_nested_execute_without_root_approval_is_never_dispatched(modes_env):
    client, _, auth, effects, generations, reviews, *_ = modes_env
    created = await create(modes_env, mode="full_access", prompt="delegate")
    final = await settled(client, auth, created["task_id"])
    assert final["status"] == "completed", final
    assert reviews == [] and effects == [], final
    assert final["turns"][0]["interactions"] == []
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "execute-once"
        and message.status == "error"
        for messages in generations
        for message in messages
    )


async def test_nested_read_tool_remains_available(modes_env):
    client, _, auth, effects, generations, reviews, *_ = modes_env
    created = await create(modes_env, mode="full_access", prompt="delegate-read")
    final = await settled(client, auth, created["task_id"])
    assert final["status"] == "completed", final
    assert reviews == [] and effects == []
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "read-once"
        and message.status == "success"
        for messages in generations
        for message in messages
    )


async def test_denied_action_cannot_be_retried_under_full_access(modes_env, monkeypatch):
    env = modes_env
    client, _, auth, effects, _, reviews, scenario, *_ = env
    scenario["decision"], scenario["repeat"] = "deny", True
    original = TaskApprovalReviewer.generate

    async def deny_then_switch(self, job):
        switched = await client.put(
            f"/api/project-tasks/{job.task_id}/approval-policy",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "expected_revision": 1,
                "mode": "full_access",
            },
        )
        assert switched.status_code == 200, switched.text
        return await original(self, job)

    monkeypatch.setattr(TaskApprovalReviewer, "generate", deny_then_switch)
    created = await create(env)
    final = await settled(client, auth, created["task_id"])
    assert final["status"] == "completed", final
    assert final["approval_policy"]["mode"] == "full_access"
    first, repeated = final["turns"][0]["interactions"]
    assert first["resolution"]["source"] == "auto_reviewer"
    assert repeated["resolution"]["source"] == "previous_denial"
    assert repeated["payload"]["approval_mode"] == "full_access"
    assert effects == [] and len(reviews) == 1


async def test_full_access_preserves_admin_reject_only_decisions(modes_env):
    env = modes_env
    client, server, auth, effects, _, reviews, *_ = env
    store = SecuritySettingsStore(settings_repo=server.services.settings_repo)
    policy = store.load().to_dict()
    policy["hitl"] = {"enabled": True, "tools": ["execute"], "allowed_decisions": ["reject"]}
    store.save(policy)
    created = await create(env, mode="full_access")
    detail, item = await interaction(env, created["task_id"], "pending")
    assert item["payload"]["allowed_decisions"] == ["deny"]
    assert (await respond(env, detail, item, approval(item))).status_code == 409
    assert (await respond(env, detail, item, approval(item, "deny"))).status_code == 200
    assert (await settled(client, auth, created["task_id"]))["status"] == "completed"
    assert reviews == [] and effects == []
