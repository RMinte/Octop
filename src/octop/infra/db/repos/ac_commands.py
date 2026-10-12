"""SQL access to AgentComputer commands; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow


@dataclass(frozen=True)
class AcCommandRow:
    pk: int
    command_id: str
    owner_id: int
    idempotency_key: str
    operation: str
    target_id: str
    payload_digest: str
    restore_epoch: int
    schema_version: int
    receipt_json: str
    created_at: int

    @property
    def id(self) -> str:
        return self.command_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcCommandRow:
        return cls(
            pk=int(r["id"]),
            command_id=str(r["command_id"]),
            owner_id=int(r["owner_id"]),
            idempotency_key=str(r["idempotency_key"]),
            operation=str(r["operation"]),
            target_id=str(r["target_id"]),
            payload_digest=str(r["payload_digest"]),
            restore_epoch=int(r["restore_epoch"]),
            schema_version=int(r["schema_version"]),
            receipt_json=str(r["receipt_json"]),
            created_at=int(r["created_at"]),
        )


class AcCommandRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, command_id: str, owner_id: int) -> AcCommandRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, command_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, command_id: str, owner_id: int) -> AcCommandRow | None:
        row = conn.execute(
            "SELECT * FROM ac_commands WHERE command_id = ? AND owner_id = ?",
            (command_id, owner_id),
        ).fetchone()
        return AcCommandRow.from_row(row) if row is not None else None

    @staticmethod
    def get_global_in_transaction(conn: Any, command_id: str) -> AcCommandRow | None:
        """Internal collision detection; never use this result as an owner response."""
        row = conn.execute(
            "SELECT * FROM ac_commands WHERE command_id = ?", (command_id,)
        ).fetchone()
        return AcCommandRow.from_row(row) if row is not None else None

    @staticmethod
    def get_by_key(conn: Any, owner_id: int, idempotency_key: str) -> AcCommandRow | None:
        row = conn.execute(
            "SELECT * FROM ac_commands WHERE owner_id = ? AND idempotency_key = ?",
            (owner_id, idempotency_key),
        ).fetchone()
        return AcCommandRow.from_row(row) if row is not None else None

    def lock_owner(self, conn: Any, owner_id: int) -> bool:
        suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        row = conn.execute(
            "SELECT id FROM users WHERE id = ? AND disabled = 0" + suffix, (owner_id,)
        ).fetchone()
        return row is not None

    @staticmethod
    def insert(
        conn: Any,
        *,
        command_id: str,
        owner_id: int,
        idempotency_key: str,
        operation: str,
        target_id: str,
        payload_digest: str,
        restore_epoch: int,
        schema_version: int = 1,
        receipt_json: str,
        created_at: int,
    ) -> bool:
        cursor = conn.execute(
            "INSERT INTO ac_commands (command_id, owner_id, idempotency_key, operation, target_id, payload_digest, restore_epoch, schema_version, receipt_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                command_id,
                owner_id,
                idempotency_key,
                operation,
                target_id,
                payload_digest,
                restore_epoch,
                schema_version,
                receipt_json,
                created_at,
            ),
        )
        return bool(cursor.rowcount == 1)
