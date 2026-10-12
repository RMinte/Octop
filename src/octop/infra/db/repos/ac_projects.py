"""SQL access to AgentComputer projects; mutations use the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow


@dataclass(frozen=True)
class AcProjectRow:
    pk: int
    project_id: str
    owner_id: int
    box_id: str
    workspace_id: str
    default_agent_id: str | None
    preparation_state: str
    revision: int
    schema_version: int
    config_json: str
    config_digest: str
    active_run_id: str | None
    created_at: int
    updated_at: int

    @property
    def id(self) -> str:
        return self.project_id

    @classmethod
    def from_row(cls, r: DbRow) -> AcProjectRow:
        return cls(
            pk=int(r["id"]),
            project_id=str(r["project_id"]),
            owner_id=int(r["owner_id"]),
            box_id=str(r["box_id"]),
            workspace_id=str(r["workspace_id"]),
            default_agent_id=str(r["default_agent_id"])
            if r["default_agent_id"] is not None
            else None,
            preparation_state=str(r["preparation_state"]),
            revision=int(r["revision"]),
            schema_version=int(r["schema_version"]),
            config_json=str(r["config_json"]),
            config_digest=str(r["config_digest"]),
            active_run_id=str(r["active_run_id"]) if r["active_run_id"] is not None else None,
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


class AcProjectRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, project_id: str, owner_id: int) -> AcProjectRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, project_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, project_id: str, owner_id: int) -> AcProjectRow | None:
        row = conn.execute(
            "SELECT * FROM ac_projects WHERE project_id = ? AND owner_id = ?",
            (project_id, owner_id),
        ).fetchone()
        return AcProjectRow.from_row(row) if row is not None else None

    @staticmethod
    def acquire_run(
        conn: Any,
        project_id: str,
        owner_id: int,
        run_id: str,
        expected_revision: int,
        *,
        updated_at: int,
    ) -> bool:
        cursor = conn.execute(
            "UPDATE ac_projects SET active_run_id = ?, updated_at = ? "
            "WHERE project_id = ? AND owner_id = ? AND revision = ? "
            "AND preparation_state = 'ready' AND active_run_id IS NULL",
            (run_id, updated_at, project_id, owner_id, expected_revision),
        )
        return bool(cursor.rowcount == 1)
