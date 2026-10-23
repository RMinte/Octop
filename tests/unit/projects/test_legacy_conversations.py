"""Explicit old thread intake preserves its source and never resumes old tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
)

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.hitl.store import HitlPendingStore
from octop.infra.gateway.process.message_keys import INBOUND_ATTACHMENTS_KEY
from octop.infra.history.projection import message_inputs
from octop.infra.history.service import HistoryArchive
from octop.infra.history.store import HistoryStore
from octop.infra.projects import legacy_conversations, runtime, tasks
from octop.infra.projects.expert_profiles import _workspace
from octop.infra.projects.legacy_conversation_models import LegacyConversationCreate
from octop.infra.utils.paths import PathLayout


class Registry:
    def __init__(self, workspace, messages):
        self.messages = messages
        self.reads = 0
        self.reserved = False
        self.busy = False
        self.next = ()
        self.interrupts = ()
        self.harness = SimpleNamespace(
            workspace=workspace, graph=SimpleNamespace(aget_state=self.state)
        )

    async def state(self, _config):
        self.reads += 1
        return SimpleNamespace(
            values={"messages": self.messages},
            config={"configurable": {"thread_id": "thread"}},
            next=self.next,
            tasks=[SimpleNamespace(interrupts=self.interrupts)] if self.interrupts else [],
        )

    def try_begin_history_backfill(self, _agent):
        if self.busy or self.reserved:
            return False
        self.reserved = True
        return True

    def end_history_backfill(self, _agent):
        self.reserved = False

    def get_agent(self, _agent):
        return self.harness


@pytest.fixture
def source(tmp_path):
    db = SqlitePool(tmp_path / "legacy.db")
    run_migrations(db)
    services = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    services.secret_repo.get_or_create("jwt", lambda: b"legacy-test-secret")
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(2,'other','user',1)")
        conn.execute(
            "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES('project',1,'Project',1,1)"
        )
        conn.execute(
            "INSERT INTO projects(project_id,owner_id,name,created_at,updated_at) VALUES('foreign',2,'Other project',1,1)"
        )
    config = {"workspace_dir": str(tmp_path / "expert-workspace")}
    services.agent_repo.create(
        agent_id="expert", user_id=1, name="Expert", config_json=json.dumps(config)
    )
    services.agent_repo.create(agent_id="foreign-expert", user_id=2, name="Other")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,title,created_at,last_active,conversation_mode) VALUES('thread','expert',1,'dashboard','dashboard:expert:1','Old conversation',1,2,'plan')"
        )
    workspace = _workspace(services, services.agent_repo.get("expert"), config)
    workspace.upload_bytes("inbound/source.csv", b"quarter,revenue\nQ1,12")
    messages = [
        SystemMessage(content="Private system instruction", id="system"),
        HumanMessage(content="First question", id="user-one"),
        AIMessage(
            content="I will inspect it.",
            id="assistant-tool",
            tool_calls=[
                {
                    "id": "call",
                    "name": "read_file",
                    "args": {"file_path": "inbound/source.csv"},
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="Q1 revenue is 12.", tool_call_id="call", id="tool-one"),
        AIMessage(content="The first answer.", id="assistant-one"),
        HumanMessage(
            content="Second question",
            id="user-two",
            additional_kwargs={
                INBOUND_ATTACHMENTS_KEY: [
                    {
                        "filename": "source.csv",
                        "workspace_path": "inbound/source.csv",
                        "media_type": "text/csv",
                    },
                    {
                        "filename": "missing.txt",
                        "workspace_path": "inbound/missing.txt",
                        "media_type": "text/plain",
                    },
                ]
            },
        ),
        AIMessage(
            content="<think>Private hidden chain.</think>The second answer.", id="assistant-two"
        ),
    ]
    registry = Registry(workspace, messages)
    hitl = HitlPendingStore()
    try:
        yield services, registry, hitl, tmp_path
    finally:
        db.close()


def body(**changes):
    return LegacyConversationCreate.model_validate(
        {
            "request_id": "legacy-request",
            "agent_id": "expert",
            "thread_id": "thread",
            "project_id": None,
            **changes,
        }
    )


async def intake(source, request=None, archive=None, owner=1):
    services, registry, hitl, _path = source
    return await legacy_conversations.create(
        services,
        owner,
        request or body(),
        registry=registry,
        history_archive=archive,
        hitl_store=hitl,
    )


async def test_shared_source_requires_owned_thread_and_preserves_member_speakers(source):
    services, registry, _hitl, _path = source
    with services.db.transaction() as conn:
        conn.execute("UPDATE agents SET user_id=2,is_shared=1 WHERE agent_id='expert'")
    registry.messages[-1] = AIMessage(
        content="Member answer", additional_kwargs={"speaker_agent_id": "foreign-expert"}
    )
    registry.messages[-2].additional_kwargs[INBOUND_ATTACHMENTS_KEY].append(
        {"workspace_path": "USER.md", "filename": "private.md"}
    )
    registry.harness.workspace.upload_bytes("USER.md", b"Owner private memory")
    download = registry.harness.workspace.adownload_bytes
    accessed = []

    async def tracked(path):
        accessed.append(path)
        return await download(path)

    registry.harness.workspace.adownload_bytes = tracked
    task_id = await intake(source)
    detail = tasks.detail(services, 1, task_id)
    assert detail.selected_expert_id == "expert"
    assert detail.turns[-1].content_blocks[-1].speaker_agent_id == "foreign-expert"
    assert detail.turns[-1].content_blocks[-1].speaker_name is None
    assert services.thread_repo.get("thread").user_id == 1
    assert "USER.md" not in accessed
    private = next(
        item for item in detail.turns[-1].legacy_attachments if item.name == "private.md"
    )
    assert not private.available and private.download_url is None
    with pytest.raises(OctopError):
        await intake(source, body(request_id="other-source-request"), owner=2)


def snapshot(services):
    with services.db.read_snapshot() as conn:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("agents", "threads", "thread_messages", "thread_history_projection")
        }


@pytest.mark.parametrize("storage", ["checkpoint", "projection", "archive"])
async def test_explicit_import_keeps_source_history_and_copies_only_visible_prefix(source, storage):
    services, registry, _hitl, path = source
    archive = None
    if storage == "projection":
        services.thread_message_repo.append_legacy_interval(
            "thread", message_inputs(registry.messages)
        )
        services.thread_message_repo.mark_projection("thread", "ready")
    elif storage == "archive":
        archive = HistoryArchive(
            HistoryStore(path / "history.sqlite", identity="test"),
            services.thread_message_repo,
            services.trajectory_event_repo,
            enabled=True,
        )
        turn = archive.begin("expert", "thread")
        archive.save_messages(
            turn, [message_to_dict(item) for item in registry.messages if item.type != "system"]
        )
        archive.finish(turn["id"], "completed")
    before = snapshot(services)
    try:
        task_id = await intake(source, archive=archive)
        assert snapshot(services) == before
        assert registry.reserved is False
        target = services.project_task_repo.get(task_id, 1)
        assert target.selected_expert_id == "expert" and target.project_id is None
        assert target.action_mode == "plan" and target.active_turn_id is None
        copied = services.project_task_turn_repo.list(task_id)
        assert [row.prompt for row in copied] == ["First question", "Second question"]
        assert copied[-1].reply == "The second answer."
        assert "Private hidden chain" not in json.dumps(
            [row.configuration_json + row.content_blocks_json + row.reply for row in copied]
        )
        first = json.loads(copied[0].configuration_json)
        assert first["branch_origin"]["source_thread_id"] == "thread"
        assert first["branch_origin"]["source_agent_id"] == "expert"
        assert first["branch_origin"]["history_only"]
        assert "source_task_id" not in first["branch_origin"]
        second = json.loads(copied[1].configuration_json)
        assert second["legacy_attachments"][0]["available"] is True
        assert (
            second["legacy_attachments"][0]["download_url"]
            == "/api/agents/expert/workspace/download?path=inbound%2Fsource.csv"
        )
        assert second["legacy_attachments"][1]["available"] is False
        assert second["legacy_attachments"][1]["download_url"] is None
        blocks = json.loads(copied[0].content_blocks_json)
        assert (
            next(item for item in blocks if item["type"] == "tool")["output"] == "Q1 revenue is 12."
        )
        assert services.project_task_turn_repo.context(task_id) is None
        with services.db.read_snapshot() as conn:
            assert (
                services.project_task_legacy_source_repo.origin(conn, task_id).source_thread_id
                == "thread"
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM project_task_tool_calls WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM project_task_execution_receipts WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0]
                == 0
            )
    finally:
        if archive is not None:
            archive.store.close()


async def test_idempotent_replay_never_reloads_or_resumes_source(source):
    services, registry, _hitl, _path = source
    task_id = await intake(source)
    reads = registry.reads
    registry.busy = True
    registry.messages.append(HumanMessage(content="New source request"))
    assert await intake(source) == task_id
    assert registry.reads == reads
    with services.db.read_snapshot() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 1
    with pytest.raises(OctopError) as failure:
        await intake(source, body(selected_expert_id=None))
    assert failure.value.code == ErrorCode.TASK_REQUEST_CONFLICT


@pytest.mark.parametrize(
    "mutation",
    [
        "other_user",
        "foreign_thread",
        "foreign_agent",
        "team",
        "resident",
        "remote",
        "busy",
        "interrupt",
        "next",
        "hitl",
        "unfinished_tool",
        "unanswered_user",
        "foreign_project",
    ],
)
async def test_ineligible_source_never_creates_a_task(source, mutation):
    services, registry, hitl, _path = source
    owner, request = 1, body()
    with services.db.transaction() as conn:
        if mutation == "other_user":
            owner = 2
        elif mutation == "foreign_thread":
            conn.execute("UPDATE threads SET user_id=2 WHERE thread_id='thread'")
        elif mutation == "foreign_agent":
            request = body(agent_id="foreign-expert")
        elif mutation == "team":
            conn.execute("UPDATE agents SET kind='team' WHERE agent_id='expert'")
        elif mutation == "resident":
            services.resident_conversation_repo.reserve_in_transaction(
                conn, 1, "expert", "thread", 1
            )
        elif mutation == "remote":
            conn.execute(
                "UPDATE agents SET config_json=? WHERE agent_id='expert'",
                (json.dumps({"backend": {"type": "s3", "bucket": "private"}}),),
            )
        elif mutation == "foreign_project":
            request = body(project_id="foreign")
    if mutation == "busy":
        registry.busy = True
    elif mutation == "interrupt":
        registry.interrupts = (object(),)
    elif mutation == "next":
        registry.next = ("tools",)
    elif mutation == "hitl":
        hitl.register(
            thread_id="thread",
            agent_id="expert",
            user_id=1,
            session_key="dashboard:expert:1",
            channel_type="dashboard",
            action_requests=[{"name": "execute"}],
            review_configs=None,
        )
    elif mutation == "unfinished_tool":
        registry.messages.append(
            AIMessage(
                content="",
                tool_calls=[{"id": "pending", "name": "execute", "args": {}, "type": "tool_call"}],
            )
        )
    elif mutation == "unanswered_user":
        registry.messages.append(HumanMessage(content="Not answered yet"))
    with pytest.raises(OctopError):
        await intake(source, request, owner=owner)
    assert registry.reserved is False
    with services.db.read_snapshot() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0] == 0


async def test_clear_expert_and_choose_project_without_changing_source(source):
    services, _registry, _hitl, _path = source
    before = snapshot(services)
    task_id = await intake(source, body(project_id="project", selected_expert_id=None))
    target = services.project_task_repo.get(task_id, 1)
    assert target.project_id == "project" and target.selected_expert_id is None
    assert snapshot(services) == before
    assert not services.agent_repo.delete_if_unreferenced("expert")


async def test_unfinished_archive_turn_is_rejected(source):
    services, registry, _hitl, path = source
    archive = HistoryArchive(
        HistoryStore(path / "unfinished.sqlite", identity="test"),
        services.thread_message_repo,
        services.trajectory_event_repo,
        enabled=True,
    )
    archive.begin("expert", "thread")
    try:
        with pytest.raises(OctopError) as failure:
            await intake(source, archive=archive)
        assert failure.value.code == ErrorCode.TASK_BUSY
        assert not registry.reserved
    finally:
        archive.store.close()


async def test_new_task_continues_without_writing_legacy_thread(source, monkeypatch):
    services, _registry, _hitl, _path = source
    task_id = await intake(source, body(selected_expert_id=None))
    original = snapshot(services)
    monkeypatch.setattr(
        tasks,
        "_configuration",
        lambda *args, **kwargs: ('{"action_timeline_enabled":false}', "provider/model"),
    )
    tasks.send(
        services,
        1,
        tasks.TaskMessage(request_id="legacy-continue", prompt="Continue here"),
        task_id=task_id,
    )
    seen = []

    async def run(shared, owner, turn, history):
        seen.extend(history)
        yield {"type": "token", "content": "New task answer"}

    monkeypatch.setattr(runtime, "run_turn", run)
    claimed = services.project_task_turn_repo.claim("new-task-worker", tasks.now_ms())
    await runtime.ProjectTaskWorker(services)._execute(claimed)
    assert services.project_task_turn_repo.get(claimed.turn_id).status == "completed"
    assert [item["role"] for item in seen] == ["user", "assistant", "user", "assistant"]
    assert snapshot(services) == original
