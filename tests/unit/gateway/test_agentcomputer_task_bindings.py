"""Task bindings are atomic and immutable through legacy conversation entry points."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from octop_gateway.models import ChannelSubject, InboundMessage, TextContent

from octop.api.routers.chat.turn import resolve_thread_id
from octop.cli.repl.runtime import CliChatSession
from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.cli.turn import prepare_cli_turn
from octop.infra.gateway.process.processor import GlobalProcessor
from octop.infra.gateway.slash import BufferSink, SlashCommand, build_default_dispatcher
from octop.infra.gateway.slash.ctx import SlashCtx
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    services = build_shared_services(
        db=db, paths=PathLayout(tmp_path.resolve()), config=OctopConfig()
    )
    user_id = services.user_repo.create(username="owner", role="user")
    services.agent_repo.create(agent_id="ordinary", user_id=user_id, name="ordinary")
    project_id = str(uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO ac_projects(project_id,owner_id,box_id,workspace_id,config_json,"
            "config_digest,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, user_id, str(uuid4()), str(uuid4()), "{}", "sha256:fixture", 1, 1),
        )
    services.agent_repo.create(
        agent_id="managed",
        user_id=user_id,
        name="managed",
        enabled=False,
        last_state="stopped",
        managed_by="agentcomputer",
        managed_project_id=project_id,
    )
    registry = ThreadRegistry(session_repo=services.session_repo, thread_repo=services.thread_repo)
    yield SimpleNamespace(
        services=services, db=db, registry=registry, user_id=user_id, project_id=project_id
    )
    db.close()


def register_task(env):
    task_id = str(uuid4())
    thread_id = "thr_fixture_" + uuid4().hex
    with env.db.transaction() as conn:
        key = env.registry.register_task_binding_in_transaction(
            conn,
            agent_id="managed",
            user_id=env.user_id,
            task_id=task_id,
            thread_id=thread_id,
            title="Fixture Task",
            created_at=123,
        )
        conn.execute(
            "INSERT INTO ac_tasks(task_id,project_id,owner_id,session_id,octop_thread_id,title,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                task_id,
                env.project_id,
                env.user_id,
                str(uuid4()),
                thread_id,
                "Fixture Task",
                123000,
                123000,
            ),
        )
    return task_id, thread_id, key


def snapshot(env):
    with env.db.connect() as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("threads", "sessions", "ac_tasks")
        }


def test_task_registration_uses_exact_internal_binding_and_no_legacy_methods(env, monkeypatch):
    for method in ("get_or_create", "get_or_create_by_key", "reset", "rebind"):
        monkeypatch.setattr(env.registry, method, AsyncMock(side_effect=AssertionError("legacy")))
    monkeypatch.setattr(
        env.services.session_repo, "upsert", MagicMock(side_effect=AssertionError("upsert"))
    )
    task_id, thread_id, key = register_task(env)
    assert key == f"managed:agentcomputer:{task_id}:dm"
    thread = env.services.thread_repo.get(thread_id)
    session = env.services.session_repo.get(key)
    assert thread.agent_id == session.agent_id == "managed"
    assert thread.user_id == session.user_id == env.user_id
    assert thread.channel_type == session.channel_type == "agentcomputer"
    assert thread.session_key == key
    assert session.thread_id == thread_id
    assert (thread.last_active, thread.created_at, session.updated_at) == (0, 123, 123)
    assert session.channel_subject_id == task_id
    assert session.chat_type == session.channel_chat_type == "dm"
    assert session.channel_id is None
    assert session.channel_metadata == {
        "channel_type": "agentcomputer",
        "user_id": env.user_id,
        "task_id": task_id,
    }
    with env.db.connect() as conn:
        projection = conn.execute(
            "SELECT status FROM thread_history_projection WHERE thread_id=?", (thread_id,)
        ).fetchone()
        assert projection[0] == "ready"


def test_session_collision_rolls_back_thread_and_projection(env):
    task_id, thread_id, key = register_task(env)
    before = snapshot(env)
    other_thread = "thr_fixture_collision"
    with pytest.raises(sqlite3.IntegrityError), env.db.transaction() as conn:
        env.registry.register_task_binding_in_transaction(
            conn,
            agent_id="managed",
            user_id=env.user_id,
            task_id=task_id,
            thread_id=other_thread,
            title="collision",
            created_at=124,
        )
    assert snapshot(env) == before
    assert env.services.session_repo.get(key).thread_id == thread_id
    with env.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM thread_history_projection WHERE thread_id=?", (other_thread,)
            ).fetchone()[0]
            == 0
        )


async def test_legacy_sources_and_targets_cannot_rebind_or_mutate_task(env):
    task_id, thread_id, key = register_task(env)
    ordinary_key = ThreadRegistry.dashboard_key(agent_id="ordinary", user_id=env.user_id)
    ordinary_thread = await env.registry.get_or_create_by_key(
        session_key=ordinary_key, agent_id="ordinary", user_id=env.user_id, channel_type="dashboard"
    )
    before = snapshot(env)
    for operation in (
        lambda: env.registry.rebind(
            session_key=key, thread_id=ordinary_thread, agent_id="ordinary"
        ),
        lambda: env.registry.rebind(
            session_key=ordinary_key, thread_id=thread_id, agent_id="managed"
        ),
        lambda: env.registry.reset_by_session_key(key),
        lambda: env.registry.reset(
            agent_id="managed",
            user_id=env.user_id,
            channel_type="agentcomputer",
            channel_subject_id=task_id,
        ),
        lambda: env.registry.get_or_create(
            agent_id="managed",
            user_id=env.user_id,
            channel_type="agentcomputer",
            channel_subject_id=task_id,
        ),
        lambda: env.registry.get_or_create_by_key(
            session_key=key, agent_id="ordinary", user_id=env.user_id, channel_type="dashboard"
        ),
        lambda: prepare_cli_turn(
            env.registry,
            agent_id="ordinary",
            user_id=env.user_id,
            thread_id=ordinary_thread,
            session_key=key,
        ),
        lambda: resolve_thread_id(
            agent_id="ordinary",
            user_id=env.user_id,
            thread_registry=env.registry,
            thread_id=ordinary_thread,
            session_key=key,
        ),
        lambda: resolve_thread_id(
            agent_id="ordinary",
            user_id=env.user_id,
            thread_registry=env.registry,
            thread_id=None,
            session_key=key,
        ),
    ):
        with pytest.raises(OctopError) as error:
            await operation()
        assert error.value.code == ErrorCode.FORBIDDEN
        assert snapshot(env) == before
    for operation in (
        lambda: env.registry.get_bound_thread_id(key),
        lambda: env.registry.update_title(thread_id, "changed"),
        lambda: env.registry.set_title_if_null(thread_id, "changed"),
        lambda: env.registry.set_pinned(thread_id, True),
        lambda: env.registry.update_composer(thread_id, model_ref="changed"),
        lambda: env.registry.append_artifacts(thread_id, ["changed"]),
        lambda: env.registry.touch_last_active(thread_id),
        lambda: env.registry.delete_thread(thread_id),
        lambda: env.registry.mark_thread_read(thread_id),
        lambda: env.registry.mark_agent_read("managed", env.user_id),
        lambda: env.registry.increment_unread(key),
        lambda: env.registry.ensure_thread(
            thread_id=thread_id,
            agent_id="ordinary",
            user_id=env.user_id,
            channel_type="dashboard",
            session_key=ordinary_key,
        ),
        lambda: env.registry.create_thread(
            thread_id="fresh",
            agent_id="ordinary",
            user_id=env.user_id,
            channel_type="dashboard",
            session_key=key,
        ),
    ):
        with pytest.raises(OctopError) as error:
            operation()
        assert error.value.code == ErrorCode.FORBIDDEN
        assert snapshot(env) == before


async def test_task_fk_blocks_legacy_mutation_even_with_disguised_channel_and_key(env):
    _task_id, thread_id, key = register_task(env)
    alias = "managed:dashboard:fixture:dm"
    with env.db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET session_key=?,channel_type='dashboard' WHERE session_key=?",
            (alias, key),
        )
        conn.execute(
            "UPDATE threads SET session_key=?,channel_type='dashboard' WHERE thread_id=?",
            (alias, thread_id),
        )
    before = snapshot(env)
    for operation in (
        lambda: env.registry.reset_by_session_key(alias),
        lambda: env.registry.get_or_create_by_key(
            session_key=alias, agent_id="managed", user_id=env.user_id, channel_type="dashboard"
        ),
    ):
        with pytest.raises(OctopError):
            await operation()
        assert snapshot(env) == before
    with pytest.raises(OctopError):
        env.registry.update_composer(thread_id, hitl_policy="changed")
    with pytest.raises(OctopError):
        env.registry.mark_agent_read("managed", env.user_id)
    assert snapshot(env) == before


@pytest.mark.parametrize("channel_type", ["cli", "telegram"])
@pytest.mark.parametrize("text", ["/new", "/title changed", "/craft", "message"])
async def test_processor_rejects_managed_agent_before_thread_or_slash_changes(
    env, channel_type, text
):
    dispatcher = build_default_dispatcher()
    processor = GlobalProcessor(
        agent_manager=MagicMock(),
        thread_registry=env.registry,
        audit_repo=env.services.audit_repo,
        agent_repo=env.services.agent_repo,
        user_repo=env.services.user_repo,
        connector_repo=env.services.connector_repo,
        dispatcher=dispatcher,
    )
    msg = InboundMessage(
        channel_id="fixture",
        channel_type=channel_type,
        tenant_id="managed",
        channel_subject=ChannelSubject(subject_id=str(env.user_id)),
        content=[TextContent(text=text)],
    )
    before = snapshot(env)
    for iterator in (processor(msg), processor.iter_turn_chunks(msg)):
        with pytest.raises(OctopError) as error:
            _ = [frame async for frame in iterator]
        assert error.value.code == ErrorCode.FORBIDDEN
        assert snapshot(env) == before


async def test_cli_runtime_rejects_managed_agent_before_preparation(env, monkeypatch):
    gateway = SimpleNamespace(
        cli_hub=MagicMock(), channel_manager=MagicMock(), thread_registry=env.registry
    )
    server = SimpleNamespace(app_runtime=SimpleNamespace(gateway=gateway), services=env.services)
    prepare = AsyncMock(side_effect=AssertionError("must not prepare"))
    monkeypatch.setattr("octop.cli.repl.runtime.prepare_cli_turn", prepare)
    session = CliChatSession(server, user_id=env.user_id)
    before = snapshot(env)
    try:
        with pytest.raises(OctopError):
            await session.run_turn(
                "managed",
                "/new",
                session_key="managed:cli:fixture:dm",
                thread_id=None,
                model=None,
                on_chunk=lambda _frame: None,
            )
    finally:
        session.close()
    prepare.assert_not_awaited()
    gateway.channel_manager.enqueue.assert_not_called()
    assert snapshot(env) == before


@pytest.mark.parametrize("target", ["session", "thread"])
@pytest.mark.parametrize("text", ["/new", "/switch fixture"])
async def test_ordinary_processor_cannot_target_task_through_metadata(env, target, text):
    _task_id, thread_id, key = register_task(env)
    processor = GlobalProcessor(
        agent_manager=MagicMock(),
        thread_registry=env.registry,
        audit_repo=env.services.audit_repo,
        agent_repo=env.services.agent_repo,
        user_repo=env.services.user_repo,
        connector_repo=env.services.connector_repo,
        dispatcher=build_default_dispatcher(),
    )
    metadata = {"session_key": key} if target == "session" else {"thread_id": thread_id}
    msg = InboundMessage(
        channel_id="fixture",
        channel_type="cli",
        tenant_id="ordinary",
        channel_subject=ChannelSubject(subject_id=str(env.user_id)),
        content=[TextContent(text=text)],
        metadata=metadata,
    )
    before = snapshot(env)
    for iterator in (processor(msg), processor.iter_turn_chunks(msg)):
        with pytest.raises(OctopError) as error:
            _ = [frame async for frame in iterator]
        assert error.value.code == ErrorCode.FORBIDDEN
        assert snapshot(env) == before


async def test_ordinary_cli_runtime_preserves_preparation_and_enqueue(env):
    gateway = SimpleNamespace(
        cli_hub=MagicMock(), channel_manager=MagicMock(), thread_registry=env.registry
    )
    server = SimpleNamespace(app_runtime=SimpleNamespace(gateway=gateway), services=env.services)
    session = CliChatSession(server, user_id=env.user_id)
    gateway.channel_manager.enqueue.side_effect = lambda *_args: session._done.set()
    try:
        await session.run_turn(
            "ordinary",
            "fixture",
            session_key=ThreadRegistry.cli_key(agent_id="ordinary", user_id=env.user_id),
            thread_id=None,
            model=None,
            on_chunk=lambda _frame: None,
        )
    finally:
        session.close()
    gateway.channel_manager.enqueue.assert_called_once()
    inbound = gateway.channel_manager.enqueue.call_args.args[1]
    assert inbound.tenant_id == "ordinary"
    assert env.services.thread_repo.get(inbound.metadata["thread_id"]).agent_id == "ordinary"


async def test_ordinary_slash_new_and_switch_still_work(env):
    key = ThreadRegistry.cli_key(agent_id="ordinary", user_id=env.user_id)
    ctx = SlashCtx(
        agent_id="ordinary",
        user_id=env.user_id,
        channel_type="cli",
        session_key=key,
        thread_registry=env.registry,
        agent_repo=env.services.agent_repo,
    )
    dispatcher = build_default_dispatcher()
    sink = BufferSink()
    assert await dispatcher.handle(SlashCommand(name="new", args="first"), ctx, sink)
    first = env.registry.get_bound_thread_id(key)
    assert await dispatcher.handle(SlashCommand(name="new", args="second"), ctx, sink)
    second = env.registry.get_bound_thread_id(key)
    assert first != second
    assert await dispatcher.handle(SlashCommand(name="switch", args=first[-6:]), ctx, sink)
    assert env.registry.get_bound_thread_id(key) == first
    assert env.services.thread_repo.get(first).title == "first"
