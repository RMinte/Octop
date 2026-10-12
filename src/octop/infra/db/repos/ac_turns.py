"""SQL access to AgentComputer turns; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow


@dataclass(frozen=True)
class AcTurnRow:
    pk: int
    turn_id: str
    task_id: str
    project_id: str
    owner_id: int
    user_message_id: str
    assistant_message_id: str
    context_mode: str
    schema_version: int
    snapshot_json: str
    snapshot_digest: str
    created_at: int

    @property
    def id(self) -> str:
        return self.turn_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcTurnRow:
        return cls(
            pk=int(r["id"]),
            turn_id=str(r["turn_id"]),
            task_id=str(r["task_id"]),
            project_id=str(r["project_id"]),
            owner_id=int(r["owner_id"]),
            user_message_id=str(r["user_message_id"]),
            assistant_message_id=str(r["assistant_message_id"]),
            context_mode=str(r["context_mode"]),
            schema_version=int(r["schema_version"]),
            snapshot_json=str(r["snapshot_json"]),
            snapshot_digest=str(r["snapshot_digest"]),
            created_at=int(r["created_at"]),
        )


class AcTurnRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def has_user_message(conn: Any, task_id: str, owner_id: int, user_message_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM ac_turns WHERE task_id = ? AND owner_id = ? AND user_message_id = ?",
            (task_id, owner_id, user_message_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def insert(
        conn: Any,
        *,
        turn_id: str,
        task_id: str,
        project_id: str,
        owner_id: int,
        user_message_id: str,
        assistant_message_id: str,
        context_mode: str = "newMessage",
        schema_version: int = 1,
        snapshot_json: str,
        snapshot_digest: str,
        created_at: int,
    ) -> AcTurnRow:
        row = conn.execute(
            "INSERT INTO ac_turns (turn_id, task_id, project_id, owner_id, user_message_id, assistant_message_id, context_mode, schema_version, snapshot_json, snapshot_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (
                turn_id,
                task_id,
                project_id,
                owner_id,
                user_message_id,
                assistant_message_id,
                context_mode,
                schema_version,
                snapshot_json,
                snapshot_digest,
                created_at,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("AgentComputer turns insert returned no row")
        return AcTurnRow.from_row(row)
