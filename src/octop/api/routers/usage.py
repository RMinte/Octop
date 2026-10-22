"""Token usage router — query expert and independent task consumption.

  GET /api/usage/summary             → caller's own roll-up
  GET /api/usage/summary?as_user=N   → admin scope; another user's roll-up
  GET /api/usage/summary?agent_id=X  → scope to one agent (must be visible)
  GET /api/usage/export.xlsx         → Excel detail for the same scope
  GET /api/admin/usage/summary       → global roll-up (admin only)
  GET /api/admin/usage/export.xlsx   → Excel detail (admin only)

Query params:
  window      = today | yesterday | last_7d | last_30d | all
                | day:YYYY-MM-DD | month:YYYY-MM
                | range:YYYY-MM-DD:YYYY-MM-DD   (default last_30d)
  granularity = total | by_day | by_agent | by_model | by_project | by_task
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from octop.api.common.content_disposition import content_disposition
from octop.api.common.usage_xlsx import build_usage_xlsx
from octop.api.deps import current_user, get_server
from octop.i18n import tr
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.locale import normalize_locale, resolve_request_locale

router = APIRouter()

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DAY_WINDOW_RE = re.compile(r"^day:(\d{4}-\d{2}-\d{2})$")
_MONTH_WINDOW_RE = re.compile(r"^month:(\d{4}-\d{2})$")
_RANGE_WINDOW_RE = re.compile(r"^range:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")


UsageGranularity = Literal["total", "by_day", "by_agent", "by_model", "by_project", "by_task"]
UsageAvailability = Literal["recorded", "partial", "not_collected", "no_data", "undefined"]


class UsageCoverage(BaseModel):
    """Field availability within retained records, not all actual model requests."""

    scope: Literal["recorded_usage_only"] = Field(
        description="Only retained usage is included; missing calls and deleted history are excluded."
    )
    input_tokens: UsageAvailability
    uncached_input_tokens: UsageAvailability
    cache_read_tokens: UsageAvailability
    cache_write_tokens: UsageAvailability
    output_tokens: UsageAvailability
    reasoning_tokens: UsageAvailability
    total_tokens: UsageAvailability
    model_calls: UsageAvailability
    turns: UsageAvailability


class UsageSummaryCoverage(UsageCoverage):
    avg_per_turn: UsageAvailability = Field(
        description="undefined means records exist but no conversation turns provide a denominator."
    )
    cache_hit_percent: UsageAvailability = Field(
        description="partial uses recorded agent inputs only; undefined means no valid denominator."
    )


class UsageCapabilities(BaseModel):
    execution_location: bool = Field(
        default=False, description="Whether local/cloud/unknown usage classification is available."
    )
    cost: bool = Field(default=False, description="Whether monetary usage costs are available.")
    savings: bool = Field(
        default=False, description="Whether baseline cost comparisons are available."
    )
    budget: bool = Field(
        default=False, description="Whether monetary budgets and their enforcement are available."
    )


class UsageCacheHitBasis(BaseModel):
    source_scope: Literal["agent_records"] = Field(
        description="Retained agent records, including resident conversations; excludes task journals."
    )
    input_tokens: int = Field(description="Recorded agent input tokens used as the denominator.")
    cache_read_tokens: int = Field(description="Recorded cache reads used as the numerator.")


class UsageCounts(BaseModel):
    input_tokens: int
    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    model_calls: int = Field(
        description="Recorded model calls; task journals do not retain this count."
    )
    turns: int = Field(
        description="Agent usage records and task turns with usage; excludes title/review jobs."
    )


class UsageBucket(UsageCounts):
    key: str = Field(
        description="Date, public resource ID or model reference. Empty project ID means no project."
    )
    label: str = Field(description="Project name, task title or the bucket key.")
    coverage: UsageCoverage = Field(
        description="Availability for this bucket's own retained records."
    )


class UsageSummary(UsageCounts):
    window: str
    granularity: str
    range_start: int
    range_end: int
    timezone: str = Field(
        description="Actual server timezone used for query boundaries and day buckets."
    )
    generated_at: int = Field(
        description="Response generation time in Unix seconds; not a frozen accounting snapshot."
    )
    avg_per_turn: int
    cache_hit_percent: int
    coverage: UsageSummaryCoverage = Field(
        description="Read before displaying numeric values; missing data may retain legacy zero values."
    )
    capabilities: UsageCapabilities = Field(
        default_factory=UsageCapabilities,
        description="Unsupported capabilities have no measured values; do not display them as zero.",
    )
    cache_hit_basis: UsageCacheHitBasis
    bucket_limit: int = Field(description="Maximum number of returned buckets.")
    buckets_truncated: bool = Field(
        description="More buckets exist; top-level totals still cover all retained records in scope."
    )
    buckets: list[UsageBucket]


def _resolve_user_scope(
    *,
    user: Any,
    as_user: int | None,
    server: Any,
) -> int:
    """Mirror of the agents router's ``?as_user=`` semantics: an admin
    may query another user's bucket; non-admins are pinned to their own
    id."""
    if as_user is None or as_user == user.id:
        return int(user.id)
    if not user.is_admin:
        raise OctopError(ErrorCode.FORBIDDEN, "as_user requires admin")
    target = server.user_manager.get_by_id(as_user)
    if target is None:
        raise OctopError(ErrorCode.NOT_FOUND, "user not found")
    return int(target.id)


def _server_timezone(server: Any) -> str:
    config = getattr(server, "config", None)
    tz = getattr(config, "default_timezone", None) if config is not None else None
    return str(tz or "UTC")


def _summary_or_raise(
    *,
    server: Any,
    user_id: int | None,
    agent_id: str | None,
    project_id: str | None,
    task_id: str | None,
    window: str,
    granularity: str,
) -> dict[str, Any]:
    try:
        return cast(
            "dict[str, Any]",
            server.services.usage_repo.summary(
                user_id=user_id,
                agent_id=agent_id,
                project_id=project_id,
                task_id=task_id,
                window=window,
                granularity=granularity,
                timezone=_server_timezone(server),
            ),
        )
    except ValueError as exc:
        raise OctopError(ErrorCode.INTERNAL_ERROR, str(exc), status=400) from exc


def _assert_agent_exists(server: Any, agent_id: str | None) -> None:
    if agent_id is None:
        return
    assert server.app_runtime is not None
    if server.app_runtime.agent_registry.get_row(agent_id) is None:
        raise OctopError(ErrorCode.AGENT_NOT_FOUND, f"agent {agent_id!r} not found")


def _agent_name_map(server: Any) -> dict[str, str]:
    rows = server.services.agent_repo.list_all(include_disabled=True)
    return {str(row.agent_id): str(row.name or row.agent_id) for row in rows}


def _username_map(server: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    users = server.user_manager.list_all(include_disabled=True)
    for user in users:
        uid = getattr(user, "id", None)
        if uid is None:
            continue
        name = getattr(user, "username", None) or getattr(user, "display_name", None) or ""
        out[int(uid)] = str(name)
    return out


def _export_filename(window: str, *, locale: str) -> str:
    """``{title}_{YYYY-MM-DD-YYYY-MM-DD}.xlsx`` (no ``range`` / preset labels)."""
    prefix = tr("usage_export.filename_prefix", normalize_locale(locale))
    range_match = _RANGE_WINDOW_RE.fullmatch(window)
    if range_match is not None:
        period = f"{range_match.group(1)}-{range_match.group(2)}"
    else:
        day_match = _DAY_WINDOW_RE.fullmatch(window)
        if day_match is not None:
            period = day_match.group(1)
        else:
            month_match = _MONTH_WINDOW_RE.fullmatch(window)
            if month_match is not None:
                period = month_match.group(1)
            else:
                period = _SAFE_FILENAME_RE.sub("_", window).strip("._") or "usage"
    return f"{prefix}_{period}.xlsx"


def _export_response(
    *,
    request: Request,
    server: Any,
    user_id: int | None,
    agent_id: str | None,
    project_id: str | None,
    task_id: str | None,
    window: str,
) -> StreamingResponse:
    timezone = _server_timezone(server)
    locale = resolve_request_locale(request)
    try:
        rows = server.services.usage_repo.list_detail(
            user_id=user_id,
            agent_id=agent_id,
            project_id=project_id,
            task_id=task_id,
            window=window,
            timezone=timezone,
        )
    except ValueError as exc:
        raise OctopError(ErrorCode.INTERNAL_ERROR, str(exc), status=400) from exc

    by_day = _summary_or_raise(
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
        granularity="by_day",
    )["buckets"]
    by_agent = _summary_or_raise(
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
        granularity="by_agent",
    )["buckets"]
    by_model = _summary_or_raise(
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
        granularity="by_model",
    )["buckets"]

    by_project = _summary_or_raise(
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
        granularity="by_project",
    )["buckets"]
    by_task = _summary_or_raise(
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
        granularity="by_task",
    )["buckets"]

    payload = build_usage_xlsx(
        rows=rows,
        by_day=by_day,
        by_agent=by_agent,
        by_model=by_model,
        by_project=by_project,
        by_task=by_task,
        agent_names=_agent_name_map(server),
        usernames=_username_map(server),
        timezone=timezone,
        locale=locale,
    )
    filename = _export_filename(window, locale=locale)
    return StreamingResponse(
        BytesIO(payload),
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.get(
    "/usage/summary",
    response_model=UsageSummary,
    summary="查看我的 Token 用量",
    description="按天、模型、专家、项目或任务汇总。项目和任务包含标题与审批消耗；专家视图只统计专家会话。仅管理员可通过 as_user 查看他人用量。",
)
async def user_summary(
    window: str = "last_30d",
    granularity: UsageGranularity = "by_day",
    agent_id: str | None = None,
    project_id: str | None = Query(
        default=None, description="Filter task usage by public project ID; empty means no project."
    ),
    task_id: str | None = Query(default=None, description="Filter usage by public task ID."),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    user_id = _resolve_user_scope(user=user, as_user=as_user, server=server)
    _assert_agent_exists(server, agent_id)
    return _summary_or_raise(
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
        granularity=granularity,
    )


@router.get(
    "/usage/export.xlsx",
    summary="导出我的 Token 用量",
    description="导出用量明细与按天、专家、模型、项目、任务汇总，支持相同的范围筛选。",
    response_class=StreamingResponse,
)
async def user_export(
    request: Request,
    window: str = "last_30d",
    agent_id: str | None = None,
    project_id: str | None = Query(
        default=None, description="Filter task usage by public project ID; empty means no project."
    ),
    task_id: str | None = Query(default=None, description="Filter usage by public task ID."),
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> StreamingResponse:
    user_id = _resolve_user_scope(user=user, as_user=as_user, server=server)
    _assert_agent_exists(server, agent_id)
    return _export_response(
        request=request,
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
    )


# --- admin --------------------------------------------------------------

admin_router = APIRouter()


@admin_router.get(
    "/usage/summary",
    response_model=UsageSummary,
    summary="查看全局 Token 用量",
    description="仅管理员可用。支持按用户、专家、项目、任务和时间范围筛选。项目和任务视图只统计独立任务消耗，专家视图只统计专家会话。",
)
async def admin_summary(
    window: str = "last_30d",
    granularity: UsageGranularity = "by_day",
    user_id: int | None = None,
    agent_id: str | None = None,
    project_id: str | None = Query(
        default=None, description="Filter task usage by public project ID; empty means no project."
    ),
    task_id: str | None = Query(default=None, description="Filter usage by public task ID."),
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Global usage roll-up. Optional ``user_id`` / ``agent_id`` narrow
    the scope; absent both fields mean *all rows*. Admin only."""
    if not user.is_admin:
        raise OctopError(ErrorCode.FORBIDDEN, "admin required")
    return _summary_or_raise(
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
        granularity=granularity,
    )


@admin_router.get(
    "/usage/export.xlsx",
    summary="导出全局 Token 用量",
    description="仅管理员可用。导出筛选范围内的专家和独立任务消耗明细及汇总。",
    response_class=StreamingResponse,
)
async def admin_export(
    request: Request,
    window: str = "last_30d",
    user_id: int | None = None,
    agent_id: str | None = None,
    project_id: str | None = Query(
        default=None, description="Filter task usage by public project ID; empty means no project."
    ),
    task_id: str | None = Query(default=None, description="Filter usage by public task ID."),
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> StreamingResponse:
    if not user.is_admin:
        raise OctopError(ErrorCode.FORBIDDEN, "admin required")
    return _export_response(
        request=request,
        server=server,
        user_id=user_id,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        window=window,
    )
