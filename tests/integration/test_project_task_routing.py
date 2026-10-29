"""Task routing is an explicit per-turn choice shared by REST and WebSocket."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from PIL import Image

from octop.infra.projects import runtime, task_submission
from octop.infra.projects.routing_classifier import DifficultyAssessment
from tests.support.auth import create_user
from tests.support.http import ws_connect, ws_token

BOX_MODEL = "Box/RM-01 VLM"
REMOTE_MODEL = "remote/cloud-chat"
SETTINGS_URL = "/api/project-tasks/routing/settings"


async def _seed_models(
    client: httpx.AsyncClient, auth: dict[str, str], *, remote: bool = True
) -> None:
    providers = [
        ("Box", "box", "http://box.internal:58000/v1", "RM-01 VLM", ""),
    ]
    if remote:
        providers.append(
            ("remote", "openai", "https://models.example/v1", "cloud-chat", "routing-secret")
        )
    for name, kind, url, model, key in providers:
        response = await client.post(
            "/api/admin/providers",
            headers=auth,
            json={
                "name": name,
                "kind": kind,
                "base_url": url,
                "api_key": key,
                "models": [{"id": model, "enabled": True, "input": ["text", "image"]}],
            },
        )
        assert response.status_code == 201, response.text
    active = await client.put(
        "/api/providers/active-model",
        headers=auth,
        json={
            "provider_name": "remote" if remote else "Box",
            "model": "cloud-chat" if remote else "RM-01 VLM",
        },
    )
    assert active.status_code == 200, active.text


async def _configure(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    *,
    remote: bool = True,
    level: int = 100,
) -> None:
    response = await client.put(
        SETTINGS_URL,
        headers=auth,
        json={
            "box_model_ref": BOX_MODEL,
            "remote_model_ref": REMOTE_MODEL if remote else None,
            "default_level": level,
        },
    )
    assert response.status_code == 200, response.text


async def _submit(
    client: Any,
    auth: dict[str, str],
    transport: str,
    body: dict[str, Any],
    *,
    task_id: str | None = None,
    retry_of: str | None = None,
    status: int = 202,
) -> dict[str, Any]:
    if transport == "rest":
        if retry_of is not None:
            url = f"/api/project-tasks/{task_id}/turns/{retry_of}/retry"
        elif task_id is not None:
            url = f"/api/project-tasks/{task_id}/messages"
        else:
            url = "/api/project-tasks"
        response = await client.post(url, headers=auth, json=body)
        assert response.status_code == status, response.text
        return response.json()
    async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
        await ws.send_json({"type": "authenticate", "token": ws_token(auth)})
        assert (await ws.receive_json())["type"] == "ready"
        command_id = "routing-" + uuid4().hex
        await ws.send_json(
            {
                "type": "task.retry" if retry_of else "task.message" if task_id else "task.create",
                "id": command_id,
                **({"task_id": task_id} if task_id else {}),
                **({"turn_id": retry_of} if retry_of else {}),
                "data": body,
            }
        )
        for _ in range(30):
            frame = await ws.receive_json(timeout=5)
            if frame.get("id") == command_id:
                if status == 202:
                    assert frame["type"] == "result", frame
                    return frame["data"]
                assert frame["type"] == "error" and frame["status"] == status, frame
                return frame
        pytest.fail("routing command did not return a result")


async def _settled(client: Any, auth: dict[str, str], task_id: str) -> dict[str, Any]:
    for _ in range(300):
        response = await client.get(f"/api/project-tasks/{task_id}", headers=auth)
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["active_turn_id"] is None:
            return detail
        await asyncio.sleep(0.01)
    pytest.fail("routed task worker did not finish")


@pytest.fixture
async def routing_env(env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    client, server, auth = env
    await _seed_models(client, auth)
    await _configure(client, auth)
    judge = AsyncMock(return_value=DifficultyAssessment(9, "basic"))
    monkeypatch.setattr(task_submission, "classify_difficulty", judge)
    calls: list[Any] = []

    async def run_turn(services: Any, owner: int, turn: Any, history: Any) -> Any:
        calls.append(turn)
        yield {"type": "token", "content": f"Executed with {turn.model_ref}"}
        if turn.prompt.startswith("fail") and not json.loads(turn.configuration_json).get(
            "retry_of_turn_id"
        ):
            raise RuntimeError("fixture execution failure")

    monkeypatch.setattr(runtime, "run_turn", run_turn)
    return client, server, auth, judge, calls


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_hybrid_rejudges_each_turn_and_uses_the_recorded_model(
    routing_env: Any, transport: str
) -> None:
    client, _server, auth, judge, calls = routing_env
    judge.side_effect = [
        DifficultyAssessment(9, "basic", input_tokens=11, output_tokens=3, duration_ms=4),
        DifficultyAssessment(10, "reasoning"),
    ]
    first = await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "routing-simple",
            "prompt": "Hello",
            "options": {"routing": {"mode": "hybrid", "level": 100}},
        },
    )
    await _settled(client, auth, first["task_id"])
    await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "routing-complex",
            "prompt": "Now diagnose and redesign the dependent components",
            "options": {"routing": {"mode": "hybrid"}},
        },
        task_id=first["task_id"],
    )
    detail = await _settled(client, auth, first["task_id"])
    assert [call.model_ref for call in calls] == [BOX_MODEL, REMOTE_MODEL]
    assert [turn["model_ref"] for turn in detail["turns"]] == [BOX_MODEL, REMOTE_MODEL]
    for turn, call, target, difficulty in zip(
        detail["turns"], calls, ["box", "remote"], [9, 10], strict=True
    ):
        routing = turn["routing"]
        assert routing["mode"] == "hybrid" and routing["level"] == 100
        assert routing["target"] == target and routing["difficulty"] == difficulty
        assert routing["model_ref"] == turn["model_ref"] == call.model_ref
        assert routing["policy_version"] == "difficulty-v1"
        assert turn["reply"] == f"Executed with {call.model_ref}"
    first_routing = detail["turns"][0]["routing"]
    assert first_routing["classifier_input_tokens"] == 11
    assert first_routing["classifier_output_tokens"] == 3
    assert first_routing["classifier_duration_ms"] == 4
    assert judge.await_count == 2
    first_facts, second_facts = [call.args[2] for call in judge.await_args_list]
    assert first_facts.prompt == "Hello" and first_facts.history_summary == ""
    assert "Hello" in second_facts.history_summary
    assert second_facts.prompt == "Now diagnose and redesign the dependent components"
    active = (await client.get("/api/providers/active-model", headers=auth)).json()
    assert active == {"provider_name": "remote", "model": "cloud-chat"}


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_explicit_and_legacy_selection_skip_routing_judge(
    routing_env: Any, transport: str
) -> None:
    client, _server, auth, judge, calls = routing_env
    for request_id, options, expected in (
        ("legacy-default", {}, REMOTE_MODEL),
        ("legacy-null", {"model_ref": None, "routing": None}, REMOTE_MODEL),
        (
            "explicit-box",
            {"model_ref": BOX_MODEL, "routing": {"mode": "hybrid", "level": 100}},
            BOX_MODEL,
        ),
    ):
        created = await _submit(
            client,
            auth,
            transport,
            {"request_id": request_id, "prompt": "Use the requested model", "options": options},
        )
        done = await _settled(client, auth, created["task_id"])
        turn = done["turns"][0]
        assert turn["model_ref"] == expected
        if request_id.startswith("legacy"):
            assert turn["routing"] is None
        else:
            assert turn["routing"]["target"] == "explicit"
            assert turn["routing"]["reason_code"] == "explicit_model"
    unavailable = await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "unavailable-explicit",
            "prompt": "Do not silently fall back",
            "options": {
                "model_ref": "missing/model",
                "routing": {"mode": "hybrid", "level": 100},
            },
        },
        status=409,
    )
    assert unavailable["error"]["code"] == "TASK_MODEL_UNAVAILABLE"
    judge.assert_not_awaited()
    assert [call.model_ref for call in calls] == [REMOTE_MODEL, REMOTE_MODEL, BOX_MODEL]


@pytest.mark.parametrize("transport", ["rest", "websocket"])
@pytest.mark.parametrize("mode", ["box", "hybrid"])
async def test_box_and_zero_level_work_without_remote_configuration(
    env: Any, monkeypatch: pytest.MonkeyPatch, transport: str, mode: str
) -> None:
    client, _server, auth = env
    await _seed_models(client, auth, remote=False)
    await _configure(client, auth, remote=False, level=0)
    judge = AsyncMock(side_effect=AssertionError("zero level must not classify"))
    monkeypatch.setattr(task_submission, "classify_difficulty", judge)
    models = []

    async def run_turn(services: Any, owner: int, turn: Any, history: Any) -> Any:
        models.append(turn.model_ref)
        yield {"type": "token", "content": "Box result"}

    monkeypatch.setattr(runtime, "run_turn", run_turn)
    created = await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "zero-remote",
            "prompt": "Work with the box alone",
            "options": {"routing": {"mode": mode, "level": 0}},
        },
    )
    done = await _settled(client, auth, created["task_id"])
    assert models == [BOX_MODEL]
    assert done["turns"][0]["routing"]["target"] == "box"
    assert done["turns"][0]["routing"]["level"] == 0
    judge.assert_not_awaited()


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_create_followup_and_retry_replays_do_not_rejudge(
    routing_env: Any, transport: str
) -> None:
    client, _server, auth, judge, calls = routing_env
    judge.side_effect = [
        DifficultyAssessment(10, "reasoning"),
        DifficultyAssessment(9, "basic"),
        DifficultyAssessment(10, "reasoning"),
    ]
    body = {
        "request_id": "replay-create",
        "prompt": "fail first round",
        "options": {"routing": {"mode": "hybrid", "level": 100}},
    }
    first = await _submit(client, auth, transport, body)
    task_id = first["task_id"]
    await _settled(client, auth, task_id)
    replay = await _submit(client, auth, transport, body)
    assert replay["task_id"] == task_id and len(replay["turns"]) == 1
    assert judge.await_count == 1
    followup = {**body, "request_id": "replay-followup", "prompt": "fail followup"}
    await _submit(client, auth, transport, followup, task_id=task_id)
    failed = await _settled(client, auth, task_id)
    assert failed["turns"][-1]["status"] == "failed"
    await _submit(client, auth, transport, followup, task_id=task_id)
    assert judge.await_count == 2
    original = failed["turns"][-1]
    retry = {"request_id": "replay-retry", "options": body["options"]}
    await _submit(client, auth, transport, retry, task_id=task_id, retry_of=original["turn_id"])
    done = await _settled(client, auth, task_id)
    await _configure(client, auth, level=0)
    replay = await _submit(
        client, auth, transport, retry, task_id=task_id, retry_of=original["turn_id"]
    )
    assert len(replay["turns"]) == 3
    assert replay["turns"][-1]["routing"] == done["turns"][-1]["routing"]
    assert replay["turns"][-1]["retry_of_turn_id"] == original["turn_id"]
    assert judge.await_count == len(calls) == 3
    assert judge.await_args_list[-1].args[2].prompt == original["prompt"]
    assert [call.model_ref for call in calls] == [REMOTE_MODEL, BOX_MODEL, REMOTE_MODEL]
    conflict = await _submit(
        client,
        auth,
        transport,
        {**retry, "options": {"routing": {"mode": "hybrid", "level": 0}}},
        task_id=task_id,
        retry_of=original["turn_id"],
        status=409,
    )
    assert conflict["error"]["code"] == "TASK_REQUEST_CONFLICT"
    assert judge.await_count == 3


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_foreign_resources_are_rejected_before_judge(
    routing_env: Any, transport: str
) -> None:
    client, _server, auth, judge, calls = routing_env
    project_response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": uuid4().hex},
        json={"name": "Owner only", "instructions": "Private project facts"},
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["project_id"]
    first = await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "owner-original",
            "prompt": "fail private original",
            "project_id": project_id,
            "options": {"model_ref": BOX_MODEL},
        },
    )
    failed = await _settled(client, auth, first["task_id"])
    alice = await create_user(client, auth, username="routing-alice")
    options = {"routing": {"mode": "hybrid", "level": 100}}
    await _submit(
        client,
        alice,
        transport,
        {"request_id": "foreign-followup", "prompt": "Read private history", "options": options},
        task_id=first["task_id"],
        status=404,
    )
    await _submit(
        client,
        alice,
        transport,
        {"request_id": "foreign-retry", "options": options},
        task_id=first["task_id"],
        retry_of=failed["turns"][0]["turn_id"],
        status=404,
    )
    await _submit(
        client,
        alice,
        transport,
        {
            "request_id": "foreign-project",
            "project_id": project_id,
            "prompt": "Read private instructions",
            "options": options,
        },
        status=404,
    )
    judge.assert_not_awaited()
    assert len(calls) == 1


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_routing_input_rejects_unsupported_modes_and_non_strict_levels(
    routing_env: Any, transport: str
) -> None:
    client, _server, auth, judge, calls = routing_env
    invalid = [
        {"mode": "remote"},
        {"mode": "hybrid", "level": True},
        {"mode": "hybrid", "level": "20"},
        {"mode": "hybrid", "level": 20.0},
        {"mode": "hybrid", "level": 10},
        {"mode": "hybrid", "level": -20},
        {"mode": "hybrid", "level": 120},
        {"mode": "box", "level": 20},
        {"mode": "hybrid", "unknown": True},
    ]
    for index, routing in enumerate(invalid):
        await _submit(
            client,
            auth,
            transport,
            {
                "request_id": f"invalid-routing-{index}",
                "prompt": "Hello",
                "options": {"routing": routing},
            },
            status=422,
        )
    judge.assert_not_awaited()
    assert calls == []
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []


async def test_routing_resources_are_safe_and_policy_changes_require_permission(
    routing_env: Any,
) -> None:
    client, _server, auth, _judge, _calls = routing_env
    alice = await create_user(client, auth, username="routing-resources")
    response = await client.get("/api/project-tasks/resources", headers=alice)
    assert response.status_code == 200, response.text
    assert response.json()["routing"] == {
        "modes": ["box", "hybrid"],
        "levels": [0, 20, 40, 60, 80, 100],
        "default_level": 100,
        "box_model_ref": BOX_MODEL,
        "remote_model_ref": REMOTE_MODEL,
        "box_available": True,
        "remote_available": True,
    }
    for secret in ("routing-secret", "box.internal", "models.example", "api_key", "base_url"):
        assert secret not in response.text
    denied = await client.put(
        SETTINGS_URL,
        headers=alice,
        json={"box_model_ref": BOX_MODEL, "remote_model_ref": REMOTE_MODEL, "default_level": 0},
    )
    assert denied.status_code == 403
    settings = await client.get(SETTINGS_URL, headers=auth)
    assert settings.status_code == 200 and settings.json()["default_level"] == 100
    invalid = await client.put(
        SETTINGS_URL,
        headers=auth,
        json={"box_model_ref": BOX_MODEL, "default_level": "20"},
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize("transport", ["rest", "websocket"])
@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_classifier_failure_falls_back_to_box_without_exposing_errors(
    routing_env: Any, transport: str, failure: str
) -> None:
    client, _server, auth, judge, calls = routing_env
    if failure == "timeout":
        judge.return_value = DifficultyAssessment(reason_code="classifier_timeout")
    else:
        judge.side_effect = RuntimeError("classifier-secret")
    created = await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "classifier-failure",
            "prompt": "Judge this task",
            "options": {"routing": {"mode": "hybrid", "level": 100}},
        },
    )
    done = await _settled(client, auth, created["task_id"])
    decision = done["turns"][0]["routing"]
    assert calls[0].model_ref == decision["model_ref"] == BOX_MODEL
    assert decision["target"] == "box" and decision["difficulty"] is None
    assert decision["reason_code"] == (
        "classifier_timeout" if failure == "timeout" else "classifier_error"
    )
    assert "classifier-secret" not in json.dumps(done)


async def test_cancelling_judge_does_not_admit_a_turn_or_hold_a_write_transaction(
    routing_env: Any,
) -> None:
    client, _server, auth, judge, calls = routing_env
    started = asyncio.Event()

    async def pending(*args: Any) -> DifficultyAssessment:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled classifier resumed")

    judge.side_effect = pending
    request = asyncio.create_task(
        client.post(
            "/api/project-tasks",
            headers=auth,
            json={
                "request_id": "cancel-before-admission",
                "prompt": "Wait for classification",
                "options": {"routing": {"mode": "hybrid", "level": 100}},
            },
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        project = await asyncio.wait_for(
            client.post(
                "/api/projects",
                headers={**auth, "Idempotency-Key": uuid4().hex},
                json={"name": "Independent concurrent write"},
            ),
            timeout=5,
        )
        assert project.status_code == 201, project.text
    finally:
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert calls == []
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []


async def test_project_change_during_judge_rejects_stale_decision(routing_env: Any) -> None:
    client, _server, auth, judge, calls = routing_env
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": uuid4().hex},
            json={"name": "Changing project", "instructions": "Initial project instructions"},
        )
    ).json()
    started, release = asyncio.Event(), asyncio.Event()

    async def pending(*args: Any) -> DifficultyAssessment:
        started.set()
        await release.wait()
        return DifficultyAssessment(10, "reasoning")

    judge.side_effect = pending
    url = f"/api/projects/{project['project_id']}/tasks"
    body = {
        "request_id": "project-routing-change",
        "prompt": "Use the current project instructions",
        "options": {"routing": {"mode": "hybrid", "level": 100}},
    }
    request = asyncio.create_task(client.post(url, headers=auth, json=body))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        changed = await client.patch(
            f"/api/projects/{project['project_id']}",
            headers=auth,
            json={"expected_revision": 1, "instructions": "Updated project instructions"},
        )
        assert changed.status_code == 200, changed.text
    finally:
        release.set()
    rejected = await request
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "TASK_REVISION_CONFLICT"
    assert calls == []
    assert judge.await_args.args[2].project_instructions == "Initial project instructions"
    assert (await client.get(url, headers=auth)).json()["items"] == []

    judge.side_effect = None
    judge.return_value = DifficultyAssessment(10, "reasoning")
    accepted = await client.post(url, headers=auth, json=body)
    assert accepted.status_code == 202, accepted.text
    done = await _settled(client, auth, accepted.json()["task_id"])
    assert judge.await_args.args[2].project_instructions == "Updated project instructions"
    assert done["turns"][0]["project_revision"] == 2
    assert done["turns"][0]["model_ref"] == calls[0].model_ref == REMOTE_MODEL


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_historical_image_keeps_box_when_remote_model_is_text_only(
    routing_env: Any, transport: str
) -> None:
    client, server, auth, judge, calls = routing_env
    remote = server.services.provider_repo.get_by_name("remote")
    assert remote is not None
    updated = await client.patch(
        f"/api/admin/providers/{remote.id}",
        headers=auth,
        json={"models": [{"id": "cloud-chat", "enabled": True, "input": ["text"]}]},
    )
    assert updated.status_code == 200, updated.text
    content = io.BytesIO()
    Image.new("RGB", (3, 2), color=(30, 90, 180)).save(content, format="PNG")
    uploaded = await client.post(
        "/api/project-tasks/attachments",
        headers=auth,
        files={"file": ("reference.png", content.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    image = uploaded.json()
    first = await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "historical-image-first",
            "prompt": "Remember this diagram",
            "attachment_ids": [image["attachment_id"]],
            "options": {"model_ref": BOX_MODEL},
        },
    )
    await _settled(client, auth, first["task_id"])
    judge.return_value = DifficultyAssessment(100, "reasoning")
    await _submit(
        client,
        auth,
        transport,
        {
            "request_id": "historical-image-followup",
            "prompt": "Diagnose the complex dependencies shown in the earlier diagram",
            "options": {"routing": {"mode": "hybrid", "level": 100}},
        },
        task_id=first["task_id"],
    )
    done = await _settled(client, auth, first["task_id"])
    assert done["turns"][0]["attachments"] == [image]
    turn = done["turns"][1]
    assert turn["attachments"] == []
    assert turn["model_ref"] == BOX_MODEL
    assert turn["routing"]["target"] == "box"
    assert turn["routing"]["reason_code"] == "capability_image_box"
    assert turn["routing"]["difficulty"] is None
    judge.assert_not_awaited()
    assert [call.model_ref for call in calls] == [BOX_MODEL, BOX_MODEL]


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_concurrent_same_request_has_one_turn_and_one_final_routing_decision(
    routing_env: Any, transport: str
) -> None:
    client, _server, auth, judge, calls = routing_env
    both_started, release = asyncio.Event(), asyncio.Event()
    entered = 0

    async def delayed(*args: Any) -> DifficultyAssessment:
        nonlocal entered
        entered += 1
        assessment = (
            DifficultyAssessment(9, "basic")
            if entered == 1
            else DifficultyAssessment(80, "reasoning")
        )
        if entered == 2:
            both_started.set()
        await release.wait()
        return assessment

    judge.side_effect = delayed
    body = {
        "request_id": "concurrent-routing-create",
        "prompt": "Admit this request once",
        "options": {"routing": {"mode": "hybrid", "level": 100}},
    }
    requests = [
        asyncio.create_task(_submit(client, auth, transport, body)),
        asyncio.create_task(_submit(client, auth, transport, body)),
    ]
    try:
        await asyncio.wait_for(both_started.wait(), timeout=5)
    finally:
        release.set()
        responses = await asyncio.gather(*requests)
    assert responses[0]["task_id"] == responses[1]["task_id"]
    assert responses[0]["turns"][0]["turn_id"] == responses[1]["turns"][0]["turn_id"]
    assert responses[0]["turns"][0]["routing"] == responses[1]["turns"][0]["routing"]
    done = await _settled(client, auth, responses[0]["task_id"])
    assert len(done["turns"]) == len(calls) == 1
    decision = done["turns"][0]["routing"]
    assert done["turns"][0]["model_ref"] == calls[0].model_ref == decision["model_ref"]
    assert decision["model_ref"] in {BOX_MODEL, REMOTE_MODEL}
    assert decision == responses[0]["turns"][0]["routing"]
    judged = judge.await_count
    accepted_replay = await _submit(client, auth, transport, body)
    assert accepted_replay["turns"][0]["routing"] == decision
    assert judge.await_count == judged
    assert len(calls) == 1


async def test_image_in_saved_tool_context_constrains_routing_without_attachments(
    routing_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, server, auth, judge, calls = routing_env
    remote = server.services.provider_repo.get_by_name("remote")
    assert remote is not None
    response = await client.patch(
        f"/api/admin/providers/{remote.id}",
        headers=auth,
        json={"models": [{"id": "cloud-chat", "enabled": True, "input": ["text"]}]},
    )
    assert response.status_code == 200, response.text
    original_runtime = runtime.run_turn

    async def save_tool_context(services: Any, owner: int, turn: Any, history: Any) -> Any:
        if turn.request_id == "tool-image-first":
            messages = [
                HumanMessage(content=turn.prompt),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "create_diagram", "args": {}, "id": "diagram-call"}],
                ),
                ToolMessage(
                    content=[
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://images.example/diagram.png"},
                        }
                    ],
                    tool_call_id="diagram-call",
                ),
                AIMessage(content="Created the diagram"),
            ]
            assert services.project_task_turn_repo.save_context(
                turn.turn_id,
                turn.worker_id,
                json.dumps([message_to_dict(message) for message in messages]),
            )
        async for event in original_runtime(services, owner, turn, history):
            yield event

    monkeypatch.setattr(runtime, "run_turn", save_tool_context)
    first = await _submit(
        client,
        auth,
        "rest",
        {
            "request_id": "tool-image-first",
            "prompt": "Generate a diagram with the tool",
            "options": {"model_ref": BOX_MODEL},
        },
    )
    await _settled(client, auth, first["task_id"])
    await _submit(
        client,
        auth,
        "rest",
        {
            "request_id": "tool-image-followup",
            "prompt": "Analyze the diagram from the earlier tool result",
            "options": {"routing": {"mode": "hybrid", "level": 100}},
        },
        task_id=first["task_id"],
    )
    done = await _settled(client, auth, first["task_id"])
    assert all(turn["attachments"] == [] for turn in done["turns"])
    decision = done["turns"][1]["routing"]
    assert decision["target"] == "box" and decision["model_ref"] == BOX_MODEL
    assert decision["reason_code"] == "capability_image_box"
    assert decision["difficulty"] is None
    assert [call.model_ref for call in calls] == [BOX_MODEL, BOX_MODEL]
    judge.assert_not_awaited()


@pytest.mark.parametrize("transport", ["rest", "websocket"])
async def test_stars_drive_routing_without_hidden_bindings(
    routing_env: Any, transport: str
) -> None:
    client, server, auth, judge, calls = routing_env
    server.services.settings_repo.delete("task_model_routing")
    for name, kind in (("second-box", "box"), ("zz-preferred-remote", "openai")):
        response = await client.post(
            "/api/admin/providers",
            headers=auth,
            json={
                "name": name,
                "kind": kind,
                "base_url": "http://models.invalid/v1",
                "api_key": "fixture-key",
                "models": [{"id": "text", "enabled": True}],
            },
        )
        assert response.status_code == 201
    remote = "zz-preferred-remote/text"
    box = "second-box/text"
    response = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_model": remote}
    )
    assert response.status_code == 200
    response = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_box_model": box}
    )
    assert response.status_code == 200
    assert response.json()["preferred_model"] == remote
    assert response.json()["preferred_box_model"] == box
    resources = (await client.get("/api/project-tasks/resources", headers=auth)).json()["routing"]
    assert resources["box_model_ref"] == box and resources["remote_model_ref"] == remote
    assert resources["box_available"] and resources["remote_available"]
    effective = (await client.get(SETTINGS_URL, headers=auth)).json()
    assert effective["box_model_ref"] == box and effective["remote_model_ref"] == remote
    judge.side_effect = [DifficultyAssessment(9, "basic"), DifficultyAssessment(90, "reasoning")]
    for number, expected in enumerate((box, remote)):
        result = await _submit(
            client,
            auth,
            transport,
            {
                "request_id": f"preferred-{number}",
                "prompt": "Route with my preferences",
                "options": {"routing": {"mode": "hybrid", "level": 60}},
            },
        )
        detail = await _settled(client, auth, result["task_id"])
        assert detail["turns"][0]["model_ref"] == expected
    assert [call.model_ref for call in calls] == [box, remote]
    # A separate user must never inherit the first user's stars.
    other_auth = await create_user(client, auth, username="independent-stars")
    other = (await client.get("/api/project-tasks/resources", headers=other_auth)).json()["routing"]
    assert other["box_model_ref"] == BOX_MODEL and other["remote_model_ref"] == REMOTE_MODEL
    invalid = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_box_model": remote}
    )
    assert invalid.status_code == 409
    saved = (await client.get("/api/preferences", headers=auth)).json()
    assert saved["preferred_box_model"] == box and saved["preferred_model"] == remote
    cleared = await client.patch(
        "/api/preferences", headers=auth, json={"preferred_box_model": None}
    )
    assert cleared.status_code == 200 and cleared.json()["preferred_model"] == remote


@pytest.mark.parametrize("transport", ["rest", "ws"])
async def test_unstarred_routing_uses_latest_flagship(routing_env: Any, transport: str) -> None:
    client, server, auth, judge, calls = routing_env
    provider = server.services.provider_repo.get_by_name("remote")
    assert provider is not None
    server.services.provider_repo.update(
        provider.id,
        models_json=json.dumps(
            [
                {"id": "cloud-chat", "enabled": True},
                {"id": "glm-4.7-flash", "enabled": True},
                {"id": "glm-5.1", "enabled": True},
                {"id": "glm-5.2", "enabled": True},
                {"id": "glm-5.3", "enabled": False},
            ]
        ),
    )
    # Existing deployment and global defaults point to cloud-chat. They must
    # not mask the new automatic policy for an unstarred user.
    effective = (await client.get(SETTINGS_URL, headers=auth)).json()
    assert effective["remote_model_ref"] == "remote/glm-5.2"
    for number, star in enumerate((None, "remote/glm-4.7-flash")):
        response = await client.patch(
            "/api/preferences", headers=auth, json={"preferred_model": star}
        )
        assert response.status_code == 200
        judge.return_value = DifficultyAssessment(90, "reasoning")
        result = await _submit(
            client,
            auth,
            transport,
            {
                "request_id": f"flagship-{number}",
                "prompt": "Use my automatic selection policy",
                "options": {"routing": {"mode": "hybrid", "level": 60}},
            },
        )
        detail = await _settled(client, auth, result["task_id"])
        assert detail["turns"][0]["model_ref"] == (star or "remote/glm-5.2")
    assert [call.model_ref for call in calls] == ["remote/glm-5.2", "remote/glm-4.7-flash"]
