"""Transactional external conversations; imports never enqueue runtime work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from octop.infra.db.repos.external_imports import ImportedSessionRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import input_attachments
from octop.infra.projects.content import TaskContentBlock
from octop.infra.projects.conversation_import_models import (
    ConversationImport,
    ImportedTurnHistory,
    ImportTurn,
)
from octop.infra.projects.deletion_state import assert_project_writable
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.tasks import TaskDetail, detail
from octop.infra.utils.ulid import new_ulid


class ConversationImportResult(ProjectModel):
    action: Literal["imported", "updated", "unchanged"]
    task: TaskDetail


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _manifest(turn: ImportTurn, turn_id: str) -> dict[str, Any]:
    return {
        "external_turn_id": turn.external_turn_id,
        "turn_id": turn_id,
        "user": _hash(turn.user_message.model_dump(mode="json")),
        "activities": [
            [activity.kind, activity.name, activity.detail] for activity in turn.activities
        ],
        "files": [change.path for change in turn.file_changes],
    }


def _blocks(turn: ImportTurn, turn_id: str) -> str:
    blocks = [
        TaskContentBlock(
            block_id=f"{turn_id}:import:{index}",
            type="tool" if activity.kind == "tool" else "progress",
            text=activity.detail or "",
            status="completed" if activity.succeeded else "failed",
            tool_name=activity.name if activity.kind == "tool" else None,
            output=activity.output,
            duration_ms=activity.duration_ms,
        )
        for index, activity in enumerate(turn.activities)
    ]
    if turn.assistant_message.text:
        blocks.append(
            TaskContentBlock(
                block_id=f"{turn_id}:import:answer",
                type="text",
                text=turn.assistant_message.text,
                status="completed",
            )
        )
    return json.dumps([block.model_dump(mode="json") for block in blocks])


def _row(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    task: ProjectTaskRow,
    body: ConversationImport,
    turn: ImportTurn,
    turn_id: str,
    previous: ProjectTaskTurnRow | None,
) -> ProjectTaskTurnRow:
    user_files = input_attachments.bind(
        services, conn, owner_id, task.task_id, turn.user_message.attachment_ids
    )
    assistant_files = input_attachments.bind(
        services, conn, owner_id, task.task_id, turn.assistant_message.attachment_ids
    )
    history = ImportedTurnHistory(
        source=body.source,
        external_turn_id=turn.external_turn_id,
        user_timestamp=turn.user_message.timestamp,
        assistant_timestamp=turn.assistant_message.timestamp,
        activities=turn.activities,
        file_changes=turn.file_changes,
        assistant_attachments=assistant_files,
        missing_user_attachments=turn.user_message.missing_attachments,
        missing_assistant_attachments=turn.assistant_message.missing_attachments,
    )
    configuration = json.dumps(
        {
            "attachment_ids": turn.user_message.attachment_ids,
            "attachments": [item.model_dump(mode="json") for item in user_files],
            "imported_history": history.model_dump(mode="json"),
            "memory_enabled": False,
            "team_enabled": False,
        }
    )
    created_at = _milliseconds(
        turn.user_message.timestamp
        or turn.assistant_message.timestamp
        or turn.completed_at
        or body.source_updated_at
    )
    updated_at = _milliseconds(
        turn.completed_at or turn.assistant_message.timestamp or body.source_updated_at
    )
    blocks = _blocks(turn, turn_id)
    if previous is not None:
        return replace(
            previous,
            reply=turn.assistant_message.text,
            configuration_json=configuration,
            updated_at=updated_at,
            content_blocks_json=blocks,
            content_revision=previous.content_revision
            + int(blocks != previous.content_blocks_json),
            finished_at=_milliseconds(turn.completed_at) if turn.completed_at else None,
        )
    return ProjectTaskTurnRow(
        turn_id=turn_id,
        task_id=task.task_id,
        request_id="import_" + _hash(turn.external_turn_id),
        prompt=turn.user_message.text,
        reply=turn.assistant_message.text,
        status="completed",
        error_code="",
        activity="",
        configuration_json=configuration,
        model_ref="",
        worker_id="",
        cancel_requested=False,
        created_at=created_at,
        updated_at=updated_at,
        content_blocks_json=blocks,
        content_revision=1,
        finished_at=_milliseconds(turn.completed_at) if turn.completed_at else None,
    )


def _assert_compatible(
    old: list[dict[str, Any]], new: list[ImportTurn], rows: list[ProjectTaskTurnRow]
) -> None:
    # Once Octop has continued the task, changing its source prefix would also
    # invalidate native checkpoints, tool receipts and the meaning of later turns.
    if len(new) < len(old) or len(rows) != len(old):
        raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
    for manifest, incoming, saved in zip(old, new, rows, strict=False):
        candidate = _manifest(incoming, saved.turn_id)
        if (
            manifest["turn_id"] != saved.turn_id
            or saved.status != "completed"
            or manifest["external_turn_id"] != candidate["external_turn_id"]
            or manifest["user"] != candidate["user"]
            or candidate["activities"][: len(manifest["activities"])] != manifest["activities"]
            or candidate["files"][: len(manifest["files"])] != manifest["files"]
        ):
            raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)


def import_conversation(
    services: SharedServices, owner_id: int, body: ConversationImport
) -> ConversationImportResult:
    fingerprint = _hash([turn.model_dump(mode="json") for turn in body.turns])
    action: Literal["imported", "updated", "unchanged"] = "imported"
    with services.db.transaction() as conn:
        tasks, turns, imports = (
            services.project_task_repo,
            services.project_task_turn_repo,
            services.external_import_repo,
        )
        if not tasks.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if body.project_id is not None:
            project = services.project_repo.get_in_transaction(conn, body.project_id, owner_id)
            if project is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            assert_project_writable(services, conn, owner_id, body.project_id)
            if project.archived:
                raise OctopError.localized(ErrorCode.PROJECT_ARCHIVED)
        previous = imports.session(conn, owner_id, body.source, body.external_session_id)
        manifest: dict[str, Any] = {}
        rows: list[ProjectTaskTurnRow] = []
        if previous is not None:
            task = tasks.get_in_transaction(conn, previous.task_id, owner_id)
            if task is None:
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            if task.project_id != body.project_id:
                raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
            if task.active_turn_id:
                raise OctopError.localized(ErrorCode.TASK_BUSY)
            manifest = json.loads(previous.manifest_json)
            action = "unchanged" if previous.fingerprint == fingerprint else "updated"
            if action == "updated":
                if task.archived:
                    raise OctopError.localized(ErrorCode.TASK_ARCHIVED)
                if _milliseconds(body.source_updated_at) < manifest["source_updated_at"]:
                    raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT)
                rows = turns.list_in_transaction(conn, task.task_id)
                _assert_compatible(manifest["turns"], body.turns, rows)
        else:
            created = _milliseconds(body.turns[0].user_message.timestamp or body.source_updated_at)
            task = ProjectTaskRow(
                task_id="task_" + new_ulid(),
                owner_id=owner_id,
                project_id=body.project_id,
                title=body.title,
                request_id="import_" + new_ulid(),
                active_turn_id=None,
                created_at=created,
                updated_at=_milliseconds(body.source_updated_at),
                status="completed",
                import_source=body.source,
                external_session_id=body.external_session_id,
            )
            tasks.insert(conn, task)
        if action != "unchanged":
            new_manifest = []
            for index, turn in enumerate(body.turns):
                old = rows[index] if index < len(rows) else None
                turn_id = old.turn_id if old else "turn_" + new_ulid()
                row = _row(services, conn, owner_id, task, body, turn, turn_id, old)
                if old is None:
                    turns.insert_imported(conn, row)
                else:
                    turns.update_imported(conn, row)
                new_manifest.append(_manifest(turn, turn_id))
            manifest["turns"] = new_manifest
        # An old retry must not roll back newer source titles/revisions. Metadata-only
        # imports retain conversation times and any manually edited Octop title.
        source_time = _milliseconds(body.source_updated_at)
        if source_time >= manifest.get("source_updated_at", source_time):
            if action != "unchanged" or task.title != body.title:
                tasks.refresh_import(
                    conn,
                    task,
                    body.title,
                    source_time if action != "unchanged" else task.updated_at,
                    content_changed=action != "unchanged",
                )
            manifest["source_updated_at"] = source_time
            manifest["source_revision"] = body.source_revision
            imports.save_session(
                conn,
                ImportedSessionRow(
                    owner_id,
                    body.source,
                    body.external_session_id,
                    task.task_id,
                    fingerprint,
                    json.dumps(manifest),
                ),
            )
    return ConversationImportResult(action=action, task=detail(services, owner_id, task.task_id))
