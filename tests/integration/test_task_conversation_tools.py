"""Complete public exports and Task-only memory maintenance."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from octop_memory.core import Memory

from octop.infra.agents.providers.box import OctopChatModelFactory
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks import task_env as task_env
from tests.support.auth import auth_header, create_user


async def test_task_memory_is_scoped_idempotent_and_preserves_checkpoint(task_env):
    client, server, auth, _ = task_env
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "memory fixture"},
    )
    assert response.status_code == 202, response.text
    task = await settled(client, auth, response.json()["task_id"])
    task_id = task["task_id"]
    root = server.services.paths.root / "project-tasks" / task_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "checkpoints.sqlite").write_bytes(b"checkpoint-content-must-not-be-mutated")
    checkpoint = list(root.rglob("checkpoints.sqlite"))
    before = {str(path): path.read_bytes() for path in checkpoint}
    body = {"request_id": str(uuid4()), "content": "Use cobalt for this task."}
    saved = await client.post(f"/api/project-tasks/{task_id}/memory", headers=auth, json=body)
    assert saved.status_code == 200, saved.text
    entry = saved.json()
    assert entry["editable"] and entry["id"].startswith("atom/")
    replay = await client.post(f"/api/project-tasks/{task_id}/memory", headers=auth, json=body)
    assert replay.json() == entry
    conflict = await client.post(
        f"/api/project-tasks/{task_id}/memory", headers=auth, json={**body, "content": "different"}
    )
    assert conflict.status_code == 409
    edited = await client.post(
        f"/api/project-tasks/{task_id}/memory",
        headers=auth,
        json={"request_id": str(uuid4()), "id": entry["id"], "content": "Use amber instead."},
    )
    assert edited.status_code == 200, edited.text
    listing = (await client.get(f"/api/project-tasks/{task_id}/memory", headers=auth)).json()
    atoms = [item for item in listing["entries"] if item["layer"] == "atom"]
    assert [item["content"] for item in atoms] == ["Use amber instead."]
    raw = next(item for item in listing["entries"] if item["layer"] == "raw")
    forbidden = await client.delete(
        f"/api/project-tasks/{task_id}/memory",
        headers=auth,
        params={"id": raw["id"], "request_id": str(uuid4())},
    )
    assert forbidden.status_code == 403
    delete = {"id": edited.json()["id"], "request_id": str(uuid4())}
    for _ in range(2):
        response = await client.delete(
            f"/api/project-tasks/{task_id}/memory", headers=auth, params=delete
        )
        assert response.status_code == 204, response.text
    assert {str(path): path.read_bytes() for path in checkpoint} == before
    await create_user(client, auth, username="memory-outsider")
    outsider = await auth_header(client, username="memory-outsider")
    assert (
        await client.get(f"/api/project-tasks/{task_id}/memory", headers=outsider)
    ).status_code == 404
    memory = Memory(task_id, backend_config={"db_path": str(root / "memory.sqlite")})
    assert not memory.list_atoms()
    memory.backend.close()


async def test_export_uses_all_public_turns_and_not_internal_configuration(task_env):
    client, server, auth, _ = task_env
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": str(uuid4()), "prompt": "export fixture"},
    )
    task = await settled(client, auth, response.json()["task_id"])
    task_id = task["task_id"]
    original = server.services.project_task_turn_repo.get(task["turns"][0]["turn_id"])
    with server.services.db.transaction() as conn:
        for index in range(55):
            server.services.project_task_branch_repo.insert_history(
                conn,
                replace(
                    original,
                    turn_id="turn_" + uuid4().hex,
                    request_id="export_" + uuid4().hex,
                    created_at=original.created_at + index + 1,
                ),
            )
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET reasoning=?,configuration_json=? WHERE task_id=?",
            (
                "private-reasoning-proof",
                json.dumps({"providers": {"api_key": "secret-config-proof"}}),
                task_id,
            ),
        )
    exported = await client.get(f"/api/project-tasks/{task_id}/export", headers=auth)
    assert exported.status_code == 200, exported.text
    data = exported.json()
    assert len(data["turns"]) == 56 and data["task"]["task_id"] == task_id
    assert "private-reasoning-proof" not in exported.text
    assert "secret-config-proof" not in exported.text
    assert "attachment" in exported.headers["content-disposition"]
    markdown = await client.get(
        f"/api/project-tasks/{task_id}/export?format=markdown", headers=auth
    )
    assert markdown.status_code == 200 and "export fixture" in markdown.text
    assert "### User" in markdown.text


async def test_polish_changes_only_draft_and_records_no_task(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    model = FakeMessagesListChatModel(responses=[AIMessage(content="A clearer draft")])
    monkeypatch.setattr(OctopChatModelFactory, "get_chat_model", lambda self, reference: model)
    with server.services.db.connect() as conn:
        before = conn.execute("SELECT count(*) FROM project_tasks").fetchone()[0]
    response = await client.post("/api/project-tasks/polish", headers=auth, json={"text": "draft"})
    assert response.status_code == 200, response.text
    assert response.json() == {"text": "A clearer draft"}
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM project_tasks").fetchone()[0] == before
