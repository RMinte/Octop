"""SQL journal for resumable, owner-scoped legacy conversation migration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.threads import ThreadRow


@dataclass(frozen=True)
class MigrationBatchRow:
    batch_id: str
    owner_id: int
    request_id: str
    fingerprint: str
    configuration_json: str
    status: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class MigrationItemRow:
    item_id: str
    batch_id: str
    owner_id: int
    source_agent_id: str
    source_thread_id: str
    route_id: str
    target_task_id: str
    status: str
    phase: str
    source_digest: str
    manifest_json: str
    error_code: str
    worker_id: str
    lease_until: int
    created_at: int
    updated_at: int


def _batch(row: Any) -> MigrationBatchRow:
    return MigrationBatchRow(**{key: row[key] for key in MigrationBatchRow.__dataclass_fields__})


def _item(row: Any) -> MigrationItemRow:
    return MigrationItemRow(**{key: row[key] for key in MigrationItemRow.__dataclass_fields__})


class ConversationMigrationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def threads(self, owner_id: int, thread_ids: list[str]) -> list[ThreadRow]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE user_id=? ORDER BY id", (owner_id,)
            ).fetchall()
        selected = set(thread_ids)
        return [
            ThreadRow.from_row(row) for row in rows if not selected or row["thread_id"] in selected
        ]

    def owners(self) -> list[int]:
        with self.db.connect() as conn:
            return [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM users WHERE disabled=0 ORDER BY id"
                ).fetchall()
            ]

    @staticmethod
    def by_request(conn: Any, owner_id: int, request_id: str) -> MigrationBatchRow | None:
        row = conn.execute(
            "SELECT * FROM conversation_migration_batches WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return _batch(row) if row else None

    def get(self, owner_id: int, batch_id: str) -> MigrationBatchRow | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_migration_batches WHERE owner_id=? AND batch_id=?",
                (owner_id, batch_id),
            ).fetchone()
        return _batch(row) if row else None

    def batches(self, owner_id: int, limit: int = 100) -> list[MigrationBatchRow]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_migration_batches WHERE owner_id=? ORDER BY id DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [_batch(row) for row in rows]

    def items(self, owner_id: int, batch_id: str) -> list[MigrationItemRow]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_migration_items WHERE owner_id=? AND batch_id=? ORDER BY id",
                (owner_id, batch_id),
            ).fetchall()
        return [_item(row) for row in rows]

    @staticmethod
    def insert_batch(conn: Any, row: MigrationBatchRow) -> None:
        keys = list(MigrationBatchRow.__dataclass_fields__)
        conn.execute(
            f"INSERT INTO conversation_migration_batches({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
            tuple(getattr(row, key) for key in keys),
        )

    @staticmethod
    def insert_item(conn: Any, row: MigrationItemRow) -> None:
        keys = list(MigrationItemRow.__dataclass_fields__)
        conn.execute(
            f"INSERT INTO conversation_migration_items({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
            tuple(getattr(row, key) for key in keys),
        )

    def claim(
        self, worker_id: str, now: int, batch_id: str | None = None
    ) -> MigrationItemRow | None:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT i.item_id FROM conversation_migration_items i JOIN conversation_migration_batches b "
                "ON b.batch_id=i.batch_id WHERE b.status IN ('queued','running','waiting') "
                "AND (i.status='pending' OR (i.status='running' AND i.lease_until<?) OR (i.status='blocked' AND i.error_code='source_active' AND i.updated_at<?)) "
                + ("AND i.batch_id=? " if batch_id else "")
                + "ORDER BY i.id LIMIT 1",
                (now, now - 5000, batch_id) if batch_id else (now, now - 5000),
            ).fetchone()
            if rows is None:
                return None
            row = conn.execute(
                "UPDATE conversation_migration_items SET status='running',worker_id=?,lease_until=?,updated_at=? "
                "WHERE item_id=? AND (status='pending' OR (status='running' AND lease_until<?) OR (status='blocked' AND error_code='source_active' AND updated_at<?)) RETURNING *",
                (worker_id, now + 120_000, now, rows["item_id"], now, now - 5000),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE conversation_migration_batches SET status='running',updated_at=? WHERE batch_id=?",
                (now, row["batch_id"]),
            )
            return _item(row)

    @staticmethod
    def locked(conn: Any, item: MigrationItemRow, now: int) -> bool:
        return bool(
            conn.execute(
                "UPDATE conversation_migration_items SET lease_until=? WHERE item_id=? AND worker_id=? AND status='running' "
                "AND EXISTS(SELECT 1 FROM conversation_migration_batches b WHERE b.batch_id=conversation_migration_items.batch_id "
                "AND b.status IN ('queued','running')) RETURNING item_id",
                (now + 120_000, item.item_id, item.worker_id),
            ).fetchone()
        )

    def progress(
        self, item: MigrationItemRow, *, phase: str, digest: str, manifest: dict[str, Any], now: int
    ) -> bool:
        with self.db.transaction() as conn:
            if not self.locked(conn, item, now):
                return False
            conn.execute(
                "UPDATE conversation_migration_items SET phase=?,source_digest=?,manifest_json=?,route_id=?,updated_at=? WHERE item_id=?",
                (phase, digest, json.dumps(manifest), item.route_id, now, item.item_id),
            )
            return True

    @staticmethod
    def finish(
        conn: Any,
        item: MigrationItemRow,
        status: str,
        code: str,
        manifest: dict[str, Any],
        now: int,
    ) -> bool:
        changed = conn.execute(
            "UPDATE conversation_migration_items SET status=?,error_code=?,manifest_json=?,phase=?,target_task_id=?,route_id=?,source_digest=?,worker_id='',lease_until=0,updated_at=? "
            "WHERE item_id=? AND worker_id=?",
            (
                status,
                code,
                json.dumps(manifest),
                "published" if status == "completed" else item.phase,
                item.target_task_id,
                item.route_id,
                manifest.get("source_digest", ""),
                now,
                item.item_id,
                item.worker_id,
            ),
        ).rowcount
        if changed != 1:
            return False
        conn.execute(
            "UPDATE conversation_migration_batches SET status=CASE WHEN EXISTS(SELECT 1 FROM conversation_migration_items WHERE batch_id=? AND status='blocked' AND error_code='source_active') THEN 'waiting' ELSE 'completed' END,updated_at=? WHERE batch_id=? "
            "AND status='running' AND NOT EXISTS(SELECT 1 FROM conversation_migration_items WHERE batch_id=? AND status IN ('pending','running'))",
            (item.batch_id, now, item.batch_id, item.batch_id),
        )
        return True

    def control(self, owner_id: int, batch_id: str, action: str, now: int) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE conversation_migration_batches SET status=?,updated_at=? WHERE owner_id=? AND batch_id=? RETURNING batch_id",
                ("paused" if action == "pause" else "queued", now, owner_id, batch_id),
            ).fetchone()
            if row and action == "resume":
                conn.execute(
                    "UPDATE conversation_migration_items SET status='pending',worker_id='',lease_until=0 WHERE batch_id=? AND status IN ('blocked','failed','running')",
                    (batch_id,),
                )
            return row is not None

    @staticmethod
    def existing_legacy_targets(conn: Any, owner_id: int, thread_id: str) -> list[str]:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT task_id FROM project_task_legacy_sources WHERE owner_id=? AND source_thread_id=?",
                (owner_id, thread_id),
            ).fetchall()
        ]
