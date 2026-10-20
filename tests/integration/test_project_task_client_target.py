"""A configured client directory is never replaced with server-side project writes."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.integration.test_project_task_client_tools import claim, result
from tests.integration.test_project_task_planning import planning_models, tool_call
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until


async def project(env, client_directory, server_directory=None):
    client, _, auth = env
    response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={
            "name": "User selected project",
            "client_directory": client_directory,
            "directory": str(server_directory) if server_directory else None,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("server_directory", [False, True])
async def test_missing_client_channel_rejects_server_replacement_even_with_both_directories(
    env_with_provider, monkeypatch, tmp_path, server_directory
):
    from deepagents.backends.protocol import ExecuteResponse
    from octop_harness.backends.bwrap_shell import BubbledLocalShellBackend
    from octop_harness.backends.local_shell import HarnessLocalShellBackend

    client, _, auth = env_with_provider
    server_root = tmp_path / "server"
    server_root.mkdir()
    (server_root / "existing.txt").write_text("original", encoding="utf-8")
    selected = "C:\\Users\\owner\\selected-project"
    saved = await project(env_with_provider, selected, server_root if server_directory else None)
    effects = []

    def execute(self, command, *, timeout=None):
        effects.append(command)
        return ExecuteResponse(output="must not execute", exit_code=0, truncated=False)

    monkeypatch.setattr(HarnessLocalShellBackend, "execute", execute)
    monkeypatch.setattr(BubbledLocalShellBackend, "execute", execute)
    observed, agents = planning_models(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "wrong-write",
                        "name": "write_file",
                        "args": {"file_path": "/replacement/app.js", "content": "must not write"},
                    },
                    {
                        "id": "wrong-edit",
                        "name": "edit_file",
                        "args": {
                            "file_path": "/existing.txt",
                            "old_string": "original",
                            "new_string": "must not edit",
                        },
                    },
                    {"id": "wrong-execute", "name": "execute", "args": {"command": "must-not-run"}},
                ],
            ),
            AIMessage(content="本机通道尚未就绪，将保留原项目目录，等待客户端接入后继续。"),
        ],
    )
    created = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "project_id": saved["project_id"],
            "prompt": "直接在我选好的目录里开发。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert created.status_code == 202, created.text
    final = await settled(client, auth, created.json()["task_id"])
    assert final["status"] == "completed", final
    turn = final["turns"][0]
    assert turn["reply"] == "本机通道尚未就绪，将保留原项目目录，等待客户端接入后继续。"
    assert turn["artifacts"] == [] and effects == []
    assert {"write_file", "edit_file", "execute"}.isdisjoint(observed[0][2])
    assert {"read_file", "ls", "publish_workspace_file", "publish_workspace_archive"} <= observed[
        0
    ][2]
    replies = {
        message.tool_call_id: message
        for message in observed[-1][1]
        if isinstance(message, ToolMessage)
    }
    for call_id in ("wrong-write", "wrong-edit", "wrong-execute"):
        assert replies[call_id].status == "error" and "client__" in replies[call_id].content
        block = next(block for block in turn["content_blocks"] if block["tool_call_id"] == call_id)
        assert block["status"] == "failed" and block["started_at"] is None
    assert (server_root / "existing.txt").read_text(encoding="utf-8") == "original"
    assert not (Path(agents[0].config.workspace_dir) / "replacement").exists()
    current = (await client.get(f"/api/projects/{saved['project_id']}", headers=auth)).json()
    assert current["client_directory"] == selected
    assert current["directory"] == (str(server_root) if server_directory else None)
    system_content = observed[0][1][0].content
    system = (
        system_content
        if isinstance(system_content, str)
        else "\n".join(str(block.get("text", "")) for block in system_content)
    )
    assert (
        json.dumps(
            {
                "server_workspace": str(agents[0].config.workspace_dir),
                "client_directory": selected,
                "client_tools": [],
            },
            ensure_ascii=False,
        )
        in system
    )


async def test_project_writes_use_declared_client_tool_and_original_selected_directory(
    env_with_provider, monkeypatch, tmp_path
):
    client, _, auth = env_with_provider
    client_root, server_root = tmp_path / "already-selected", tmp_path / "server"
    client_root.mkdir()
    server_root.mkdir()
    saved = await project(env_with_provider, str(client_root), server_root)
    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call("client__write_file", "local-write", path="app.txt", content="client-proof"),
            AIMessage(content="已在原来选定的本机项目目录写入 app.txt。"),
        ],
    )
    client_id = "selected-client-install"
    definition = {
        "name": "write_file",
        "description": "Write a file within the already selected client project directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    }
    session, ws = await connected(client, auth)
    try:
        await command(ws, "client.attach", "attach-original-directory", client_id=client_id)
        created = await client.post(
            "/api/project-tasks",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "project_id": saved["project_id"],
                "prompt": "在已选目录创建 app.txt。",
                "options": {
                    "client_id": client_id,
                    "client_tools": [definition],
                    "action_timeline_enabled": False,
                },
            },
        )
        assert created.status_code == 202, created.text
        call = (await until(ws, lambda frame: frame["type"] == "tool.call"))["data"]
        assert call["arguments"] == {"path": "app.txt", "content": "client-proof"}
        token = (await claim(ws, call))["data"]["claim_token"]
        # This is the client's action under its existing directory authorization.
        destination = client_root / call["arguments"]["path"]
        destination.write_text(call["arguments"]["content"], encoding="utf-8")
        accepted = await result(ws, call, token, {"path": str(destination), "saved": True})
        assert accepted["type"] == "result", accepted
        final = await settled(client, auth, created.json()["task_id"])
        assert final["status"] == "completed", final
        assert destination.read_text(encoding="utf-8") == "client-proof"
        assert not (server_root / "app.txt").exists()
        assert final["turns"][0]["artifacts"] == []
        assert "client__write_file" in observed[0][2]
        assert {"write_file", "edit_file", "execute"}.isdisjoint(observed[0][2])
        receipt = next(
            message
            for message in observed[-1][1]
            if isinstance(message, ToolMessage) and message.tool_call_id == "local-write"
        )
        assert "client-proof" not in receipt.content and str(destination) in receipt.content
    finally:
        await session.__aexit__(None, None, None)
