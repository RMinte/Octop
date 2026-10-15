"""Projects and tasks use the standard local API without a dedicated protocol."""

from __future__ import annotations

from unittest.mock import AsyncMock


async def test_project_task_contract_is_documented_without_dedicated_protocol(env):
    client, _, _ = env
    schema = client._octop_app.openapi()
    paths = schema["paths"]
    assert not any("agentcomputer" in path for path in paths)
    assert not any(tag["name"] == "agentcomputer" for tag in schema["tags"])
    assert "ProtocolErrorEnvelope" not in schema["components"]["schemas"]
    project_paths = {
        path: value
        for path, value in paths.items()
        if path.startswith(("/api/projects", "/api/project-tasks"))
    }
    assert "/api/projects/sync" in project_paths
    assert "/api/projects/{project_id}/tasks/sync" in project_paths
    assert "/api/project-tasks/sync" in project_paths
    assert "/api/project-tasks/{task_id}/messages" in project_paths
    for item in project_paths.values():
        for operation in item.values():
            assert operation["summary"] and operation["tags"] == ["projects"]
            assert "BearerAuth" in operation["security"][0]
            assert operation["responses"]
    for path in ("/api/projects/{project_id}/tasks", "/api/project-tasks"):
        assert paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/TaskListPage")
    assert any(
        p["in"] == "header" and p["name"] == "idempotency-key" and p["required"]
        for p in paths["/api/projects"]["post"]["parameters"]
    )


async def test_retired_protocol_routes_are_unavailable_and_never_relay(env, monkeypatch):
    client, server, auth = env
    relay = AsyncMock()
    monkeypatch.setattr(server.app_runtime.bridge_manager, "tunnel_http", relay)
    resources = {
        "GET": [
            "/capabilities",
            "/projects",
            "/projects/resources",
            "/projects/sync",
            "/projects/proj_example",
            "/projects/proj_example/tasks",
            "/projects/proj_example/tasks/sync",
            "/tasks",
            "/tasks/sync",
            "/tasks/task_example",
            "/tasks/task_example/turns",
        ],
        "POST": [
            "/projects",
            "/projects/proj_example/tasks",
            "/tasks",
            "/tasks/task_example/turns",
            "/tasks/task_example/cancel",
        ],
        "PATCH": ["/projects/proj_example"],
        "PUT": ["/projects/proj_example"],
    }
    for prefix in (
        "/api/agentcomputer/v1",
        "/api/bridge/connections/01REMOTE/agentcomputer/v1",
    ):
        for method, suffixes in resources.items():
            for suffix in suffixes:
                response = await client.request(method, prefix + suffix, headers=auth)
                # The dashboard GET fallback returns 405 for unsupported write methods.
                assert response.status_code in {404, 405}, (method, prefix + suffix, response.text)
    for suffix in ("/projects", "/projects/sync", "/project-tasks", "/project-tasks/sync"):
        response = await client.get("/api/bridge/connections/01REMOTE" + suffix, headers=auth)
        assert response.status_code == 404
    relay.assert_not_awaited()
