"""Imported coding sessions persist as task history without starting a model or tools."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest

from octop.api.routers import project_imports
from octop.infra.projects import runtime
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks import task_env as task_env
from tests.support.auth import create_user


def source_turn(number=1):
    return {
        "external_turn_id": f"source-turn-{number}",
        "user_message": {
            "text": f"Remember imported fact {number}",
            "timestamp": f"2026-10-01T08:0{number}:00Z",
        },
        "assistant_message": {
            "text": f"Imported answer {number}",
            "timestamp": f"2026-10-01T08:0{number}:05Z",
        },
        "activities": [],
        "file_changes": [],
        "completed_at": f"2026-10-01T08:0{number}:06Z",
    }


def source_session(source="codex", **fields):
    return {
        "source": source,
        "external_session_id": "external-session-1",
        "project_id": None,
        "title": "Imported coding session",
        "source_updated_at": "2026-10-01T08:05:00Z",
        "source_revision": "first-export",
        "turns": [source_turn()],
        **fields,
    }


async def imported(client, auth, body, action="imported"):
    response = await client.post("/api/project-tasks/import", headers=auth, json=body)
    assert response.status_code in {200, 201}, response.text
    result = response.json()
    assert result["action"] == action, result
    return result["task"]


async def project(client, auth, **fields):
    response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Import destination", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_import_without_model_preserves_history_and_never_runs_tools(
    env, monkeypatch, tmp_path, source
):
    client, server, auth = env
    before = server.services.agent_repo.list_all()
    calls = []

    async def unexpected_run(*args):
        calls.append(args)
        yield {"type": "token", "content": "must not execute"}

    monkeypatch.setattr(runtime, "run_turn", unexpected_run)
    workspace = tmp_path / "destination"
    workspace.mkdir()
    existing = workspace / "proof.txt"
    existing.write_text("current server file", encoding="utf8")
    client_only = tmp_path / "client-only-never-created"
    destination = await project(
        client, auth, directory=str(workspace), client_directory=str(client_only)
    )
    body = source_session(source, project_id=destination["project_id"])
    body["turns"][0]["activities"] = [
        {
            "kind": "commentary",
            "detail": "Inspecting the imported source tree",
            "timestamp": "2026-10-01T08:01:01Z",
        },
        {
            "kind": "tool",
            "name": "exec_command",
            "detail": "Display the saved source file",
            "output": "historical command output",
            "succeeded": False,
            "timestamp": "2026-10-01T08:01:02Z",
            "duration_ms": 50,
            "exit_code": 1,
        },
    ]
    body["turns"][0]["file_changes"] = [
        {"path": "proof.txt", "old_content": "historical old", "new_content": "historical new"}
    ]
    task = await imported(client, auth, body)
    assert task["project_id"] == destination["project_id"]
    assert task["import_source"] == source
    assert task["external_session_id"] == body["external_session_id"]
    assert task["status"] == "completed" and task["active_turn_id"] is None
    assert len(task["turns"]) == 1
    turn = task["turns"][0]
    assert turn["status"] == "completed"
    assert turn["prompt"] == "Remember imported fact 1"
    assert turn["reply"] == "Imported answer 1"
    assert turn["model_ref"] == ""
    assert turn["input_tokens"] == turn["output_tokens"] == 0
    assert datetime.fromisoformat(turn["created_at"]) == datetime.fromisoformat(
        body["turns"][0]["user_message"]["timestamp"]
    )
    blocks = turn["content_blocks"]
    assert any(block["text"] == "Inspecting the imported source tree" for block in blocks)
    tool = next(block for block in blocks if block["type"] == "tool")
    assert tool["tool_name"] == "exec_command"
    assert tool["output"] == "historical command output" and tool["status"] == "failed"
    assert tool["tool_call_id"] is None and tool["arguments"] is None
    assert tool["duration_ms"] == 50
    history = turn["imported_history"]
    assert history["source"] == source and history["external_turn_id"] == "source-turn-1"
    assert len(history["activities"]) == 2
    assert history["activities"][0]["detail"] == "Inspecting the imported source tree"
    assert history["activities"][1]["exit_code"] == 1
    assert history["activities"][1]["output"] == "historical command output"
    assert turn["imported_history"]["file_changes"] == body["turns"][0]["file_changes"]
    assert existing.read_text(encoding="utf8") == "current server file"
    assert not client_only.exists()
    assert calls == [] and server.services.agent_repo.list_all() == before
    reread = await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)
    assert reread.status_code == 200 and reread.json() == task
    listing = (
        await client.get(f"/api/projects/{destination['project_id']}/tasks", headers=auth)
    ).json()["items"]
    assert len(listing) == 1 and listing[0]["import_source"] == source
    assert listing[0]["external_session_id"] == body["external_session_id"]


async def test_import_replay_answer_update_and_append_preserve_existing_turn_ids(env):
    client, server, auth = env
    body = source_session()
    first = await imported(client, auth, body)
    assert await imported(client, auth, body, "unchanged") == first
    metadata = {
        **body,
        "title": "Renamed source session",
        "source_revision": "new-export-same-transcript",
        "source_updated_at": "2026-10-02T08:05:00Z",
    }
    renamed = await imported(client, auth, metadata, "unchanged")
    assert renamed["task_id"] == first["task_id"]
    assert renamed["title"] == metadata["title"]
    assert renamed["turns"] == first["turns"]
    with server.services.db.connect() as conn:
        saved = conn.execute(
            "SELECT manifest_json FROM external_import_sessions WHERE task_id=?",
            (first["task_id"],),
        ).fetchone()["manifest_json"]
    assert json.loads(saved)["source_revision"] == metadata["source_revision"]
    assert await imported(client, auth, body, "unchanged") == renamed
    with server.services.db.connect() as conn:
        replayed = conn.execute(
            "SELECT manifest_json FROM external_import_sessions WHERE task_id=?",
            (first["task_id"],),
        ).fetchone()["manifest_json"]
    assert replayed == saved
    changed = deepcopy(metadata)
    changed["turns"][0]["assistant_message"]["text"] = "Corrected imported answer"
    changed["turns"].append(source_turn(2))
    updated = await imported(client, auth, changed, "updated")
    assert updated["task_id"] == first["task_id"]
    assert len(updated["turns"]) == 2
    assert updated["turns"][0]["turn_id"] == first["turns"][0]["turn_id"]
    assert updated["turns"][0]["reply"] == "Corrected imported answer"
    assert updated["turns"][1]["prompt"] == "Remember imported fact 2"
    assert await imported(client, auth, changed, "unchanged") == updated
    manual = await client.patch(
        f"/api/project-tasks/{first['task_id']}",
        headers=auth,
        json={"expected_revision": updated["list_revision"], "title": "My Octop title"},
    )
    assert manual.status_code == 200, manual.text
    after_rename = await imported(
        client, auth, {**changed, "title": "Another source title"}, "unchanged"
    )
    assert after_rename == manual.json()
    assert await imported(client, auth, changed, "unchanged") == after_rename


@pytest.mark.parametrize("change", ["prompt", "timestamp", "identity", "shorten"])
async def test_import_rejects_rewriting_source_user_history_without_mutation(env, change):
    client, _, auth = env
    body = source_session(turns=[source_turn(), source_turn(2)])
    first = await imported(client, auth, body)
    changed = deepcopy(body)
    if change == "prompt":
        changed["turns"][0]["user_message"]["text"] = "Rewritten user message"
    elif change == "timestamp":
        changed["turns"][0]["user_message"]["timestamp"] = "2026-10-01T08:01:01Z"
    elif change == "identity":
        changed["turns"][0]["external_turn_id"] = "different-source-turn"
    else:
        changed["turns"].pop()
    response = await client.post("/api/project-tasks/import", headers=auth, json=changed)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TASK_REQUEST_CONFLICT"
    reread = await client.get(f"/api/project-tasks/{first['task_id']}", headers=auth)
    assert reread.json() == first


async def test_import_source_and_user_are_isolated_and_destination_cannot_change(env):
    client, _, auth = env
    destination = await project(client, auth)
    body = source_session(project_id=destination["project_id"])
    codex = await imported(client, auth, body)
    claude = await imported(client, auth, {**body, "source": "claude"})
    assert claude["task_id"] != codex["task_id"]
    alice = await create_user(client, auth, username="import-alice")
    forbidden = await client.post("/api/project-tasks/import", headers=alice, json=body)
    assert forbidden.status_code == 404, forbidden.text
    alice_task = await imported(client, alice, {**body, "project_id": None})
    assert alice_task["task_id"] not in {codex["task_id"], claude["task_id"]}
    assert (
        await client.get(f"/api/project-tasks/{codex['task_id']}", headers=alice)
    ).status_code == 404
    reassigned = await client.post(
        "/api/project-tasks/import", headers=auth, json={**body, "project_id": None}
    )
    assert reassigned.status_code == 409, reassigned.text
    assert reassigned.json()["error"]["code"] == "TASK_REQUEST_CONFLICT"
    assert await imported(client, auth, body, "unchanged") == codex


@pytest.mark.parametrize(
    "source_prompt,source_answer",
    [
        ("Remember imported fact 1", "Imported answer 1"),
        ("/compact", "Imported summary: the blue bird is the chosen project mascot."),
    ],
)
async def test_imported_history_reaches_followup_with_current_project_configuration(
    task_env, source_prompt, source_answer
):
    client, _, auth, calls = task_env
    destination = await project(client, auth, instructions="Original project instructions")
    body = source_session(project_id=destination["project_id"])
    body["turns"][0]["user_message"]["text"] = source_prompt
    body["turns"][0]["assistant_message"]["text"] = source_answer
    first = await imported(client, auth, body)
    changed = await client.patch(
        f"/api/projects/{destination['project_id']}",
        headers=auth,
        json={"expected_revision": 1, "instructions": "Current project instructions"},
    )
    assert changed.status_code == 200, changed.text
    response = await client.post(
        f"/api/project-tasks/{first['task_id']}/messages",
        headers=auth,
        json={"request_id": "follow-import", "prompt": "Continue with that fact"},
    )
    assert response.status_code == 202, response.text
    done = await settled(client, auth, first["task_id"])
    assert done["status"] == "completed" and len(done["turns"]) == 2
    assert len(calls) == 1
    turn, history = calls[0]
    assert history == [
        {"role": "user", "content": source_prompt},
        {"role": "assistant", "content": source_answer},
    ]
    assert json.loads(turn.configuration_json)["instructions"] == "Current project instructions"
    assert turn.model_ref == "openai/gpt-4o" and done["turns"][-1]["project_revision"] == 2
    assert done["turns"][0] == first["turns"][0]
    assert await imported(client, auth, body, "unchanged") == done
    changed_source = deepcopy(body)
    changed_source["turns"][0]["assistant_message"]["text"] = "New source answer"
    conflict = await client.post("/api/project-tasks/import", headers=auth, json=changed_source)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "TASK_REQUEST_CONFLICT"
    assert (await client.get(f"/api/project-tasks/{first['task_id']}", headers=auth)).json() == done


async def test_import_replay_is_rejected_while_followup_is_running(task_env, monkeypatch):
    client, _, auth, _ = task_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        await release.wait()
        yield {"type": "token", "content": "Followup completed"}

    monkeypatch.setattr(runtime, "run_turn", blocked)
    body = source_session()
    first = await imported(client, auth, body)
    response = await client.post(
        f"/api/project-tasks/{first['task_id']}/messages",
        headers=auth,
        json={"request_id": "running-follow-import", "prompt": "Keep working"},
    )
    assert response.status_code == 202, response.text
    try:
        await asyncio.wait_for(entered.wait(), 3)
        conflict = await client.post("/api/project-tasks/import", headers=auth, json=body)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "TASK_BUSY"
    finally:
        release.set()
        await settled(client, auth, first["task_id"])


async def test_import_uses_owned_uploaded_attachments_and_marks_missing_originals(env):
    client, _, auth = env
    response = await client.post(
        "/api/project-tasks/attachments",
        headers=auth,
        files={"file": ("source.txt", b"historical uploaded bytes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    attachment = response.json()
    body = source_session()
    body["turns"][0]["user_message"]["attachment_ids"] = [attachment["attachment_id"]]
    body["turns"][0]["assistant_message"]["attachment_ids"] = [attachment["attachment_id"]]
    body["turns"][0]["user_message"]["missing_attachments"] = [
        {"name": "missing.pdf", "media_type": "application/pdf"}
    ]
    task = await imported(client, auth, body)
    turn = task["turns"][0]
    assert turn["attachments"] == [attachment]
    history = turn["imported_history"]
    assert history["assistant_attachments"] == [attachment]
    assert history["missing_user_attachments"] == [
        {"name": "missing.pdf", "media_type": "application/pdf"}
    ]
    download = await client.get(attachment["preview_url"], headers=auth)
    assert download.status_code == 200 and download.content == b"historical uploaded bytes"
    alice = await create_user(client, auth, username="attachment-import-alice")
    forbidden = await client.post("/api/project-tasks/import", headers=alice, json=body)
    assert forbidden.status_code in {404, 422}, forbidden.text
    assert (await client.get("/api/project-tasks", headers=alice)).json()["items"] == []


async def test_import_rejects_invalid_history_and_local_file_references_before_writing(env):
    client, _, auth = env
    invalid = []
    duplicate = source_session()
    duplicate["turns"].append(source_turn())
    invalid.append(duplicate)
    no_timezone = source_session(source_updated_at="2026-10-01T08:05:00")
    invalid.append(no_timezone)
    local_attachment = source_session()
    local_attachment["turns"][0]["user_message"]["attachments"] = [
        {"url": "file:///private/never-read.txt"}
    ]
    invalid.append(local_attachment)
    reasoning = source_session()
    reasoning["turns"][0]["activities"] = [{"kind": "reasoning", "detail": "private thought"}]
    invalid.append(reasoning)
    for body in invalid:
        response = await client.post("/api/project-tasks/import", headers=auth, json=body)
        assert response.status_code == 422, response.text
    assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []


@pytest.mark.parametrize("kind", ["history", "configuration"])
async def test_chunked_import_counts_raw_body_bytes_before_saving(env, monkeypatch, kind):
    client, _, auth = env
    if kind == "history":
        endpoint = "/api/project-tasks/import"
        listing = "/api/project-tasks"
        body = source_session()
        selected_limit, other_limit = "_MAX_HISTORY_BODY", "_MAX_CONFIGURATION_BODY"
    else:
        endpoint = listing = "/api/projects/import/configurations"
        body = {
            "source": "codex",
            "category": "settings",
            "name": "Explicitly selected settings",
            "files": [{"relative_path": "settings.json", "content_base64": "e30="}],
        }
        selected_limit, other_limit = "_MAX_CONFIGURATION_BODY", "_MAX_HISTORY_BODY"
    payload = json.dumps(body).encode()
    monkeypatch.setattr(project_imports, selected_limit, len(payload))
    monkeypatch.setattr(project_imports, other_limit, 1)

    async def chunks(data):
        width = len(payload) // 3
        for position in range(0, len(data), width):
            yield data[position : position + width]

    request = client.build_request(
        "POST",
        endpoint,
        headers={**auth, "Content-Type": "application/json"},
        content=chunks(b" " + payload),
    )
    assert "content-length" not in request.headers
    response = await client.send(request)
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "ATTACHMENT_TOO_LARGE"
    assert response.json()["error"]["details"]["max_bytes"] == len(payload)
    unchanged = (await client.get(listing, headers=auth)).json()
    assert (unchanged["items"] if kind == "history" else unchanged) == []
    accepted = await client.post(
        endpoint,
        headers={**auth, "Content-Type": "application/json"},
        content=chunks(payload),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["action"] == "imported"
