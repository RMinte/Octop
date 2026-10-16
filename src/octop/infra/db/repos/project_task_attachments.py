"""SQL-only storage for immutable task file inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int

_COLUMNS = "attachment_id,owner_id,task_id,name,media_type,byte_count,sha256,created_at"


@dataclass(frozen=True)
class ProjectTaskAttachmentRow:
    attachment_id: str
    owner_id: int
    task_id: str | None
    name: str
    media_type: str
    byte_count: int
    sha256: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskAttachmentRow:
        return cls(
            str(row["attachment_id"]),
            strict_int(row["owner_id"]),
            str(row["task_id"]) if row["task_id"] is not None else None,
            str(row["name"]),
            str(row["media_type"]),
            strict_int(row["byte_count"]),
            str(row["sha256"]),
            strict_int(row["created_at"]),
        )


class ProjectTaskAttachmentRepo:
    @staticmethod
    def insert(conn: Any, row: ProjectTaskAttachmentRow, content: bytes) -> None:
        conn.execute(
            f"INSERT INTO project_task_attachments({_COLUMNS},content) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                row.attachment_id,
                row.owner_id,
                row.task_id,
                row.name,
                row.media_type,
                row.byte_count,
                row.sha256,
                row.created_at,
                content,
            ),
        )

    @staticmethod
    def get(conn: Any, owner_id: int, attachment_id: str) -> ProjectTaskAttachmentRow | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM project_task_attachments WHERE owner_id=? AND attachment_id=?",
            (owner_id, attachment_id),
        ).fetchone()
        return ProjectTaskAttachmentRow.from_row(row) if row else None

    @staticmethod
    def download(
        conn: Any, owner_id: int, attachment_id: str
    ) -> tuple[ProjectTaskAttachmentRow, bytes] | None:
        row = conn.execute(
            f"SELECT {_COLUMNS},content FROM project_task_attachments "
            "WHERE owner_id=? AND attachment_id=?",
            (owner_id, attachment_id),
        ).fetchone()
        return (ProjectTaskAttachmentRow.from_row(row), bytes(row["content"])) if row else None

    @staticmethod
    def bind(conn: Any, owner_id: int, task_id: str, attachment_id: str) -> None:
        conn.execute(
            "UPDATE project_task_attachments SET task_id=? "
            "WHERE owner_id=? AND attachment_id=? AND task_id IS NULL",
            (task_id, owner_id, attachment_id),
        )

    @staticmethod
    def has_for_task(conn: Any, owner_id: int, task_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM project_task_attachments WHERE owner_id=? AND task_id=? LIMIT 1",
                (owner_id, task_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def for_task(conn: Any, owner_id: int, task_id: str) -> list[ProjectTaskAttachmentRow]:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM project_task_attachments "
            "WHERE owner_id=? AND task_id=? ORDER BY id",
            (owner_id, task_id),
        ).fetchall()
        return [ProjectTaskAttachmentRow.from_row(row) for row in rows]

    @staticmethod
    def delete_expired(conn: Any, owner_id: int, before: int) -> None:
        conn.execute(
            "DELETE FROM project_task_attachments "
            "WHERE owner_id=? AND task_id IS NULL AND created_at<=?",
            (owner_id, before),
        )
