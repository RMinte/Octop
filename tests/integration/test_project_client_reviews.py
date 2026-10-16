"""Client command reviews cross the authenticated WebSocket without executing locally."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from octop_harness import HarnessAgent
from octop_harness.llm.factory import ChatModelFactory

from octop.infra.projects import approval_policy, approval_reviewer, runtime, title
from octop.infra.projects.approval_models import TaskApprovalSettings
from tests.integration.test_project_task_checkpoints import parked
from tests.integration.test_project_task_client_tools import CLIENT, claim, identity, result
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until
from tests.support.auth import create_user

ARGUMENTS = {"command": ["fixture-command", "--check"]}


@pytest.fixture
async def review_env(env_with_provider, monkeypatch):
    from octop.api.routers import project_tasks_ws

    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    approval_policy.save_settings(
        server.services,
        TaskApprovalSettings(allowed_modes=["ask_for_approval", "approve_for_me", "full_access"]),
    )
    state = SimpleNamespace(
        generations=[],
        reviews=[],
        name="exec_command",
        decision="allow_once",
        reason_code="review_allow",
        repeated_arguments=None,
        entered=asyncio.Event(),
        release=asyncio.Event(),
    )
    state.release.set()

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            state.generations.append(messages)
            if isinstance(messages[-1], HumanMessage):
                message = AIMessage(
                    content="Waiting for the computer. ",
                    tool_calls=[
                        {
                            "id": "native-exec-001",
                            "name": "client__" + state.name,
                            "args": ARGUMENTS,
                        }
                    ],
                )
            else:
                assert isinstance(messages[-1], ToolMessage)
                if (
                    state.repeated_arguments is not None
                    and sum(isinstance(item, ToolMessage) for item in messages) == 1
                ):
                    message = AIMessage(
                        content="Proposing a second command. ",
                        tool_calls=[
                            {
                                "id": "native-exec-002",
                                "name": "client__" + state.name,
                                "args": state.repeated_arguments,
                            }
                        ],
                    )
                else:
                    message = AIMessage(content="Client result received.")
            message.usage_metadata = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
            return ChatResult(generations=[ChatGeneration(message=message)])

    class Factory(ChatModelFactory):
        def get_chat_model(self, ref):
            return Model(responses=[])

    async def review(self, job):
        state.reviews.append(job)
        state.entered.set()
        await state.release.wait()
        return approval_reviewer.ApprovalReviewResult(state.decision, state.reason_code, 11, 4)

    async def no_title(self, job):
        return title.TitleResult()

    monkeypatch.setattr(
        runtime,
        "HarnessAgent",
        lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
    )
    monkeypatch.setattr(approval_reviewer.TaskApprovalReviewer, "generate", review)
    monkeypatch.setattr(title.TaskTitleAgent, "generate", no_title)
    try:
        yield client, server, auth, state
    finally:
        state.release.set()


async def start_exec(env, *, mode="approve_for_me"):
    client, server, auth, state = env
    session, ws = await connected(client, auth)
    await command(ws, "client.attach", "attach-review-client", client_id=CLIENT)
    created = await command(
        ws,
        "task.create",
        "create-review-task",
        data={
            "request_id": "create-client-review",
            "prompt": "Check the local workspace",
            "approval_mode": mode,
            "options": {
                "client_id": CLIENT,
                "client_tools": [
                    {
                        "name": state.name,
                        "description": "Execute a command on this computer.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["command"],
                            "additionalProperties": False,
                        },
                    }
                ],
            },
        },
    )
    assert created["type"] == "result", created
    call = (await until(ws, lambda frame: frame["type"] == "tool.call"))["data"]
    await parked(server, call["turn_id"])
    assert call["name"] == state.name and call["arguments"] == ARGUMENTS
    return session, ws, created["data"], call


def review_body(call, token):
    return {
        "request_id": "client-review-request",
        "claim_token": token,
        "action_digest": call["approval_context"]["action_digest"],
        "context": {
            # This deliberately does not exist on the server. Only its text is reviewed.
            "working_directory": "Z:\\client-only\\never-upload",
            "workspace_authorization_digest": "a" * 64,
            "command_authorization_id": "local-command-permission",
            "execution_profile": "workspace_write",
            "context_digest": "b" * 64,
        },
    }


async def submit_review(ws, call, body, key="review-command-001"):
    return await command(ws, "tool.review", key, **identity(call), data=body)


async def get_call(ws, call):
    response = await command(ws, "tool.get", "get-review-state", **identity(call))
    assert response["type"] == "result", response
    return response["data"]


async def reviewed(ws, call):
    async with asyncio.timeout(5):
        while True:
            current = await get_call(ws, call)
            if current["review"] and current["review"]["status"] in {"completed", "cancelled"}:
                return current
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("decision", ["allow_once", "deny", "requires_user"])
async def test_review_is_advice_and_only_the_actual_client_result_resumes_native_turn(
    review_env, decision
):
    client, server, auth, state = review_env
    state.decision = decision
    state.reason_code = {
        "allow_once": "review_allow",
        "deny": "review_deny",
        "requires_user": "review_requires_user",
    }[decision]
    session, ws, task, call = await start_exec(review_env)
    try:
        frozen = call["approval_context"]
        assert frozen["mode"] == "approve_for_me" and frozen["revision"] == 1
        assert not frozen["requires_user"] and call["dispatch_allowed"]
        token = (await claim(ws, call))["data"]["claim_token"]
        body = review_body(call, token)
        requested = await submit_review(ws, call, body)
        assert requested["type"] == "result", requested
        current = await reviewed(ws, call)
        receipt = current["review"]
        assert receipt["decision"] == decision
        assert receipt["action_digest"] == frozen["action_digest"]
        assert receipt["context_digest"] == body["context"]["context_digest"]
        assert current["approval_context"] == frozen and current["status"] == "claimed"
        assert "claim_token" not in json.dumps(current)
        assert len(state.reviews) == len(state.generations) == 1
        proposal = json.loads(state.reviews[0].proposal_json)
        assert proposal["arguments"] == ARGUMENTS
        assert proposal["working_directory"] == body["context"]["working_directory"]
        assert proposal["execution_host"] == "client"
        assert proposal["user_goal"] == "Check the local workspace"
        live = (await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)).json()
        assert live["status"] == "waiting_client_tool"
        assert live["active_turn_id"] == call["turn_id"]
        assert server.services.usage_repo.total_tokens_for_user(state.reviews[0].owner_id) == 25
        accepted = await result(
            ws, call, token, {"proof": "local-result"}, is_error=decision != "allow_once"
        )
        assert accepted["type"] == "result", accepted
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "completed" and len(final["turns"]) == 1
        assert final["turns"][0]["turn_id"] == call["turn_id"]
        assert len(state.generations) == 2 and len(state.reviews) == 1
        assert server.services.usage_repo.total_tokens_for_user(state.reviews[0].owner_id) == 35
    finally:
        await session.__aexit__(None, None, None)


async def test_review_retries_freeze_exact_facts_and_stream_a_token_free_receipt(review_env):
    _client, _server, _auth, state = review_env
    state.release.clear()
    session, ws, _task, call = await start_exec(review_env)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        body = review_body(call, token)
        first = await submit_review(ws, call, body)
        assert first["type"] == "result", first
        await asyncio.wait_for(state.entered.wait(), 5)
        duplicate = await submit_review(ws, call, body, "review-command-retry")
        assert duplicate["data"]["review"]["review_id"] == first["data"]["review"]["review_id"]
        different = deepcopy(body)
        different["context"]["working_directory"] = "different-client-directory"
        conflict = await submit_review(ws, call, different, "review-command-conflict")
        assert conflict["error"]["code"] == "TASK_APPROVAL_REVIEW_CONFLICT"
        changed = {**body, "arguments": {"command": ["rewrite-is-forbidden"]}}
        invalid = await submit_review(ws, call, changed, "review-command-invalid")
        assert invalid["error"]["code"] == "TASK_FRAME_INVALID"
        state.release.set()
        pushed = await until(
            ws,
            lambda frame: (
                frame["type"] == "tool.call"
                and frame["data"].get("review", {}).get("status") == "completed"
            ),
        )
        assert pushed["data"]["review"]["decision"] == "allow_once"
        assert "claim_token" not in json.dumps(pushed)
        assert len(state.reviews) == 1
    finally:
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize(
    "failure",
    ["unclaimed", "wrong_token", "wrong_digest", "no_attach", "wrong_client", "wrong_owner"],
)
async def test_review_rejects_unclaimed_or_cross_identity_requests_without_disclosing_the_call(
    review_env, failure
):
    client, _server, auth, state = review_env
    session, ws, _task, call = await start_exec(review_env)
    extra_session = None
    try:
        token = "unclaimed-token-0001"
        if failure != "unclaimed":
            token = (await claim(ws, call))["data"]["claim_token"]
        body = review_body(call, token)
        target = ws
        expected = "FORBIDDEN"
        if failure == "wrong_token":
            body["claim_token"] = "wrong-claim-token-0001"
        elif failure == "wrong_digest":
            body["action_digest"] = "c" * 64
            expected = "TASK_APPROVAL_REVIEW_CONFLICT"
        elif failure in {"no_attach", "wrong_client", "wrong_owner"}:
            other_auth = (
                await create_user(client, auth, username="review-outsider")
                if failure == "wrong_owner"
                else auth
            )
            extra_session, target = await connected(client, other_auth)
            if failure != "no_attach":
                await command(
                    target,
                    "client.attach",
                    "attach-identity-check",
                    client_id=CLIENT if failure == "wrong_owner" else "different-computer",
                )
            expected = {
                "no_attach": "TASK_FRAME_INVALID",
                "wrong_client": "FORBIDDEN",
                "wrong_owner": "NOT_FOUND",
            }[failure]
        response = await submit_review(target, call, body)
        assert response["type"] == "error" and response["error"]["code"] == expected, response
        assert token not in json.dumps(response)
        assert not state.reviews
        assert (await get_call(ws, call))["review"] is None
    finally:
        if extra_session is not None:
            await extra_session.__aexit__(None, None, None)
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("mode", ["ask_for_approval", "full_access"])
async def test_nonautomatic_modes_do_not_create_a_remote_review(review_env, mode):
    _client, _server, _auth, state = review_env
    session, ws, _task, call = await start_exec(review_env, mode=mode)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        response = await submit_review(ws, call, review_body(call, token))
        assert response["error"]["code"] == "TASK_APPROVAL_MODE_UNAVAILABLE", response
        assert (await get_call(ws, call))["review"] is None and not state.reviews
    finally:
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("before", [False, True])
async def test_administrator_required_human_approval_blocks_auto_review_even_after_proposal(
    review_env, before
):
    _client, server, _auth, state = review_env
    settings = TaskApprovalSettings(
        allowed_modes=["ask_for_approval", "approve_for_me", "full_access"],
        require_human_tools=["client__exec_command"],
    )
    if before:
        approval_policy.save_settings(server.services, settings)
    session, ws, _task, call = await start_exec(review_env)
    try:
        if not before:
            approval_policy.save_settings(server.services, settings)
        current = await get_call(ws, call)
        assert current["manual_review_required"]
        assert call["approval_context"]["requires_user"] is before
        token = (await claim(ws, call))["data"]["claim_token"]
        response = await submit_review(ws, call, review_body(call, token))
        assert response["error"]["code"] == "TASK_APPROVAL_REVIEW_INVALID", response
        assert not state.reviews
    finally:
        await session.__aexit__(None, None, None)


async def test_only_command_contracts_can_request_automatic_review(review_env):
    _client, _server, _auth, state = review_env
    state.name = "read_note"
    session, ws, _task, call = await start_exec(review_env)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        response = await submit_review(ws, call, review_body(call, token))
        assert response["error"]["code"] == "TASK_APPROVAL_REVIEW_INVALID", response
        assert not state.reviews
    finally:
        await session.__aexit__(None, None, None)


async def test_completed_review_survives_worker_restart_and_keeps_frozen_mode(review_env):
    client, server, auth, state = review_env
    session, ws, task, call = await start_exec(review_env)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        body = review_body(call, token)
        await submit_review(ws, call, body)
        done = await reviewed(ws, call)
        changed = await client.put(
            f"/api/project-tasks/{task['task_id']}/approval-policy",
            headers=auth,
            json={
                "request_id": "switch-future-calls",
                "expected_revision": 1,
                "mode": "ask_for_approval",
            },
        )
        assert changed.status_code == 200, changed.text
        await server.project_task_worker.close()
        server.project_task_worker = runtime.ProjectTaskWorker(server.services)
        await server.project_task_worker.start()
        duplicate = await submit_review(ws, call, body, "review-after-restart")
        assert duplicate["data"]["review"] == done["review"]
        assert duplicate["data"]["approval_context"] == call["approval_context"]
        assert len(state.reviews) == len(state.generations) == 1
        await result(ws, call, token, {"proof": "after-restart"})
        assert (await settled(client, auth, task["task_id"]))["status"] == "completed"
        assert len(state.reviews) == 1
    finally:
        await session.__aexit__(None, None, None)


async def test_pause_prevents_dispatch_and_cancel_discards_late_allow_but_bills_usage(review_env):
    _client, server, _auth, state = review_env
    state.release.clear()
    session, ws, task, call = await start_exec(review_env)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        await submit_review(ws, call, review_body(call, token))
        await asyncio.wait_for(state.entered.wait(), 5)
        paused = await command(
            ws,
            "task.pause",
            "pause-before-client-dispatch",
            task_id=task["task_id"],
            data={"request_id": "pause-client-review", "turn_id": call["turn_id"]},
        )
        assert paused["type"] == "result", paused
        assert not (await get_call(ws, call))["dispatch_allowed"]
        cancelled = await command(
            ws,
            "task.cancel",
            "cancel-client-review",
            task_id=task["task_id"],
            data={"turn_id": call["turn_id"]},
        )
        assert cancelled["data"]["status"] == "cancelled", cancelled
        state.release.set()
        current = await reviewed(ws, call)
        assert current["status"] == "cancelled" and not current["dispatch_allowed"]
        assert current["review"]["status"] == "cancelled" and current["review"]["decision"] is None
        async with asyncio.timeout(3):
            while server.services.usage_repo.total_tokens_for_user(state.reviews[0].owner_id) != 25:
                await asyncio.sleep(0.01)
        late = await result(ws, call, token, {"proof": "late"})
        assert late["error"]["code"] == "TASK_CLIENT_TOOL_CLOSED"
        assert len(state.generations) == len(state.reviews) == 1
    finally:
        await session.__aexit__(None, None, None)


async def test_review_timeout_becomes_human_advice_without_executing_the_client_command(
    review_env, monkeypatch
):
    _client, _server, _auth, state = review_env
    state.release.clear()
    monkeypatch.setattr(approval_reviewer, "_GENERATION_TIMEOUT", 0.01)
    session, ws, _task, call = await start_exec(review_env)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        await submit_review(ws, call, review_body(call, token))
        current = await reviewed(ws, call)
        assert current["review"]["decision"] == "requires_user"
        assert current["review"]["reason_code"] == "review_timeout"
        assert current["status"] == "claimed" and len(state.generations) == 1
        assert len(state.reviews) == 1
    finally:
        await session.__aexit__(None, None, None)


@pytest.mark.parametrize("switch_to_full", [False, True])
async def test_denied_command_cannot_be_reproposed_with_new_call_id_or_full_access(
    review_env, switch_to_full
):
    client, server, auth, state = review_env
    state.decision, state.reason_code = "deny", "review_deny"
    state.repeated_arguments = deepcopy(ARGUMENTS)
    session, ws, task, call = await start_exec(review_env)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        await submit_review(ws, call, review_body(call, token))
        assert (await reviewed(ws, call))["review"]["decision"] == "deny"
        if switch_to_full:
            response = await client.put(
                f"/api/project-tasks/{task['task_id']}/approval-policy",
                headers=auth,
                json={
                    "request_id": "full-after-review-deny",
                    "expected_revision": 1,
                    "mode": "full_access",
                },
            )
            assert response.status_code == 200, response.text
        await result(ws, call, token, {"error": "Denied by remote review"}, is_error=True)
        final = await settled(client, auth, task["task_id"])
        assert final["status"] == "failed", final
        assert final["turns"][0]["error_code"] == "TASK_CLIENT_TOOL_CLOSED"
        assert len(state.generations) == 2 and len(state.reviews) == 1
        with server.services.db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM project_task_tool_calls WHERE turn_id=?",
                    (call["turn_id"],),
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM project_task_reviews WHERE turn_id=?", (call["turn_id"],)
                ).fetchone()[0]
                == 1
            )
    finally:
        await session.__aexit__(None, None, None)


async def test_a_changed_command_can_be_reviewed_after_an_earlier_denial(review_env):
    client, _server, auth, state = review_env
    state.decision, state.reason_code = "deny", "review_deny"
    state.repeated_arguments = {"command": ["fixture-command", "--read-only"]}
    session, ws, task, call = await start_exec(review_env)
    try:
        token = (await claim(ws, call))["data"]["claim_token"]
        await submit_review(ws, call, review_body(call, token))
        assert (await reviewed(ws, call))["review"]["decision"] == "deny"
        await result(ws, call, token, {"error": "Denied"}, is_error=True)
        second = (
            await until(
                ws,
                lambda frame: (
                    frame["type"] == "tool.call" and frame["data"]["call_id"] != call["call_id"]
                ),
            )
        )["data"]
        assert second["arguments"] == state.repeated_arguments
        state.decision, state.reason_code = "allow_once", "review_allow"
        second_token = (await claim(ws, second))["data"]["claim_token"]
        response = await submit_review(ws, second, review_body(second, second_token))
        assert response["type"] == "result", response
        assert (await reviewed(ws, second))["review"]["decision"] == "allow_once"
        await result(ws, second, second_token, {"proof": "safer-command"})
        assert (await settled(client, auth, task["task_id"]))["status"] == "completed"
        assert len(state.generations) == 3 and len(state.reviews) == 2
    finally:
        await session.__aexit__(None, None, None)
