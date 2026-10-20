"""SQL-only private memory capture policy and optimistic revision updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import strict_int


@dataclass(frozen=True)
class ResidentMemoryPolicyRow:
    capture_enabled: bool
    revision: int


class ResidentMemoryPolicyRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, owner_id: int, thread_id: str) -> ResidentMemoryPolicyRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT memory_capture_enabled,memory_revision FROM resident_conversations "
                "WHERE owner_id=? AND thread_id=?",
                (owner_id, thread_id),
            ).fetchone()
        return (
            ResidentMemoryPolicyRow(
                bool(row["memory_capture_enabled"]), strict_int(row["memory_revision"])
            )
            if row is not None
            else None
        )

    def update(
        self, owner_id: int, thread_id: str, expected_revision: int, capture_enabled: bool
    ) -> ResidentMemoryPolicyRow | None:
        with self._db.transaction() as conn:
            return self.update_in_transaction(
                conn, owner_id, thread_id, expected_revision, capture_enabled
            )

    @staticmethod
    def update_in_transaction(
        conn: Any, owner_id: int, thread_id: str, expected_revision: int, capture_enabled: bool
    ) -> ResidentMemoryPolicyRow | None:
        changed = conn.execute(
            "UPDATE resident_conversations SET memory_capture_enabled=?,"
            "memory_revision=memory_revision+1 WHERE owner_id=? AND thread_id=? "
            "AND memory_revision=?",
            (int(capture_enabled), owner_id, thread_id, expected_revision),
        ).rowcount
        return ResidentMemoryPolicyRow(capture_enabled, expected_revision + 1) if changed else None
