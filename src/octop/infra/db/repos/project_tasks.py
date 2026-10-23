"""SQL persistence for independent task identities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import UNSET, DbRow, strict_int
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
    status: str = "queued"
    wait_kind: str = ""
    approval_mode: str = "ask_for_approval"
    approval_revision: int = 1
    approval_updated_at: int = 0
    approval_requests_json: str = "[]"
    web_search_enabled: bool = True
    import_source: str = ""
    external_session_id: str = ""
    selected_expert_id: str | None = None
    configuration_revision: int = 1
    action_mode: str = "craft"
    pinned: bool = False
    internal_kind: str = ""

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
            str(row["status"]),
            str(row["wait_kind"]),
            str(row["approval_mode"]),
            strict_int(row["approval_revision"]),
            strict_int(row["approval_updated_at"]),
            str(row["approval_requests_json"]),
            bool(row["web_search_enabled"]),
            str(row["import_source"]),
            str(row["external_session_id"]),
            str(row["selected_expert_id"]) if row["selected_expert_id"] is not None else None,
            strict_int(row["configuration_revision"]),
            str(row["action_mode"]),
            bool(row["pinned"]),
            str(row["internal_kind"]),
        )


@dataclass(frozen=True)
class ProjectTaskConfigurationEventRow:
    configuration_revision: int
    previous_expert_id: str | None
    selected_expert_id: str | None
    selected_expert_name: str
    previous_action_mode: str
    action_mode: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> ProjectTaskConfigurationEventRow:
        return cls(
            configuration_revision=strict_int(row["configuration_revision"]),
            previous_expert_id=(
                str(row["previous_expert_id"]) if row["previous_expert_id"] is not None else None
            ),
            selected_expert_id=(
                str(row["selected_expert_id"]) if row["selected_expert_id"] is not None else None
            ),
            selected_expert_name=str(row["selected_expert_name"]),
            previous_action_mode=str(row["previous_action_mode"]),
            action_mode=str(row["action_mode"]),
            created_at=strict_int(row["created_at"]),
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
        boundary: tuple[int, str] | tuple[int, int, str] | None = None,
        archived: bool = False,
        *,
        scope: str | None = None,
        selected_expert_id: str | None | object = UNSET,
        status: str | None = None,
        query: str | None = None,
    ) -> list[ProjectTaskRow]:
        params: list[Any] = [owner_id]
        condition = "project_id IS NULL"
        if scope == "all":
            condition = "1=1"
        elif project_id is not None:
            condition = "project_id=?"
            params.append(project_id)
        condition += " AND internal_kind='' AND archived=?"
        params.append(int(archived))
        if selected_expert_id is None:
            condition += " AND selected_expert_id IS NULL"
        elif selected_expert_id is not UNSET:
            condition += " AND selected_expert_id=?"
            params.append(selected_expert_id)
        if status is not None:
            condition += (
                " AND (CASE WHEN status='running' THEN CASE wait_kind "
                "WHEN 'approval' THEN 'waiting_approval' WHEN 'answer' THEN 'waiting_answer' "
                "WHEN 'client_tool' THEN 'waiting_client_tool' WHEN 'user' THEN 'paused' "
                "WHEN 'interrupted' THEN 'interrupted' ELSE status END ELSE status END)=?"
            )
            params.append(status)
        if query:
            condition += " AND LOWER(title) LIKE LOWER(?) ESCAPE '!'"
            escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            params.append("%" + escaped + "%")
        position = ""
        if boundary is not None:
            if len(boundary) == 3:
                position = " AND (pinned, updated_at, task_id)<(?, ?, ?)"
                params.extend(boundary)
            else:
                position = (
                    " AND (pinned, updated_at, task_id)<(COALESCE((SELECT pinned "
                    "FROM project_tasks WHERE task_id=? AND owner_id=?),0), ?, ?)"
                )
                params.extend((boundary[1], owner_id, *boundary))
        params.append(limit)
        rows = conn.execute(
            "SELECT * FROM project_tasks WHERE owner_id=? AND "
            + condition
            + position
            + " ORDER BY pinned DESC, updated_at DESC, task_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [ProjectTaskRow.from_row(row) for row in rows]

    def get(self, task_id: str, owner_id: int) -> ProjectTaskRow | None:
        with self._db.connect() as conn:
            return self.get_in_transaction(conn, task_id, owner_id)

    @staticmethod
    def get_in_transaction(conn: Any, task_id: str, owner_id: int) -> ProjectTaskRow | None:
        row = conn.execute(
            "SELECT * FROM project_tasks WHERE task_id=? AND owner_id=? AND internal_kind=''",
            (task_id, owner_id),
        ).fetchone()
        return ProjectTaskRow.from_row(row) if row else None

    def get_internal(self, task_id: str, owner_id: int) -> ProjectTaskRow | None:
        with self._db.connect() as conn:
            return self.get_internal_in_transaction(conn, task_id, owner_id)

    @staticmethod
    def get_internal_in_transaction(
        conn: Any, task_id: str, owner_id: int
    ) -> ProjectTaskRow | None:
        row = conn.execute(
            "SELECT * FROM project_tasks WHERE task_id=? AND owner_id=?", (task_id, owner_id)
        ).fetchone()
        return ProjectTaskRow.from_row(row) if row else None

    @staticmethod
    def for_project(conn: Any, project_id: str, owner_id: int) -> list[ProjectTaskRow]:
        rows = conn.execute(
            "SELECT * FROM project_tasks WHERE project_id=? AND owner_id=? ORDER BY task_id",
            (project_id, owner_id),
        ).fetchall()
        return [ProjectTaskRow.from_row(row) for row in rows]

    @staticmethod
    def delete(conn: Any, row: ProjectTaskRow) -> None:
        conn.execute(
            "UPDATE unified_source_inbox SET payload_json='{}',status='target_deleted' WHERE task_id=?",
            (row.task_id,),
        )
        for table in (
            "unified_source_deliveries",
            "project_task_execution_receipts",
            "project_task_artifacts",
            "project_task_tool_calls",
            "project_task_contexts",
            "project_task_titles",
            "project_task_configuration_events",
            "project_task_branches",
            "project_task_legacy_sources",
            "project_task_turns",
        ):
            conn.execute(f"DELETE FROM {table} WHERE task_id=?", (row.task_id,))
        conn.execute(
            "DELETE FROM project_tasks WHERE task_id=? AND owner_id=?",
            (row.task_id, row.owner_id),
        )
        record_change(
            conn,
            row.owner_id,
            task_scope(row.project_id),
            row.task_id,
            replace(row, list_revision=row.list_revision + 1),
            deleted=True,
        )

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
            "SELECT * FROM project_tasks WHERE owner_id=? AND request_id=? AND internal_kind=''",
            (owner_id, request_id),
        ).fetchone()
        return ProjectTaskRow.from_row(row) if row else None

    @staticmethod
    def insert(conn: Any, row: ProjectTaskRow) -> None:
        conn.execute(
            "INSERT INTO project_tasks(task_id, owner_id, project_id, title, request_id, "
            "active_turn_id, created_at, updated_at, status, title_source, "
            "approval_mode, approval_revision, approval_updated_at, web_search_enabled, "
            "import_source, external_session_id, selected_expert_id, configuration_revision, "
            "action_mode, pinned, archived, internal_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisional', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.task_id,
                row.owner_id,
                row.project_id,
                row.title,
                row.request_id,
                row.active_turn_id,
                row.created_at,
                row.updated_at,
                row.status,
                row.approval_mode,
                row.approval_revision,
                row.approval_updated_at,
                int(row.web_search_enabled),
                row.import_source,
                row.external_session_id,
                row.selected_expert_id,
                row.configuration_revision,
                row.action_mode,
                int(row.pinned),
                int(row.archived),
                row.internal_kind,
            ),
        )
        record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)

    @staticmethod
    def refresh_import(
        conn: Any, row: ProjectTaskRow, title: str, updated_at: int, *, content_changed: bool
    ) -> None:
        result = conn.execute(
            "UPDATE project_tasks SET title=CASE WHEN title_source IN ('provisional','imported') "
            "THEN ? ELSE title END, title_source=CASE WHEN title_source='provisional' "
            "THEN 'imported' ELSE title_source END, updated_at=?, list_revision=list_revision+1 "
            "WHERE task_id=? AND owner_id=? AND (?=1 OR title_source='provisional' "
            "OR (title_source='imported' AND title<>?)) RETURNING *",
            (title, updated_at, row.task_id, row.owner_id, int(content_changed), title),
        ).fetchone()
        if result is None:
            return
        saved = ProjectTaskRow.from_row(result)
        record_change(conn, saved.owner_id, task_scope(saved.project_id), saved.task_id, saved)

    @staticmethod
    def approval_changed(conn: Any, owner_id: int) -> None:
        rows = conn.execute(
            "UPDATE project_tasks SET list_revision=list_revision+1 WHERE owner_id=? RETURNING *",
            (owner_id,),
        ).fetchall()
        for result in rows:
            row = ProjectTaskRow.from_row(result)
            record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)

    @staticmethod
    def activate(conn: Any, task_id: str, turn_id: str, now: int) -> bool:
        result = conn.execute(
            "UPDATE project_tasks SET active_turn_id=?, status='queued', updated_at=?, "
            "list_revision=list_revision+1 "
            "WHERE task_id=? AND active_turn_id IS NULL RETURNING *",
            (turn_id, now, task_id),
        ).fetchone()
        if result is None:
            return False
        row = ProjectTaskRow.from_row(result)
        record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)
        return True

    @staticmethod
    def start(conn: Any, task_id: str, turn_id: str, now: int) -> None:
        result = conn.execute(
            "UPDATE project_tasks SET status='running',wait_kind='', updated_at=?, list_revision=list_revision+1 "
            "WHERE task_id=? AND active_turn_id=? AND (status='queued' OR wait_kind<>'') RETURNING *",
            (now, task_id, turn_id),
        ).fetchone()
        if result is not None:
            row = ProjectTaskRow.from_row(result)
            record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)

    @staticmethod
    def finish(conn: Any, task_id: str, turn_id: str, now: int) -> None:
        result = conn.execute(
            "UPDATE project_tasks SET status=(SELECT status FROM project_task_turns WHERE turn_id=?), "
            "active_turn_id=NULL,wait_kind='', updated_at=?, list_revision=list_revision+1 "
            "WHERE task_id=? AND active_turn_id=? RETURNING *",
            (turn_id, now, task_id, turn_id),
        ).fetchone()
        if result is not None:
            row = ProjectTaskRow.from_row(result)
            record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)

    @staticmethod
    def wait(conn: Any, task_id: str, turn_id: str, kind: str, now: int) -> None:
        result = conn.execute(
            "UPDATE project_tasks SET wait_kind=?,updated_at=?,list_revision=list_revision+1 "
            "WHERE task_id=? AND active_turn_id=? AND wait_kind<>? RETURNING *",
            (kind, now, task_id, turn_id, kind),
        ).fetchone()
        if result is not None:
            row = ProjectTaskRow.from_row(result)
            record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)

    @staticmethod
    def update_metadata(
        conn: Any, row: ProjectTaskRow, expected_revision: int, *, manual_title: bool = False
    ) -> ProjectTaskRow | None:
        result = conn.execute(
            "UPDATE project_tasks SET title=?, archived=?, web_search_enabled=?, pinned=?, updated_at=?, "
            "list_revision=list_revision+1, "
            "title_source=CASE WHEN ?=1 THEN 'manual' ELSE title_source END, "
            "title_revision=title_revision+? "
            "WHERE task_id=? AND owner_id=? AND list_revision=? RETURNING *",
            (
                row.title,
                int(row.archived),
                int(row.web_search_enabled),
                int(row.pinned),
                row.updated_at,
                int(manual_title),
                int(manual_title),
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

    @staticmethod
    def update_configuration(
        conn: Any,
        row: ProjectTaskRow,
        expected_configuration_revision: int,
        *,
        expected_list_revision: int | None = None,
    ) -> ProjectTaskRow | None:
        list_revision = (
            row.list_revision if expected_list_revision is None else expected_list_revision
        )
        previous = ProjectTaskRepo.get_in_transaction(conn, row.task_id, row.owner_id)
        if (
            previous is None
            or previous.active_turn_id is not None
            or previous.configuration_revision != expected_configuration_revision
            or previous.list_revision != list_revision
        ):
            return None
        if (previous.selected_expert_id, previous.action_mode) == (
            row.selected_expert_id,
            row.action_mode,
        ):
            unchanged = conn.execute(
                "UPDATE project_tasks SET configuration_revision=configuration_revision "
                "WHERE task_id=? AND owner_id=? AND configuration_revision=? "
                "AND list_revision=? AND active_turn_id IS NULL RETURNING *",
                (row.task_id, row.owner_id, expected_configuration_revision, list_revision),
            ).fetchone()
            return ProjectTaskRow.from_row(unchanged) if unchanged is not None else None
        result = conn.execute(
            "UPDATE project_tasks SET selected_expert_id=?, action_mode=?, updated_at=?, "
            "configuration_revision=configuration_revision+1, list_revision=list_revision+1 "
            "WHERE task_id=? AND owner_id=? AND configuration_revision=? "
            "AND list_revision=? AND active_turn_id IS NULL RETURNING *",
            (
                row.selected_expert_id,
                row.action_mode,
                row.updated_at,
                row.task_id,
                row.owner_id,
                expected_configuration_revision,
                list_revision,
            ),
        ).fetchone()
        if result is None:
            return None
        updated = ProjectTaskRow.from_row(result)
        conn.execute(
            "INSERT INTO project_task_configuration_events(task_id,configuration_revision,"
            "previous_expert_id,selected_expert_id,selected_expert_name,previous_action_mode,"
            "action_mode,created_at) VALUES(?,?,?,?,COALESCE((SELECT name FROM agents "
            "WHERE agent_id=?),''),?,?,?)",
            (
                updated.task_id,
                updated.configuration_revision,
                previous.selected_expert_id,
                updated.selected_expert_id,
                updated.selected_expert_id,
                previous.action_mode,
                updated.action_mode,
                updated.updated_at,
            ),
        )
        record_change(
            conn, updated.owner_id, task_scope(updated.project_id), updated.task_id, updated
        )
        return updated

    @staticmethod
    def configuration_events(conn: Any, task_id: str) -> list[ProjectTaskConfigurationEventRow]:
        rows = conn.execute(
            "SELECT * FROM project_task_configuration_events WHERE task_id=? "
            "ORDER BY configuration_revision",
            (task_id,),
        ).fetchall()
        return [ProjectTaskConfigurationEventRow.from_row(row) for row in rows]

    @staticmethod
    def allow_legacy_title_generation(
        conn: Any, task_id: str, owner_id: int, title_revision: int
    ) -> None:
        conn.execute(
            "UPDATE project_tasks SET title_source='provisional' "
            "WHERE task_id=? AND owner_id=? AND title_source='manual' AND title_revision=?",
            (task_id, owner_id, title_revision),
        )

    @staticmethod
    def apply_generated_title(
        conn: Any, task_id: str, owner_id: int, title_revision: int, title: str
    ) -> bool:
        result = conn.execute(
            "UPDATE project_tasks SET title=?, title_source='generated', "
            "title_revision=title_revision+1, list_revision=list_revision+1 "
            "WHERE task_id=? AND owner_id=? AND title_source='provisional' "
            "AND title_revision=? RETURNING *",
            (title, task_id, owner_id, title_revision),
        ).fetchone()
        if result is None:
            return False
        row = ProjectTaskRow.from_row(result)
        record_change(conn, row.owner_id, task_scope(row.project_id), row.task_id, row)
        return True

    def runtime_owner(self, task_id: str) -> int | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM project_tasks t JOIN users u ON u.id=t.owner_id "
                "WHERE task_id=? AND u.disabled=0",
                (task_id,),
            ).fetchone()
        return strict_int(row["owner_id"]) if row else None
