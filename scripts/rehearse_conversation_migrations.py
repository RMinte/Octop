"""Copy real SQLite stores read-only, then rehearse migration without a server/model.

Only aggregate diagnostics are printed. The private rehearsal directory contains
the backup and must be handled with the same care as an Octop home.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from octop.config import load_config
from octop.infra.agents.memory.backend import open_memory_kwargs
from octop.infra.agents.workspace.dir import workspace_dir_from_config
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.projects.conversation_migrations import (
    ConversationMigrationWorker,
    MigrationCreate,
    create,
    detail,
)
from octop.infra.utils.paths import PathLayout


def backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        original.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target)) as copied:
            original.backup(copied)


def table_digest(db: SqlitePool, table: str) -> tuple[int, str]:
    # Called only for fixed names returned by sqlite_master, quote identifiers.
    quoted = '"' + table.replace('"', '""') + '"'
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM " + quoted + " ORDER BY rowid").fetchall()
    payload = json.dumps([dict(row) for row in rows], sort_keys=True, default=str).encode()
    return len(rows), hashlib.sha256(payload).hexdigest()


async def rehearse(source_home: Path) -> dict:
    source_paths = PathLayout(source_home.resolve())
    # load_config creates defaults when missing; only ever call it on a copy.
    source_config = source_paths.config.read_bytes()
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="octop-migration-rehearsal-", dir="/private/tmp"))
    root.chmod(0o700)
    copied_config_path = root / "config.json"
    copied_config_path.write_bytes(source_config)
    config = load_config(copied_config_path)
    if not config.database.is_sqlite:
        raise ValueError("SQLite source required for this rehearsal")
    control_source = config.database.resolve_sqlite_path(source_paths.root)
    backup(control_source, root / "octop.db")
    archive_source = source_paths.root / "history_v2.sqlite"
    if archive_source.exists():
        backup(archive_source, root / "history_v2.sqlite")
    if archive_source.with_suffix(".required").exists():
        (root / "history_v2.required").write_text(
            "Source archive required; copied for rehearsal.\n"
        )
    db = SqlitePool(root / "octop.db")
    try:
        run_migrations(db)
        # Archive identity intentionally remains the original control-plane
        # identity; services.db below always points at the private backup.
        copied_config = replace(
            config, database=replace(config.database, sqlite_path=str(control_source.resolve()))
        )
        services = SharedServices(PathLayout(root), copied_config, RepoBundle.from_pool(db))
        memories: dict[str, Path] = {}
        diagnostics: Counter[str] = Counter()
        # No runtime is started. Disable network-producing configuration in
        # the copy as a second boundary before constructing the offline worker.
        with db.transaction() as conn:
            for table in ("providers", "cron_jobs", "channels"):
                columns = {
                    row["name"]
                    for row in conn.execute('PRAGMA table_info("' + table + '")').fetchall()
                }
                if "enabled" in columns:
                    conn.execute('UPDATE "' + table + '" SET enabled=0')
        for agent in services.agent_repo.list_all():
            raw = json.loads(agent.config_json or "{}")
            workspace = workspace_dir_from_config(
                raw, paths=source_paths, agent_id=agent.agent_id, ensure=False
            )
            if not raw.get("workspace_dir"):
                raw["workspace_dir"] = str(workspace)
            _, kind, options = open_memory_kwargs(
                agent_id=agent.agent_id, cfg=raw, octop_config=config, workspace_dir=workspace
            )
            target = (
                root
                / "source-checkpoints"
                / (hashlib.sha256(agent.agent_id.encode()).hexdigest() + ".sqlite")
            )
            if kind == "sqlite":
                path = Path(str((options or {})["db_path"]))
                if path.is_absolute() and path.is_file():
                    key = str(path.resolve())
                    if key not in memories:
                        try:
                            backup(path, target)
                            memories[key] = target
                        except sqlite3.Error as error:
                            diagnostics["checkpoint_backup_unavailable"] += 1
                            diagnostics[
                                "checkpoint_backup_"
                                + str(getattr(error, "sqlite_errorname", "unknown"))
                            ] += 1
                            raw["_migration_checkpoint_unavailable"] = True
                    target = memories.get(key, target)
                elif not path.is_absolute():
                    diagnostics["relative_checkpoint_path_unresolved"] += 1
            else:
                diagnostics["postgres_checkpoint_not_read_in_sqlite_rehearsal"] += 1
                raw["_migration_checkpoint_unavailable"] = True
            raw["memory"] = {
                **(raw.get("memory") or {}),
                "backend": {"type": "sqlite", "db_path": str(target)},
            }
            services.agent_repo.update_config(agent.agent_id, config_json=json.dumps(raw))
        before = {
            table: table_digest(db, table)
            for table in ("threads", "thread_messages", "trajectory_events")
        }
        with db.connect() as conn:
            prototypes = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ac\\_%' ESCAPE '\\'"
                ).fetchall()
            ]
            model_calls = int(conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0])
        prototype_before = {table: table_digest(db, table) for table in prototypes}
        worker = ConversationMigrationWorker(services)
        batch_ids = []
        for owner_id in services.conversation_migration_repo.owners():
            batch = create(
                services, owner_id, MigrationCreate(request_id=f"rehearsal-owner-{owner_id}")
            )
            batch_ids.append((owner_id, batch.batch_id))
            while await worker.run_once(batch.batch_id):
                pass
        totals: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        migrated_attachments = 0
        migrated_trajectory = 0
        for owner_id, batch_id in batch_ids:
            batch = detail(services, owner_id, batch_id)
            for item in batch.items:
                totals[item.status] += 1
                if item.error_code:
                    reasons[item.error_code] += 1
                migrated_attachments += int(item.manifest.get("attachment_count", 0))
                migrated_trajectory += int(item.manifest.get("trajectory_event_count", 0))
        with db.connect() as conn:
            tasks_first = int(conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0])
        # A second, independently keyed batch must converge to the same routes.
        for owner_id, _ in batch_ids:
            repeated = create(
                services, owner_id, MigrationCreate(request_id=f"rehearsal-repeat-{owner_id}")
            )
            while await worker.run_once(repeated.batch_id):
                pass
        with db.connect() as conn:
            tasks_second = int(conn.execute("SELECT COUNT(*) FROM project_tasks").fetchone()[0])
            new_calls = (
                int(conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]) - model_calls
            )
            integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        report = {
            "rehearsal_directory": str(root),
            "source_opened_read_only": True,
            "cross_store_atomic_snapshot": False,
            "source_snapshots_unchanged": all(
                table_digest(db, table) == value for table, value in before.items()
            ),
            "prototype_tables_preserved": all(
                table_digest(db, table) == value for table, value in prototype_before.items()
            ),
            "prototype_table_count": len(prototypes),
            "checkpoint_backups": len(memories),
            "owner_count": len(batch_ids),
            "items": dict(totals),
            "reasons": dict(reasons),
            "diagnostics": dict(diagnostics),
            "copied_attachments": migrated_attachments,
            "copied_trajectory_events": migrated_trajectory,
            "repeat_added_tasks": tasks_second - tasks_first,
            "model_calls": new_calls,
            "quick_check": integrity,
            "foreign_key_violations": foreign_keys,
        }
        (root / "rehearsal-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        return report
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", type=Path, required=True)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    try:
        result = asyncio.run(rehearse(arguments.source_home))
    except Exception as error:
        # Exception messages may include source paths or connection details.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))
