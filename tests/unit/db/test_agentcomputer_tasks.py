"""Atomic SQL mapping fixtures; these tests do not enable a ready project or engine."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.ac_commands import AcCommandRepo
from octop.infra.db.repos.ac_projects import AcProjectRepo
from octop.infra.db.repos.ac_tasks import AcTaskRepo
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.sessions import SessionRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo


def _id() -> str:
    return str(uuid4())


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "tasks.db")
    try:
        run_migrations(pool)
        yield pool
    finally:
        pool.close()


def _scope(db: SqlitePool, *, owner_id: int | None = None) -> dict[str, Any]:
    owner_id = owner_id or UserRepo(db).create(username="fixture-" + uuid4().hex, role="user")
    project_id, agent_id = _id(), "ac-" + uuid4().hex
    with db.transaction() as conn:
        project = AcProjectRepo.insert(
            conn,
            project_id=project_id,
            owner_id=owner_id,
            box_id=_id(),
            workspace_id=_id(),
            name="内部映射 Fixture",
            config_json="{}",
            config_digest="sha256:fixture",
            created_at=1_700_000_000_123,
            updated_at=1_700_000_000_123,
        )
        assert project is not None
        conn.execute(
            "INSERT INTO agents(agent_id,user_id,name,enabled,last_state,managed_by,"
            "managed_project_id,created_at,updated_at) "
            "VALUES(?,?,?,0,'stopped','agentcomputer',?,1700000000,1700000000)",
            (agent_id, owner_id, agent_id, project_id),
        )
        assert AcProjectRepo.bind_agent(
            conn, project_id, owner_id, agent_id, updated_at=1_700_000_000_123
        )
        # Only this isolated database fixture marks a project ready.
        AcProjectRepo.set_preparation(
            conn, project_id, owner_id, "ready", updated_at=1_700_000_000_123
        )
    return {"project_id": project_id, "agent_id": agent_id, "owner_id": owner_id}


def _binding(scope: dict[str, Any]) -> dict[str, Any]:
    task_id = _id()
    return {
        "task_id": task_id,
        "session_id": _id(),
        "thread_id": "thr_" + uuid4().hex,
        "session_key": f"{scope['agent_id']}:agentcomputer:{task_id}:dm",
    }


def _insert_thread_session(conn: Any, scope: dict[str, Any], binding: dict[str, Any]) -> None:
    ThreadRepo.insert_in_transaction(
        conn,
        thread_id=binding["thread_id"],
        agent_id=scope["agent_id"],
        user_id=scope["owner_id"],
        channel_type="agentcomputer",
        session_key=binding["session_key"],
        title="固定 Task",
        last_active=0,
        created_at=1_700_000_000,
    )
    SessionRepo.insert_in_transaction(
        conn,
        session_key=binding["session_key"],
        agent_id=scope["agent_id"],
        user_id=scope["owner_id"],
        channel_type="agentcomputer",
        chat_type="dm",
        thread_id=binding["thread_id"],
        updated_at=1_700_000_000,
        channel_subject_id=binding["task_id"],
        channel_chat_type="dm",
        channel_metadata={
            "channel_type": "agentcomputer",
            "user_id": scope["owner_id"],
            "task_id": binding["task_id"],
        },
    )


def _task_fields(scope: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": binding["task_id"],
        "project_id": scope["project_id"],
        "owner_id": scope["owner_id"],
        "session_id": binding["session_id"],
        "octop_thread_id": binding["thread_id"],
        "title": "固定 Task",
        "created_at": 1_700_000_000_123,
        "updated_at": 1_700_000_000_123,
    }


def _register(db: SqlitePool, scope: dict[str, Any]) -> dict[str, Any]:
    binding = _binding(scope)
    with db.transaction() as conn:
        fields = {
            **_task_fields(scope, binding),
            "octop_thread_id": None,
            "preparation_state": "pending",
        }
        assert AcTaskRepo.insert(conn, **fields) is not None
        _insert_thread_session(conn, scope, binding)
        assert AcTaskRepo.bind_thread(
            conn,
            binding["task_id"],
            scope["owner_id"],
            binding["thread_id"],
            updated_at=1_700_000_000_123,
        )
    return binding


def _counts(db: SqlitePool) -> dict[str, int]:
    with db.connect() as conn:
        return {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in (
                "ac_tasks",
                "threads",
                "sessions",
                "thread_history_projection",
                "ac_commands",
                "ac_turns",
                "ac_runs",
                "ac_events",
            )
        }


def test_mapping_reads_own_uncommitted_rows_from_the_same_connection(db: SqlitePool) -> None:
    scope, binding = _scope(db), None
    binding = _binding(scope)
    with db.transaction() as conn:
        _insert_thread_session(conn, scope, binding)
        task = AcTaskRepo.insert(conn, **_task_fields(scope, binding))
        assert task is not None
        assert AcTaskRepo.get_in_transaction(conn, binding["task_id"], scope["owner_id"]) == task
        assert (
            AcTaskRepo.get_in_transaction(conn, binding["task_id"], scope["owner_id"] + 1) is None
        )
        thread = ThreadRepo.get_in_transaction(conn, binding["thread_id"])
        session = SessionRepo.get_in_transaction(conn, binding["session_key"])
        raw = SessionRepo.get_raw_in_transaction(conn, binding["session_key"])
        agent = AgentRepo.get_in_transaction(conn, scope["agent_id"])
        assert thread is not None and session is not None and raw is not None and agent is not None
        assert thread.agent_id == session.agent_id == agent.agent_id == scope["agent_id"]
        assert thread.user_id == session.user_id == scope["owner_id"]
        assert session.thread_id == thread.thread_id == task.octop_thread_id
        assert raw["channel_subject_id"] == binding["task_id"]
        assert thread.created_at == session.updated_at == 1_700_000_000
        assert task.created_at == task.updated_at == 1_700_000_000_123
        assert task.preparation_state == "ready" and task.revision == task.next_sequence == 1
        assert task.active_run_id is None and agent.managed_project_id == scope["project_id"]
        assert conn.execute("SELECT status FROM thread_history_projection").fetchone()[0] == "ready"
    assert AcTaskRepo(db).get(binding["task_id"], scope["owner_id"]) == task
    assert ThreadRepo(db).is_task_thread(binding["thread_id"])
    assert not ThreadRepo(db).is_task_thread("missing-thread")


def test_all_mapping_rows_and_receipt_roll_back_together(db: SqlitePool) -> None:
    scope = _scope(db)
    binding = _binding(scope)
    before = _counts(db)
    with pytest.raises(RuntimeError, match="injected"), db.transaction() as conn:
        assert (
            AcTaskRepo.insert(
                conn,
                **{
                    **_task_fields(scope, binding),
                    "octop_thread_id": None,
                    "preparation_state": "pending",
                },
            )
            is not None
        )
        _insert_thread_session(conn, scope, binding)
        assert AcTaskRepo.bind_thread(
            conn,
            binding["task_id"],
            scope["owner_id"],
            binding["thread_id"],
            updated_at=1_700_000_000_123,
        )
        assert AcCommandRepo.insert(
            conn,
            command_id=_id(),
            owner_id=scope["owner_id"],
            idempotency_key=_id(),
            operation="task.create",
            target_id=binding["task_id"],
            payload_digest="sha256:fixture",
            restore_epoch=1,
            receipt_json="{}",
            created_at=1_700_000_000_123,
        )
        raise RuntimeError("injected")
    assert _counts(db) == before


@pytest.mark.parametrize("column", ["task_id", "session_id", "octop_thread_id"])
def test_task_unique_conflicts_do_not_overwrite_other_owner_or_abort_transaction(
    db: SqlitePool, column: str
) -> None:
    original_scope = _scope(db)
    original = _register(db, original_scope)
    original_task = AcTaskRepo(db).get(original["task_id"], original_scope["owner_id"])
    other_scope = _scope(db)
    other = _binding(other_scope)
    fields = _task_fields(other_scope, other)
    original_values = _task_fields(original_scope, original)
    with db.transaction() as conn:
        _insert_thread_session(conn, other_scope, other)
        assert AcTaskRepo.insert(conn, **{**fields, column: original_values[column]}) is None
        assert AcTaskRepo.insert(conn, **fields) is not None
    assert AcTaskRepo(db).get(original["task_id"], original_scope["owner_id"]) == original_task
    assert AcTaskRepo(db).get(original["task_id"], other_scope["owner_id"]) is None


@pytest.mark.parametrize("column", ["project_id", "owner_id", "octop_thread_id"])
def test_task_foreign_keys_reject_missing_thread_and_cross_owner_project(
    db: SqlitePool, column: str
) -> None:
    scope = _scope(db)
    binding = _binding(scope)
    with db.transaction() as conn:
        _insert_thread_session(conn, scope, binding)
    changes = {
        "project_id": _id(),
        "owner_id": UserRepo(db).create(username="other", role="user"),
        "octop_thread_id": "missing-thread",
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.transaction() as conn:
        AcTaskRepo.insert(conn, **{**_task_fields(scope, binding), column: changes[column]})
    assert AcTaskRepo(db).get(binding["task_id"], scope["owner_id"]) is None


def test_legacy_pending_tasks_with_null_thread_remain_readable(db: SqlitePool) -> None:
    scope = _scope(db)
    for _ in range(2):
        binding = _binding(scope)
        fields = {
            **_task_fields(scope, binding),
            "octop_thread_id": None,
            "preparation_state": "pending",
        }
        with db.transaction() as conn:
            row = AcTaskRepo.insert(conn, **fields)
        assert (
            row is not None and row.octop_thread_id is None and row.preparation_state == "pending"
        )
        assert AcTaskRepo(db).get(binding["task_id"], scope["owner_id"]) == row


def test_pending_reservation_binds_once_without_changing_revision_or_sequence(
    db: SqlitePool,
) -> None:
    scope = _scope(db)
    binding = _binding(scope)
    fields = {
        **_task_fields(scope, binding),
        "octop_thread_id": None,
        "preparation_state": "pending",
    }
    with db.transaction() as conn:
        reserved = AcTaskRepo.insert(conn, **fields)
        assert reserved is not None and reserved.octop_thread_id is None
        _insert_thread_session(conn, scope, binding)
        assert AcTaskRepo.bind_thread(
            conn,
            binding["task_id"],
            scope["owner_id"],
            binding["thread_id"],
            updated_at=1_700_000_000_456,
        )
        assert not AcTaskRepo.bind_thread(
            conn,
            binding["task_id"],
            scope["owner_id"],
            binding["thread_id"],
            updated_at=1_700_000_000_789,
        )
        final = AcTaskRepo.get_in_transaction(conn, binding["task_id"], scope["owner_id"])
        assert final is not None and final.preparation_state == "ready"
        assert (
            final.octop_thread_id == binding["thread_id"] and final.updated_at == 1_700_000_000_456
        )
        assert final.revision == final.next_sequence == 1 and final.active_run_id is None
    assert AcTaskRepo(db).get(binding["task_id"], scope["owner_id"]) == final


@pytest.mark.parametrize(
    "state,bound", [("ready", False), ("preparing", False), ("failed", False), ("pending", True)]
)
def test_bind_thread_does_not_override_existing_binding_or_non_pending_state(
    db: SqlitePool, state: str, bound: bool
) -> None:
    scope = _scope(db)
    original, other = _binding(scope), _binding(scope)
    fields = {
        **_task_fields(scope, original),
        "octop_thread_id": original["thread_id"] if bound else None,
        "preparation_state": state,
    }
    with db.transaction() as conn:
        _insert_thread_session(conn, scope, original)
        _insert_thread_session(conn, scope, other)
        row = AcTaskRepo.insert(conn, **fields)
        assert row is not None
        assert not AcTaskRepo.bind_thread(
            conn,
            original["task_id"],
            scope["owner_id"],
            other["thread_id"],
            updated_at=1_700_000_000_456,
        )
        assert AcTaskRepo.get_in_transaction(conn, original["task_id"], scope["owner_id"]) == row


def test_bind_thread_wrong_owner_and_missing_task_do_not_change_reservation(db: SqlitePool) -> None:
    scope = _scope(db)
    binding = _binding(scope)
    with db.transaction() as conn:
        _insert_thread_session(conn, scope, binding)
        row = AcTaskRepo.insert(
            conn,
            **{
                **_task_fields(scope, binding),
                "octop_thread_id": None,
                "preparation_state": "pending",
            },
        )
        assert row is not None
        assert not AcTaskRepo.bind_thread(
            conn,
            binding["task_id"],
            scope["owner_id"] + 1,
            binding["thread_id"],
            updated_at=1_700_000_000_456,
        )
        assert not AcTaskRepo.bind_thread(
            conn, _id(), scope["owner_id"], binding["thread_id"], updated_at=1_700_000_000_456
        )
        assert AcTaskRepo.get_in_transaction(conn, binding["task_id"], scope["owner_id"]) == row


def test_one_project_allows_multiple_fixed_tasks_without_run_or_lane_claim(db: SqlitePool) -> None:
    scope = _scope(db)
    bindings = [_register(db, scope) for _ in range(3)]
    assert len({binding["session_id"] for binding in bindings}) == 3
    assert len({binding["thread_id"] for binding in bindings}) == 3
    counts = _counts(db)
    assert counts["ac_tasks"] == counts["threads"] == counts["sessions"] == 3
    assert counts["ac_turns"] == counts["ac_runs"] == counts["ac_events"] == 0
    project = AcProjectRepo(db).get(scope["project_id"], scope["owner_id"])
    assert project is not None and project.active_run_id is None and project.revision == 1


def test_strict_session_collision_rolls_back_new_thread_projection_and_does_not_rebind(
    db: SqlitePool,
) -> None:
    scope = _scope(db)
    original = _register(db, scope)
    before = _counts(db)
    row = SessionRepo(db).get(original["session_key"])
    other = {**_binding(scope), "session_key": original["session_key"]}
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), db.transaction() as conn:
        _insert_thread_session(conn, scope, other)
    assert _counts(db) == before
    assert SessionRepo(db).get(original["session_key"]) == row
    assert ThreadRepo(db).get(other["thread_id"]) is None


def test_plain_thread_insert_keeps_duplicate_exception_and_title_clipping(db: SqlitePool) -> None:
    scope = _scope(db)
    repo = ThreadRepo(db)
    thread_id = "thr_" + uuid4().hex
    values = {
        "thread_id": thread_id,
        "agent_id": scope["agent_id"],
        "user_id": scope["owner_id"],
        "channel_type": "dashboard",
        "session_key": "legacy-key",
        "title": "x" * 50,
        "last_active": 0,
    }
    repo.insert(**values)
    original = repo.get(thread_id)
    assert original is not None and original.title == "x" * 39 + "…"
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        repo.insert(**{**values, "title": "replacement"})
    assert repo.get(thread_id) == original


@pytest.mark.parametrize("metadata", [None, "{broken", "[]", "{}"])
def test_raw_session_reads_do_not_hide_null_subject_or_malformed_metadata(
    db: SqlitePool, metadata: str | None
) -> None:
    scope = _scope(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(session_key,agent_id,user_id,channel_type,thread_id,updated_at,channel_metadata) "
            "VALUES('legacy:dashboard:subject:dm',?,?,'dashboard','legacy-thread',1,?)",
            (scope["agent_id"], scope["owner_id"], metadata),
        )
        raw = SessionRepo.get_raw_in_transaction(conn, "legacy:dashboard:subject:dm")
        compatible = SessionRepo.get_in_transaction(conn, "legacy:dashboard:subject:dm")
        assert (
            raw is not None
            and raw["channel_subject_id"] is None
            and raw["channel_metadata"] == metadata
        )
        assert compatible is not None and compatible.channel_subject_id == "subject"
        assert SessionRepo.get_raw_in_transaction(conn, "missing") is None


@pytest.mark.parametrize("signal", ["channel", "namespace", "short_namespace", "task_link"])
def test_bulk_session_guard_finds_scoped_task_bindings_when_metadata_is_changed(
    db: SqlitePool, signal: str
) -> None:
    scope = _scope(db)
    binding = _register(db, scope)
    if signal != "task_link":
        with db.connect() as conn:
            conn.execute("DELETE FROM ac_tasks WHERE task_id = ?", (binding["task_id"],))
    key = "legacy:dashboard:subject:dm"
    channel = "dashboard"
    if signal == "channel":
        channel = "agentcomputer"
    elif signal == "namespace":
        key = "changed:agentcomputer:subject:dm"
    elif signal == "short_namespace":
        key = "changed:agentcomputer"
    with db.connect() as conn:
        conn.execute(
            "UPDATE sessions SET session_key = ?,channel_type = ?,channel_metadata = NULL",
            (key, channel),
        )
    repo = SessionRepo(db)
    assert repo.has_task_bindings_for_agent_user(scope["agent_id"], scope["owner_id"])
    assert not repo.has_task_bindings_for_agent_user(scope["agent_id"], scope["owner_id"] + 1)
    assert not repo.has_task_bindings_for_agent_user("other-agent", scope["owner_id"])


def test_bulk_session_guard_preserves_ordinary_namespace_and_empty_metadata_json(
    db: SqlitePool,
) -> None:
    scope = _scope(db)
    repo = SessionRepo(db)
    with db.transaction() as conn:
        SessionRepo.insert_in_transaction(
            conn,
            session_key="legacy:dashboard:agentcomputer:dm",
            agent_id=scope["agent_id"],
            user_id=scope["owner_id"],
            channel_type="dashboard",
            chat_type="dm",
            thread_id="legacy",
            updated_at=1,
            channel_subject_id="agentcomputer",
            channel_chat_type="dm",
            channel_metadata={},
        )
        raw = SessionRepo.get_raw_in_transaction(conn, "legacy:dashboard:agentcomputer:dm")
        assert raw is not None and raw["channel_metadata"] == "{}"
    assert not repo.has_task_bindings_for_agent_user(scope["agent_id"], scope["owner_id"])
