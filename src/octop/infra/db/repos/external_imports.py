"""SQL-only identity mappings and inert configuration snapshots for external imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ImportedSessionRow:
    owner_id: int
    source: str
    external_session_id: str
    task_id: str
    fingerprint: str
    manifest_json: str


@dataclass(frozen=True)
class ImportedConfigurationRow:
    configuration_id: str
    owner_id: int
    source: str
    category: str
    name: str
    source_path: str | None
    digest: str
    byte_count: int
    file_count: int
    created_at: int
    files_json: str = ""

    @classmethod
    def from_row(cls, row: DbRow, *, detail: bool = False) -> ImportedConfigurationRow:
        return cls(
            configuration_id=str(row["configuration_id"]),
            owner_id=strict_int(row["owner_id"]),
            source=str(row["source"]),
            category=str(row["category"]),
            name=str(row["name"]),
            source_path=str(row["source_path"]) if row["source_path"] is not None else None,
            digest=str(row["digest"]),
            byte_count=strict_int(row["byte_count"]),
            file_count=strict_int(row["file_count"]),
            created_at=strict_int(row["created_at"]),
            files_json=str(row["files_json"]) if detail else "",
        )


_CONFIGURATION_COLUMNS = "configuration_id,owner_id,source,category,name,source_path,digest,byte_count,file_count,created_at"


class ExternalImportRepo:
    @staticmethod
    def project(conn: Any, owner_id: int, source: str, external_project_id: str) -> str | None:
        row = conn.execute(
            "SELECT project_id FROM external_import_projects "
            "WHERE owner_id=? AND source=? AND external_project_id=?",
            (owner_id, source, external_project_id),
        ).fetchone()
        return str(row["project_id"]) if row is not None else None

    @staticmethod
    def save_project(
        conn: Any, owner_id: int, source: str, external_project_id: str, project_id: str
    ) -> None:
        conn.execute(
            "INSERT INTO external_import_projects(owner_id,source,external_project_id,project_id) "
            "VALUES (?,?,?,?)",
            (owner_id, source, external_project_id, project_id),
        )

    @staticmethod
    def session(
        conn: Any, owner_id: int, source: str, external_session_id: str
    ) -> ImportedSessionRow | None:
        row = conn.execute(
            "SELECT * FROM external_import_sessions "
            "WHERE owner_id=? AND source=? AND external_session_id=?",
            (owner_id, source, external_session_id),
        ).fetchone()
        if row is None:
            return None
        return ImportedSessionRow(
            owner_id=strict_int(row["owner_id"]),
            source=str(row["source"]),
            external_session_id=str(row["external_session_id"]),
            task_id=str(row["task_id"]),
            fingerprint=str(row["fingerprint"]),
            manifest_json=str(row["manifest_json"]),
        )

    @staticmethod
    def save_session(conn: Any, row: ImportedSessionRow) -> None:
        conn.execute(
            "INSERT INTO external_import_sessions(owner_id,source,external_session_id,task_id,"
            "fingerprint,manifest_json) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(owner_id,source,external_session_id) DO UPDATE SET "
            "task_id=excluded.task_id,fingerprint=excluded.fingerprint,manifest_json=excluded.manifest_json",
            (
                row.owner_id,
                row.source,
                row.external_session_id,
                row.task_id,
                row.fingerprint,
                row.manifest_json,
            ),
        )

    @staticmethod
    def configuration_by_digest(
        conn: Any, owner_id: int, source: str, digest: str
    ) -> ImportedConfigurationRow | None:
        row = conn.execute(
            f"SELECT {_CONFIGURATION_COLUMNS} FROM external_import_configurations "
            "WHERE owner_id=? AND source=? AND digest=?",
            (owner_id, source, digest),
        ).fetchone()
        return ImportedConfigurationRow.from_row(row) if row is not None else None

    @staticmethod
    def save_configuration(conn: Any, row: ImportedConfigurationRow) -> None:
        conn.execute(
            "INSERT INTO external_import_configurations(configuration_id,owner_id,source,category,"
            "name,source_path,digest,byte_count,file_count,created_at,files_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.configuration_id,
                row.owner_id,
                row.source,
                row.category,
                row.name,
                row.source_path,
                row.digest,
                row.byte_count,
                row.file_count,
                row.created_at,
                row.files_json,
            ),
        )

    @staticmethod
    def configurations(
        conn: Any,
        owner_id: int,
        *,
        limit: int,
        offset: int,
        source: str | None = None,
        category: str | None = None,
    ) -> list[ImportedConfigurationRow]:
        where = "owner_id=?"
        params: list[object] = [owner_id]
        if source is not None:
            where += " AND source=?"
            params.append(source)
        if category is not None:
            where += " AND category=?"
            params.append(category)
        rows = conn.execute(
            f"SELECT {_CONFIGURATION_COLUMNS} FROM external_import_configurations "
            f"WHERE {where} ORDER BY created_at DESC,configuration_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [ImportedConfigurationRow.from_row(row) for row in rows]

    @staticmethod
    def configuration(
        conn: Any, owner_id: int, configuration_id: str
    ) -> ImportedConfigurationRow | None:
        row = conn.execute(
            "SELECT * FROM external_import_configurations WHERE owner_id=? AND configuration_id=?",
            (owner_id, configuration_id),
        ).fetchone()
        return ImportedConfigurationRow.from_row(row, detail=True) if row is not None else None
