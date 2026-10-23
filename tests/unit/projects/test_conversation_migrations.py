"""Migration preserves source bytes and publishes only verified, owned history."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import OctopError
from octop.infra.gateway.process.message_keys import INBOUND_ATTACHMENTS_KEY
from octop.infra.history.projection import message_inputs
from octop.infra.history.service import HistoryArchive
from octop.infra.history.store import HistoryStore
from octop.infra.history.trajectory.types import TrajectoryEvent
from octop.infra.projects import conversation_migrations as migration
from octop.infra.projects import source_routes, tasks
from octop.infra.projects.expert_profiles import _workspace
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def source(tmp_path):
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    services = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    services.secret_repo.get_or_create("jwt", lambda: b"migration-test-secret")
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(2,'other','user',1)")
    config = {"workspace_dir": str(tmp_path / "expert")}
    services.agent_repo.create(
        agent_id="expert", user_id=1, name="Expert", config_json=json.dumps(config)
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,title,pinned,created_at,last_active) VALUES('thread','expert',1,'dashboard','dashboard:expert:1','Old conversation',1,1,2)"
        )
    workspace = _workspace(services, services.agent_repo.get("expert"), config)
    workspace.upload_bytes("inbound/source.csv", b"quarter,revenue\nQ1,12")
    messages = [
        HumanMessage(
            content="Question",
            id="user",
            additional_kwargs={
                INBOUND_ATTACHMENTS_KEY: [
                    {
                        "filename": "source.csv",
                        "workspace_path": "inbound/source.csv",
                        "media_type": "text/csv",
                    }
                ]
            },
        ),
        AIMessage(content="Answer", id="assistant"),
    ]
    services.thread_message_repo.replace_all("thread", message_inputs(messages))
    try:
        yield services, workspace, messages
    finally:
        db.close()


def body(request_id="migration-request"):
    return migration.MigrationCreate(request_id=request_id)


async def drain(services, batch_id):
    worker = migration.ConversationMigrationWorker(services)
    while await worker.run_once(batch_id):
        pass
    return migration.detail(services, 1, batch_id)


@pytest.mark.parametrize("channel", ["dashboard", "telegram", "cli", "cron"])
async def test_all_ordinary_channels_copy_history_and_attachment_without_source_changes(
    source, channel
):
    services, workspace, _messages = source
    with services.db.transaction() as conn:
        conn.execute("UPDATE threads SET channel_type=? WHERE thread_id='thread'", (channel,))
    original = services.thread_message_repo.range_rows("thread", 0, 100)
    batch = migration.create(services, 1, body())
    result = await drain(services, batch.batch_id)
    assert result.items[0].status == "completed", result.items[0].error_code
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    assert task.pinned and task.selected_expert_id == "expert"
    assert task.project_id is None and task.turns[0].history_only
    assert task.turns[0].prompt == "Question" and task.turns[0].reply == "Answer"
    assert task.turns[0].source_thread_id == "thread"
    assert services.thread_message_repo.range_rows("thread", 0, 100) == original
    attachment = task.turns[0].attachments[0]
    workspace.upload_bytes("inbound/source.csv", b"changed source")
    assert (
        migration.input_attachments.download(
            services, 1, attachment.attachment_id, task_id=task.task_id
        )[1]
        == b"quarter,revenue\nQ1,12"
    )
    assert migration.legacy_link(services, 1, "thread").task_id == task.task_id
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    with pytest.raises(OctopError):
        migration.detail(services, 2, batch.batch_id)
    with pytest.raises(OctopError):
        migration.legacy_link(services, 2, "thread")


async def test_duplicate_batch_and_new_request_reuse_canonical_target(source):
    services, _workspace, _messages = source
    batch = migration.create(services, 1, body())
    assert migration.create(services, 1, body()).batch_id == batch.batch_id
    first = await drain(services, batch.batch_id)
    second = await drain(services, migration.create(services, 1, body("second-request")).batch_id)
    assert first.items[0].target_task_id == second.items[0].target_task_id
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 1


async def test_writer_fence_waits_for_active_source_then_retries(source):
    services, _workspace, _messages = source
    batch = migration.create(services, 1, body())
    fence = source_routes.writer_fence(services, "thread")
    fence.__enter__()
    try:
        first = await drain(services, batch.batch_id)
        assert first.status == "waiting" and first.items[0].error_code == "source_active"
        assert services.project_task_repo.get(first.items[0].target_task_id, 1) is None
    finally:
        fence.__exit__(None, None, None)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE conversation_migration_items SET updated_at=0 WHERE batch_id=?",
            (batch.batch_id,),
        )
    assert (await drain(services, batch.batch_id)).items[0].status == "completed"


async def test_competing_batch_cannot_release_another_batches_cutover_fence(source):
    services, _workspace, _messages = source
    first = migration.create(services, 1, body("first-batch"))
    second = migration.create(services, 1, body("second-batch"))
    worker = migration.ConversationMigrationWorker(services)
    item = services.conversation_migration_repo.claim(
        "first-worker", migration.now_ms(), first.batch_id
    )
    route, item = worker._drain(source_routes.legacy_route(services, 1, "thread", "expert"), item)
    assert await worker.run_once(second.batch_id)
    failed = migration.detail(services, 1, second.batch_id)
    assert failed.items[0].error_code == "source_mapping_conflict"
    current = source_routes.legacy_route(services, 1, "thread", "expert")
    assert current.state == "draining" and current.pending_task_id == item.target_task_id
    assert current.revision == route.revision


def test_late_migration_lease_cannot_release_reclaimed_source_route(source):
    services, _workspace, _messages = source
    batch = migration.create(services, 1, body())
    worker = migration.ConversationMigrationWorker(services)
    item = services.conversation_migration_repo.claim(
        "old-worker", migration.now_ms(), batch.batch_id
    )
    route, item = worker._drain(source_routes.legacy_route(services, 1, "thread", "expert"), item)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE conversation_migration_items SET worker_id='new-worker' WHERE item_id=?",
            (item.item_id,),
        )
    worker._finish(item, "failed", "source_unavailable", {}, release_route=route)
    current = source_routes.legacy_route(services, 1, "thread", "expert")
    assert current.state == "draining" and current.revision == route.revision
    assert (
        services.conversation_migration_repo.items(1, batch.batch_id)[0].worker_id == "new-worker"
    )


async def test_staged_failure_resumes_without_duplicate_side_effects(source, monkeypatch):
    services, _workspace, _messages = source
    batch = migration.create(services, 1, body())
    worker = migration.ConversationMigrationWorker(services)
    original = worker._publish
    monkeypatch.setattr(
        worker, "_publish", lambda *_args: (_ for _ in ()).throw(OSError("simulated interruption"))
    )
    await worker.run_once(batch.batch_id)
    failed = migration.detail(services, 1, batch.batch_id)
    assert failed.items[0].status == "failed"
    assert services.project_task_repo.get(failed.items[0].target_task_id, 1) is None
    migration.control(services, 1, batch.batch_id, "resume")
    monkeypatch.setattr(worker, "_publish", original)
    assert await worker.run_once(batch.batch_id)
    result = migration.detail(services, 1, batch.batch_id)
    assert result.items[0].status == "completed"
    assert result.items[0].target_task_id == failed.items[0].target_task_id
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM project_task_attachments").fetchone()[0] == 1


async def test_pause_and_automatic_queue_are_persistent_and_idempotent(source):
    services, _workspace, _messages = source
    worker = migration.ConversationMigrationWorker(services)
    assert worker.enqueue_automatic() == []
    migration.automation(services, 1, True)
    first = worker.enqueue_automatic()
    assert first == worker.enqueue_automatic()
    migration.control(services, 1, first[0], "pause")
    assert not await worker.run_once(first[0])
    migration.control(services, 1, first[0], "resume")
    assert (await drain(services, first[0])).items[0].status == "completed"


def test_resident_and_prototype_are_explicitly_excluded(source):
    services, _workspace, _messages = source
    with services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO resident_conversations(owner_id,role,agent_id,thread_id,created_at,updated_at) VALUES(1,'primary','expert','thread',1,1)"
        )
    preview = migration.preflight(services, 1, body())
    assert (
        preview.eligible == 0 and preview.candidates[0].disposition == "resident_adapter_required"
    )
    assert migration.create(services, 1, body()).items[0].status == "skipped"


async def test_incomplete_history_is_preserved_without_running_pending_tool(source):
    services, _workspace, messages = source
    messages.append(HumanMessage(content="Unfinished prompt", id="pending-user"))
    messages.append(
        AIMessage(
            content="Working",
            id="pending-ai",
            tool_calls=[
                {
                    "id": "old-call",
                    "name": "execute",
                    "args": {"command": "never run"},
                    "type": "tool_call",
                }
            ],
        )
    )
    services.thread_message_repo.replace_all("thread", message_inputs(messages))
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    assert task.turns[-1].status == "cancelled"
    assert task.turns[-1].content_blocks[-1].status == "interrupted"
    assert task.turns[-1].history_only
    assert not task.active_turn_id


async def test_mixed_archive_reads_legacy_prefix_and_v2_without_touching_archive(source):
    services, _workspace, _messages = source
    path = services.paths.root / "history_v2.sqlite"
    archive = HistoryArchive(
        HistoryStore(path, identity=str(services.paths.root / "octop.db")),
        services.thread_message_repo,
        services.trajectory_event_repo,
        enabled=True,
    )
    turn = archive.begin("expert", "thread")
    archive.save_messages(
        turn,
        [
            message_to_dict(HumanMessage(content="New archive question", id="new-user")),
            message_to_dict(AIMessage(content="New archive answer", id="new-ai")),
        ],
    )
    archive.finish(turn["id"], "completed")
    archive.store.close()
    before = path.read_bytes()
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    assert result.items[0].status == "completed", result.items[0].error_code
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    assert [turn.prompt for turn in task.turns] == ["Question", "New archive question"]
    assert path.read_bytes() == before


async def test_mixed_trajectory_preserves_public_events_without_new_execution(source):
    from dataclasses import asdict

    services, _workspace, _messages = source
    services.agent_repo.create(agent_id="trajectory-speaker", user_id=1, name="Source speaker")
    events = [
        TrajectoryEvent(
            "legacy-tool",
            "thread",
            "expert",
            1,
            123.0,
            "tool",
            "old-turn",
            2,
            False,
            "Read source",
            {
                "call_id": "legacy-call",
                "name": "read_file",
                "args": json.dumps({"file_path": "source.csv", "api_key": "must-be-redacted"}),
                "result": "Visible result",
                "reasoning_content": "private-thinking",
                "speaker_agent_id": "member",
                "speaker_name": "Member",
            },
        ),
        TrajectoryEvent(
            "legacy-system",
            "thread",
            "expert",
            2,
            124.0,
            "system",
            "old-turn",
            None,
            False,
            "private-system-summary",
            {"content": "private-system-content"},
        ),
        TrajectoryEvent(
            "legacy-incomplete",
            "thread",
            "expert",
            3,
            125.0,
            "tool",
            "old-turn",
            3,
            False,
            "Interrupted write",
            {
                "call_id": "incomplete-call",
                "name": "write_file",
                "args": {"file_path": "never-written.txt"},
            },
        ),
    ]
    for event in events:
        services.trajectory_event_repo.append(event)
    archive = HistoryArchive(
        HistoryStore(
            services.paths.root / "history_v2.sqlite",
            identity=str(services.paths.root / "octop.db"),
        ),
        services.thread_message_repo,
        services.trajectory_event_repo,
        enabled=True,
    )
    turn = archive.begin("expert", "thread")
    archive.save_messages(
        turn,
        [
            message_to_dict(HumanMessage(content="Archive question", id="archive-user")),
            message_to_dict(AIMessage(content="Archive answer", id="archive-ai")),
        ],
    )
    archived = TrajectoryEvent(
        "v2-event",
        "thread",
        "expert",
        4,
        126.0,
        "assistant",
        turn["id"],
        1,
        False,
        "Visible archive summary",
        {
            "content": "Visible <think>private</think> archive content",
            "message_id": "archive-ai",
            "speaker_agent_id": "trajectory-speaker",
            "speaker_name": "Source speaker",
        },
    )
    with archive.store.transaction() as conn:
        archive.store.put_document(
            conn,
            doc_id="event:v2-event",
            thread_id="thread",
            turn_id=turn["id"],
            kind="event",
            seq=4,
            value=asdict(archived),
        )
    archive.finish(turn["id"], "completed")
    archive.store.close()
    before = list(services.trajectory_event_repo.iter_for_export("thread"))
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    assert result.items[0].status == "completed", result.items[0].error_code
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    configurations = [
        json.loads(services.project_task_turn_repo.get(turn.turn_id).configuration_json)
        for turn in task.turns
    ]
    copied = [
        event for config in configurations for event in config["migrated_history"]["trajectory"]
    ]
    assert [event["source_event_id"] for event in copied] == [
        "legacy-tool",
        "legacy-system",
        "legacy-incomplete",
        "v2-event",
    ]
    assert all(event["history_only"] and not event["restorable"] for event in copied)
    public = [event for turn in task.turns for event in turn.source_trajectory]
    assert [event.source_event_id for event in public] == [
        event["source_event_id"] for event in copied
    ]
    assert not services.agent_repo.delete_if_unreferenced("trajectory-speaker")
    assert (
        copied[1]["source_status"] == "redacted"
        and copied[1]["payload"] == {}
        and not copied[1]["summary"]
    )
    assert copied[2]["source_status"] == "incomplete"
    assert copied[-1]["mapping"] == "message" and copied[-1]["source_turn_id"] == turn["id"]
    serialized = json.dumps(configurations)
    assert (
        "must-be-redacted" not in serialized
        and "private-thinking" not in serialized
        and "private-system-content" not in serialized
    )
    blocks = [block for turn in task.turns for block in turn.content_blocks if block.type == "tool"]
    assert len(blocks) == 2
    assert blocks[0].output == "Visible result" and blocks[0].speaker_agent_id == "member"
    assert blocks[1].status == "interrupted" and blocks[1].output is None
    assert not (services.paths.root / "never-written.txt").exists()
    assert list(services.trajectory_event_repo.iter_for_export("thread")) == before
    assert result.items[0].manifest["trajectory_event_count"] == 4
    with services.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM project_task_execution_receipts").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


async def test_trajectory_change_during_staging_prevents_cutover(source, monkeypatch):
    services, _workspace, _messages = source
    worker = migration.ConversationMigrationWorker(services)
    original = worker._stage_files

    async def stage(*args):
        files = await original(*args)
        services.trajectory_event_repo.append(
            TrajectoryEvent(
                "late-event",
                "thread",
                "expert",
                1,
                123.0,
                "assistant",
                "old",
                None,
                False,
                "Late",
                {"content": "Late visible text"},
            )
        )
        return files

    monkeypatch.setattr(worker, "_stage_files", stage)
    batch = migration.create(services, 1, body())
    assert await worker.run_once(batch.batch_id)
    result = migration.detail(services, 1, batch.batch_id)
    assert result.items[0].status == "failed" and result.items[0].error_code == "source_changed"
    assert services.project_task_repo.get(result.items[0].target_task_id, 1) is None


async def test_visible_tool_wire_does_not_copy_credential_or_thinking_fields(source):
    services, _workspace, _messages = source
    messages = [
        HumanMessage(content="Read", id="user"),
        AIMessage(
            content="",
            id="call",
            tool_calls=[
                {
                    "id": "wire-call",
                    "name": "read_file",
                    "args": {"file_path": "source.csv", "api_key": "wire-secret"},
                }
            ],
        ),
        ToolMessage(
            content=json.dumps(
                {
                    "text": "Visible",
                    "Authorization": "private-auth",
                    "thinking": "private-reasoning",
                }
            ),
            tool_call_id="wire-call",
            id="result",
        ),
        AIMessage(content="Done", id="assistant"),
    ]
    services.thread_message_repo.replace_all("thread", message_inputs(messages))
    before = services.thread_message_repo.range_rows("thread", 0, 100)
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    tools = [block for block in task.turns[0].content_blocks if block.type == "tool"]
    assert tools[0].arguments == {"file_path": "source.csv"}
    assert tools[0].output == {"text": "Visible"}
    assert services.thread_message_repo.range_rows("thread", 0, 100) == before


async def test_offline_compact_delta_checkpoint_is_read_without_starting_expert(source):
    from deepagents.graph import DeepAgentState
    from langgraph.graph import END, START, StateGraph
    from octop_memory.storage.backends.sqlite_checkpoint import CompactSqliteSaver

    services, workspace, _messages = source
    path = Path(workspace.workspace_dir) / "memory.sqlite"
    with sqlite3.connect(path, check_same_thread=False) as conn:
        saver = CompactSqliteSaver(conn)
        graph = StateGraph(DeepAgentState)
        graph.add_node(
            "agent",
            lambda _state: {"messages": [AIMessage(content="Checkpoint answer", id="cp-ai")]},
        )
        graph.add_edge(START, "agent")
        graph.add_edge("agent", END)
        compiled = graph.compile(checkpointer=saver)
        compiled.invoke(
            {"messages": [HumanMessage(content="Checkpoint question", id="cp-user")]},
            {"configurable": {"thread_id": "thread"}},
        )
    services.thread_message_repo.mark_projection("thread", "pending")
    before = path.read_bytes()
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    assert result.items[0].status == "completed", result.items[0].error_code
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    assert task.turns[0].prompt == "Checkpoint question"
    assert path.read_bytes() == before


async def test_deleted_target_is_not_resurrected(source):
    services, _workspace, _messages = source
    first = await drain(services, migration.create(services, 1, body()).batch_id)
    tasks.delete(services, 1, first.items[0].target_task_id)
    result = await drain(services, migration.create(services, 1, body("after-delete")).batch_id)
    assert result.items[0].status == "failed" and result.items[0].error_code == "target_deleted"
    assert migration.legacy_link(services, 1, "thread").state == "target_deleted"


async def test_ambiguous_jsonl_is_not_assigned_by_timestamp(source):
    services, workspace, _messages = source
    services.thread_message_repo.replace_all("thread", [])
    workspace.upload_bytes(
        "sessions/old.jsonl", b'{"role":"user","content":"Private unknown","ts":1}\n'
    )
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    assert result.items[0].error_code == "jsonl_owner_ambiguous"
    assert services.project_task_repo.get(result.items[0].target_task_id, 1) is None


@pytest.mark.parametrize("shared", [True, False])
async def test_shared_source_copies_only_currently_authorized_media(source, shared):
    services, workspace, messages = source
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE agents SET user_id=2,is_shared=? WHERE agent_id='expert'", (int(shared),)
        )
    workspace.upload_bytes("USER.md", b"Publisher private preference")
    messages[0].additional_kwargs[INBOUND_ATTACHMENTS_KEY].append(
        {"filename": "USER.md", "workspace_path": "USER.md", "media_type": "text/markdown"}
    )
    services.thread_message_repo.replace_all("thread", message_inputs(messages))
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    assert result.items[0].status == "completed"
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    assert task.selected_expert_id == ("expert" if shared else None)
    assert [attachment.name for attachment in task.turns[0].attachments] == (
        ["source.csv"] if shared else []
    )
    assert any(
        attachment.name == "USER.md" and not attachment.available
        for attachment in task.turns[0].legacy_attachments
    )


async def test_missing_and_inline_attachments_have_explicit_integrity_results(source):
    import base64
    from io import BytesIO

    from PIL import Image

    services, _workspace, messages = source
    stream = BytesIO()
    Image.new("RGB", (2, 2)).save(stream, format="PNG")
    messages[0].content = [
        {"type": "text", "text": "Question"},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
            },
        },
    ]
    messages[0].additional_kwargs[INBOUND_ATTACHMENTS_KEY].append(
        {"filename": "missing.txt", "workspace_path": "inbound/missing.txt"}
    )
    services.thread_message_repo.replace_all("thread", message_inputs(messages))
    result = await drain(services, migration.create(services, 1, body()).batch_id)
    task = tasks.detail(services, 1, result.items[0].target_task_id)
    assert len(task.turns[0].attachments) == 2
    assert result.items[0].manifest["missing_attachment_count"] == 1
    assert any(file.media_type == "image/png" for file in task.turns[0].attachments)


async def test_http_management_is_typed_and_owner_scoped(source):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from octop.api.app import _install_exception_handlers
    from octop.api.deps import current_user, get_server
    from octop.api.routers.conversation_migrations import router

    services, _workspace, _messages = source
    app = FastAPI()
    _install_exception_handlers(app)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=services, database_bound=True
    )
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/project-task-migrations", json={"request_id": "http-migration"}
        )
        assert response.status_code == 200
        batch_id = response.json()["batch_id"]
        await drain(services, batch_id)
        linked = await client.get("/api/project-task-migrations/legacy/thread")
        assert linked.json()["url"].startswith("/conversations/task_")
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=2)
        assert (await client.get("/api/project-task-migrations/" + batch_id)).status_code == 404
        assert (await client.get("/api/project-task-migrations/legacy/thread")).status_code == 404
    assert "MigrationBatch" in app.openapi()["components"]["schemas"]


def test_cli_preflight_and_offline_run_need_no_agent(source, monkeypatch):
    from contextlib import contextmanager

    from click.testing import CliRunner

    from octop.cli.commands import conversation_migrations as command

    services, _workspace, _messages = source

    @contextmanager
    def opened():
        yield services

    monkeypatch.setattr(command, "open_cli_services", opened)
    runner = CliRunner()
    preview = runner.invoke(command.conversation_migrations, ["preflight", "--user", "owner"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["eligible"] == 1
    batch = migration.create(services, 1, body())
    result = runner.invoke(
        command.conversation_migrations, ["run", batch.batch_id, "--user", "owner"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["items"][0]["status"] == "completed"
