"""SQL-only immutable attachments. History queries never load content bytes."""

from __future__ import annotations

from dataclasses import dataclass
from string import ascii_lowercase, ascii_uppercase
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int
from octop.infra.utils.artifact_kinds import (
    ARTIFACT_KIND_RULES,
    artifact_business_category,
    ascii_lower,
)

_COLUMNS = (
    "artifact_id,task_id,turn_id,tool_call_id,request_hash,name,media_type,byte_count,"
    "sha256,source_path,created_at,business_category"
)
_LIBRARY_COLUMNS = ",".join(f"a.{column}" for column in _COLUMNS.split(",")) + (
    ",t.title AS task_title,t.archived AS task_archived,r.status AS turn_status,"
    "p.project_id,p.name AS project_name,p.archived AS project_archived,"
    "r.wait_kind AS turn_wait_kind,r.pause_requested AS turn_pause_requested,"
    "r.worker_id AS turn_worker_id,r.checkpoint_id AS turn_checkpoint_id"
)
_LIBRARY_FROM = (
    " FROM project_task_artifacts a "
    "JOIN project_tasks t ON t.task_id=a.task_id "
    "JOIN project_task_turns r ON r.turn_id=a.turn_id AND r.task_id=t.task_id "
    "LEFT JOIN projects p ON p.project_id=t.project_id AND p.owner_id=t.owner_id "
    "WHERE t.owner_id=?"
)


def _ascii_lower_sql(expression: str) -> str:
    # Both databases implement replace literally. Their lower()/LIKE Unicode
    # behavior differs, so fold exactly the same 26 letters as the Python rule.
    for upper, lower in zip(ascii_uppercase, ascii_lowercase, strict=True):
        expression = f"replace({expression},'{upper}','{lower}')"
    return expression


def _kind_sql() -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    for kind, extensions, _ in ARTIFACT_KIND_RULES:
        for length in sorted({len(extension) for extension in extensions}):
            suffixes = [extension for extension in extensions if len(extension) == length]
            suffix_sql = _ascii_lower_sql(f"substr(a.name,length(a.name)-{length}+1)")
            placeholders = ",".join("?" for _ in suffixes)
            clauses.append(f"WHEN {suffix_sql} IN ({placeholders}) THEN ?")
            params.extend((*suffixes, kind))
    media_type_sql = _ascii_lower_sql("a.media_type")
    for kind, _, media_types in ARTIFACT_KIND_RULES:
        placeholders = ",".join("?" for _ in media_types)
        clauses.append(f"WHEN {media_type_sql} IN ({placeholders}) THEN ?")
        params.extend((*media_types, kind))
    return "CASE " + " ".join(clauses) + " ELSE 'other' END", tuple(params)


_KIND_SQL, _KIND_PARAMS = _kind_sql()


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
    business_category: str = "other"

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
            str(row["business_category"]),
        )


@dataclass(frozen=True)
class ProjectTaskArtifactLibraryRow:
    artifact: ProjectTaskArtifactRow
    task_title: str
    task_archived: bool
    turn_status: str
    project_id: str | None
    project_name: str | None
    project_archived: bool | None
    turn_wait_kind: str = ""
    turn_pause_requested: bool = False
    turn_worker_id: str = ""
    turn_checkpoint_id: str = ""

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskArtifactLibraryRow:
        return cls(
            artifact=ProjectTaskArtifactRow.from_row(row),
            task_title=str(row["task_title"]),
            task_archived=bool(row["task_archived"]),
            turn_status=str(row["turn_status"]),
            project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            project_name=str(row["project_name"]) if row["project_name"] is not None else None,
            project_archived=bool(row["project_archived"])
            if row["project_archived"] is not None
            else None,
            turn_wait_kind=str(row["turn_wait_kind"]),
            turn_pause_requested=bool(row["turn_pause_requested"]),
            turn_worker_id=str(row["turn_worker_id"]),
            turn_checkpoint_id=str(row["turn_checkpoint_id"]),
        )


class ProjectTaskArtifactRepo:
    @staticmethod
    def library_page(
        conn: Any,
        owner_id: int,
        *,
        scope: str = "all",
        project_id: str | None = None,
        task_id: str | None = None,
        kind: str = "all",
        business_category: str = "all",
        archived: str = "all",
        q: str = "",
        limit: int = 51,
        boundary: tuple[int, str] | None = None,
    ) -> list[ProjectTaskArtifactLibraryRow]:
        clauses: list[str] = []
        params: list[Any] = [owner_id]
        if scope == "project":
            clauses.append("p.project_id=?")
            params.append(project_id)
        elif scope == "standalone":
            clauses.append("t.project_id IS NULL")
        if task_id is not None:
            clauses.append("a.task_id=?")
            params.append(task_id)
        if archived == "active":
            clauses.append("t.archived=0 AND COALESCE(p.archived,0)=0")
        elif archived == "archived":
            clauses.append("(t.archived=1 OR p.archived=1)")
        if kind != "all":
            clauses.append(f"({_KIND_SQL})=?")
            params.extend((*_KIND_PARAMS, kind))
        if business_category != "all":
            clauses.append("a.business_category=?")
            params.append(business_category)
        query = ascii_lower(q.strip())
        if query:
            clauses.append(_ascii_lower_sql("a.name") + " LIKE ? ESCAPE '!'")
            escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            params.append("%" + escaped + "%")
        if boundary is not None:
            clauses.append("(a.created_at,a.artifact_id)<(?,?)")
            params.extend(boundary)
        params.append(limit)
        where = " AND " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            "SELECT "
            + _LIBRARY_COLUMNS
            + _LIBRARY_FROM
            + where
            + " ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [ProjectTaskArtifactLibraryRow.from_row(row) for row in rows]

    @staticmethod
    def library_get(
        conn: Any, owner_id: int, artifact_id: str
    ) -> ProjectTaskArtifactLibraryRow | None:
        row = conn.execute(
            "SELECT " + _LIBRARY_COLUMNS + _LIBRARY_FROM + " AND a.artifact_id=?",
            (owner_id, artifact_id),
        ).fetchone()
        return ProjectTaskArtifactLibraryRow.from_row(row) if row else None

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
            f"INSERT INTO project_task_artifacts({_COLUMNS},content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                artifact_business_category(row.name),
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
