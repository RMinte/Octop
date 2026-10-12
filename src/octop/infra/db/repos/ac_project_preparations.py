"""SQL access to project preparation facts; the caller owns every transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_bool_int, strict_int


@dataclass(frozen=True)
class AcProjectPreparationRow:
    pk: int
    preparation_id: str
    project_id: str
    owner_id: int
    planned_agent_id: str
    intent_json: str
    intent_digest: str
    restore_epoch: int
    schema_version: int
    revision: int
    agent_state: str
    workspace_state: str
    reason_code: str | None
    retryable: bool
    created_at: int
    updated_at: int

    @property
    def id(self) -> str:
        return self.preparation_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcProjectPreparationRow:
        return cls(
            pk=strict_int(r["id"]),
            preparation_id=str(r["preparation_id"]),
            project_id=str(r["project_id"]),
            owner_id=strict_int(r["owner_id"]),
            planned_agent_id=str(r["planned_agent_id"]),
            intent_json=str(r["intent_json"]),
            intent_digest=str(r["intent_digest"]),
            restore_epoch=strict_int(r["restore_epoch"]),
            schema_version=strict_int(r["schema_version"]),
            revision=strict_int(r["revision"]),
            agent_state=str(r["agent_state"]),
            workspace_state=str(r["workspace_state"]),
            reason_code=str(r["reason_code"]) if r["reason_code"] is not None else None,
            retryable=strict_bool_int(r["retryable"]),
            created_at=strict_int(r["created_at"]),
            updated_at=strict_int(r["updated_at"]),
        )


class AcProjectPreparationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, project_id: str, owner_id: int) -> AcProjectPreparationRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, project_id, owner_id)

    def get_by_project(self, project_id: str, owner_id: int) -> AcProjectPreparationRow | None:
        return self.get(project_id, owner_id)

    @staticmethod
    def get_in_transaction(
        conn: Any, project_id: str, owner_id: int
    ) -> AcProjectPreparationRow | None:
        row = conn.execute(
            "SELECT * FROM ac_project_preparations WHERE project_id = ? AND owner_id = ?",
            (project_id, owner_id),
        ).fetchone()
        return AcProjectPreparationRow.from_row(row) if row is not None else None

    @staticmethod
    def insert(
        conn: Any,
        *,
        preparation_id: str,
        project_id: str,
        owner_id: int,
        planned_agent_id: str,
        intent_json: str,
        intent_digest: str,
        restore_epoch: int,
        created_at: int,
        updated_at: int,
    ) -> AcProjectPreparationRow:
        conn.execute(
            "INSERT INTO ac_project_preparations (preparation_id, project_id, owner_id, "
            "planned_agent_id, intent_json, intent_digest, restore_epoch, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                preparation_id,
                project_id,
                owner_id,
                planned_agent_id,
                intent_json,
                intent_digest,
                restore_epoch,
                created_at,
                updated_at,
            ),
        )
        row = AcProjectPreparationRepo.get_in_transaction(conn, project_id, owner_id)
        assert row is not None
        return row

    @staticmethod
    def confirm_agent(conn: Any, project_id: str, owner_id: int, *, updated_at: int) -> None:
        conn.execute(
            "UPDATE ac_project_preparations SET agent_state = 'verified', "
            "revision = revision + 1, updated_at = ? "
            "WHERE project_id = ? AND owner_id = ? AND agent_state = 'pending'",
            (updated_at, project_id, owner_id),
        )

    @staticmethod
    def confirm_workspace(conn: Any, project_id: str, owner_id: int, *, updated_at: int) -> None:
        conn.execute(
            "UPDATE ac_project_preparations SET workspace_state = 'verified', "
            "revision = revision + 1, updated_at = ? "
            "WHERE project_id = ? AND owner_id = ? AND workspace_state = 'pending'",
            (updated_at, project_id, owner_id),
        )

    @staticmethod
    def fail(
        conn: Any,
        project_id: str,
        owner_id: int,
        reason_code: str,
        retryable: bool,
        *,
        updated_at: int,
    ) -> None:
        conn.execute(
            "UPDATE ac_project_preparations SET reason_code = ?, retryable = ?, "
            "revision = revision + 1, updated_at = ? WHERE project_id = ? AND owner_id = ?",
            (reason_code, int(retryable), updated_at, project_id, owner_id),
        )

    @staticmethod
    def clear_reason(conn: Any, project_id: str, owner_id: int, *, updated_at: int) -> None:
        conn.execute(
            "UPDATE ac_project_preparations SET reason_code = NULL, retryable = 0, "
            "revision = revision + 1, updated_at = ? WHERE project_id = ? AND owner_id = ? "
            "AND (reason_code IS NOT NULL OR retryable <> 0)",
            (updated_at, project_id, owner_id),
        )
