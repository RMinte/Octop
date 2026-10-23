"""One-time legacy thread import receipts; the source remains independent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class LegacyConversationSourceRow:
    task_id: str
    owner_id: int
    request_id: str
    fingerprint: str
    source_agent_id: str
    source_thread_id: str
    source_digest: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> LegacyConversationSourceRow:
        return cls(
            task_id=str(row["task_id"]),
            owner_id=strict_int(row["owner_id"]),
            request_id=str(row["request_id"]),
            fingerprint=str(row["fingerprint"]),
            source_agent_id=str(row["source_agent_id"]),
            source_thread_id=str(row["source_thread_id"]),
            source_digest=str(row["source_digest"]),
            created_at=strict_int(row["created_at"]),
        )


class LegacyConversationSourceRepo:
    @staticmethod
    def by_request(conn: Any, owner_id: int, request_id: str) -> LegacyConversationSourceRow | None:
        row = conn.execute(
            "SELECT * FROM project_task_legacy_sources WHERE owner_id=? AND request_id=?",
            (owner_id, request_id),
        ).fetchone()
        return LegacyConversationSourceRow.from_row(row) if row is not None else None

    @staticmethod
    def origin(conn: Any, task_id: str) -> LegacyConversationSourceRow | None:
        row = conn.execute(
            "SELECT * FROM project_task_legacy_sources WHERE task_id=?", (task_id,)
        ).fetchone()
        return LegacyConversationSourceRow.from_row(row) if row is not None else None

    @staticmethod
    def insert(conn: Any, row: LegacyConversationSourceRow) -> None:
        conn.execute(
            "INSERT INTO project_task_legacy_sources(task_id,owner_id,request_id,fingerprint,"
            "source_agent_id,source_thread_id,source_digest,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                row.task_id,
                row.owner_id,
                row.request_id,
                row.fingerprint,
                row.source_agent_id,
                row.source_thread_id,
                row.source_digest,
                row.created_at,
            ),
        )
