"""Remote summaries stay references and peer credentials never reach local clients."""

from __future__ import annotations

import httpx
import pytest

from octop.infra.bridge.tasks import RemoteTasks
from octop.infra.errors import OctopError
from tests.support.auth import resolve_user_id


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
