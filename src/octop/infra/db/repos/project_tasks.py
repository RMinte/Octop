"""SQL persistence for independent task identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.db.repos.project_sync import record_change, task_scope


@dataclass(frozen=True)
class ProjectTaskRow:
    task_id: str
    owner_id: int
    project_id: str | None
    title: str
    request_id: str
    active_turn_id: str | None
    created_at: int
    updated_at: int
    list_revision: int = 1
    archived: bool = False

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskRow:
        return cls(
            str(row["task_id"]),
            strict_int(row["owner_id"]),
            str(row["project_id"]) if row["project_id"] is not None else None,
            str(row["title"]),
            str(row["request_id"]),
            str(row["active_turn_id"]) if row["active_turn_id"] is not None else None,
            strict_int(row["created_at"]),
            strict_int(row["updated_at"]),
            strict_int(row["list_revision"]),
            bool(row["archived"]),
        )


class ProjectTaskRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @staticmethod
    def page(
        conn: Any,
        owner_id: int,
        project_id: str | None,
        limit: int,
        boundary: tuple[int, str] | None = None,
        archived: bool = False,
    ) -> list[ProjectTaskRow]:
        params: list[Any] = [owner_id]
        scope = "project_id IS NULL"
        if project_id is not None:
            scope = "project_id=?"
            params.append(project_id)
        scope += " AND archived=?"
        params.append(int(archived))
        position = ""
        if boundary is not None:
            position = " AND (updated_at, task_id)<(?, ?)"
            params.extend(boundary)
        params.append(limit)
        rows = conn.execute(
            "SELECT * FROM project_tasks WHERE owner_id=? AND "
            + scope
            + position
            + " ORDER BY updated_at DESC, task_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [ProjectTaskRow.from_row(row) for row in rows]

    def get(self, task_id: str, owner_id: int) -> ProjectTaskRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, task_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, task_id: str, owner_id: int) -> ProjectTaskRow | None:
        row = conn.execute(
            "SELECT * FROM project_tasks WHERE task_id=? AND owner_id=?", (task_id, owner_id)
        ).fetchone()
        return ProjectTaskRow.from_row(row) if row else None

    def lock_owner(self, conn: Any, owner_id: int) -> bool:
        suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        return bool(
            conn.execute(
                "SELECT id FROM users WHERE id=? AND disabled=0" + suffix, (owner_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def by_request(conn: Any, owner_id: int, request_id: str) -> ProjectTaskRow | None:
        row = conn.execute(
            "SELECT * FROM project_tasks WHERE owner_id=? AND request_id=?", (owner_id, request_id)
        ).fetchone()
        return ProjectTaskRow.from_row(row) if row else None

    @staticmethod
    def insert(conn: Any, row: ProjectTaskRow) -> None:
        conn.execute(
            "INSERT INTO project_tasks(task_id, owner_id, project_id, title, request_id, "
            "active_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.task_id,
                row.owner_id,
                row.project_id,
                row.title,
                row.request_id,
                row.active_turn_id,
                row.created_at,
                row.updated_at,
            ),
        )
        record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)

    @staticmethod
    def activate(conn: Any, task_id: str, turn_id: str, now: int) -> bool:
        result = conn.execute(
            "UPDATE project_tasks SET active_turn_id=?, updated_at=?, list_revision=list_revision+1 "
            "WHERE task_id=? AND active_turn_id IS NULL RETURNING *",
            (turn_id, now, task_id),
        ).fetchone()
        if result is None:
            return False
        row = ProjectTaskRow.from_row(result)
        record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)
        return True

    @staticmethod
    def finish(conn: Any, task_id: str, turn_id: str, now: int) -> None:
        result = conn.execute(
            "UPDATE project_tasks SET active_turn_id=NULL, updated_at=?, list_revision=list_revision+1 "
            "WHERE task_id=? AND active_turn_id=? RETURNING *",
            (now, task_id, turn_id),
        ).fetchone()
        if result is not None:
            row = ProjectTaskRow.from_row(result)
            record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)

    @staticmethod
    def update_metadata(
        conn: Any, row: ProjectTaskRow, expected_revision: int
    ) -> ProjectTaskRow | None:
        result = conn.execute(
            "UPDATE project_tasks SET title=?, archived=?, updated_at=?, list_revision=list_revision+1 "
            "WHERE task_id=? AND owner_id=? AND list_revision=? RETURNING *",
            (
                row.title,
                int(row.archived),
                row.updated_at,
                row.task_id,
                row.owner_id,
                expected_revision,
            ),
        ).fetchone()
        if result is None:
            return None
        updated = ProjectTaskRow.from_row(result)
        record_change(
            conn, updated.owner_id, task_scope(updated.project_id), updated.task_id, updated
        )
        return updated

    def runtime_owner(self, task_id: str) -> int | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM project_tasks t JOIN users u ON u.id=t.owner_id "
                "WHERE task_id=? AND u.disabled=0",
                (task_id,),
            ).fetchone()
        return strict_int(row["owner_id"]) if row else None
