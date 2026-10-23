"""Remote summaries stay references and peer credentials never reach local clients."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from octop.infra.bridge.task_streams import TaskStreamRelay
from octop.infra.bridge.tasks import RemoteTasks
from octop.infra.bridge.transport import BridgeSession
from octop.infra.errors import OctopError
from tests.support.app import octop_client
from tests.support.auth import auth_header, bootstrap_admin, resolve_user_id
from tests.support.http import ws_connect, ws_token


async def test_remote_approval_policy_http_updates_only_peer_and_preserves_retries(env, tmp_path):
    from octop.infra.db.repos.project_tasks import ProjectTaskRow
    from octop.infra.projects.tasks import now_ms

    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    peer_home = tmp_path / "peer"
    async with octop_client(peer_home) as (peer_client, peer_server):
        await bootstrap_admin(peer_client, peer_home, username="peer-owner")
        peer_auth = await auth_header(peer_client, username="peer-owner")
        peer_owner = await resolve_user_id(peer_client, peer_auth, "peer-owner")
        task_id = "remote-policy-task"
        with peer_server.services.db.transaction() as conn:
            peer_server.services.project_task_repo.insert(
                conn,
                ProjectTaskRow(
                    task_id,
                    peer_owner,
                    None,
                    "Policy task",
                    "remote-policy-create",
                    None,
                    now_ms(),
                    now_ms(),
                    status="completed",
                ),
            )
        connection_id = "approval-peer"
        for services, uid, username in (
            (server.services, owner, "peer-owner"),
            (peer_server.services, peer_owner, "admin"),
        ):
            services.bridge_connection_repo.create(
                connection_id=connection_id,
                owner_user_id=uid,
                peer_base_url="http://peer.invalid",
                peer_username=username,
                display_name="Policy peer",
            )
        local, peer = server.app_runtime.bridge_manager, peer_server.app_runtime.bridge_manager

        async def to_peer(raw):
            await peer_session.handle_message(raw)

        async def to_local(raw):
            await local_session.handle_message(raw)

        local_session = BridgeSession(connection_id=connection_id, send_text=to_peer)
        peer_session = BridgeSession(
            connection_id=connection_id,
            send_text=to_local,
            on_tunnel_request=lambda payload: peer._handle_inbound_tunnel(connection_id, payload),
        )
        await local._register_session(connection_id, local_session)
        await peer._register_session(connection_id, peer_session)
        policy_path = "/api/project-tasks/approval-policy"
        proxy_prefix = f"/api/bridge/connections/{connection_id}/task-api"
        global_url = proxy_prefix + "/project-tasks/approval-policy"
        task_url = proxy_prefix + f"/project-tasks/{task_id}/approval-policy"
        initial = (await client.get(policy_path, headers=auth)).json()
        try:
            remote_initial = await client.get(global_url, headers=auth)
            assert remote_initial.status_code == 200, remote_initial.text
            assert remote_initial.json()["mode"] == "ask_for_approval"
            body = {
                "request_id": "remote-global-policy",
                "expected_revision": remote_initial.json()["revision"],
                "mode": "full_access",
            }
            changed = await client.put(global_url, headers=auth, json=body)
            assert changed.status_code == 200, changed.text
            policy = changed.json()
            assert policy["mode"] == "full_access"
            assert policy["revision"] == body["expected_revision"] + 1
            assert (await peer_client.get(policy_path, headers=peer_auth)).json() == policy
            # The original business request remains idempotent across both HTTP entry points.
            replay = await client.put(task_url, headers=auth, json=body)
            assert replay.status_code == 200, replay.text
            assert replay.json()["approval_policy"] == policy
            for update, code in (
                ({**body, "mode": "approve_for_me"}, "TASK_REQUEST_CONFLICT"),
                ({**body, "request_id": "remote-stale-policy"}, "TASK_APPROVAL_POLICY_CONFLICT"),
            ):
                conflict = await client.put(task_url, headers=auth, json=update)
                assert conflict.status_code == 409, conflict.text
                assert conflict.json()["error"]["code"] == code
            task_body = {
                "request_id": "remote-task-policy",
                "expected_revision": policy["revision"],
                "mode": "approve_for_me",
            }
            task_changed = await client.put(task_url, headers=auth, json=task_body)
            assert task_changed.status_code == 200, task_changed.text
            policy = task_changed.json()["approval_policy"]
            assert policy["mode"] == "approve_for_me"
            assert policy["revision"] == task_body["expected_revision"] + 1
            assert (await client.get(global_url, headers=auth)).json() == policy
            assert (await peer_client.get(policy_path, headers=peer_auth)).json() == policy
            for url in (global_url, task_url):
                unsupported = await client.patch(url, headers=auth, json=task_body)
                assert unsupported.status_code == 403, unsupported.text
                assert unsupported.json()["error"]["code"] == "BRIDGE_REMOTE_UNSUPPORTED"
            assert (await peer_client.get(policy_path, headers=peer_auth)).json() == policy
            assert (await client.get(policy_path, headers=auth)).json() == initial
            assert server.services.project_task_repo.get(task_id, owner) is None
        finally:
            await local._unregister_session(connection_id)
            await peer._unregister_session(connection_id)


async def test_remote_proxy_pins_peer_identity_caches_only_headers_and_scrubs_auth(env):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    row = server.services.bridge_connection_repo.create(
        connection_id="remote-host",
        owner_user_id=owner,
        peer_base_url="http://peer.example",
        peer_username="peer-owner",
        display_name="Peer",
    )

    class Manager:
        account = "remote-account-one"
        product_status = 200
        seen = []

        def get_owned(self, connection_id, owner_id):
            assert connection_id == row.connection_id and owner_id == owner
            return row

        async def tunnel_http(self, **kwargs):
            self.seen.append(kwargs)
            if kwargs["path"] == "/api/project-tasks/capabilities":
                return httpx.Response(
                    200,
                    json={
                        "contract": "unified-task-conversations",
                        "version": 1,
                        "protocol_version": 3,
                        "instance_id": "peer-instance",
                        "account_id": self.account,
                        "capabilities": {"tasks": True},
                    },
                )
            if kwargs["path"] == "/api/project-tasks/attachments/json-file":
                return httpx.Response(
                    200,
                    content=b'{\n "url":"/api/unchanged"\n}\n',
                    headers={
                        "content-type": "application/json",
                        "content-disposition": "attachment; filename=input.json",
                    },
                )
            return httpx.Response(
                self.product_status,
                headers={
                    "x-octop-access-token": "peer-credential",
                    "set-cookie": "peer-cookie",
                    "content-security-policy": "default-src 'none'; sandbox",
                    "x-content-type-options": "nosniff",
                },
                json={
                    "task_id": "task_same",
                    "title": "Remote task",
                    "turns": [{"prompt": "not cached"}],
                    "attachments": [
                        {
                            "download_url": "/api/project-tasks/attachments/attach?access_token=peer-secret"
                        }
                    ],
                },
            )

    manager = Manager()
    remote = RemoteTasks(server.services, manager)
    response = await remote.proxy(
        owner,
        row.connection_id,
        "GET",
        "/api/project-tasks/task_same",
        "access_token=forged&q=x",
        {"authorization": "local-user-token", "range": "bytes=0-9"},
        b"",
    )
    assert response.status_code == 200
    assert "x-octop-access-token" not in response.headers and "set-cookie" not in response.headers
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "peer-secret" not in response.text
    assert (
        "/api/bridge/connections/remote-host/task-api/project-tasks/attachments/attach"
        in response.text
    )
    assert manager.seen[-1]["query"] == "q=x"
    assert manager.seen[-1]["headers"] == {"range": "bytes=0-9"}
    identity = server.services.federated_task_repo.peer_identity(owner, row.connection_id)
    refs = server.services.federated_task_repo.list(owner, row.connection_id, identity)
    assert len(refs) == 1 and refs[0]["task_id"] == "task_same"
    assert "turns" not in refs[0]["summary"]
    assert server.services.project_task_repo.get("task_same", owner) is None
    file = await remote.proxy(
        owner, row.connection_id, "GET", "/api/project-tasks/attachments/json-file", "", {}, b""
    )
    assert file.content == b'{\n "url":"/api/unchanged"\n}\n'
    manager.product_status = 401
    with pytest.raises(OctopError) as peer_auth:
        await remote.proxy(
            owner, row.connection_id, "GET", "/api/project-tasks/task_same", "", {}, b""
        )
    assert peer_auth.value.status == 403
    manager.product_status = 200
    manager.account = "another-account"
    with pytest.raises(OctopError) as peer_changed:
        await remote.proxy(owner, row.connection_id, "POST", "/api/project-tasks", "", {}, b"{}")
    assert peer_changed.value.status == 403
    assert manager.seen[-1]["path"] == "/api/project-tasks/capabilities"


async def test_internal_task_is_hidden_from_all_public_task_surfaces(env_with_provider):
    from octop.infra.db.repos.project_tasks import ProjectTaskRow
    from octop.infra.projects.tasks import now_ms

    client, server, auth = env_with_provider
    owner = await resolve_user_id(client, auth, "admin")
    with server.services.db.transaction() as conn:
        server.services.project_task_repo.insert(
            conn,
            ProjectTaskRow(
                "internal-resident",
                owner,
                None,
                "Private internal",
                "internal-request",
                None,
                now_ms(),
                now_ms(),
                internal_kind="resident",
            ),
        )
    assert (
        server.services.project_task_repo.get_internal("internal-resident", owner).internal_kind
        == "resident"
    )
    for suffix in ("", "/export", "/memory", "/workbench"):
        response = await client.get("/api/project-tasks/internal-resident" + suffix, headers=auth)
        assert response.status_code == 404, (suffix, response.status_code)
    page = (await client.get("/api/project-tasks?scope=all", headers=auth)).json()
    assert not page["items"]
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM project_sync_changes WHERE entity_id='internal-resident'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("frame", [[], 1, {"type": "authenticate", "token": {"bad": "type"}}])
async def test_remote_ws_malformed_authentication_closes_with_policy_code(env, frame):
    from starlette.websockets import WebSocketDisconnect

    from tests.support.http import ws_connect

    client, _server, _auth = env
    async with ws_connect(
        client._octop_app, "/api/bridge/connections/missing/project-tasks/ws"
    ) as ws:
        await ws.send_json(frame)
        with pytest.raises(WebSocketDisconnect) as closed:
            await ws.receive_json()
        assert closed.value.code == 1008


async def test_remote_proxy_methods_have_distinct_openapi_operations(env, monkeypatch):
    from dataclasses import replace

    from octop.api.app import build_app

    _original_client, server, auth = env
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, enable_api_docs=True)),
    )
    app = build_app(server)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/openapi.json", headers=auth)
        assert response.status_code == 200
        schema = response.json()
        proxy = schema["paths"]["/api/bridge/connections/{connection_id}/task-api/{peer_path}"]
        operations = [
            proxy[method]["operationId"]
            for method in ("get", "post", "patch", "put", "delete", "head")
        ]
        assert len(set(operations)) == 6
        assert schema["paths"]["/api/bridge/connections/{connection_id}/task-capabilities"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/RemoteTaskCapabilities"
        )
        assert (await client.get("/api/docs", headers=auth)).status_code == 200


@pytest.mark.parametrize("authentication", ["bearer", "query", "first_frame"])
@pytest.mark.parametrize("surface", ["main", "browser-stream", "terminal"])
async def test_remote_ws_authenticates_locally_and_filters_before_bridge(
    env, monkeypatch, authentication, surface
):
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    connection_id = "remote-auth-stream"
    server.services.bridge_connection_repo.create(
        connection_id=connection_id,
        owner_user_id=owner,
        peer_base_url="http://peer.invalid",
        peer_username="peer-owner",
        display_name="Peer",
    )
    local = server.app_runtime.bridge_manager
    scopes, inbound, outbound, returned, handshakes = [], [], [], [], []

    async def capabilities(remote, owner_id, cid):
        assert remote.manager is local
        assert owner_id == owner and cid == connection_id
        handshakes.append((owner_id, cid))
        return {"available": True, "protocol_version": 3, "capabilities": {"tasks": True}}

    monkeypatch.setattr(RemoteTasks, "capabilities", capabilities)

    async def peer_app(scope, receive, send):
        scopes.append(scope)
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": '{"type":"ready"}'})
        await send(
            {
                "type": "websocket.send",
                "text": '{"type":"session","access_token":"peer-renewal-secret"}',
            }
        )
        message = await receive()
        assert message["type"] == "websocket.receive"
        inbound.append(json.loads(message["text"]))
        await send({"type": "websocket.send", "text": message["text"]})
        await send({"type": "websocket.close", "code": 1000})

    async def peer_token(cid):
        assert cid == connection_id
        return "peer-owner-auth"

    peer = SimpleNamespace(_asgi_app=peer_app, _owner_access_token=peer_token)
    peer.task_streams = TaskStreamRelay(peer)
    peer.require_session = lambda cid: peer.session

    async def to_peer(raw):
        outbound.append(json.loads(raw))
        await peer.session.handle_message(raw)

    async def to_local(raw):
        returned.append(json.loads(raw))
        await local_session.handle_message(raw)

    local_session = BridgeSession(
        connection_id=connection_id,
        send_text=to_peer,
        on_raw=lambda frame: local.task_streams.handle(connection_id, frame),
    )
    peer.session = BridgeSession(
        connection_id=connection_id,
        send_text=to_local,
        on_raw=lambda frame: peer.task_streams.handle(connection_id, frame),
    )
    await local._register_session(connection_id, local_session)

    if surface == "main":
        peer_path = "/api/project-tasks/ws"
        allowed_query = {}
        command = {"type": "ping", "id": "remote-ping-command"}
    elif surface == "browser-stream":
        peer_path = "/api/project-tasks/task_remote/browser-stream/ws"
        allowed_query = {
            "expected_scope_revision": "scope-revision",
            "listen_only": "1",
            "width": "1024",
            "height": "768",
        }
        command = {"type": "start", "url": "", "reuse_session": True}
    else:
        peer_path = "/api/project-tasks/task_remote/terminal/ws"
        allowed_query = {"expected_scope_revision": "scope-revision", "session_id": "pty-session"}
        command = {"type": "resize", "cols": 100, "rows": 30}
    local_token = ws_token(auth)
    query = {
        **allowed_query,
        "access_token": local_token,
        "arbitrary": "private-query-value",
        "profile_id": "private-profile",
        "path": "private-path",
    }
    if authentication == "query":
        query["token"] = local_token
    headers = auth if authentication == "bearer" else None
    path = f"/api/bridge/connections/{connection_id}" + peer_path.removeprefix("/api")
    try:
        async with ws_connect(
            client._octop_app, path + "?" + urlencode(query), headers=headers
        ) as ws:
            if authentication == "first_frame":
                await ws.send_json({"type": "authenticate", "token": local_token})
            assert await ws.receive_json(timeout=3) == {"type": "ready"}
            # Even duplicate credential frames are consumed before Bridge send.
            await ws.send_json({"type": "authenticate", "token": local_token})
            await ws.send_json({"type": "session", "access_token": local_token})
            await ws.send_json(command)
            assert await ws.receive_json(timeout=3) == command
    finally:
        await local._unregister_session(connection_id)
        await peer.task_streams.disconnect(connection_id)
        await peer.session.close()

    assert handshakes == [(owner, connection_id)]
    assert inbound == [command]
    opened = [frame for frame in outbound if frame["type"] == "task-stream.open"]
    assert len(opened) == 1
    assert opened[0]["path"] == peer_path
    assert opened[0]["query"] == allowed_query
    assert [frame["frame"] for frame in outbound if frame["type"] == "task-stream.client"] == [
        command
    ]
    assert len(scopes) == 1
    assert (b"authorization", b"Bearer peer-owner-auth") in scopes[0]["headers"]
    assert parse_qs(scopes[0]["query_string"].decode()) == {
        **{key: [value] for key, value in allowed_query.items()},
        "token": ["peer-owner-auth"],
    }
    for secret in (
        local_token,
        "peer-owner-auth",
        "peer-renewal-secret",
        "private-query-value",
        "private-profile",
        "private-path",
    ):
        assert secret not in json.dumps([outbound, returned])
    assert not local.task_streams.waiters and not peer.task_streams.runners
