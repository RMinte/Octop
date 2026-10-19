"""Server tools publish durable attachments through the real harness and task APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle
from octop.infra.errors import OctopError
from octop.infra.projects import artifacts, runtime, tasks
from octop.infra.projects.artifact_models import CreateArtifactArgs, PublishWorkspaceFileArgs
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until
from tests.support.auth import create_user, resolve_user_id
from tests.support.fakes import FakeHarnessAgent


@pytest.fixture
def real_model(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from octop_harness import HarnessAgent
    from octop_harness.llm.factory import ChatModelFactory

    def configure(responses, gate=None):
        observed, names = [], set()

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools: Any, **kwargs: Any):
                from langchain_core.utils.function_calling import convert_to_openai_tool

                for item in tools:
                    names.add(item.name if hasattr(item, "name") else item.get("name"))
                    if getattr(item, "name", "") in artifacts.SERVER_ARTIFACT_TOOLS:
                        parameters = convert_to_openai_tool(item)["function"]["parameters"]
                        assert "call_id" not in parameters["properties"]
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                observed.append(messages)
                return super()._generate(messages, stop, run_manager, **kwargs)

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                if observed and gate is not None:
                    await gate.wait()
                return self._generate(messages, stop, run_manager, **kwargs)

        model = Model(responses=responses)

        class Factory(ChatModelFactory):
            def get_chat_model(self, ref):
                return model

        monkeypatch.setattr(
            runtime,
            "HarnessAgent",
            lambda cfg, **kwargs: HarnessAgent(cfg, model_factory=Factory(cfg.providers), **kwargs),
        )
        return observed, names

    return configure


async def test_real_tools_stream_and_restore_immutable_attachments(
    env_with_provider, tmp_path, monkeypatch, real_model
):
    from langchain_core.messages import AIMessage, ToolMessage

    from octop.api.routers import project_tasks_ws

    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    root = tmp_path / "server"
    root.mkdir()
    content = b"PK\x03\x04binary-server-delivery\x00\xff"
    (root / "slides.pptx").write_bytes(content)
    gate = asyncio.Event()
    observed, names = real_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "create-report",
                        "name": "create_artifact",
                        "args": {"name": "报告.md", "content": "实际报告内容"},
                    },
                    {
                        "id": "publish-slides",
                        "name": "publish_workspace_file",
                        "args": {"path": "slides.pptx", "name": "演示.pptx"},
                    },
                ],
            ),
            AIMessage(content="两份附件已交付"),
        ],
        gate,
    )
    project = (
        await client.post(
            "/api/projects",
            headers={**auth, "Idempotency-Key": "artifact-project-001"},
            json={
                "name": "Artifacts",
                "directory": str(root),
                "client_directory": "Z:\\client-only",
            },
        )
    ).json()
    experts = server.services.agent_repo.list_all()
    session, ws = await connected(client, auth)
    try:
        created = (
            await command(
                ws,
                "task.create",
                "create-artifacts-001",
                data={
                    "request_id": "artifact-message-001",
                    "prompt": "请交付报告和演示",
                    "project_id": project["project_id"],
                    "options": {"action_timeline_enabled": False},
                },
            )
        )["data"]
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "subscribe-artifacts-001",
                "task_id": created["task_id"],
                "cursor": created["event_cursor"],
            }
        )
        live = await until(
            ws, lambda f: f["type"] == "turn" and len(f["data"]["turn"]["artifacts"]) == 2
        )
        assert live["data"]["turn"]["status"] == "running"
        attached = live["data"]["turn"]["artifacts"]
        assert {a["name"] for a in attached} == {"报告.md", "演示.pptx"}
        assert {"create_artifact", "publish_workspace_file"} <= names
        assert not {"client__create_artifact", "client__publish_workspace_file"} & names
        assert str(root) not in json.dumps(attached) and "binary-server-delivery" not in json.dumps(
            live
        )
        await session.__aexit__(None, None, None)
        session, ws = await connected(client, auth)
        await ws.send_json(
            {"type": "task.subscribe", "id": "restore-artifacts-001", "task_id": created["task_id"]}
        )
        snapshot = await until(ws, lambda f: f["type"] == "snapshot")
        assert snapshot["data"]["turns"][0]["artifacts"] == attached
        (root / "slides.pptx").unlink()
        for artifact in attached:
            response = await client.get(artifact["download_url"], headers=auth)
            expected = content if artifact["source_path"] else "实际报告内容".encode()
            assert response.content == expected
            assert artifact["sha256"] == hashlib.sha256(expected).hexdigest()
            assert artifact["byte_count"] == len(expected)
            assert response.headers["content-disposition"].startswith("attachment;")
            assert "filename*=UTF-8''" in response.headers["content-disposition"]
            assert response.headers["x-content-type-options"] == "nosniff"
        gate.set()
        done = await settled(client, auth, created["task_id"])
        assert done["status"] == "completed" and done["turns"][0]["artifacts"] == attached
        replies = [json.loads(m.content) for m in observed[-1] if isinstance(m, ToolMessage)]
        assert {r["artifact_id"] for r in replies} == {a["artifact_id"] for a in attached}
        assert all(r["status"] == "attached" for r in replies)
        assert server.services.agent_repo.list_all() == experts
    finally:
        gate.set()
        await session.__aexit__(None, None, None)


async def test_no_project_text_attachment_and_tool_failure_do_not_fake_delivery(
    env_with_provider, real_model
):
    from langchain_core.messages import AIMessage, ToolMessage

    client, _, auth = env_with_provider
    observed, _ = real_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "unsupported-text",
                        "name": "create_artifact",
                        "args": {"name": "fake.pptx", "content": "not-a-presentation"},
                    },
                    {
                        "id": "missing-source",
                        "name": "publish_workspace_file",
                        "args": {"path": "missing.pdf"},
                    },
                    {
                        "id": "valid-text",
                        "name": "create_artifact",
                        "args": {"name": "note.txt", "content": ""},
                    },
                ],
            ),
            AIMessage(content="note.txt 已附加；fake.pptx 和 missing.pdf 交付失败。"),
        ]
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "standalone-artifact",
            "prompt": "Create attachments",
            "options": {"action_timeline_enabled": False},
        },
    )
    done = await settled(client, auth, response.json()["task_id"])
    assert done["status"] == "completed"
    attachments = done["turns"][0]["artifacts"]
    assert len(attachments) == 1 and attachments[0]["name"] == "note.txt", [
        m.content for m in observed[-1] if isinstance(m, ToolMessage)
    ]
    assert (await client.get(attachments[0]["download_url"], headers=auth)).content == b""
    results = [json.loads(m.content) for m in observed[-1] if isinstance(m, ToolMessage)]
    assert {r["error_code"] for r in results if r.get("is_error")} == {
        "TASK_ARTIFACT_INVALID",
        "TASK_ARTIFACT_UNAVAILABLE",
    }


@pytest.fixture
async def publisher_env(env_with_provider, tmp_path, monkeypatch):
    client, server, auth = env_with_provider
    captured, gate = [], asyncio.Event()

    async def run(services, owner, turn, history):
        captured.append((owner, turn))
        yield {"type": "token", "content": "working"}
        await gate.wait()
        yield {"type": "token", "content": " done"}

    monkeypatch.setattr(runtime, "run_turn", run)
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={"request_id": "publisher-message", "prompt": "Publish"},
    )
    for _ in range(100):
        if captured:
            break
        await asyncio.sleep(0.01)
    owner, turn = captured[0]
    root = tmp_path / "workspace"
    fake = FakeHarnessAgent(workspace_dir=root)
    publisher = artifacts.TaskArtifactPublisher(server.services, owner, turn, root, "en")
    publisher.workspace = fake.workspace
    yield client, server, auth, publisher, root, response.json()
    gate.set()


async def test_call_idempotency_survives_source_deletion_and_turn_completion(publisher_env):
    client, server, auth, publisher, root, task = publisher_env
    (root / "result.pdf").write_bytes(b"pdf snapshot")
    args = PublishWorkspaceFileArgs(path="result.pdf")
    original = await publisher.publish("same-call", args)
    (root / "result.pdf").unlink()
    assert await publisher.publish("same-call", args) == original
    with pytest.raises(OctopError) as conflict:
        await publisher.publish("same-call", PublishWorkspaceFileArgs(path="other.pdf"))
    assert conflict.value.code == "TASK_ARTIFACT_CONFLICT"
    await client.post(
        f"/api/project-tasks/{task['task_id']}/cancel",
        headers=auth,
        json={"turn_id": publisher.turn.turn_id},
    )
    await settled(client, auth, task["task_id"])
    assert await publisher.publish("same-call", args) == original
    with pytest.raises(OctopError) as closed:
        await publisher.create("new-call", CreateArtifactArgs(name="late.txt", content="late"))
    assert closed.value.code == "TASK_ARTIFACT_CLOSED"
    owner = await resolve_user_id(client, auth, "admin")
    _, data = artifacts.download(server.services, owner, task["task_id"], original.artifact_id)
    assert data == b"pdf snapshot"


async def test_download_ownership_and_task_scope_are_enforced(publisher_env):
    client, _, auth, publisher, _, task = publisher_env
    artifact = await publisher.create(
        "private-call", CreateArtifactArgs(name="private.html", content="<script>secret</script>")
    )
    alice = await create_user(client, auth, username="artifact-alice")
    assert (await client.get(artifact.download_url, headers=alice)).status_code == 404
    assert (await client.get(artifact.download_url)).status_code == 401
    wrong = artifact.download_url.replace(task["task_id"], "task_unknown")
    assert (await client.get(wrong, headers=auth)).status_code == 404
    absent = artifact.download_url.replace(artifact.artifact_id, "artifact_unknown")
    assert (await client.get(absent, headers=auth)).status_code == 404


async def test_concurrent_publications_share_persistent_turn_quota(publisher_env):
    _, server, _, publisher, _, _ = publisher_env
    outcomes = await asyncio.gather(
        *(
            publisher.create(f"call-{i}", CreateArtifactArgs(name=f"{i}.md", content="proof"))
            for i in range(6)
        ),
        return_exceptions=True,
    )
    assert (
        sum(
            isinstance(item, OctopError) and item.code == "TASK_ARTIFACT_LIMIT" for item in outcomes
        )
        == 1
    )
    with server.services.db.connect() as conn:
        assert server.services.project_task_artifact_repo.usage(conn, publisher.turn.turn_id) == (
            5,
            25,
        )


async def test_byte_limits_and_post_cancel_publication_are_checked_atomically(
    publisher_env, monkeypatch
):
    client, _, auth, publisher, root, task = publisher_env
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 10)
    monkeypatch.setattr(artifacts, "MAX_TURN_BYTES", 15)
    with pytest.raises(OctopError) as large:
        await publisher.create("large", CreateArtifactArgs(name="large.txt", content="x" * 11))
    assert large.value.code == "TASK_ARTIFACT_LIMIT"
    (root / "large.pdf").write_bytes(b"x" * 11)
    with pytest.raises(OctopError) as file_large:
        await publisher.publish("large-file", PublishWorkspaceFileArgs(path="large.pdf"))
    assert file_large.value.code == "TASK_ARTIFACT_LIMIT"
    await publisher.create("small-1", CreateArtifactArgs(name="1.txt", content="x" * 8))
    with pytest.raises(OctopError) as total:
        await publisher.create("small-2", CreateArtifactArgs(name="2.txt", content="x" * 8))
    assert total.value.code == "TASK_ARTIFACT_LIMIT"
    await client.post(
        f"/api/project-tasks/{task['task_id']}/cancel",
        headers=auth,
        json={"turn_id": publisher.turn.turn_id},
    )
    with pytest.raises(OctopError) as cancelled:
        await publisher.create("after-cancel", CreateArtifactArgs(name="late.txt", content="x"))
    assert cancelled.value.code == "TASK_ARTIFACT_CLOSED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
async def test_symlinks_and_directories_cannot_be_published(publisher_env, tmp_path):
    _, _, _, publisher, root, _ = publisher_env
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("private outside workspace")
    (root / "link.txt").symlink_to(external / "secret.txt")
    (root / "linked-dir").symlink_to(external, target_is_directory=True)
    (root / "directory").mkdir()
    for path in ["link.txt", "linked-dir/secret.txt", "directory"]:
        with pytest.raises(OctopError) as exc:
            await publisher.publish(path, PublishWorkspaceFileArgs(path=path))
        assert exc.value.code == "TASK_ARTIFACT_INVALID"


async def test_snapshots_survive_database_backup_and_repeated_migrations(publisher_env, tmp_path):
    from octop.infra.backup.snapshot import snapshot_sqlite_file

    _, server, _, publisher, _, task = publisher_env
    artifact = await publisher.create(
        "durable", CreateArtifactArgs(name="proof.txt", content="backup proof")
    )
    backup = tmp_path / "backup.sqlite"
    snapshot_sqlite_file(server.services.db.path, backup)
    restored = SqlitePool(backup)
    try:
        run_migrations(restored)
        run_migrations(restored)
        services = replace(server.services, repos=RepoBundle.from_pool(restored))
        metadata, data = artifacts.download(
            services, publisher.owner_id, task["task_id"], artifact.artifact_id
        )
        assert metadata == artifact and data == b"backup proof"
    finally:
        restored.close()


async def test_historical_attachment_pagination_and_archived_download(publisher_env):
    client, _, auth, publisher, _, task = publisher_env
    artifact = await publisher.create(
        "history", CreateArtifactArgs(name="history.md", content="history")
    )
    await client.post(
        f"/api/project-tasks/{task['task_id']}/cancel",
        headers=auth,
        json={"turn_id": publisher.turn.turn_id},
    )
    done = await settled(client, auth, task["task_id"])
    archived = await client.patch(
        f"/api/project-tasks/{task['task_id']}",
        headers=auth,
        json={"expected_revision": done["list_revision"], "archived": True},
    )
    assert archived.status_code == 200
    assert (await client.get(artifact.download_url, headers=auth)).content == b"history"
    restored = await client.patch(
        f"/api/project-tasks/{task['task_id']}",
        headers=auth,
        json={"expected_revision": archived.json()["list_revision"], "archived": False},
    )
    assert restored.status_code == 200
    followup = await client.post(
        f"/api/project-tasks/{task['task_id']}/messages",
        headers=auth,
        json={"request_id": "next-history-turn", "prompt": "next"},
    )
    assert followup.status_code == 202
    latest = (
        await client.get(f"/api/project-tasks/{task['task_id']}?limit=1", headers=auth)
    ).json()
    assert latest["turns"][0]["artifacts"] == []
    older = (
        await client.get(
            f"/api/project-tasks/{task['task_id']}",
            headers=auth,
            params={"limit": 1, "page_cursor": latest["next_page_cursor"]},
        )
    ).json()
    assert older["turns"][0]["artifacts"] == [artifact.model_dump(mode="json")]


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "folder/../secret",
        "/etc/file",
        "C:\\Users\\secret",
        "file:stream",
        "folder//file",
        "./file",
    ],
)
def test_publish_requires_a_server_workspace_relative_path(path):
    with pytest.raises(ValidationError):
        PublishWorkspaceFileArgs(path=path)


@pytest.mark.parametrize("name", ["create_artifact", "publish_workspace_file"])
def test_clients_cannot_register_server_artifact_tools(name):
    with pytest.raises(ValidationError):
        tasks.TaskExecutionOptions.model_validate(
            {
                "client_id": "client-001",
                "client_tools": [
                    {
                        "name": name,
                        "description": "not a client tool",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
        )
