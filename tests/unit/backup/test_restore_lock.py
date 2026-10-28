"""System restore requires exclusive ownership of both conversation writers."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import pytest

from octop.config import DatabaseConfig
from octop.infra.agentcomputer.bootstrap import DaemonBootstrap
from octop.infra.agents.resident_worker_lock import ResidentWorkerLock
from octop.infra.backup import system_archive
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.paths import PathLayout


def _database(stack: ExitStack, root: Path, username: str) -> tuple[PathLayout, SqlitePool]:
    paths = PathLayout(root)
    pool = SqlitePool(paths.db)
    stack.callback(pool.close)
    run_migrations(pool)
    with pool.connect() as conn:
        conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at) VALUES(?, 'hash','admin',1)",
            (username,),
        )
    return paths, pool


@pytest.mark.parametrize("writer", ["daemon", "resident"])
def test_restore_refuses_live_writer_without_changing_database_or_task_files(
    tmp_path: Path, writer: str
) -> None:
    with ExitStack() as stack:
        source, pool = _database(stack, tmp_path / "source", "backup-owner")
        archive = tmp_path / "backup.tar.gz"
        system_archive.create_system_backup(
            paths=source, agent_rows=[], pool=pool, db_config=DatabaseConfig(), dest=archive
        )
        target, live_pool = _database(stack, tmp_path / "target", "live-owner")
        live_file = target.root / "project-tasks" / "live-task" / "memory.sqlite"
        live_file.parent.mkdir(parents=True)
        live_file.write_bytes(b"unchanged live task state")
        if writer == "daemon":
            controller = DaemonBootstrap(target.agentcomputer_identity.parent)
            controller.acquire_sync()
            release = controller.close_sync
        else:
            resident = ResidentWorkerLock(target.root / "resident-worker.lock")
            resident.acquire()
            release = resident.release
        try:
            with pytest.raises(OctopError) as rejected:
                system_archive.restore_system_backup(
                    archive, paths=target, pool=live_pool, db_config=DatabaseConfig()
                )
            assert rejected.value.code == ErrorCode.BACKUP_OFFLINE_REQUIRED
            assert rejected.value.status == 409
            with live_pool.connect() as conn:
                assert conn.execute("SELECT username FROM users").fetchone()[0] == "live-owner"
            assert live_file.read_bytes() == b"unchanged live task state"
            if writer == "resident":
                # Failing to obtain the second lock must release the first.
                with DaemonBootstrap(target.agentcomputer_identity.parent):
                    pass
        finally:
            release()


def _assert_writers_blocked(paths: PathLayout) -> None:
    with (
        pytest.raises(OctopError) as rejected,
        DaemonBootstrap(paths.agentcomputer_identity.parent),
    ):
        pass
    assert rejected.value.code == ErrorCode.CONTROL_PLANE_BUSY
    contender = ResidentWorkerLock(paths.root / "resident-worker.lock")
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            contender.acquire()
    finally:
        contender.release()


def _assert_writers_released(paths: PathLayout) -> None:
    with DaemonBootstrap(paths.agentcomputer_identity.parent):
        resident = ResidentWorkerLock(paths.root / "resident-worker.lock")
        resident.acquire()
        resident.release()


def test_restore_holds_writer_locks_through_task_files_then_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ExitStack() as stack:
        source, pool = _database(stack, tmp_path / "source", "backup-owner")
        archive = tmp_path / "backup.tar.gz"
        system_archive.create_system_backup(
            paths=source, agent_rows=[], pool=pool, db_config=DatabaseConfig(), dest=archive
        )
        target, live_pool = _database(stack, tmp_path / "target", "live-owner")
        restore_files = system_archive.restore_task_files
        called = False

        def checked_restore_files(
            extracted: Path, home: Path, *, include_workspaces: bool
        ) -> tuple[int, int, int]:
            nonlocal called
            _assert_writers_blocked(target)
            called = True
            return restore_files(extracted, home, include_workspaces=include_workspaces)

        monkeypatch.setattr(system_archive, "restore_task_files", checked_restore_files)
        system_archive.restore_system_backup(
            archive, paths=target, pool=live_pool, db_config=DatabaseConfig()
        )
        assert called
        _assert_writers_released(target)


def test_restore_releases_writer_locks_after_invalid_archive(tmp_path: Path) -> None:
    with ExitStack() as stack:
        target, pool = _database(stack, tmp_path / "target", "live-owner")
        with pytest.raises(OctopError) as rejected:
            system_archive.restore_system_backup(
                tmp_path / "missing.tar.gz", paths=target, pool=pool, db_config=DatabaseConfig()
            )
        assert rejected.value.code == ErrorCode.NOT_FOUND
        _assert_writers_released(target)
