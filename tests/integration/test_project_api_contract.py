"""Projects and tasks use the standard local API without a dedicated protocol."""

from __future__ import annotations

from unittest.mock import AsyncMock


async def test_project_task_contract_is_documented_without_dedicated_protocol(env, monkeypatch):
    from dataclasses import replace

    import httpx

    from octop.api.app import build_app

    client, server, _ = env
    schema = client._octop_app.openapi()
    project_tag = next(tag for tag in schema["tags"] if tag["name"] == "projects")
    assert "/api/project-tasks/ws" in project_tag["description"]
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
    assert "/api/project-tasks/resources" in project_paths
    assert "post" in project_paths["/api/project-tasks/attachments"]
    assert "get" in project_paths["/api/project-tasks/attachments/{attachment_id}"]
    assert "patch" in project_paths["/api/project-tasks/{task_id}"]
    for path in ("/api/projects/{project_id}", "/api/project-tasks/{task_id}"):
        deletion = project_paths[path]["delete"]
        assert "204" in deletion["responses"]
        assert not any(p["name"] == "expected_revision" for p in deletion["parameters"])
    assert "TaskExecutionOptions" in schema["components"]["schemas"]
    turn = schema["components"]["schemas"]["TaskTurn"]
    assert {
        "content_blocks",
        "content_revision",
        "plan",
        "plan_revision",
        "interactions",
        "interaction_revision",
        "pause_requested",
        "approval_policy_at_start",
    } <= set(turn["required"])
    assert turn["properties"]["content_blocks"]["items"]["$ref"].endswith("/TaskContentBlock")
    assert turn["properties"]["file_changes"]["items"]["$ref"].endswith("/TaskFileChange")
    assert {"path", "additions", "deletions", "diff"} <= set(
        schema["components"]["schemas"]["TaskFileChange"]["properties"]
    )
    assert {"type": "null"} in turn["properties"]["recovery"]["anyOf"]
    assert {
        "can_resume",
        "needs_reconciliation",
        "phase",
        "reason_code",
        "cause_type",
        "diagnostic_id",
        "resource_type",
        "resource_id",
        "status_code",
    } <= set(schema["components"]["schemas"]["TaskRecovery"]["properties"])
    plan_schema = schema["components"]["schemas"]["TaskPlan"]
    assert plan_schema["properties"]["steps"]["items"]["$ref"].endswith("/TaskPlanStep")
    assert {"plan_id", "active_step_id", "retired_steps"} <= set(plan_schema["properties"])
    assert {"step_id", "result", "started_at", "finished_at", "removed_at"} <= set(
        schema["components"]["schemas"]["TaskPlanStep"]["properties"]
    )
    assert {
        "plan_id",
        "step_id",
        "arguments",
        "output",
        "started_at",
        "finished_at",
        "duration_ms",
    } <= set(schema["components"]["schemas"]["TaskContentBlock"]["properties"])
    assert {"plan", "progress"} <= set(
        schema["components"]["schemas"]["TaskContentBlock"]["properties"]["type"]["enum"]
    )
    assert turn["properties"]["attachments"]["items"]["$ref"].endswith("/TaskInputAttachment")
    message = schema["components"]["schemas"]["TaskMessage"]
    assert message["properties"]["attachment_ids"]["maxItems"] == 9
    assert message["properties"]["prompt"]["default"] == ""
    assert "supports_images" in schema["components"]["schemas"]["TaskModelOption"]["required"]
    assert (
        "multipart/form-data"
        in project_paths["/api/project-tasks/attachments"]["post"]["requestBody"]["content"]
    )
    assert project_paths["/api/project-tasks/attachments"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/TaskInputAttachment")
    assert (
        "image/png"
        in project_paths["/api/project-tasks/attachments/{attachment_id}"]["get"]["responses"][
            "200"
        ]["content"]
    )
    assert "patch" in paths["/api/projects/{project_id}/archive"]
    assert "archived" in schema["components"]["schemas"]["ProjectView"]["properties"]
    task_summary = schema["components"]["schemas"]["TaskSummary"]
    assert "status" in task_summary["required"]
    assert "approval_policy" in task_summary["required"]
    assert "/api/project-tasks/{task_id}/approval-policy" in project_paths
    assert task_summary["properties"]["status"]["enum"] == [
        "queued",
        "running",
        "waiting_approval",
        "waiting_answer",
        "waiting_client_tool",
        "paused",
        "interrupted",
        "completed",
        "failed",
        "cancelled",
    ]
    assert "/api/project-tasks/{task_id}/pause" in project_paths
    assert "/api/project-tasks/{task_id}/resume" in project_paths
    assert (
        "/api/project-tasks/{task_id}/turns/{turn_id}/interactions/{interaction_id}/respond"
        in project_paths
    )
    assert "/api/project-tasks/{task_id}/messages" in project_paths
    assert "/api/project-tasks/{task_id}/events" in project_paths
    assert "/api/project-tasks/{task_id}/turns/{turn_id}/retry" in project_paths
    assert (
        "text/event-stream"
        in paths["/api/project-tasks/{task_id}/events"]["get"]["responses"]["200"]["content"]
    )
    cancel_schema = paths["/api/project-tasks/{task_id}/cancel"]["post"]["requestBody"]
    assert cancel_schema["required"]
    assert schema["components"]["schemas"]["TaskCancel"]["required"] == ["turn_id"]
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
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, enable_api_docs=True)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(server)), base_url="http://testserver"
    ) as docs_client:
        docs = await docs_client.get("/api/docs")
        assert docs.status_code == 200 and "/api/openapi.json" in docs.text


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
