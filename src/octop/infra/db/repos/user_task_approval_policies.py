"""SQL-only persistence for each account's task approval policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class UserTaskApprovalPolicyRow:
    owner_id: int
    mode: str
    revision: int
    updated_at: int
    requests_json: str

    @classmethod
    def from_row(cls, row: DbRow) -> UserTaskApprovalPolicyRow:
        return cls(
            strict_int(row["owner_id"]),
            str(row["mode"]),
            strict_int(row["revision"]),
            strict_int(row["updated_at"]),
            str(row["requests_json"]),
        )


class UserTaskApprovalPolicyRepo:
    @staticmethod
    def get(conn: Any, owner_id: int) -> UserTaskApprovalPolicyRow | None:
        # Accounts created after the migration have the same durable default until first write.
        row = conn.execute(
            "SELECT u.id AS owner_id, COALESCE(p.mode, 'ask_for_approval') AS mode, "
            "COALESCE(p.revision, 1) AS revision, "
            "COALESCE(p.updated_at, u.created_at * 1000) AS updated_at, "
            "COALESCE(p.requests_json, '[]') AS requests_json "
            "FROM users u LEFT JOIN user_task_approval_policies p ON p.owner_id=u.id "
            "WHERE u.id=? AND u.disabled=0",
            (owner_id,),
        ).fetchone()
        return UserTaskApprovalPolicyRow.from_row(row) if row else None

    @staticmethod
    def save(conn: Any, row: UserTaskApprovalPolicyRow) -> None:
        conn.execute(
            "INSERT INTO user_task_approval_policies(owner_id,mode,revision,updated_at,requests_json) "
            "VALUES (?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
            "mode=excluded.mode,revision=excluded.revision,updated_at=excluded.updated_at,"
            "requests_json=excluded.requests_json",
            (row.owner_id, row.mode, row.revision, row.updated_at, row.requests_json),
        )
