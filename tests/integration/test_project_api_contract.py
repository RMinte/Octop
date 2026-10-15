"""AgentComputer keeps a dedicated protocol over the shared project/task domain."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from octop.infra.projects import runtime
from tests.support.auth import create_user

AC = "/api/agentcomputer/v1"


async def settled(client, auth, task_id):
    for _ in range(300):
        response = await client.get(f"{AC}/tasks/{task_id}", headers=auth)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["active_turn_id"] is None:
            return result
        await asyncio.sleep(0.01)
    pytest.fail("task did not finish")


@pytest.fixture
async def task_env(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    calls = []

    async def run(services, owner, turn, history):
        calls.append((turn, history))
        yield {"type": "token", "content": f"Reply {len(history) // 2 + 1}"}

    monkeypatch.setattr(runtime, "run_turn", run)
    return client, server, auth, calls


async def test_dedicated_and_web_contracts_are_both_documented(env):
    client, _, _ = env
    schema = client._octop_app.openapi()
    assert "/api/projects" in schema["paths"]
    assert "/api/project-tasks" in schema["paths"]
    direct = {path: value for path, value in schema["paths"].items() if path.startswith(AC)}
    assert sum(len(value) for value in direct.values()) == 15
    assert AC + "/capabilities" in direct
    assert AC + "/tasks/{task_id}/turns" in direct
    assert not any("/runs/" in path or "/documents" in path for path in direct)
    for path, item in direct.items():
        for operation in item.values():
            assert operation["summary"] and operation["tags"] == ["agentcomputer"]
            assert not any(p["name"] == "access_token" for p in operation.get("parameters", []))
            assert "BearerAuth" in operation["security"][0]
            assert operation["responses"]["422"]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("/ProtocolErrorEnvelope")
            peer = "/api/bridge/connections/{connection_id}/agentcomputer/v1" + path.removeprefix(
                AC
            )
            assert peer in schema["paths"]
    post = direct[AC + "/projects"]["post"]
    assert any(
        p["in"] == "header" and p["name"] == "idempotency-key" and p["required"]
        for p in post["parameters"]
    )


async def test_projects_share_data_and_idempotency_without_creating_experts(env):
    client, server, auth = env
    before = server.services.agent_repo.list_all()
    key = str(uuid4())
    fields = {
        "name": "Shared",
        "client_directory": "C:/client-only/project",
        "instructions": "First",
    }
    headers = {**auth, "Idempotency-Key": key}
    created = await client.post(AC + "/projects", headers=headers, json=fields)
    assert created.status_code == 201, created.text
    project = created.json()
    web = "/api/projects/" + project["project_id"]
    dedicated = AC + "/projects/" + project["project_id"]
    assert (await client.get(web, headers=auth)).json() == project
    assert (await client.get(AC + "/projects", headers=auth)).json() == [project]
    assert (await client.post("/api/projects", headers=headers, json=fields)).json() == project
    changed = await client.patch(
        web, headers=auth, json={"name": "Updated", "expected_revision": 1}
    )
    assert changed.status_code == 200, changed.text
    assert (await client.get(dedicated, headers=auth)).json() == changed.json()
    assert changed.json()["client_directory"] == fields["client_directory"]
    stale = await client.patch(
        dedicated, headers=auth, json={"name": "Stale", "expected_revision": 1}
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "revisionConflict",
        "retryable": False,
        "reasonCode": "PROJECT_REVISION_CONFLICT",
    }
    conflict = await client.post(AC + "/projects", headers=headers, json={"name": "Different"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotencyConflict"
    assert created.headers["cache-control"] == "no-store"
    assert server.services.agent_repo.list_all() == before
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 0


async def test_dedicated_authentication_ownership_and_validation(env):
    client, _, auth = env
    token = auth["Authorization"].split(" ", 1)[1]
    for prefix in (AC, "/api/bridge/connections/missing/agentcomputer/v1"):
        for headers, suffix in (
            ({}, ""),
            ({}, "?access_token=" + token),
            (auth, "?access_token=" + token),
        ):
            rejected = await client.get(prefix + "/projects" + suffix, headers=headers)
            assert rejected.status_code == 401
            assert rejected.json()["error"]["code"] == "authenticationRequired"
    project = (
        await client.post(
            AC + "/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Private"},
        )
    ).json()
    other = await create_user(client, auth, username="other")
    base = AC + "/projects/" + project["project_id"]
    assert (await client.get(AC + "/projects", headers=other)).json() == []
    denied = await client.get(base, headers=other)
    assert denied.status_code == 404 and denied.json()["error"]["code"] == "resourceNotFound"
    assert (
        await client.patch(base, headers=other, json={"name": "Steal", "expected_revision": 1})
    ).status_code == 404
    invalid = await client.post(
        AC + "/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Bad", "expert_id": "private-secret"},
    )
    assert invalid.status_code == 422 and "private-secret" not in invalid.text
    oversized = await client.post(
        AC + "/tasks", headers=auth, json={"request_id": "too-large", "prompt": "x" * 65536}
    )
    assert oversized.status_code == 413


async def test_shared_tasks_multi_turn_configuration_and_retries(task_env):
    client, server, auth, calls = task_env
    before = server.services.agent_repo.list_all()
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Conversation", "instructions": "First"},
        )
    ).json()
    project_id = project["project_id"]
    first = {"request_id": "first-message", "prompt": "Remember a blue bird"}
    created = await client.post(f"{AC}/projects/{project_id}/tasks", headers=auth, json=first)
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    done = await settled(client, auth, task_id)
    assert done["turns"][0]["reply"] == "Reply 1"
    assert (await client.get(f"/api/project-tasks/{task_id}", headers=auth)).json() == done
    retry = await client.post(f"/api/projects/{project_id}/tasks", headers=auth, json=first)
    assert retry.json()["task_id"] == task_id and len(calls) == 1
    await client.patch(
        f"/api/projects/{project_id}",
        headers=auth,
        json={"instructions": "Updated", "expected_revision": 1},
    )
    second = {"request_id": "second-message", "prompt": "Which bird?"}
    sent = await client.post(f"{AC}/tasks/{task_id}/turns", headers=auth, json=second)
    assert sent.status_code == 202, sent.text
    done = await settled(client, auth, task_id)
    assert len(done["turns"]) == 2 and done["turns"][1]["project_revision"] == 2
    assert calls[1][1] == [
        {"role": "user", "content": first["prompt"]},
        {"role": "assistant", "content": "Reply 1"},
    ]
    assert json.loads(calls[1][0].configuration_json)["instructions"] == "Updated"
    assert (await client.get(f"{AC}/tasks/{task_id}/turns", headers=auth)).json() == done["turns"]
    assert (await client.get(f"{AC}/projects/{project_id}/tasks", headers=auth)).json()[0][
        "task_id"
    ] == task_id
    assert (await client.get(AC + "/tasks", headers=auth)).json() == []
    conflict = await client.post(
        f"{AC}/tasks/{task_id}/turns", headers=auth, json={**second, "prompt": "Different"}
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotencyConflict"
    assert server.services.agent_repo.list_all() == before
    assert server.agentcomputer_text_worker is None


async def test_projectless_cancel_and_isolation(task_env, monkeypatch):
    client, server, auth, calls = task_env
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(services, owner, turn, history):
        entered.set()
        await release.wait()
        yield {"type": "token", "content": "Done"}

    monkeypatch.setattr(runtime, "run_turn", blocked)
    result = await client.post(
        AC + "/tasks", headers=auth, json={"request_id": "standalone-first", "prompt": "Wait"}
    )
    assert result.status_code == 202, result.text
    task = result.json()
    task_id = task["task_id"]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert task["project_id"] is None
        assert (await client.get("/api/project-tasks", headers=auth)).json()[0][
            "task_id"
        ] == task_id
        busy = await client.post(
            f"{AC}/tasks/{task_id}/turns",
            headers=auth,
            json={"request_id": "busy-message", "prompt": "More"},
        )
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "activeRunConflict"
        other = await create_user(client, auth, username="task-other")
        assert (await client.get(f"{AC}/tasks/{task_id}", headers=other)).status_code == 404
        assert (await client.post(f"{AC}/tasks/{task_id}/cancel", headers=other)).status_code == 404
        assert (await client.post(f"{AC}/tasks/{task_id}/cancel", headers=auth)).status_code == 200
        done = await settled(client, auth, task_id)
        assert done["turns"][0]["status"] == "cancelled"
    finally:
        release.set()


async def test_discovery_reports_shared_contract_and_actual_model_availability(env, monkeypatch):
    client, server, auth = env
    first = await client.get(AC + "/capabilities", headers=auth)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["contract"] == "independent-project-tasks"
    assert body["capabilities"]["projects"] is True
    assert body["capabilities"]["events"] is False
    assert body["executionAvailable"] is False and body["unavailableReason"] == "modelUnavailable"
    assert (await client.get(AC + "/capabilities", headers=auth)).json()["boxID"] == body["boxID"]
    monkeypatch.setattr(server.project_task_worker, "closing", True)
    stopped = await client.get(AC + "/capabilities", headers=auth)
    assert stopped.json()["unavailableReason"] == "runtimeUnavailable"


async def test_discovery_uses_same_model_as_shared_worker(task_env):
    client, _, auth, _ = task_env
    response = await client.get(AC + "/capabilities", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["executionAvailable"] is True
    assert response.json()["unavailableReason"] is None


async def test_shutdown_admission_race_keeps_protocol_error(env, monkeypatch):
    from unittest.mock import AsyncMock

    from octop.infra.agentcomputer.commands import CommandAdmissionError

    client, server, auth = env
    monkeypatch.setattr(
        server.agentcomputer_http_operations,
        "run_thread",
        AsyncMock(side_effect=CommandAdmissionError("runtimeUnavailable")),
    )
    response = await client.get(AC + "/projects", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "runtimeUnavailable", "retryable": False}}
