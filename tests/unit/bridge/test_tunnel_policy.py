"""Unit tests for Bridge HTTP tunnel path allowlist."""

from __future__ import annotations

from octop.infra.bridge.tunnel_policy import is_tunnel_path_allowed


def test_allows_agent_list_get() -> None:
    assert is_tunnel_path_allowed("GET", "/api/agents")
    assert is_tunnel_path_allowed("GET", "/api/agents/")
    assert not is_tunnel_path_allowed("POST", "/api/agents")


def test_allows_agent_resource_paths() -> None:
    assert is_tunnel_path_allowed("GET", "/api/agents/01ABC")
    assert is_tunnel_path_allowed("GET", "/api/agents/01ABC/threads")
    assert is_tunnel_path_allowed("POST", "/api/agents/01ABC/uploads")
    assert is_tunnel_path_allowed("GET", "/api/agents/bridge:cid:aid/avatar")
    assert is_tunnel_path_allowed("GET", "/api/agents/01ABC/history/versions")


def test_allows_composer_readonly_paths() -> None:
    assert is_tunnel_path_allowed("GET", "/api/providers/resolved")
    assert is_tunnel_path_allowed("GET", "/api/providers/active-model")
    assert is_tunnel_path_allowed("GET", "/api/knowledge-bases")
    assert is_tunnel_path_allowed("GET", "/api/knowledge-bases/capability")
    assert is_tunnel_path_allowed("GET", "/api/agents/01ABC/status")
    assert is_tunnel_path_allowed("GET", "/api/agents/01ABC/subagents")
    assert is_tunnel_path_allowed("GET", "/api/agents/01ABC/tool-settings")
    assert is_tunnel_path_allowed("PATCH", "/api/agents/01ABC/tool-settings/shell")
    assert is_tunnel_path_allowed("POST", "/api/agents/01ABC/reload")
    assert is_tunnel_path_allowed("GET", "/api/agents/01ABC/acp")
    assert is_tunnel_path_allowed("PUT", "/api/agents/01ABC/acp/tool")
    assert not is_tunnel_path_allowed("GET", "/api/agents/01ABC/history-migration/status")
    assert not is_tunnel_path_allowed("GET", "/api/agents/01ABC/skill-packages")
    assert not is_tunnel_path_allowed("POST", "/api/providers/resolved")
    assert not is_tunnel_path_allowed("PUT", "/api/providers/active-model")
    assert not is_tunnel_path_allowed("POST", "/api/knowledge-bases")
    assert not is_tunnel_path_allowed("GET", "/api/knowledge-bases/kb1")
    assert not is_tunnel_path_allowed("GET", "/api/knowledge-bases/kb1/documents")
    assert not is_tunnel_path_allowed("GET", "/api/providers")


def test_allows_browser_viewer_paths() -> None:
    assert is_tunnel_path_allowed("GET", "/api/browser/env-status")
    assert is_tunnel_path_allowed("GET", "/api/browser/harness-sessions")
    assert is_tunnel_path_allowed("POST", "/api/browser/sessions/user-1/handoff")
    assert not is_tunnel_path_allowed("POST", "/api/browser/env-status")
    assert not is_tunnel_path_allowed("POST", "/api/browser/install")
    assert not is_tunnel_path_allowed("POST", "/api/browser/uninstall")
    assert not is_tunnel_path_allowed("POST", "/api/browser/shutdown")
    assert not is_tunnel_path_allowed("GET", "/api/browser/record-replay/status")


def test_allows_experts_non_agent_paths() -> None:
    assert is_tunnel_path_allowed("GET", "/api/plugins/agents/01ABC")
    assert is_tunnel_path_allowed("PATCH", "/api/plugins/agents/01ABC/tools")
    assert is_tunnel_path_allowed("GET", "/api/mbti/current")
    assert is_tunnel_path_allowed("POST", "/api/mbti/apply")
    assert is_tunnel_path_allowed("GET", "/api/subagent-catalog")
    assert is_tunnel_path_allowed("GET", "/api/subagent-catalog/divisions")
    assert is_tunnel_path_allowed("GET", "/api/acp")
    assert is_tunnel_path_allowed("PUT", "/api/acp/opencode")
    assert is_tunnel_path_allowed("GET", "/api/cron/settings")
    assert is_tunnel_path_allowed("GET", "/api/connector-instances")
    assert not is_tunnel_path_allowed("GET", "/api/plugins")
    assert not is_tunnel_path_allowed("POST", "/api/plugins/install")
    assert not is_tunnel_path_allowed("POST", "/api/subagent-catalog")
    assert not is_tunnel_path_allowed("POST", "/api/cron/settings")
    assert not is_tunnel_path_allowed("POST", "/api/connector-instances")
    assert not is_tunnel_path_allowed("GET", "/api/connector-instances/inst1")


def test_denies_management_and_auth_paths() -> None:
    assert not is_tunnel_path_allowed("GET", "/api/users")
    assert not is_tunnel_path_allowed("POST", "/api/auth/login")
    assert not is_tunnel_path_allowed("GET", "/api/bridge/connections")
    assert not is_tunnel_path_allowed("GET", "/api/admin/audit")
    assert not is_tunnel_path_allowed("POST", "/api/settings")
    assert not is_tunnel_path_allowed("DELETE", "/api/setup/password")


def test_agentcomputer_tunnel_only_allows_project_task_conversation_contract() -> None:
    uid = "11111111-1111-4111-8111-111111111111"
    allowed = {
        "GET": [
            "/api/agentcomputer/v1/capabilities",
            "/api/agentcomputer/v1/projects",
            f"/api/agentcomputer/v1/projects/{uid}",
            f"/api/agentcomputer/v1/projects/{uid}/documents",
            f"/api/agentcomputer/v1/projects/{uid}/documents/brief.md",
            f"/api/agentcomputer/v1/projects/{uid}/documents/说明.txt",
            f"/api/agentcomputer/v1/projects/{uid}/tasks",
            f"/api/agentcomputer/v1/tasks/{uid}",
            f"/api/agentcomputer/v1/tasks/{uid}/turns",
            f"/api/agentcomputer/v1/runs/{uid}",
            f"/api/agentcomputer/v1/runs/{uid}/result",
            f"/api/agentcomputer/v1/commands/{uid}",
        ],
        "POST": [
            "/api/agentcomputer/v1/projects",
            f"/api/agentcomputer/v1/projects/{uid}/tasks",
            f"/api/agentcomputer/v1/tasks/{uid}/turns",
        ],
        "PUT": [f"/api/agentcomputer/v1/projects/{uid}/documents/brief.md"],
    }
    for method, paths in allowed.items():
        for path in paths:
            assert is_tunnel_path_allowed(method, path), (method, path)

    denied = [
        ("POST", "/api/agentcomputer/v1/capabilities"),
        ("DELETE", "/api/agentcomputer/v1/projects"),
        ("POST", f"/api/agentcomputer/v1/tasks/{uid}"),
        ("POST", f"/api/agentcomputer/v1/runs/{uid}/cancel"),
        ("GET", f"/api/agentcomputer/v1/tasks/{uid}/snapshot"),
        ("PUT", f"/api/agentcomputer/v1/projects/{uid}/documents"),
        ("POST", f"/api/agentcomputer/v1/projects/{uid}/documents/brief.md"),
        ("DELETE", f"/api/agentcomputer/v1/projects/{uid}/documents/brief.md"),
        ("GET", f"/api/agentcomputer/v1/projects/{uid}/documents/.hidden.md"),
        ("GET", f"/api/agentcomputer/v1/projects/{uid}/documents/bad%2Fname.md"),
        ("GET", f"/api/agentcomputer/v1/projects/{uid}/documents/bad:name.md"),
        ("GET", f"/api/agentcomputer/v1/projects/{uid}/documents/brief.pdf"),
        ("GET", f"/api/agentcomputer/v1/projects/{uid}/documents/brief.md/extra"),
        ("GET", "/api/agentcomputer/v1/projects/not-a-uuid"),
        ("GET", f"/api/agentcomputer/v1/projects/{uid}/tasks/extra"),
        ("GET", "/api/agentcomputer/v1/../auth/login"),
    ]
    for method, path in denied:
        assert not is_tunnel_path_allowed(method, path), (method, path)
