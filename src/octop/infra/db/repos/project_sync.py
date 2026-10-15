"""SQL-only ordered change journals for project and task list projections."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from octop.infra.db.repos._base import strict_int


def task_scope(project_id: str | None) -> str:
    return "tasks:" + (project_id or "")


def record_change(conn: Any, owner_id: int, scope: str, entity_id: str, row: Any) -> None:
    # UPDATE takes the stream lock until commit, so sequence order is commit order.
    conn.execute(
        "INSERT INTO project_sync_scopes(owner_id, scope) VALUES (?, ?) "
        "ON CONFLICT(owner_id, scope) DO NOTHING",
        (owner_id, scope),
    )
    head = conn.execute(
        "UPDATE project_sync_scopes SET sequence=sequence+1 "
        "WHERE owner_id=? AND scope=? RETURNING sequence",
        (owner_id, scope),
    ).fetchone()
    assert head is not None
    conn.execute(
        "INSERT INTO project_sync_changes(owner_id, scope, sequence, entity_id, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (owner_id, scope, head["sequence"], entity_id, json.dumps(asdict(row))),
    )


@dataclass(frozen=True)
class ListChange:
    entity_id: str
    payload_json: str


class ProjectSyncRepo:
    @staticmethod
    def head(conn: Any, owner_id: int, scope: str) -> int:
        row = conn.execute(
            "SELECT sequence FROM project_sync_scopes WHERE owner_id=? AND scope=?",
            (owner_id, scope),
        ).fetchone()
        return strict_int(row["sequence"]) if row else 0

    @staticmethod
    def changes(
        conn: Any, owner_id: int, scope: str, after: int, cutoff: int, last_id: str, limit: int
    ) -> list[ListChange]:
        rows = conn.execute(
            "SELECT c.entity_id, c.payload_json FROM project_sync_changes c JOIN ("
            "SELECT entity_id, MAX(sequence) AS sequence FROM project_sync_changes "
            "WHERE owner_id=? AND scope=? AND sequence>? AND sequence<=? GROUP BY entity_id"
            ") latest ON c.entity_id=latest.entity_id AND c.sequence=latest.sequence "
            "WHERE c.owner_id=? AND c.scope=? AND c.entity_id>? ORDER BY c.entity_id LIMIT ?",
            (owner_id, scope, after, cutoff, owner_id, scope, last_id, limit),
        ).fetchall()
        return [ListChange(str(row["entity_id"]), str(row["payload_json"])) for row in rows]
