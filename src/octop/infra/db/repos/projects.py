"""SQL-only persistence for projects, independent of agents and their workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.project_sync import record_change


@dataclass(frozen=True)
class ProjectRow:
    project_id: str
    owner_id: int
    name: str
    description: str
    directory: str | None
    client_directory: str | None
    instructions: str
    skills_json: str
    mcp_servers_json: str
    revision: int
    created_at: int
    updated_at: int
    capabilities_json: str = "{}"
    archived: bool = False

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectRow:
        return cls(
            project_id=str(row["project_id"]),
            owner_id=strict_int(row["owner_id"]),
            name=str(row["name"]),
            description=str(row["description"]),
            directory=str(row["directory"]) if row["directory"] is not None else None,
            client_directory=str(row["client_directory"])
            if row["client_directory"] is not None
            else None,
            instructions=str(row["instructions"]),
            skills_json=str(row["skills_json"]),
            mcp_servers_json=str(row["mcp_servers_json"]),
            revision=strict_int(row["revision"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            capabilities_json=str(row["capabilities_json"]),
            archived=bool(row["archived"]),
        )


class ProjectRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list_for_owner(self, owner_id: int) -> list[ProjectRow]:
        with self._db.connect() as conn:
            return self.list_in_transaction(conn, owner_id)

    @staticmethod
    def list_in_transaction(conn: Any, owner_id: int) -> list[ProjectRow]:
        rows = conn.execute(
            "SELECT * FROM projects WHERE owner_id=? ORDER BY updated_at DESC, project_id DESC",
            (owner_id,),
        ).fetchall()
        return [ProjectRow.from_row(row) for row in rows]

    def get(self, project_id: str, owner_id: int) -> ProjectRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, project_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, project_id: str, owner_id: int) -> ProjectRow | None:
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id=? AND owner_id=?", (project_id, owner_id)
        ).fetchone()
        return ProjectRow.from_row(row) if row is not None else None

    def lock_owner(self, conn: Any, owner_id: int) -> bool:
        suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        return (
            conn.execute(
                "SELECT id FROM users WHERE id=? AND disabled=0" + suffix, (owner_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def is_deleting(conn: Any, owner_id: int, project_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM settings WHERE key=?",
                (f"project_delete:{owner_id}:{project_id}",),
            ).fetchone()
            is not None
        )

    @staticmethod
    def mark_deleting(conn: Any, owner_id: int, project_id: str) -> None:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,'requested') ON CONFLICT(key) DO NOTHING",
            (f"project_delete:{owner_id}:{project_id}",),
        )

    @staticmethod
    def clear_deleting(conn: Any, owner_id: int, project_id: str) -> None:
        conn.execute(
            "DELETE FROM settings WHERE key=?", (f"project_delete:{owner_id}:{project_id}",)
        )

    @staticmethod
    def creation_request(conn: Any, owner_id: int, request_id: str) -> tuple[str, str] | None:
        row = conn.execute(
            "SELECT fingerprint, response_json FROM project_create_requests "
            "WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return (str(row["fingerprint"]), str(row["response_json"])) if row else None

    @staticmethod
    def record_creation(
        conn: Any, row: ProjectRow, request_id: str, fingerprint: str, response_json: str
    ) -> None:
        conn.execute(
            "INSERT INTO project_create_requests(owner_id, request_id, project_id, fingerprint, response_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (row.owner_id, request_id, row.project_id, fingerprint, response_json),
        )

    @staticmethod
    def create(conn: Any, row: ProjectRow) -> ProjectRow:
        result = conn.execute(
            "INSERT INTO projects(project_id, owner_id, name, description, directory, client_directory, "
            "instructions, skills_json, mcp_servers_json, revision, created_at, updated_at, capabilities_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) RETURNING *",
            (
                row.project_id,
                row.owner_id,
                row.name,
                row.description,
                row.directory,
                row.client_directory,
                row.instructions,
                row.skills_json,
                row.mcp_servers_json,
                row.created_at,
                row.updated_at,
                row.capabilities_json,
            ),
        ).fetchone()
        assert result is not None
        saved = ProjectRow.from_row(result)
        record_change(conn, saved.owner_id, "projects", saved.project_id, saved)
        return saved

    def update(self, row: ProjectRow, expected_revision: int) -> ProjectRow | None:
        with self._db.transaction() as conn:
            if not self.lock_owner(conn, row.owner_id):
                return None
            result = conn.execute(
                "UPDATE projects SET name=?, description=?, directory=?, client_directory=?, instructions=?, "
                "skills_json=?, mcp_servers_json=?, revision=revision+1, updated_at=?, capabilities_json=? "
                "WHERE project_id=? AND owner_id=? AND revision=? "
                "AND NOT EXISTS(SELECT 1 FROM settings WHERE key=?) RETURNING *",
                (
                    row.name,
                    row.description,
                    row.directory,
                    row.client_directory,
                    row.instructions,
                    row.skills_json,
                    row.mcp_servers_json,
                    row.updated_at,
                    row.capabilities_json,
                    row.project_id,
                    row.owner_id,
                    expected_revision,
                    f"project_delete:{row.owner_id}:{row.project_id}",
                ),
            ).fetchone()
            if result is None:
                return None
            saved = ProjectRow.from_row(result)
            record_change(conn, saved.owner_id, "projects", saved.project_id, saved)
            return saved

    @staticmethod
    def set_archived(conn: Any, row: ProjectRow, archived: bool, now: int) -> ProjectRow | None:
        result = conn.execute(
            "UPDATE projects SET archived=?, updated_at=?, revision=revision+1 "
            "WHERE project_id=? AND owner_id=? AND revision=? RETURNING *",
            (int(archived), now, row.project_id, row.owner_id, row.revision),
        ).fetchone()
        if result is None:
            return None
        saved = ProjectRow.from_row(result)
        record_change(conn, saved.owner_id, "projects", saved.project_id, saved)
        return saved

    @staticmethod
    def delete(conn: Any, row: ProjectRow) -> None:
        conn.execute(
            "INSERT INTO settings(key,value) SELECT 'deleted_cron_binding:' || cron_id,'1' "
            "FROM cron_jobs WHERE user_id=? AND project_id=? ON CONFLICT(key) DO NOTHING",
            (row.owner_id, row.project_id),
        )
        conn.execute(
            "UPDATE cron_jobs SET enabled=0 WHERE user_id=? AND project_id=?",
            (row.owner_id, row.project_id),
        )
        conn.execute(
            "DELETE FROM project_create_requests WHERE project_id=? AND owner_id=?",
            (row.project_id, row.owner_id),
        )
        conn.execute(
            "DELETE FROM projects WHERE project_id=? AND owner_id=?",
            (row.project_id, row.owner_id),
        )
        record_change(conn, row.owner_id, "projects", row.project_id, row, deleted=True)
        ProjectRepo.clear_deleting(conn, row.owner_id, row.project_id)
