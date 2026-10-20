"""Durable command identities, cancellation targets and lease fencing."""

from __future__ import annotations

import asyncio
import re
import shutil

import pytest

from octop.config import OctopConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.agents.resident_identity import ensure_resident_identity
from octop.infra.db import migrate
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.resident_requests import ResidentRequestBusy, ResidentRequestConflict
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    migrate.run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    services.user_repo.create(username="owner", password_hash="x", role="user")
    services.user_repo.create(username="other", password_hash="x", role="user")
    manager = AgentManager(repos=services.repos, paths=paths)
    asyncio.run(ensure_resident_identity(services, manager, 1))
    asyncio.run(ensure_resident_identity(services, manager, 2))
    yield services
    db.close()


def test_retries_keep_one_turn_and_do_not_repeat_or_change_input(services):
    binding = services.resident_conversation_repo.get(1)
    repo = services.resident_request_repo
    before, _ = repo.snapshot(1, binding.thread_id)
    first, created = repo.create(
        1, binding.thread_id, "request", "turn", "Hello", 1000, '{"model_ref":"first"}'
    )
    assert created and first.configuration_json == '{"model_ref":"first"}'
    revision, rows = repo.snapshot(1, binding.thread_id)
    assert revision > before and rows == [first]
    duplicate, created = repo.create(1, binding.thread_id, "request", "new-turn", "Hello", 2000)
    assert not created and duplicate == first
    assert repo.snapshot(1, binding.thread_id)[0] == revision
    with pytest.raises(ResidentRequestConflict):
        repo.create(1, binding.thread_id, "request", "turn-2", "Changed", 2000)
    with pytest.raises(ResidentRequestBusy):
        repo.create(1, binding.thread_id, "other-request", "turn-2", "More", 2000)
    assert repo.get(2, "request") is None
    assert repo.get_by_turn("turn", 2) is None
    with pytest.raises(ResidentRequestConflict):
        repo.create(2, binding.thread_id, "cross-owner", "turn-2", "Hello", 2000)


def test_progress_is_fenced_and_thread_revision_survives_turn_changes(services):
    thread_id = services.resident_conversation_repo.get(1).thread_id
    repo = services.resident_request_repo
    repo.create(1, thread_id, "first", "turn-1", "Hello", 1000)
    assert repo.mark_running("turn-1", "worker", 1100, 3000)
    assert not repo.mark_running("turn-1", "other-worker", 1101, 4000)
    assert not repo.update_reply("turn-1", "other-worker", "bad", 1200)
    revision = repo.snapshot(1, thread_id)[0]
    assert repo.update_reply("turn-1", "worker", "Partial", 1200)
    assert repo.snapshot(1, thread_id)[0] > revision
    assert repo.renew("turn-1", "worker", 2000, 5000)
    assert repo.finish("turn-1", "worker", "completed", "Full", "", 3100)
    previous = repo.snapshot(1, thread_id)[0]
    second, _ = repo.create(1, thread_id, "second", "turn-2", "Continue", 3200)
    revision, rows = repo.snapshot(1, thread_id, limit=1)
    assert revision > previous
    assert rows == [second]
    assert [row.turn_id for row in repo.list(thread_id, 1, before=second.pk)] == ["turn-1"]
    assert not repo.finish("turn-1", "worker", "failed", "stale", "bad", 3300)
    assert repo.get(1, "first").reply == "Full"


def test_retry_cannot_add_or_replace_host_authorization(services):
    thread_id = services.resident_conversation_repo.get(1).thread_id
    repo = services.resident_request_repo
    first, _ = repo.create(1, thread_id, "authorization", "turn", "Hello", 1000)
    with pytest.raises(ResidentRequestConflict):
        repo.create(
            1,
            thread_id,
            "authorization",
            "new-turn",
            "Hello",
            2000,
            input_json='{"read_tools":true}',
        )
    assert repo.get(1, "authorization") == first


def test_cancel_receipt_is_idempotent_and_never_cancels_a_later_turn(services):
    thread_id = services.resident_conversation_repo.get(1).thread_id
    repo = services.resident_request_repo
    repo.create(1, thread_id, "first", "turn-1", "Hello", 1000)
    cancelled = repo.cancel(1, thread_id, "stop-1", "turn-1", 1100)
    assert cancelled.status == "cancelled" and cancelled.finished_at == 1100
    assert not repo.mark_running("turn-1", "worker", 1200, 4000)
    repo.create(1, thread_id, "second", "turn-2", "Next", 1300)
    revision = repo.snapshot(1, thread_id)[0]
    assert repo.cancel(1, thread_id, "stop-1", "turn-1", 1400) == cancelled
    assert repo.snapshot(1, thread_id)[0] == revision
    assert repo.get(1, "second").status == "queued"
    with pytest.raises(ResidentRequestConflict):
        repo.cancel(1, thread_id, "stop-1", "turn-2", 1400)
    with pytest.raises(ResidentRequestConflict):
        repo.cancel(1, thread_id, "stop-1", "nonexistent-turn", 1400)
    assert repo.cancel(2, thread_id, "wrong-user", "turn-2", 1400) is None
    assert repo.mark_running("turn-2", "worker", 1500, 4000)
    repo.cancel(1, thread_id, "stop-2", "turn-2", 1600)
    assert repo.finish("turn-2", "worker", "failed", "Final race", "INTERNAL_ERROR", 1700)
    assert repo.get(1, "second").status == "cancelled"
    assert repo.get(1, "second").error_code == ""


def test_recovery_interrupts_only_expired_running_work_without_reexecution(services):
    repo = services.resident_request_repo
    first = services.resident_conversation_repo.get(1).thread_id
    second = services.resident_conversation_repo.get(2).thread_id
    repo.create(1, first, "first", "turn-1", "Hello", 1000)
    repo.create(2, second, "second", "turn-2", "Hello", 1000)
    assert repo.mark_running("turn-1", "dead", 1100, 2000)
    assert repo.mark_running("turn-2", "live", 1100, 4000)
    assert repo.interrupt_active(2500) == 1
    assert repo.get(1, "first").status == "interrupted"
    assert repo.get(1, "first").error_code == "RESIDENT_INTERRUPTED"
    assert repo.get(2, "second").status == "running"
    assert not repo.update_reply("turn-1", "dead", "late", 2600)
    assert not repo.renew("turn-1", "dead", 2600, 5000)
    assert not repo.mark_running("turn-1", "new", 2600, 5000)
    queued, _ = repo.create(1, first, "third", "turn-3", "New input", 2700)
    assert repo.pending(2800) == [queued]


def test_migration_from_50_preserves_existing_records(tmp_path, monkeypatch):
    old = tmp_path / "old-migrations"
    old.mkdir()
    for version, source in migrate._discover("sqlite"):
        if version <= 50:
            shutil.copy2(source, old / source.name)
    pool = SqlitePool(tmp_path / "upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrate, "_MIGRATIONS_DIR", old)
            migrate.run_migrations(pool)
        with pool.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES('old','x','user',1)"
            )
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,enabled,created_at,updated_at) VALUES('old-agent',1,'Old',1,1,1)"
            )
            before = dict(conn.execute("SELECT * FROM agents").fetchone())
        migrate.run_migrations(pool)
        migrate.run_migrations(pool)
        with pool.connect() as conn:
            assert dict(conn.execute("SELECT * FROM agents").fetchone()) == before
            assert conn.execute("SELECT COUNT(*) FROM resident_conversations").fetchone()[0] == 0
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 53
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        pool.close()


def test_postgres_and_sqlite_resident_schema_have_matching_constraints():
    sqlite = (migrate._MIGRATIONS_DIR / "051_resident_conversations.sql").read_text()
    postgres = (migrate._MIGRATIONS_DIR / "051_resident_conversations.pg.sql").read_text()
    postgres = postgres.replace(
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    postgres = postgres.replace("owner_id BIGINT", "owner_id INTEGER")
    assert re.sub(r"\s+", " ", postgres).strip() == re.sub(r"\s+", " ", sqlite).strip()
