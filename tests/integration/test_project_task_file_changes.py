"""Actual file-tool changes survive the task journal and later conversation turns."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from tests.integration.test_project_capabilities import create_task, follow_up, harness_models
from tests.integration.test_project_task_planning import tool_call


async def test_real_file_changes_persist_and_stay_with_their_turn(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    harness_models(
        monkeypatch,
        [
            tool_call("write_file", "write-note", file_path="note.txt", content="before\nbefore\n"),
            tool_call(
                "edit_file",
                "edit-note",
                file_path="note.txt",
                old_string="before",
                new_string="after",
                replace_all=True,
            ),
            tool_call(
                "edit_file",
                "failed-edit",
                file_path="note.txt",
                old_string="missing text",
                new_string="must not count",
            ),
            tool_call("publish_workspace_file", "publish-note", path="note.txt"),
            AIMessage(content="Updated and attached the note."),
            AIMessage(content="The note contains two identical lines."),
        ],
    )
    final = await create_task(client, auth, options={"action_timeline_enabled": False})
    assert final["status"] == "completed", final
    turn = final["turns"][0]
    changes = turn["file_changes"]
    assert len(changes) == 2
    assert {change["path"] for change in changes} == {"note.txt"}
    assert [(change["additions"], change["deletions"]) for change in changes] == [(2, 0), (2, 2)]
    assert "+before" in changes[0]["diff"]
    assert "-before" in changes[1]["diff"] and "+after" in changes[1]["diff"]
    blocks = {
        block["tool_call_id"]: block for block in turn["content_blocks"] if block["type"] == "tool"
    }
    assert blocks["failed-edit"]["status"] == "failed"
    assert not blocks["failed-edit"]["file_changes"]
    saved = server.services.project_task_turn_repo.get(turn["turn_id"])
    receipts = [
        change
        for block in json.loads(saved.content_blocks_json)
        for change in block["file_changes"]
    ]
    assert receipts == changes

    reopened = await client.get(f"/api/project-tasks/{final['task_id']}", headers=auth)
    assert reopened.json()["turns"][0]["file_changes"] == changes
    continued = await follow_up(
        client, auth, final["task_id"], options={"action_timeline_enabled": False}
    )
    assert continued["status"] == "completed", continued
    assert continued["turns"][0]["file_changes"] == changes
    assert continued["turns"][1]["file_changes"] == []
