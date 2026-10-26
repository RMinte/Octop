"""Independent project deltas and lazy task-list pagination."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from octop.infra.db.repos._base import UNSET
from octop.infra.db.repos.project_sync import ListChange, all_task_scope, task_scope
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.repos.projects import ProjectRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_policy, task_search
from octop.infra.projects.cursors import CursorCodec, cursor_codec, cursor_reset
from octop.infra.projects.models import ProjectModel, ProjectView
from octop.infra.projects.service import _view
from octop.infra.projects.tasks import TaskSummary, summary


class ProjectChange(ProjectModel):
    operation: Literal["upsert", "delete"] = "upsert"
    project_id: str
    data: ProjectView | None = Field(default=None, description="删除时为 null。")


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
    scope: Literal["all"] | None = Field(
        default=None, description="全局列表每页回显 all；普通项目或无项目列表可为 null。"
    )
    q: str | None = Field(
        default=None, description="全局列表每页回显去首尾空白的原始查询，空查询为空字符串。"
    )


class TaskChange(ProjectModel):
    operation: Literal["upsert", "delete"] = "upsert"
    task_id: str
    list_revision: int
    data: TaskSummary | None = Field(default=None, description="删除时为 null。")


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
    archived: bool = False,
    scope: Literal["standalone", "all"] = "standalone",
    expert_id: str | None = None,
    status: str | None = None,
    query: str | None = None,
) -> TaskListPage:
    storage_scope = all_task_scope() if scope == "all" else task_scope(project_id)
    search = scope == "all" and query is not None
    if search:
        query = query.strip() if query is not None else ""
    filters = json.dumps([scope, expert_id, status, query], ensure_ascii=False)
    purpose = ":global-search-v1:" if search else ":archive-v2:"
    codec = cursor_codec(
        services,
        owner_id,
        storage_scope + f"{purpose}{archived}:" + hashlib.sha256(filters.encode()).hexdigest(),
    )
    cursor = codec.decode(page_cursor, "history") if page_cursor is not None else None
    boundary = None
    if cursor:
        pin, _, identifier = cursor.after.partition(":")
        if pin not in {"0", "1"} or not identifier:
            raise cursor_reset()
        boundary = (int(pin), cursor.updated_at, identifier)
    with services.db.read_snapshot() as conn:
        if (
            project_id is not None
            and services.project_repo.get_in_transaction(conn, project_id, owner_id) is None
        ):
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        policy = approval_policy.in_transaction(conn, services, owner_id)
        head = services.project_sync_repo.head(conn, owner_id, storage_scope)
        if search:
            rows = task_search.page(
                conn,
                services,
                owner_id,
                query=query or "",
                limit=limit + 1,
                boundary=boundary,
                archived=archived,
                expert_id=expert_id,
                status=status,
            )
        else:
            rows = services.project_task_repo.page(
                conn,
                owner_id,
                project_id,
                limit + 1,
                boundary,
                archived,
                scope="all" if scope == "all" else None,
                selected_expert_id=expert_id if expert_id is not None else UNSET,
                status=status,
                query=query,
            )
    items = rows[:limit]
    next_page = None
    if len(rows) > limit:
        last = items[-1]
        next_page = codec.encode(
            "history", updated_at=last.updated_at, after=f"{int(last.pinned)}:{last.task_id}"
        )
    return TaskListPage(
        items=[summary(row, policy) for row in items],
        next_page_cursor=next_page,
        sync_cursor=codec.encode("sync", position=head) if cursor is None else None,
        scope="all" if scope == "all" else None,
        q=(query or "") if scope == "all" else None,
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
            and scope not in {"tasks:", all_task_scope()}
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
    changes = []
    for row in rows:
        payload = json.loads(row.payload_json)
        payload.setdefault("archived", 0)
        deleted = payload.pop("deleted", False)
        changes.append(
            ProjectChange(
                operation="delete" if deleted else "upsert",
                project_id=row.entity_id,
                data=None if deleted else _view(ProjectRow.from_row(payload)),
            )
        )
    return ProjectSyncPage(
        mode="delta",
        base_sync_cursor=sync_cursor,
        next_sync_cursor=next_sync,
        next_page_cursor=next_page,
        changes=changes,
    )


def task_sync(
    services: SharedServices,
    owner_id: int,
    project_id: str | None,
    *,
    sync_cursor: str,
    page_cursor: str | None = None,
    limit: int = 50,
    archived: bool = False,
    scope: Literal["standalone", "all"] = "standalone",
    expert_id: str | None = None,
    status: str | None = None,
    query: str | None = None,
) -> TaskSyncPage:
    filters = json.dumps([scope, expert_id, status, query], ensure_ascii=False)
    storage_scope = all_task_scope() if scope == "all" else task_scope(project_id)
    codec = cursor_codec(
        services,
        owner_id,
        storage_scope + f":archive-v2:{archived}:" + hashlib.sha256(filters.encode()).hexdigest(),
    )
    rows, next_sync, next_page = _delta(
        services,
        owner_id,
        storage_scope,
        codec,
        sync_cursor,
        page_cursor,
        limit,
    )
    changes = []
    policy = approval_policy.current(services, owner_id)
    for row in rows:
        payload = json.loads(row.payload_json)
        payload.setdefault("web_search_enabled", True)
        deleted = payload.pop("deleted", False)
        data = summary(ProjectTaskRow.from_row(payload), policy)
        deleted = deleted or (
            (expert_id is not None and data.selected_expert_id != expert_id)
            or (status is not None and data.status != status)
            or (query is not None and query.casefold() not in data.title.casefold())
        )
        changes.append(
            TaskChange(
                operation="delete" if deleted else "upsert",
                task_id=row.entity_id,
                list_revision=data.list_revision,
                data=None if deleted else data,
            )
        )
    return TaskSyncPage(
        base_sync_cursor=sync_cursor,
        next_sync_cursor=next_sync,
        next_page_cursor=next_page,
        changes=changes,
    )
