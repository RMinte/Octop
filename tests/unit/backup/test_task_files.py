"""System backups retain the independent Task filesystem and SQLite state."""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from octop.config import DatabaseConfig
from octop.infra.backup.manifest import BackupManifest
from octop.infra.backup.system_archive import create_system_backup, restore_system_backup
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.projects.resource_snapshot import load_snapshot, save_snapshot
from octop.infra.utils.paths import PathLayout


def _pool(stack: ExitStack, home: Path) -> tuple[PathLayout, SqlitePool]:
    layout = PathLayout(home)
    pool = SqlitePool(layout.db)
    stack.callback(pool.close)
    run_migrations(pool)
    return layout, pool


def _seed_task(pool: SqlitePool, *, directory: Path | None = None) -> str:
    configuration = json.dumps(
        {"directory": str(directory) if directory else None, "skills": [], "plugins": []}
    )
    with pool.connect() as conn:
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) "
            "VALUES(1,'owner','hash','admin',1)"
        )
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,"
            "created_at,updated_at,status,wait_kind) "
            "VALUES('task-one',1,'Task','request-task','turn-one',1,1,'running','user')"
        )
        conn.execute(
            "INSERT INTO project_task_turns(turn_id,task_id,request_id,prompt,status,"
            "configuration_json,model_ref,checkpoint_id,wait_kind,created_at,updated_at) "
            "VALUES('turn-one','task-one','request-turn','Continue','running',"
            "?,'provider/model','saved-checkpoint','user',1,1)",
            (configuration,),
        )
    return configuration


def _wal_database(stack: ExitStack, path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    stack.callback(conn.close)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE records(value TEXT)")
    conn.execute("INSERT INTO records VALUES(?)", (value,))
    conn.commit()
    assert path.with_name(path.name + "-wal").stat().st_size > 0


def _read_database(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(conn.execute("SELECT value FROM records").fetchone()[0])


@pytest.mark.parametrize("include_chats", [False, True], ids=["default", "all"])
def test_task_backup_restores_files_and_live_wal_databases(
    tmp_path: Path, include_chats: bool
) -> None:
    with ExitStack() as stack:
        source, pool = _pool(stack, tmp_path / "source")
        configuration = _seed_task(pool)
        task = source.root / "project-tasks" / "task-one"
        workspace = task / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "draft.txt").write_bytes(b"work in progress")
        resources = task / "turn-one" / "resources"
        skills = [("writer", [("SKILL.md", b"---\nname: writer\ndescription: Write\n---\n")])]
        save_snapshot(resources, configuration, skills)
        _wal_database(stack, task / "memory.sqlite", "task memory in WAL")
        _wal_database(
            stack, source.root / "expert-memory-users" / "1" / "memory.sqlite", "expert memory"
        )
        checkpoint_file = task / "turn-one" / "checkpoints.sqlite"
        checkpoint_conn = sqlite3.connect(checkpoint_file)
        stack.callback(checkpoint_conn.close)
        checkpoint_conn.execute("PRAGMA wal_autocheckpoint=0")
        saver = SqliteSaver(checkpoint_conn)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "saved-checkpoint"
        checkpoint["channel_values"] = {"answer": "pending"}
        saver.put(
            {"configurable": {"thread_id": "turn-one", "checkpoint_ns": ""}},
            checkpoint,
            {"source": "loop", "step": 1, "parents": {}},
            {},
        )
        assert checkpoint_file.with_name("checkpoints.sqlite-wal").stat().st_size > 0

        archive = tmp_path / "backup.tar.gz"
        options = {"include_chats": True} if include_chats else {}
        create_system_backup(
            paths=source,
            agent_rows=[],
            pool=pool,
            db_config=DatabaseConfig(),
            dest=archive,
            **options,
        )
        with tarfile.open(archive) as tf:
            names = tf.getnames()
            manifest = BackupManifest.load_text(tf.extractfile("manifest.json").read().decode())
        assert manifest.includes_task_state is True
        assert manifest.includes_task_workspaces is True
        assert manifest.includes_chats is include_chats
        assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in names)

        target, target_pool = _pool(stack, tmp_path / "target")
        stale_task = target.root / "project-tasks" / "task-one"
        stale_task.mkdir(parents=True)
        (stale_task / "memory.sqlite-wal").write_bytes(b"stale WAL")
        (stale_task / "stale-state.json").write_bytes(b"stale")
        result = restore_system_backup(
            archive,
            paths=target,
            pool=target_pool,
            db_config=DatabaseConfig(),
            restore_config=False,
        )
        restored = target.root / "project-tasks" / "task-one"
        with target_pool.connect() as conn:
            turn = conn.execute(
                "SELECT checkpoint_id,configuration_json FROM project_task_turns "
                "WHERE turn_id='turn-one'"
            ).fetchone()
        assert turn["checkpoint_id"] == "saved-checkpoint"
        assert (
            load_snapshot(restored / "turn-one" / "resources", turn["configuration_json"]) == skills
        )
        assert (restored / "workspace" / "draft.txt").read_bytes() == b"work in progress"
        assert _read_database(restored / "memory.sqlite") == "task memory in WAL"
        assert (
            _read_database(target.root / "expert-memory-users" / "1" / "memory.sqlite")
            == "expert memory"
        )
        with sqlite3.connect(restored / "turn-one" / "checkpoints.sqlite") as conn:
            restored_checkpoint = SqliteSaver(conn).get_tuple(
                {
                    "configurable": {
                        "thread_id": "turn-one",
                        "checkpoint_id": turn["checkpoint_id"],
                        "checkpoint_ns": "",
                    }
                }
            )
        assert restored_checkpoint is not None
        assert restored_checkpoint.checkpoint["channel_values"] == {"answer": "pending"}
        assert not (restored / "stale-state.json").exists()
        assert result["task_state_files"] == 3
        assert result["task_workspace_files"] == 1
        assert result["expert_memory_files"] == 1


def test_omitting_workspaces_keeps_task_recovery_and_existing_workspace(tmp_path: Path) -> None:
    with ExitStack() as stack:
        source, pool = _pool(stack, tmp_path / "source")
        external = tmp_path / "external-project"
        external.mkdir()
        (external / "private.txt").write_bytes(b"external project")
        _seed_task(pool, directory=external)
        task = source.root / "project-tasks" / "task-one"
        (task / "workspace").mkdir(parents=True)
        (task / "workspace" / "draft.txt").write_bytes(b"omitted draft")
        _wal_database(stack, task / "memory.sqlite", "kept task memory")
        (task / "turn-one").mkdir()
        (task / "turn-one" / "recovery.json").write_bytes(b"recovery")
        archive = tmp_path / "without-workspaces.tar.gz"
        create_system_backup(
            paths=source,
            agent_rows=[],
            pool=pool,
            db_config=DatabaseConfig(),
            dest=archive,
            include_workspaces=False,
        )
        with tarfile.open(archive) as tf:
            assert "project-tasks/task-one/memory.sqlite" in tf.getnames()
            assert not any("workspace" in name or "private.txt" in name for name in tf.getnames())
        target, target_pool = _pool(stack, tmp_path / "target")
        kept_workspace = target.root / "project-tasks" / "task-one" / "workspace"
        kept_workspace.mkdir(parents=True)
        (kept_workspace / "local.txt").write_bytes(b"keep local draft")
        restore_system_backup(
            archive,
            paths=target,
            pool=target_pool,
            db_config=DatabaseConfig(),
            restore_config=False,
        )
        assert (kept_workspace / "local.txt").read_bytes() == b"keep local draft"
        assert not (kept_workspace / "draft.txt").exists()
        assert (kept_workspace.parent / "turn-one" / "recovery.json").read_bytes() == b"recovery"
        assert _read_database(kept_workspace.parent / "memory.sqlite") == "kept task memory"
        assert (external / "private.txt").read_bytes() == b"external project"


def test_legacy_archive_does_not_replace_live_task_files(tmp_path: Path) -> None:
    with ExitStack() as stack:
        source, pool = _pool(stack, tmp_path / "source")
        archive = tmp_path / "legacy.tar.gz"
        create_system_backup(
            paths=source, agent_rows=[], pool=pool, db_config=DatabaseConfig(), dest=archive
        )
        with tarfile.open(archive) as tf:
            entries = {
                member.name: tf.extractfile(member).read() for member in tf if member.isfile()
            }
        manifest = json.loads(entries["manifest.json"])
        manifest.pop("includes_task_state")
        manifest.pop("includes_task_workspaces")
        entries["manifest.json"] = json.dumps(manifest).encode()
        with tarfile.open(archive, "w:gz") as tf:
            for name, data in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tf.addfile(member, BytesIO(data))
        target, target_pool = _pool(stack, tmp_path / "target")
        live = target.root / "project-tasks" / "task-one" / "workspace" / "draft.txt"
        live.parent.mkdir(parents=True)
        live.write_bytes(b"keep")
        restore_system_backup(
            archive,
            paths=target,
            pool=target_pool,
            db_config=DatabaseConfig(),
            restore_config=False,
        )
        assert live.read_bytes() == b"keep"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_task_backup_does_not_follow_external_symlinks(tmp_path: Path) -> None:
    with ExitStack() as stack:
        source, pool = _pool(stack, tmp_path / "source")
        external = tmp_path / "external-client"
        external.mkdir()
        (external / "private.txt").write_bytes(b"private")
        task = source.root / "project-tasks" / "task-one"
        task.mkdir(parents=True)
        (task / "workspace").symlink_to(external, target_is_directory=True)
        (task / "external.txt").symlink_to(external / "private.txt")
        archive = tmp_path / "backup.tar.gz"
        create_system_backup(
            paths=source, agent_rows=[], pool=pool, db_config=DatabaseConfig(), dest=archive
        )
        with tarfile.open(archive) as tf:
            assert not any(name.startswith("project-tasks/") for name in tf.getnames())
