"""Owner-scoped metadata browsing over existing immutable task publications."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from octop.infra.db.repos.project_task_artifacts import ProjectTaskArtifactLibraryRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.artifact_library_models import (
    ArtifactLibraryQuery,
    TaskArtifactLibraryItem,
    TaskArtifactLibraryPage,
)
from octop.infra.projects.artifacts import view
from octop.infra.projects.cursors import cursor_codec
from octop.infra.projects.interaction_models import public_status
from octop.infra.projects.tasks import TurnStatus
from octop.infra.utils.artifact_kinds import artifact_kind


def _item(row: ProjectTaskArtifactLibraryRow) -> TaskArtifactLibraryItem:
    waiting = (
        row.turn_status == "running"
        and not row.turn_worker_id
        and bool(row.turn_checkpoint_id or row.turn_wait_kind == "interrupted")
    )
    wait_kind = (
        "user"
        if row.turn_pause_requested and row.turn_wait_kind != "interrupted"
        else row.turn_wait_kind
    )
    return TaskArtifactLibraryItem(
        **view(row.artifact).model_dump(),
        file_kind=artifact_kind(row.artifact.name, row.artifact.media_type),
        task_title=row.task_title,
        task_archived=row.task_archived,
        turn_status=cast(TurnStatus, public_status(row.turn_status, wait_kind if waiting else "")),
        project_id=row.project_id,
        project_name=row.project_name,
        project_archived=row.project_archived,
    )


def page(
    services: SharedServices, owner_id: int, query: ArtifactLibraryQuery
) -> TaskArtifactLibraryPage:
    filters = query.model_dump(exclude={"limit", "page_cursor"})
    fingerprint = hashlib.sha256(
        json.dumps(filters, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    codec = cursor_codec(services, owner_id, f"artifact-library-v3:{fingerprint}")
    cursor = codec.decode(query.page_cursor, "history") if query.page_cursor is not None else None
    boundary = (cursor.updated_at, cursor.after) if cursor else None
    with services.db.read_snapshot() as conn:
        if (
            query.project_id is not None
            and services.project_repo.get_in_transaction(conn, query.project_id, owner_id) is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if (
            query.task_id is not None
            and services.project_task_repo.get_in_transaction(conn, query.task_id, owner_id) is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        rows = services.project_task_artifact_repo.library_page(
            conn,
            owner_id,
            scope=query.scope,
            project_id=query.project_id,
            task_id=query.task_id,
            kind=query.kind,
            business_category=query.business_category,
            archived=query.archived,
            q=query.q,
            limit=query.limit + 1,
            boundary=boundary,
        )
    selected = rows[: query.limit]
    next_page = None
    if len(rows) > query.limit:
        last = selected[-1].artifact
        next_page = codec.encode("history", updated_at=last.created_at, after=last.artifact_id)
    return TaskArtifactLibraryPage(
        items=[_item(row) for row in selected], next_page_cursor=next_page
    )


def detail(services: SharedServices, owner_id: int, artifact_id: str) -> TaskArtifactLibraryItem:
    with services.db.read_snapshot() as conn:
        row = services.project_task_artifact_repo.library_get(conn, owner_id, artifact_id)
    if row is None:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return _item(row)
