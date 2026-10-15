"""Independent project deltas and lazy task-list pagination."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from octop.infra.db.repos.project_sync import ListChange, task_scope
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.projects import ProjectRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.cursors import CursorCodec, cursor_codec, cursor_reset
from octop.infra.projects.models import ProjectModel, ProjectView
from octop.infra.projects.service import _view
from octop.infra.projects.tasks import TaskSummary, summary


class ProjectChange(ProjectModel):
    operation: Literal["upsert"] = "upsert"
    project_id: str
    data: ProjectView


class ProjectSyncPage(ProjectModel):
    mode: Literal["snapshot", "delta"]
    base_sync_cursor: str | None
    next_sync_cursor: str | None
    next_page_cursor: str | None
    changes: list[ProjectChange]


class TaskListPage(ProjectModel):
    items: list[TaskSummary]
    next_page_cursor: str | None = Field(description="历史任务下一页位置；null 表示已到末尾。")
    sync_cursor: str | None = Field(description="仅首页返回的增量基线，不表示历史任务已全部下载。")


class TaskChange(ProjectModel):
    operation: Literal["upsert"] = "upsert"
    task_id: str
    list_revision: int
    data: TaskSummary


class TaskSyncPage(ProjectModel):
    base_sync_cursor: str
    next_sync_cursor: str | None = Field(description="本批最后一页才返回可提交的同步位置。")
    next_page_cursor: str | None = Field(description="本批变更下一页，与历史任务分页游标不可混用。")
    changes: list[TaskChange]


def task_page(
    services: SharedServices,
    owner_id: int,
    project_id: str | None,
    *,
    limit: int = 50,
    page_cursor: str | None = None,
) -> TaskListPage:
    codec = cursor_codec(services, owner_id, task_scope(project_id))
    cursor = codec.decode(page_cursor, "history") if page_cursor is not None else None
    boundary = (cursor.updated_at, cursor.after) if cursor else None
    with services.db.read_snapshot() as conn:
        if (
            project_id is not None
            and services.project_repo.get_in_transaction(conn, project_id, owner_id) is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        head = services.project_sync_repo.head(conn, owner_id, task_scope(project_id))
        rows = services.project_task_repo.page(conn, owner_id, project_id, limit + 1, boundary)
    items = rows[:limit]
    next_page = None
    if len(rows) > limit:
        last = items[-1]
        next_page = codec.encode("history", updated_at=last.updated_at, after=last.task_id)
    return TaskListPage(
        items=[summary(row) for row in items],
        next_page_cursor=next_page,
        sync_cursor=codec.encode("sync", position=head) if cursor is None else None,
    )


def _delta(
    services: SharedServices,
    owner_id: int,
    scope: str,
    codec: CursorCodec,
    sync_cursor: str,
    page_cursor: str | None,
    limit: int,
) -> tuple[list[ListChange], str | None, str | None]:
    base = codec.decode(sync_cursor, "sync")
    page = codec.decode(page_cursor, "delta") if page_cursor is not None else None
    with services.db.read_snapshot() as conn:
        if (
            scope.startswith("tasks:")
            and scope != "tasks:"
            and services.project_repo.get_in_transaction(
                conn, scope.removeprefix("tasks:"), owner_id
            )
            is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        head = services.project_sync_repo.head(conn, owner_id, scope)
        cutoff = head if page is None else page.cutoff
        if base.position > cutoff or cutoff > head or (page and page.position != base.position):
            raise cursor_reset()
        rows = services.project_sync_repo.changes(
            conn,
            owner_id,
            scope,
            base.position,
            cutoff,
            page.after if page else "",
            limit + 1,
        )
    more = len(rows) > limit
    selected = rows[:limit]
    return (
        selected,
        None if more else codec.encode("sync", position=cutoff),
        codec.encode("delta", position=base.position, cutoff=cutoff, after=selected[-1].entity_id)
        if more
        else None,
    )


def project_sync(
    services: SharedServices,
    owner_id: int,
    *,
    sync_cursor: str | None = None,
    page_cursor: str | None = None,
    limit: int = 50,
) -> ProjectSyncPage:
    codec = cursor_codec(services, owner_id, "projects")
    if sync_cursor is None:
        if page_cursor is not None:
            raise cursor_reset()
        with services.db.read_snapshot() as conn:
            head = services.project_sync_repo.head(conn, owner_id, "projects")
            projects = services.project_repo.list_in_transaction(conn, owner_id)
        return ProjectSyncPage(
            mode="snapshot",
            base_sync_cursor=None,
            next_sync_cursor=codec.encode("sync", position=head),
            next_page_cursor=None,
            changes=[ProjectChange(project_id=row.project_id, data=_view(row)) for row in projects],
        )
    rows, next_sync, next_page = _delta(
        services,
        owner_id,
        "projects",
        codec,
        sync_cursor,
        page_cursor,
        limit,
    )
    return ProjectSyncPage(
        mode="delta",
        base_sync_cursor=sync_cursor,
        next_sync_cursor=next_sync,
        next_page_cursor=next_page,
        changes=[
            ProjectChange(
                project_id=row.entity_id,
                data=_view(ProjectRow.from_row(json.loads(row.payload_json))),
            )
            for row in rows
        ],
    )


def task_sync(
    services: SharedServices,
    owner_id: int,
    project_id: str | None,
    *,
    sync_cursor: str,
    page_cursor: str | None = None,
    limit: int = 50,
) -> TaskSyncPage:
    scope = task_scope(project_id)
    codec = cursor_codec(services, owner_id, scope)
    rows, next_sync, next_page = _delta(
        services,
        owner_id,
        scope,
        codec,
        sync_cursor,
        page_cursor,
        limit,
    )
    changes = []
    for row in rows:
        data = summary(ProjectTaskRow.from_row(json.loads(row.payload_json)))
        changes.append(
            TaskChange(task_id=row.entity_id, list_revision=data.list_revision, data=data)
        )
    return TaskSyncPage(
        base_sync_cursor=sync_cursor,
        next_sync_cursor=next_sync,
        next_page_cursor=next_page,
        changes=changes,
    )
