"""SQL for the immutable default private Agent/thread binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, strict_int


@dataclass(frozen=True)
class ResidentConversationRow:
    owner_id: int
    role: str
    agent_id: str
    thread_id: str
    status: str
    error_code: str
    revision: int
    created_at: int
    updated_at: int
    web_search_enabled: bool = True
    configuration_revision: int = 1

    @classmethod
    def from_row(cls, row: DbRow) -> ResidentConversationRow:
        return cls(
            owner_id=strict_int(row["owner_id"]),
            role=str(row["role"]),
            agent_id=str(row["agent_id"]),
            thread_id=str(row["thread_id"]),
            status=str(row["status"]),
            error_code=str(row["error_code"]),
            revision=strict_int(row["revision"]),
            created_at=strict_int(row["created_at"]),
            updated_at=strict_int(row["updated_at"]),
            web_search_enabled=bool(row["web_search_enabled"]),
            configuration_revision=strict_int(row["configuration_revision"]),
        )


class ResidentConversationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, owner_id: int) -> ResidentConversationRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, owner_id)

    @staticmethod
    def get_in_transaction(
        conn: Any, owner_id: int, role: str = "primary"
    ) -> ResidentConversationRow | None:
        row = conn.execute(
            "SELECT * FROM resident_conversations WHERE owner_id=? AND role=?",
            (owner_id, role),
        ).fetchone()
        return ResidentConversationRow.from_row(row) if row else None

    @staticmethod
    def get_by_thread_in_transaction(
        conn: Any, owner_id: int, thread_id: str
    ) -> ResidentConversationRow | None:
        row = conn.execute(
            "SELECT * FROM resident_conversations WHERE owner_id=? AND thread_id=?",
            (owner_id, thread_id),
        ).fetchone()
        return ResidentConversationRow.from_row(row) if row else None

    @staticmethod
    def update_settings_in_transaction(
        conn: Any,
        owner_id: int,
        agent_id: str,
        thread_id: str,
        expected_revision: int,
        web_search_enabled: bool,
        now: int,
    ) -> ResidentConversationRow | None:
        changed = conn.execute(
            "UPDATE resident_conversations SET web_search_enabled=?,"
            "configuration_revision=configuration_revision+1,revision=revision+1,updated_at=? "
            "WHERE owner_id=? AND agent_id=? AND thread_id=? AND status='ready' "
            "AND configuration_revision=? AND web_search_enabled<>?",
            (
                int(web_search_enabled),
                now,
                owner_id,
                agent_id,
                thread_id,
                expected_revision,
                int(web_search_enabled),
            ),
        ).rowcount
        row = ResidentConversationRepo.get_by_thread_in_transaction(conn, owner_id, thread_id)
        if (
            row is not None
            and row.agent_id == agent_id
            and row.status == "ready"
            and row.configuration_revision == expected_revision + int(bool(changed))
            and row.web_search_enabled == web_search_enabled
        ):
            return row
        return None

    def get_by_agent(self, agent_id: str) -> ResidentConversationRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM resident_conversations WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return ResidentConversationRow.from_row(row) if row else None

    def get_by_thread(self, thread_id: str) -> ResidentConversationRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM resident_conversations WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return ResidentConversationRow.from_row(row) if row else None

    @staticmethod
    def reserve_in_transaction(
        conn: Any,
        owner_id: int,
        agent_id: str,
        thread_id: str,
        now: int,
        role: str = "primary",
    ) -> bool:
        return bool(
            conn.execute(
                "INSERT INTO resident_conversations(owner_id,role,agent_id,thread_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,role) DO NOTHING",
                (owner_id, role, agent_id, thread_id, now, now),
            ).rowcount
        )

    def finish_initialization(
        self,
        owner_id: int,
        status: str,
        error_code: str,
        now: int,
        role: str = "primary",
    ) -> ResidentConversationRow:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE resident_conversations SET status=?,error_code=?,revision=revision+1,"
                "updated_at=? WHERE owner_id=? AND role=? AND status<>'ready'",
                (status, error_code, now, owner_id, role),
            )
            row = self.get_in_transaction(conn, owner_id, role)
            assert row is not None
            return row
