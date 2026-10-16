"""SQL-only immutable attachments. History queries never load content bytes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int

_COLUMNS = "artifact_id,task_id,turn_id,tool_call_id,request_hash,name,media_type,byte_count,sha256,source_path,created_at"


@dataclass(frozen=True)
class ProjectTaskArtifactRow:
    artifact_id: str
    task_id: str
    turn_id: str
    tool_call_id: str
    request_hash: str
    name: str
    media_type: str
    byte_count: int
    sha256: str
    source_path: str | None
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskArtifactRow:
        return cls(
            str(row["artifact_id"]),
            str(row["task_id"]),
            str(row["turn_id"]),
            str(row["tool_call_id"]),
            str(row["request_hash"]),
            str(row["name"]),
            str(row["media_type"]),
            strict_int(row["byte_count"]),
            str(row["sha256"]),
            str(row["source_path"]) if row["source_path"] is not None else None,
            strict_int(row["created_at"]),
        )


class ProjectTaskArtifactRepo:
    @staticmethod
    def for_turns(conn: Any, task_id: str, turn_ids: list[str]) -> list[ProjectTaskArtifactRow]:
        if not turn_ids:
            return []
        placeholders = ",".join("?" for _ in turn_ids)
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM project_task_artifacts WHERE task_id=? "
            f"AND turn_id IN ({placeholders}) ORDER BY id",
            (task_id, *turn_ids),
        ).fetchall()
        return [ProjectTaskArtifactRow.from_row(row) for row in rows]

    @staticmethod
    def by_call(conn: Any, turn_id: str, tool_call_id: str) -> ProjectTaskArtifactRow | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM project_task_artifacts WHERE turn_id=? AND tool_call_id=?",
            (turn_id, tool_call_id),
        ).fetchone()
        return ProjectTaskArtifactRow.from_row(row) if row else None

    @staticmethod
    def usage(conn: Any, turn_id: str) -> tuple[int, int]:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(byte_count),0) AS size "
            "FROM project_task_artifacts WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        return strict_int(row["n"]), strict_int(row["size"])

    @staticmethod
    def insert(conn: Any, row: ProjectTaskArtifactRow, content: bytes) -> None:
        conn.execute(
            f"INSERT INTO project_task_artifacts({_COLUMNS},content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.artifact_id,
                row.task_id,
                row.turn_id,
                row.tool_call_id,
                row.request_hash,
                row.name,
                row.media_type,
                row.byte_count,
                row.sha256,
                row.source_path,
                row.created_at,
                content,
            ),
        )

    @staticmethod
    def download(
        conn: Any, task_id: str, artifact_id: str
    ) -> tuple[ProjectTaskArtifactRow, bytes] | None:
        row = conn.execute(
            f"SELECT {_COLUMNS},content FROM project_task_artifacts WHERE task_id=? AND artifact_id=?",
            (task_id, artifact_id),
        ).fetchone()
        return (ProjectTaskArtifactRow.from_row(row), bytes(row["content"])) if row else None
