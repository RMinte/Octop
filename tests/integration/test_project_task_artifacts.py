"""Server tools publish durable attachments through the real harness and task APIs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
from zipfile import ZipFile

import pytest
from langgraph.prebuilt.tool_node import ToolCallRequest
from pydantic import ValidationError

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle
from octop.infra.errors import OctopError
from octop.infra.projects import artifacts, runtime, tasks
from octop.infra.projects import execution as execution_module
from octop.infra.projects.artifact_models import (
    CreateArtifactArgs,
    PublishWorkspaceArchiveArgs,
    PublishWorkspaceFileArgs,
)
from octop.infra.projects.execution import TaskExecution
from tests.integration.test_project_task_planning import (
    latest_plan,
    planning_models,
    step_command,
    tool_call,
)
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until
from tests.support.auth import create_user, resolve_user_id
from tests.support.fakes import FakeHarnessAgent


@pytest.fixture
def real_model(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from octop_harness import HarnessAgent
    from octop_harness.llm.factory import ChatModelFactory

    def configure(responses, gate=None, *, gate_after=1, waiting=None):
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
                if len(observed) >= gate_after and gate is not None:
                    if waiting is not None:
                        waiting.set()
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
                        "args": {
                            "name": "报告.md",
                            "content": "实际报告内容",
                        },
                    },
                    {
                        "id": "publish-slides",
                        "name": "publish_workspace_file",
                        "args": {
                            "path": "slides.pptx",
                            "name": "演示.pptx",
                        },
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
        assert {a["name"]: a["business_category"] for a in attached} == {
            "报告.md": "document",
            "演示.pptx": "presentation",
        }
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
        assert {r["business_category"] for r in replies} == {"document", "presentation"}
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
    assert attachments[0]["business_category"] == "document"
    assert (await client.get(attachments[0]["download_url"], headers=auth)).content == b""
    results = [json.loads(m.content) for m in observed[-1] if isinstance(m, ToolMessage)]
    assert {r["error_code"] for r in results if r.get("is_error")} == {
        "TASK_ARTIFACT_INVALID",
        "TASK_ARTIFACT_UNAVAILABLE",
    }


async def test_standalone_plan_cannot_finish_before_publishing_generated_application(
    env_with_provider, monkeypatch
):
    from langchain_core.messages import AIMessage, ToolMessage

    client, _, auth = env_with_provider
    files = {
        "notes-app/index.html": "<title>Notes</title>",
        "notes-app/app.js": "const notes = [];",
    }

    def deliver_after_rejection(messages):
        rejected = next(
            message
            for message in reversed(messages)
            if isinstance(message, ToolMessage) and message.tool_call_id == "premature-finish"
        )
        assert rejected.status == "error" and "publish_workspace_archive" in rejected.content
        assert latest_plan(messages)["revision"] == 1
        return tool_call(
            "publish_workspace_archive", "deliver-app", paths=list(files), name="notes-app.zip"
        )

    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call(
                "planning",
                "make-plan",
                command="create",
                title="创建笔记应用",
                steps=["实现并交付应用"],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"write-{index}",
                        "name": "write_file",
                        "args": {"file_path": "/" + path, "content": content},
                    }
                    for index, (path, content) in enumerate(files.items())
                ],
            ),
            step_command(
                "mark_step", "premature-finish", status="completed", result="文件已写入服务端。"
            ),
            deliver_after_rejection,
            step_command(
                "mark_step", "actual-finish", status="completed", result="应用已打包并附加。"
            ),
            AIMessage(content="笔记应用已作为 ZIP 交付。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "制作可使用的笔记网页。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    final = await settled(client, auth, response.json()["task_id"])
    assert final["status"] == "completed", final
    turn = final["turns"][0]
    assert turn["plan_revision"] == 2 and turn["plan"]["steps"][0]["status"] == "completed"
    assert turn["reply"] == "笔记应用已作为 ZIP 交付。" and len(observed) == 6
    assert len(turn["artifacts"]) == 1
    attachment = turn["artifacts"][0]
    assert attachment["name"] == "notes-app.zip" and attachment["media_type"] == "application/zip"
    download = await client.get(attachment["download_url"], headers=auth)
    with ZipFile(io.BytesIO(download.content)) as archive:
        assert set(archive.namelist()) == set(files)
        assert {name: archive.read(name).decode() for name in archive.namelist()} == files


async def test_unplanned_file_task_keeps_public_text_while_correcting_missing_delivery(
    env_with_provider, monkeypatch
):
    from langchain_core.messages import AIMessage, HumanMessage

    client, _, auth = env_with_provider
    premature = "文件已保存在服务端 app.html，需要 ZIP 可以再告诉我。"

    def publish_after_correction(messages):
        assert isinstance(messages[-1], HumanMessage)
        assert "publish_workspace_file" in messages[-1].content
        assert messages[-2].content == premature
        return tool_call("publish_workspace_file", "deliver-file", path="app.html")

    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call("write_file", "write-app", file_path="/app.html", content="<h1>Ready</h1>"),
            AIMessage(content=premature),
            publish_after_correction,
            AIMessage(content="应用文件已附加，可直接下载。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "生成一个网页文件。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    final = await settled(client, auth, response.json()["task_id"])
    assert final["status"] == "completed", final
    turn = final["turns"][0]
    assert turn["reply"] == premature + "应用文件已附加，可直接下载。" and len(observed) == 4
    assert turn["reply"].count(premature) == 1 and premature not in turn["reasoning"]
    texts = [block for block in turn["content_blocks"] if block["type"] == "text"]
    assert [block["text"] for block in texts] == [premature, "应用文件已附加，可直接下载。"]
    assert len({block["block_id"] for block in texts}) == 2
    assert all(block["status"] == "completed" for block in texts)
    assert len(turn["artifacts"]) == 1 and turn["artifacts"][0]["name"] == "app.html"
    downloaded = await client.get(turn["artifacts"][0]["download_url"], headers=auth)
    assert downloaded.content == b"<h1>Ready</h1>"


async def test_public_text_is_live_before_delivery_correction_and_does_not_prove_delivery(
    env_with_provider, real_model
):
    from langchain_core.messages import AIMessage, HumanMessage

    client, server, auth = env_with_provider
    release, correction_waiting = asyncio.Event(), asyncio.Event()
    premature = "服务端文件已写好，接下来交付。"
    observed, _ = real_model(
        [
            tool_call("write_file", "write-app", file_path="/app.html", content="<h1>Ready</h1>"),
            AIMessage(content=premature),
            tool_call("publish_workspace_file", "deliver-file", path="app.html"),
            AIMessage(content="附件已交付。"),
        ],
        release,
        gate_after=2,
        waiting=correction_waiting,
    )
    session, ws = await connected(client, auth)
    try:
        created = (
            await command(
                ws,
                "task.create",
                "create-corrected-delivery",
                data={
                    "request_id": str(uuid4()),
                    "prompt": "生成一个网页文件。",
                    "options": {"action_timeline_enabled": False},
                },
            )
        )["data"]
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "watch-corrected-delivery",
                "task_id": created["task_id"],
            }
        )
        await asyncio.wait_for(correction_waiting.wait(), 5)
        live = await until(
            ws,
            lambda frame: (
                frame["type"] in {"snapshot", "turn"}
                and (
                    frame["data"]["turns"][0]["reply"]
                    if frame["type"] == "snapshot"
                    else frame["data"]["turn"]["reply"]
                )
                == premature
            ),
        )
        live_turn = live["data"]["turns"][0] if live["type"] == "snapshot" else live["data"]["turn"]
        assert live_turn["status"] == "running"
        assert live_turn["reasoning"] == "" and live_turn["artifacts"] == []
        detail = (await client.get(f"/api/project-tasks/{created['task_id']}", headers=auth)).json()
        assert detail["turns"][0]["reply"] == premature
        row = await asyncio.get_running_loop().run_in_executor(
            None, server.services.project_task_turn_repo.get, live_turn["turn_id"]
        )
        assert row is not None and row.reply == premature and row.status == "running"
        original_block = next(
            block for block in live_turn["content_blocks"] if block["type"] == "text"
        )
        release.set()
        final = await settled(client, auth, created["task_id"])
        turn = final["turns"][0]
        assert turn["status"] == "completed" and turn["reply"] == premature + "附件已交付。"
        assert turn["reasoning"] == "" and len(turn["artifacts"]) == 1
        texts = [block for block in turn["content_blocks"] if block["type"] == "text"]
        assert [block["text"] for block in texts] == [premature, "附件已交付。"]
        assert texts[0]["block_id"] == original_block["block_id"]
        assert len(observed) == 4
        assert isinstance(observed[2][-1], HumanMessage)
        assert observed[2][-2].content == premature
        assert "publish_workspace_file" in observed[2][-1].content
    finally:
        release.set()
        await session.__aexit__(None, None, None)


async def test_repeated_path_only_answers_cannot_complete_a_standalone_file_task(
    env_with_provider, monkeypatch
):
    from langchain_core.messages import AIMessage

    client, _, auth = env_with_provider
    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call("write_file", "write-app", file_path="/app.html", content="<h1>Ready</h1>"),
            AIMessage(content="文件在服务端 app.html，之后可以打包。"),
            AIMessage(content="仍然只提供服务端路径。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "prompt": "生成网页。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    final = await settled(client, auth, response.json()["task_id"])
    assert final["status"] == "failed", final
    turn = final["turns"][0]
    assert turn["reply"] == "文件在服务端 app.html，之后可以打包。仍然只提供服务端路径。"
    assert turn["reasoning"] == "" and turn["artifacts"] == []
    texts = [block for block in turn["content_blocks"] if block["type"] == "text"]
    assert [block["text"] for block in texts] == [
        "文件在服务端 app.html，之后可以打包。",
        "仍然只提供服务端路径。",
    ]
    assert [block["status"] for block in texts] == ["completed", "interrupted"]
    assert len(observed) == 3


async def test_existing_project_edits_do_not_require_a_downloadable_copy(
    env_with_provider, monkeypatch, tmp_path
):
    from langchain_core.messages import AIMessage

    client, _, auth = env_with_provider
    root = tmp_path / "existing-project"
    root.mkdir()
    (root / "app.js").write_text("const before = 1;", encoding="utf-8")
    project_response = await client.post(
        "/api/projects",
        headers={**auth, "Idempotency-Key": str(uuid4())},
        json={"name": "Existing", "directory": str(root)},
    )
    assert project_response.status_code == 201, project_response.text
    observed, _ = planning_models(
        monkeypatch,
        [
            tool_call(
                "edit_file",
                "edit-app",
                file_path="/app.js",
                old_string="before",
                new_string="after",
            ),
            AIMessage(content="项目文件已修改。"),
        ],
    )
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "project_id": project_response.json()["project_id"],
            "prompt": "修改这个已有项目文件。",
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    final = await settled(client, auth, response.json()["task_id"])
    assert final["status"] == "completed", final
    assert final["turns"][0]["reply"] == "项目文件已修改。"
    assert final["turns"][0]["artifacts"] == [] and len(observed) == 2
    assert (root / "app.js").read_text(encoding="utf-8") == "const after = 1;"


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


@pytest.mark.parametrize(
    "tool_name,arguments,category",
    [
        (
            "create_artifact",
            {"name": "note.MD", "content": "note", "media_type": "image/png"},
            "document",
        ),
        ("publish_workspace_file", {"path": "source.md", "name": "renamed.PPTX"}, "presentation"),
        ("publish_workspace_archive", {"paths": ["source.md"], "name": "source.ZIP"}, "other"),
    ],
)
async def test_all_publish_tool_wrappers_classify_final_name_automatically(
    publisher_env, tool_name, arguments, category
):
    client, _, auth, publisher, root, task = publisher_env
    (root / "source.md").write_bytes(b"source content")
    tool = next(tool for tool in publisher.tools() if tool.name == tool_name)
    assert "business_category" not in tool.args_schema.model_json_schema()["properties"]
    result = await tool.ainvoke(
        {
            "type": "tool_call",
            "id": "category-call",
            "name": tool_name,
            "args": arguments,
        }
    )
    published = json.loads(result.content)
    assert published["status"] == "attached", published
    assert published["business_category"] == category
    detail = await client.get(
        f"/api/project-tasks/artifacts/{published['artifact_id']}", headers=auth
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["business_category"] == category
    history = await client.get(f"/api/project-tasks/{task['task_id']}", headers=auth)
    assert history.status_code == 200, history.text
    assert history.json()["turns"][0]["artifacts"] == [
        {key: value for key, value in published.items() if key != "status"}
    ]


@pytest.mark.parametrize(
    "tool_name,method_name,args_model,legacy_arguments,expected_category",
    [
        (
            "create_artifact",
            "create",
            CreateArtifactArgs,
            {"name": "legacy.md", "content": "legacy content", "media_type": None},
            "document",
        ),
        (
            "publish_workspace_file",
            "publish",
            PublishWorkspaceFileArgs,
            {"path": "legacy.md", "name": None},
            "document",
        ),
        (
            "publish_workspace_archive",
            "archive",
            PublishWorkspaceArchiveArgs,
            {"paths": ["legacy.md"], "name": "legacy.zip"},
            "other",
        ),
    ],
)
async def test_business_category_preserves_legacy_request_hash_and_replay(
    publisher_env, tool_name, method_name, args_model, legacy_arguments, expected_category
):
    _, server, _, publisher, root, _ = publisher_env
    (root / "legacy.md").write_bytes(b"legacy source")
    expected_hash = hashlib.sha256(
        json.dumps([tool_name, legacy_arguments], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    omitted = args_model.model_validate(legacy_arguments)
    assert publisher._request_hash(tool_name, omitted) == expected_hash
    operation = getattr(publisher, method_name)
    original = await operation("legacy-call", omitted)
    assert original.business_category == expected_category
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT request_hash FROM project_task_artifacts WHERE artifact_id=?",
                (original.artifact_id,),
            ).fetchone()[0]
            == expected_hash
        )
    (root / "legacy.md").unlink()
    assert await operation("legacy-call", omitted) == original
    with pytest.raises(OctopError) as conflict:
        await operation(
            "legacy-call",
            args_model.model_validate(
                {
                    **legacy_arguments,
                    "name": "changed.md" if method_name != "archive" else "changed.zip",
                }
            ),
        )
    assert conflict.value.code == "TASK_ARTIFACT_CONFLICT"


async def test_archive_publishes_only_explicit_files_and_replays_without_sources(publisher_env):
    _, server, _, publisher, root, task = publisher_env
    (root / "app").mkdir()
    (root / "app" / "index.html").write_text("<h1>Notes</h1>", encoding="utf-8")
    (root / "app" / "app.js").write_text("const notes = [];", encoding="utf-8")
    (root / "unrelated-secret.txt").write_text("must not package", encoding="utf-8")
    arguments = PublishWorkspaceArchiveArgs(paths=["app/index.html", "app/app.js"], name="app.zip")
    artifact = await publisher.archive("zip-call", arguments)
    assert await publisher.has_artifacts()
    metadata, data = artifacts.download(
        server.services, publisher.owner_id, task["task_id"], artifact.artifact_id
    )
    assert metadata.media_type == "application/zip"
    with ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == arguments.paths
        assert archive.read("app/index.html") == b"<h1>Notes</h1>"
        assert archive.read("app/app.js") == b"const notes = [];"
    (root / "app" / "app.js").unlink()
    assert await publisher.archive("zip-call", arguments) == artifact
    with pytest.raises(OctopError) as conflict:
        await publisher.archive(
            "zip-call", PublishWorkspaceArchiveArgs(paths=["unrelated-secret.txt"], name="app.zip")
        )
    assert conflict.value.code == "TASK_ARTIFACT_CONFLICT"


async def test_restored_completed_plan_can_publish_only_while_delivery_is_missing(
    publisher_env, monkeypatch
):
    _, _, _, publisher, root, _ = publisher_env
    (root / "app.html").write_bytes(b"<h1>Recovered app</h1>")
    monkeypatch.setattr(
        execution_module,
        "get_config",
        lambda: {"configurable": {"thread_id": publisher.turn.turn_id}},
    )
    monkeypatch.setattr(execution_module, "get_stream_writer", lambda: lambda _: None)
    middleware = TaskExecution(
        SimpleNamespace(phase="completed", binding=lambda step_id=None: (None, None)),
        "en",
        publisher.turn.turn_id,
        artifact_publisher=publisher,
    )
    middleware.restore(
        [
            {
                "type": "tool",
                "tool_name": "write_file",
                "tool_call_id": "previous-write",
                "status": "completed",
                "started_at": datetime.now(UTC).isoformat(),
                "arguments": {"file_path": "/app.html"},
            }
        ]
    )
    tool = next(tool for tool in publisher.tools() if tool.name == "publish_workspace_file")
    call = {
        "name": tool.name,
        "args": {"path": "app.html"},
        "id": "recovered-delivery",
        "type": "tool_call",
    }
    assert await middleware.delivery_pending()
    assert middleware.rejection(call, tool) is None

    async def dispatch(actual):
        return await actual.tool.ainvoke(actual.tool_call)

    result = await middleware.run(
        ToolCallRequest(tool_call=call, tool=tool, state={}, runtime=None), dispatch
    )
    delivered = json.loads(result.content)
    assert delivered["status"] == "attached" and delivered["name"] == "app.html"
    assert not await middleware.delivery_pending()
    assert middleware.rejection(call, tool) is not None


async def test_archive_rejects_directories_missing_files_and_uncompressed_size_limit(
    publisher_env, monkeypatch
):
    _, _, _, publisher, root, _ = publisher_env
    (root / "directory").mkdir()
    for source, expected in [
        ("directory", "TASK_ARTIFACT_INVALID"),
        ("missing.txt", "TASK_ARTIFACT_UNAVAILABLE"),
    ]:
        with pytest.raises(OctopError) as rejected:
            await publisher.archive(
                source, PublishWorkspaceArchiveArgs(paths=[source], name="app.zip")
            )
        assert rejected.value.code == expected
    (root / "one.txt").write_bytes(b"123")
    (root / "two.txt").write_bytes(b"456")
    monkeypatch.setattr(artifacts, "MAX_TURN_BYTES", 5)
    with pytest.raises(OctopError) as limited:
        await publisher.archive(
            "too-large", PublishWorkspaceArchiveArgs(paths=["one.txt", "two.txt"], name="app.zip")
        )
    assert limited.value.code == "TASK_ARTIFACT_LIMIT"
    assert not await publisher.has_artifacts()


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
        "durable",
        CreateArtifactArgs(name="proof.txt", content="backup proof"),
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


@pytest.mark.parametrize("category", ["document", "report", "all", "", None])
@pytest.mark.parametrize(
    "args_model,arguments",
    [
        (CreateArtifactArgs, {"name": "note.md", "content": "note"}),
        (PublishWorkspaceFileArgs, {"path": "note.md"}),
        (PublishWorkspaceArchiveArgs, {"paths": ["note.md"], "name": "notes.zip"}),
    ],
)
def test_publish_business_category_is_read_only(args_model, arguments, category):
    with pytest.raises(ValidationError):
        args_model.model_validate({**arguments, "business_category": category})


@pytest.mark.parametrize("name", sorted(artifacts.SERVER_ARTIFACT_TOOLS))
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


@pytest.mark.parametrize(
    "arguments",
    [
        {"paths": [], "name": "app.zip"},
        {"paths": ["app.html", "app.html"], "name": "app.zip"},
        {"paths": ["../secret"], "name": "app.zip"},
        {"paths": ["/absolute.txt"], "name": "app.zip"},
        {"paths": ["app.html"], "name": "app.txt"},
    ],
)
def test_archive_requires_a_safe_explicit_manifest(arguments):
    with pytest.raises(ValidationError):
        PublishWorkspaceArchiveArgs.model_validate(arguments)
