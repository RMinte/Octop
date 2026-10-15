"""Bridge relays only the dedicated independent project/task protocol."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from octop.infra.bridge.http_tunnel import encode_tunnel_response, execute_local_http
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/bridge/connections/01REMOTE/agentcomputer/v1"


async def connection(client, server, auth):
    owner = await resolve_user_id(client, auth, "admin")
    server.services.bridge_connection_repo.create(
        connection_id="01REMOTE",
        owner_user_id=owner,
        peer_base_url="https://peer.example",
        peer_username="peer",
        display_name="Remote",
    )
    return server.app_runtime.bridge_manager


async def test_bridge_shares_actual_domain_and_forwards_creation_idempotency(env):
    client, server, auth = env
    manager = await connection(client, server, auth)
    token = auth["Authorization"].split(" ", 1)[1]
    session = MagicMock()

    async def local_peer(**kwargs):
        return await execute_local_http(app=client._octop_app, access_token=token, **kwargs)

    session.tunnel_request = AsyncMock(side_effect=local_peer)
    key = str(uuid4())
    body = {
        "name": "Bridge project",
        "instructions": "Shared instructions",
        "client_directory": "/client/only",
    }
    before = server.services.agent_repo.list_all()
    with patch.object(manager, "require_session", return_value=session):
        created = await client.post(
            PREFIX + "/projects", headers={**auth, "Idempotency-Key": key}, json=body
        )
        assert created.status_code == 201, created.text
        project = created.json()
        assert (
            await client.get("/api/projects/" + project["project_id"], headers=auth)
        ).json() == project
        assert (await client.get(PREFIX + "/projects", headers=auth)).json() == [project]
        changed = await client.patch(
            PREFIX + "/projects/" + project["project_id"],
            headers=auth,
            json={"instructions": "Changed", "expected_revision": 1},
        )
        assert changed.status_code == 200, changed.text
        assert (
            await client.get(
                "/api/agentcomputer/v1/projects/" + project["project_id"], headers=auth
            )
        ).json() == changed.json()
        replay = await client.post(
            PREFIX + "/projects", headers={**auth, "Idempotency-Key": key}, json=body
        )
        assert replay.json() == project
        conflict = await client.post(
            PREFIX + "/projects",
            headers={**auth, "Idempotency-Key": key},
            json={**body, "name": "Other"},
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "idempotencyConflict"
        )
    forwarded = session.tunnel_request.await_args_list[0].kwargs
    assert forwarded["path"] == "/api/agentcomputer/v1/projects"
    assert forwarded["headers"] == {"content-type": "application/json", "idempotency-key": key}
    assert json.loads(forwarded["body"]) == body
    assert server.services.agent_repo.list_all() == before


async def test_bridge_keeps_bearer_ownership_typed_responses_and_safe_errors(env):
    client, server, auth = env
    manager = await connection(client, server, auth)
    session = MagicMock()
    session.tunnel_request = AsyncMock(
        return_value=encode_tunnel_response(
            status=200,
            headers={"Set-Cookie": "secret=leak", "Authorization": "Bearer leak"},
            body=b"[]",
        )
    )
    with patch.object(manager, "require_session", return_value=session):
        response = await client.get(PREFIX + "/projects", headers=auth)
        assert response.status_code == 200 and response.json() == []
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers and "authorization" not in response.headers
        token = auth["Authorization"].split(" ", 1)[1]
        assert (await client.get(PREFIX + "/projects?access_token=" + token)).status_code == 401
        other = await create_user(client, auth, username="bridge-other")
        assert (await client.get(PREFIX + "/projects", headers=other)).status_code == 404
        assert (await client.get(PREFIX + "/users", headers=auth)).status_code == 404
        assert session.tunnel_request.await_count == 1
        session.tunnel_request.return_value = encode_tunnel_response(
            status=200, headers={}, body=b'{"private":"secret-diagnostic"}'
        )
        invalid = await client.get(PREFIX + "/projects", headers=auth)
        assert invalid.status_code == 502 and "secret-diagnostic" not in invalid.text
        session.tunnel_request.return_value = encode_tunnel_response(
            status=409,
            headers={},
            body=b'{"error":{"code":"resourceConflict","retryable":false,"reasonCode":"private-diagnostic"},"secret":"private-value"}',
        )
        refusal = await client.get(PREFIX + "/projects", headers=auth)
        assert refusal.status_code == 409
        assert refusal.json() == {"error": {"code": "resourceConflict", "retryable": False}}
        session.tunnel_request.return_value = encode_tunnel_response(
            status=404, headers={}, body=b'{"detail":"private diagnostic"}'
        )
        missing = await client.get(PREFIX + "/projects", headers=auth)
        assert missing.status_code == 404
        assert missing.json() == {"error": {"code": "resourceNotFound", "retryable": False}}
    for call in session.tunnel_request.await_args_list:
        assert call.kwargs["headers"] is None and call.kwargs["query"] == ""


async def test_bridge_rejects_invalid_or_oversized_commands_before_peer_hop(env):
    client, server, auth = env
    manager = await connection(client, server, auth)
    session = MagicMock()
    session.tunnel_request = AsyncMock()
    with patch.object(manager, "require_session", return_value=session):
        missing_key = await client.post(
            PREFIX + "/projects", headers=auth, json={"name": "Missing key"}
        )
        assert (
            missing_key.status_code == 422
            and missing_key.json()["error"]["code"] == "invalidRequest"
        )
        invalid = await client.post(
            PREFIX + "/projects",
            headers={**auth, "Idempotency-Key": str(uuid4())},
            json={"name": "Bad", "agent_id": "private-secret"},
        )
        assert invalid.status_code == 422 and "private-secret" not in invalid.text
        oversized = await client.post(
            PREFIX + "/tasks", headers=auth, json={"request_id": "oversized", "prompt": "x" * 65536}
        )
        assert oversized.status_code == 413
        assert (await client.get(PREFIX + "/runs/old/output", headers=auth)).status_code == 404
        assert (
            await client.put(
                PREFIX + "/projects/proj_01ABC/documents/file.md",
                headers=auth,
                json={"content": "text"},
            )
        ).status_code in {404, 405}
        assert session.tunnel_request.await_count == 0


async def test_bridge_task_conversations_use_shared_worker_and_history(
    env_with_provider, monkeypatch
):
    import asyncio

    from octop.infra.projects import runtime

    client, server, auth = env_with_provider
    manager = await connection(client, server, auth)
    token = auth["Authorization"].split(" ", 1)[1]
    calls = []

    async def run(services, owner, turn, history):
        calls.append(history)
        yield {"type": "token", "content": f"Reply {len(history) // 2 + 1}"}

    monkeypatch.setattr(runtime, "run_turn", run)

    async def local_peer(**kwargs):
        return await execute_local_http(app=client._octop_app, access_token=token, **kwargs)

    session = MagicMock()
    session.tunnel_request = AsyncMock(side_effect=local_peer)

    async def done(task_id):
        for _ in range(300):
            response = await client.get(PREFIX + "/tasks/" + task_id, headers=auth)
            assert response.status_code == 200, response.text
            task = response.json()
            if task["active_turn_id"] is None:
                return task
            await asyncio.sleep(0.01)
        raise AssertionError("task did not finish")

    before = server.services.agent_repo.list_all()
    with patch.object(manager, "require_session", return_value=session):
        capabilities = await client.get(PREFIX + "/capabilities", headers=auth)
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["executionAvailable"] is True
        resources = await client.get(PREFIX + "/projects/resources", headers=auth)
        assert resources.status_code == 200, resources.text
        first = {"request_id": "bridge-first-message", "prompt": "Remember a bird"}
        created = await client.post(PREFIX + "/tasks", headers=auth, json=first)
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        task = await done(task_id)
        assert task["project_id"] is None and task["turns"][0]["reply"] == "Reply 1"
        assert (await client.get("/api/project-tasks/" + task_id, headers=auth)).json() == task
        assert (await client.get(PREFIX + "/tasks", headers=auth)).json()["items"][0][
            "task_id"
        ] == task_id
        next_turn = {"request_id": "bridge-second-message", "prompt": "Which bird?"}
        response = await client.post(
            PREFIX + "/tasks/" + task_id + "/turns", headers=auth, json=next_turn
        )
        assert response.status_code == 202, response.text
        task = await done(task_id)
        assert task["turns"][1]["reply"] == "Reply 2"
        assert calls[1] == [
            {"role": "user", "content": first["prompt"]},
            {"role": "assistant", "content": "Reply 1"},
        ]
        turns = await client.get(PREFIX + "/tasks/" + task_id + "/turns", headers=auth)
        assert turns.json() == task["turns"]
        cancelled = await client.post(PREFIX + "/tasks/" + task_id + "/cancel", headers=auth)
        assert cancelled.status_code == 200 and cancelled.json() == task
    assert server.services.agent_repo.list_all() == before


async def test_bridge_forwards_only_list_paging_and_sync_query_parameters(env):
    from urllib.parse import parse_qs

    from octop.infra.db.repos.project_tasks import ProjectTaskRow

    client, server, auth = env
    manager = await connection(client, server, auth)
    owner = await resolve_user_id(client, auth, "admin")
    token = auth["Authorization"].split(" ", 1)[1]
    session = MagicMock()

    async def local_peer(**kwargs):
        return await execute_local_http(app=client._octop_app, access_token=token, **kwargs)

    session.tunnel_request = AsyncMock(side_effect=local_peer)
    with server.services.db.transaction() as conn:
        for index in range(3):
            server.services.project_task_repo.insert(
                conn,
                ProjectTaskRow(
                    f"task_0{index}",
                    owner,
                    None,
                    f"Task {index}",
                    f"bridge-seed-{index}",
                    None,
                    1000,
                    1000,
                ),
            )
    with patch.object(manager, "require_session", return_value=session):
        response = await client.get(
            PREFIX + "/tasks", headers=auth, params={"limit": 1, "ignored": "private"}
        )
        assert response.status_code == 200, response.text
        first = response.json()
        assert len(first["items"]) == 1 and first["items"][0]["task_id"] == "task_02"
        assert parse_qs(session.tunnel_request.await_args.kwargs["query"]) == {"limit": ["1"]}
        page = await client.get(
            PREFIX + "/tasks",
            headers=auth,
            params={"limit": 1, "page_cursor": first["next_page_cursor"]},
        )
        assert page.status_code == 200 and page.json()["items"][0]["task_id"] == "task_01"
        with server.services.db.transaction() as conn:
            server.services.project_task_repo.activate(conn, "task_00", "turn_bridge", 2000)
        delta = await client.get(
            PREFIX + "/tasks/sync", headers=auth, params={"sync_cursor": first["sync_cursor"]}
        )
        assert delta.status_code == 200, delta.text
        assert delta.json()["changes"][0]["task_id"] == "task_00"
        assert parse_qs(session.tunnel_request.await_args.kwargs["query"]) == {
            "sync_cursor": [first["sync_cursor"]]
        }
        projects = await client.get(PREFIX + "/projects/sync", headers=auth)
        assert projects.status_code == 200 and projects.json()["mode"] == "snapshot"
        wrong = await client.get(
            PREFIX + "/tasks/sync",
            headers=auth,
            params={"sync_cursor": projects.json()["next_sync_cursor"]},
        )
        assert wrong.status_code == 409
        assert wrong.json()["error"]["code"] == "reconciliationRequired"
        assert wrong.json()["error"]["reasonCode"] == "PROJECT_SYNC_RESET"
