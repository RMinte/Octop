"""HTTP surface for independent projects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from octop.api.common.content_disposition import content_disposition
from octop.api.common.upload_limit import read_upload_capped
from octop.api.deps import current_user, get_server, require_database, require_permission
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import (
    approval_policy,
    artifact_library,
    artifacts,
    branches,
    composer_resources,
    deletion,
    expert_memory_management,
    input_attachments,
    interactions,
    legacy_conversations,
    model_routing,
    service,
    sync,
    task_submission,
    tasks,
)
from octop.infra.projects.approval_models import ApprovalPolicyUpdate, TaskApprovalPolicy
from octop.infra.projects.artifact_library_models import (
    ArtifactLibraryQuery,
    LibraryArchive,
    LibraryBusinessCategory,
    LibraryKind,
    LibraryScope,
    TaskArtifactLibraryItem,
    TaskArtifactLibraryPage,
)
from octop.infra.projects.input_attachment_models import TaskInputAttachment
from octop.infra.projects.interaction_models import InteractionResponse, TaskControl
from octop.infra.projects.legacy_conversation_models import LegacyConversationCreate
from octop.infra.projects.models import (
    ProjectArchive,
    ProjectFields,
    ProjectPatch,
    ProjectResources,
    ProjectUpdate,
    ProjectView,
)
from octop.infra.projects.runtime import ProjectTaskWorker
from octop.infra.projects.settings import TaskSettings, load_settings
from octop.infra.projects.usage import TaskContextUsage, read_usage
from octop.infra.projects.workbench import WorkspaceEntry
from octop.infra.server import OctopServer
from octop.infra.users.identity import User
from octop.infra.utils.locale import resolve_request_locale

_TASK_STREAM_SECONDS = 25.0

router = APIRouter(prefix="/projects")


def _services(server: OctopServer) -> SharedServices:
    require_database(server)
    assert server.services is not None
    return server.services


@router.get(
    "",
    response_model=list[ProjectView],
    summary="列出我的项目",
    description="每次读取当前服务端的最新项目。按最近更新排序，只返回本人数据；无需专家、模型或 Bridge。",
)
async def list_projects(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> list[ProjectView]:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(service.list_projects, _services(server), user.id)


@router.get(
    "/resources",
    response_model=ProjectResources,
    summary="列出项目可用的目录、Skill、MCP、插件和子代理角色",
    description="返回共享技能库和本人有权使用的 MCP 连接。只返回引用和配置状态，不返回密钥，不连接 MCP。",
)
async def project_resources(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectResources:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(service.resources, _services(server), user.id)


@router.get(
    "/{project_id}/workspace/tree-children",
    response_model=list[WorkspaceEntry],
    summary="列出新任务草稿可引用的项目文件",
    description="仅列出本人项目的服务端目录，按项目版本校验；不创建任务，不读取客户端目录或专家工作区。path 为项目内相对目录。",
)
async def composer_project_files(
    project_id: str,
    response: Response,
    expected_project_revision: int = Query(ge=1),
    path: str = Query(default=".", max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> list[WorkspaceEntry]:
    response.headers["Cache-Control"] = "no-store"
    return await composer_resources.project_tree(
        _services(server), user.id, project_id, expected_project_revision, path
    )


@router.post(
    "",
    response_model=ProjectView,
    status_code=201,
    summary="新建独立项目",
    description="必须提供 Idempotency-Key 请求头；同一用户、同一标识和相同内容重试返回首次创建结果，内容不同返回 409。保存两类目录、Skill/MCP、插件和子代理引用、记忆与团队配置及行动指南；不创建专家，不上传或访问客户端文件。同名项目返回 409。",
)
async def create_project(
    body: ProjectFields,
    response: Response,
    idempotency_key: str = Header(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="本次创建的唯一标识；断网重试必须复用，新的创建使用新标识。",
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        service.create_project, _services(server), user.id, body, idempotency_key
    )


@router.get(
    "/sync",
    response_model=sync.ProjectSyncPage,
    summary="同步项目最终状态",
    description="不带同步游标返回完整项目快照；之后只返回变过的项目，删除记录的 operation=delete、data=null。游标与任务独立。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_projects(
    response: Response,
    sync_cursor: str | None = Query(
        default=None, max_length=4096, description="已有项目同步位置；首次可省略。"
    ),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="本批变更下一页游标。"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.ProjectSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.project_sync,
        _services(server),
        user.id,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
    )


@router.get(
    "/{project_id}/tasks/sync",
    response_model=sync.TaskSyncPage,
    summary="同步任务列表最终状态",
    description="只返回该列表基线之后变化的任务摘要，包含任务 status 和 list_revision，同一任务合并为最终状态，不包含正文。也返回移出当前归档筛选的摘要，调用方应按 archived 字段更新显示范围。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_project_tasks(
    project_id: str,
    response: Response,
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
    sync_cursor: str = Query(min_length=1, max_length=4096, description="任务首页返回的增量基线。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="本批变更下一页游标。"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_sync,
        _services(server),
        user.id,
        project_id,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
        archived=archived,
    )


@router.get(
    "/{project_id}",
    response_model=ProjectView,
    summary="读取项目详情",
    description="返回该项目自己的完整配置和版本。其他用户的项目返回 404。",
)
async def get_project(
    project_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(service.get_project, _services(server), user.id, project_id)


@router.patch(
    "/{project_id}",
    response_model=ProjectView,
    summary="局部修改项目",
    description="Octop 网页的项目编辑入口。只修改提交的字段；省略保持原值，目录传 null 解除关联，Skill/MCP、插件和子代理列表传 [] 清空。必须提交 expected_revision；版本冲突返回 409。名称、简介、行动指南、能力列表及记忆/团队开关不接受 null。",
)
async def patch_project(
    project_id: str,
    body: ProjectPatch,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        service.patch_project, _services(server), user.id, project_id, body
    )


@router.put(
    "/{project_id}",
    response_model=ProjectView,
    summary="修改项目",
    description="完整替换项目字段，保存后 revision 加一；expected_revision 不匹配返回 409。"
    "只修改项目配置，不修改专家或目录内容；客户端路径不会在服务端解析、检查或访问。",
)
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        service.update_project, _services(server), user.id, project_id, body
    )


@router.patch(
    "/{project_id}/archive",
    response_model=ProjectView,
    summary="归档或恢复项目",
    description="校验 expected_revision 后归档或恢复本人项目，保留全部会话和文件，也保留任务各自的归档状态。归档前须结束或停止所有排队/运行任务；归档后可查看历史，恢复前不能新建任务、继续对话或重试。",
)
async def archive_project(
    project_id: str,
    body: ProjectArchive,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        service.archive_project,
        _services(server),
        user.id,
        project_id,
        body.expected_revision,
        body.archived,
    )


@router.delete(
    "/{project_id}",
    status_code=204,
    response_class=Response,
    summary="停止活动任务并删除项目及全部会话",
    description="按 ID 删除本人项目及其中全部任务和会话，无需版本参数。服务端先封住新执行、停止活动轮次并等待退出，再清理工作台会话并原子删除记录。停止未确认时返回 PROJECT_TASKS_BUSY，保留记录，可重试删除。保留工作目录文件和专家记忆。其他用户的项目返回 404。",
)
async def delete_project(
    project_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    from octop.api.routers.project_task_workbench import close_task_sessions

    async def close_sessions(task_id: str) -> None:
        await close_task_sessions(task_id, user.id)

    await deletion.delete_project(
        _services(server),
        server.project_task_worker,
        user.id,
        project_id,
        close_sessions=close_sessions,
    )
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get(
    "/{project_id}/tasks",
    response_model=sync.TaskListPage,
    summary="列出项目任务",
    description="按最近活动位置分页，包含每个任务的 status 和 list_revision，无需逐个读取详情。默认 50 条；仅首页返回独立同步基线，不读取任务正文。",
)
async def list_project_tasks(
    project_id: str,
    response: Response,
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskListPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_page,
        _services(server),
        user.id,
        project_id,
        limit=limit,
        page_cursor=page_cursor,
        archived=archived,
    )


@router.post(
    "/{project_id}/tasks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="在项目中发送首条消息并创建任务",
    description="保存任务及首条消息后排队执行，返回任务详情。web_search_enabled 设置任务网络搜索开关，默认开启，用户显式关闭后追问沿用。每轮采用发送时的项目配置及本轮 options；不创建专家。request_id 用于幂等重试。模型或资源未配置时不会创建空任务。",
)
async def create_project_task(
    project_id: str,
    body: tasks.ProjectTaskCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await task_submission.submit(_services(server), user.id, body, project_id=project_id)
    worker.wake()
    return result


def _task_worker(server: OctopServer) -> ProjectTaskWorker:
    worker = server.project_task_worker
    if worker is None or worker.closing:
        raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY)
    return worker


task_router = APIRouter(prefix="/project-tasks")


def _artifact_library_query(
    scope: LibraryScope = Query(
        default="all", description="all 包含全部任务；project 指定项目；standalone 仅无项目任务。"
    ),
    project_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=256,
        description="scope=project 时必填，其他 scope 不可携带。",
    ),
    task_id: str | None = Query(
        default=None, min_length=1, max_length=256, description="可选来源任务 ID，与其他条件求交。"
    ),
    kind: LibraryKind = Query(default="all", description="文件浏览分类，默认全部类型。"),
    business_category: LibraryBusinessCategory = Query(
        default="all",
        description="按后缀自动归类：document 文本和 PDF、presentation 幻灯片、spreadsheet 表格、image 图片、other 其他；all 为全部。与文件格式及其他条件求交。",
    ),
    archived: LibraryArchive = Query(
        default="all",
        description="all 包含归档成果；active 仅任务与项目均未归档；archived 为任一已归档。",
    ),
    q: str = Query(
        default="",
        max_length=200,
        description="文件名的字面子串，去首尾空白，ASCII 字母忽略大小写。",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: str | None = Query(
        default=None,
        min_length=1,
        max_length=4096,
        description="原筛选下的下一页游标，不可跨账号、筛选或用途复用。",
    ),
) -> ArtifactLibraryQuery:
    try:
        return ArtifactLibraryQuery(
            scope=scope,
            project_id=project_id,
            task_id=task_id,
            kind=kind,
            business_category=business_category,
            archived=archived,
            q=q,
            limit=limit,
            page_cursor=page_cursor,
        )
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("query", *error["loc"])} for error in exc.errors()]
        ) from exc


@task_router.get(
    "/artifacts",
    response_model=TaskArtifactLibraryPage,
    summary="按发布时间列出我的任务成果资料",
    description="跨项目和无项目任务读取本人正式发布的文件元数据，按发布时间倒序、同一时间按成果 ID 倒序分页，默认 50 条、最多 100。支持文件名、项目、任务、文件格式、资料分类和归档筛选，全部条件先求交再分页。资料分类按最终文件名后缀自动保存，历史成果按同一规则回填，无法识别的后缀归 other；默认 all 包含全部。游标绑定全部筛选，失效返回 409 PROJECT_SYNC_RESET。不读取文件内容，不扫描工作目录。",
)
async def task_artifact_library(
    response: Response,
    query: ArtifactLibraryQuery = Depends(_artifact_library_query),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> TaskArtifactLibraryPage:
    response.headers["Cache-Control"] = "private, no-store"
    return await asyncio.to_thread(artifact_library.page, _services(server), user.id, query)


@task_router.get(
    "/artifacts/{artifact_id}",
    response_model=TaskArtifactLibraryItem,
    summary="读取任务成果资料及来源",
    description="按成果 ID 读取本人已发布文件的元数据及当前任务、项目信息。归档后仍可读取，内容通过返回的鉴权 download_url 下载。不加载完整会话或文件字节；不存在或他人成果返回 404。永久删除任务会删除其成果，有历史的项目须归档保留。",
)
async def task_artifact_library_detail(
    artifact_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> TaskArtifactLibraryItem:
    response.headers["Cache-Control"] = "private, no-store"
    return await asyncio.to_thread(artifact_library.detail, _services(server), user.id, artifact_id)


@task_router.get(
    "/settings",
    response_model=TaskSettings,
    summary="读取任务过程显示与生成默认值",
    description="登录用户可读取两个全局布尔默认值，不返回环境变量内容或密钥。动作链路默认值在新轮次入队时固定；步骤面板只影响客户端显示。每次读取持久化配置，不使用缓存。",
)
async def task_settings(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> TaskSettings:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(load_settings, _services(server))


@task_router.post(
    "/attachments",
    response_model=TaskInputAttachment,
    status_code=201,
    summary="上传任务输入附件",
    description="上传图片、文档或其他文件；无需预先创建任务或运行专家。单个文件最多 25 MiB，且不超过管理员上传限制。每条消息最多 9 个附件，使用返回的 attachment_id 发送首条消息或追问，首次发送后归属该任务。未发送的附件 24 小时后过期。PNG、JPEG、WebP 和 GIF 经过真实格式校验并提供视觉输入；其他文件在本轮服务端工作目录的 inbound/ 下提供给工具读取，不访问客户端目录。",
)
async def upload_task_attachment(
    response: Response,
    file: UploadFile = File(...),  # noqa: B008
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> TaskInputAttachment:
    services = _services(server)
    content = await read_upload_capped(
        file,
        max_bytes=input_attachments.max_upload_bytes(services),
        code=ErrorCode.TASK_ATTACHMENT_LIMIT,
    )
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        input_attachments.save,
        services,
        user.id,
        file.filename or "upload.bin",
        content,
        file.content_type,
    )


@task_router.get(
    "/attachments/{attachment_id}",
    response_class=Response,
    summary="读取任务输入附件",
    description="需要附件所属账号鉴权。读取不可变的上传内容，不访问客户端或当前项目目录。不存在、过期或他人附件返回 404。视觉图片以内联方式预览，其他文件作为附件下载。",
    responses={
        200: {
            "description": "上传附件原始内容；视觉图片的 Content-Type 使用服务端识别的真实格式。",
            "content": {
                media: {"schema": {"type": "string", "format": "binary"}}
                for media in (
                    "application/octet-stream",
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                    "image/gif",
                )
            },
        }
    },
)
async def download_task_attachment(
    attachment_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    attachment, content = await asyncio.to_thread(
        input_attachments.download, _services(server), user.id, attachment_id
    )
    return Response(
        content=content,
        media_type=attachment.media_type,
        headers={
            "Content-Disposition": content_disposition(
                attachment.name,
                disposition="inline"
                if input_attachments.is_image(attachment.media_type)
                else "attachment",
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@task_router.get(
    "",
    response_model=sync.TaskListPage,
    summary="列出本人任务与会话",
    description="scope=all 读取本人全部任务，每页回显 scope 和 q；同时传 q 时按任务标题及当前项目名进行 Unicode 归一、多词全部命中的全局搜索，空 q 返回全部。默认 standalone 仅无项目任务。支持专家、状态筛选，置顶优先；仅首页返回同步基线，游标绑定筛选条件。搜索游标不得用于列表 /sync。",
)
async def list_standalone_tasks(
    response: Response,
    scope: Literal["standalone", "all"] = Query(default="standalone"),
    expert_id: str | None = Query(default=None, min_length=1, max_length=256),
    status: tasks.TurnStatus | None = Query(default=None),
    q: str | None = Query(
        default=None,
        max_length=256,
        description="全局搜索词：去首尾空白回显；按 Unicode 空白拆词，忽略大小写、重音及全半角，%/_/! 按字面匹配。",
    ),
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskListPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_page,
        _services(server),
        user.id,
        None,
        limit=limit,
        page_cursor=page_cursor,
        archived=archived,
        scope=scope,
        expert_id=expert_id,
        status=status,
        query=q,
    )


@task_router.post(
    "",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="创建任务并发送首条消息",
    description="project_id 可为 null，创建无项目任务；也可指定本人项目。首条消息和任务原子保存，web_search_enabled 设置任务网络搜索开关，默认开启，用户显式关闭后追问沿用。可用 options 选择本轮模型、Skill、MCP、插件、子代理及记忆/团队设置。request_id 重试不重复执行。",
)
async def create_task(
    body: tasks.TaskCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await task_submission.submit(
        _services(server), user.id, body, project_id=body.project_id
    )
    worker.wake()
    return result


@task_router.get(
    "/approval-policy",
    response_model=TaskApprovalPolicy,
    summary="读取账号全局任务审批模式",
    description="本人所有项目、已有及新任务共用同一个审批模式与独立版本。没有任务时也可读取。",
)
async def get_global_task_approval_policy(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> TaskApprovalPolicy:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(approval_policy.current, _services(server), user.id)


@task_router.put(
    "/approval-policy",
    response_model=TaskApprovalPolicy,
    summary="切换账号全局任务审批模式",
    description="原子更新本人全部任务及未来新任务的后续动作模式。expected_revision 检查全局版本，request_id 全账号幂等；不改变已固定的动作、人工卡、强制人工规则或执行电脑授权。",
)
async def update_global_task_approval_policy(
    body: ApprovalPolicyUpdate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> TaskApprovalPolicy:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        approval_policy.set_global_policy, _services(server), user.id, body
    )


@task_router.put(
    "/{task_id}/approval-policy",
    response_model=tasks.TaskDetail,
    summary="从任务切换账号全局审批模式",
    description="验证任务归属后，原子切换当前账号所有任务及未来任务后续动作的审批模式，不放行已存在的审批。expected_revision 校验审批模式版本；request_id 保证重试幂等，重复标识的内容不同返回 409。强制人工工具和执行端资源授权仍生效。",
)
async def update_task_approval_policy(
    task_id: str,
    body: ApprovalPolicyUpdate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        approval_policy.set_policy, _services(server), user.id, task_id, body
    )


@task_router.get(
    "/sync",
    response_model=sync.TaskSyncPage,
    summary="同步任务列表最终状态",
    description="只返回该列表基线之后变化的任务摘要，包含任务 status 和 list_revision，同一任务合并为最终状态，不包含正文。也返回移出当前归档筛选的摘要，调用方应按 archived 字段更新显示范围。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_standalone_tasks(
    response: Response,
    scope: Literal["standalone", "all"] = Query(default="standalone"),
    expert_id: str | None = Query(default=None, min_length=1, max_length=256),
    status: tasks.TurnStatus | None = Query(default=None),
    q: str | None = Query(default=None, max_length=256),
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
    sync_cursor: str = Query(min_length=1, max_length=4096, description="任务首页返回的增量基线。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="本批变更下一页游标。"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_sync,
        _services(server),
        user.id,
        None,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
        archived=archived,
        scope=scope,
        expert_id=expert_id,
        status=status,
        query=q,
    )


@task_router.get(
    "/resources",
    response_model=tasks.TaskResources,
    summary="读取本轮可选择的模型和项目能力",
    description="已登录客户端读取 models 和 default_model_ref，用 models[].model_ref 填充创建或追问请求的 options.model_ref；provider_kind=box 表示盒子模型。不指定模型时使用发送时的服务端默认模型。普通用户可读，不返回模型密钥或连接地址；选择仅影响本轮，不修改项目或全局默认值。",
)
async def task_resources(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskResources:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(tasks.resources, _services(server), user.id)


@task_router.get(
    "/routing/settings",
    response_model=model_routing.ModelRoutingSettings,
    summary="读取盒子与混合模式配置",
    description="登录用户可读取两侧模型引用及默认难度档位；不返回服务地址或凭据，不改变现有默认模型。",
)
async def read_model_routing_settings(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> model_routing.ModelRoutingSettings:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(model_routing.load_settings, _services(server))


@task_router.put(
    "/routing/settings",
    response_model=model_routing.ModelRoutingSettings,
    summary="配置盒子和混合模式使用的模型",
    description="需要模型配置权限，完整替换两侧模型引用及默认档位；仅影响之后受理的显式路由请求，已有轮次、项目和全局默认模型保持原值。",
)
async def update_model_routing_settings(
    body: model_routing.ModelRoutingSettings,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(require_permission("providers")),
) -> model_routing.ModelRoutingSettings:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(model_routing.save_settings, _services(server), body)


@task_router.post(
    "/from-thread",
    response_model=tasks.TaskDetail,
    status_code=201,
    summary="明确将旧专家会话续接为统一任务",
    description="仅接入本人普通本地专家的空闲会话。复制可见历史及附件来源引用，不启动专家、不改旧历史、不复制审批、工具执行授权或检查点，也不自动发送新消息。重复请求复用同一任务；项目必须显式选择或传 null。",
)
async def continue_legacy_conversation(
    body: LegacyConversationCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    if server.app_runtime is None:
        raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY)
    services = _services(server)
    runtime = server.app_runtime
    task_id = await legacy_conversations.create(
        services,
        user.id,
        body,
        registry=runtime.agent_registry,
        history_archive=runtime.history_archive,
        hitl_store=runtime.gateway.processor.hitl_coordinator.store,
    )
    return await asyncio.to_thread(tasks.detail, services, user.id, task_id)


@task_router.patch(
    "/{task_id}",
    response_model=tasks.TaskDetail,
    summary="修改统一会话的标题、专家、模式或归档状态",
    description="必须提供 expected_revision（list_revision）；切换专家或行动模式还须提供 expected_configuration_revision 且任务空闲，历史轮次身份不变。置顶、网络搜索和归档随任务保存；归档保留会话，恢复后可继续。版本冲突返回 409。",
)
async def patch_task(
    task_id: str,
    body: tasks.TaskPatch,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    result = await asyncio.to_thread(tasks.patch, _services(server), user.id, task_id, body)
    if body.archived is True or {"selected_expert_id", "action_mode"} & body.model_fields_set:
        from octop.api.routers.project_task_workbench import close_task_sessions

        await close_task_sessions(task_id, user.id)
    return result


@task_router.delete(
    "/{task_id}",
    status_code=204,
    response_class=Response,
    summary="永久删除任务",
    description="按 ID 删除本人任务及全部会话轮次、已发布附件和工具记录，无需版本参数，支持项目内和无项目任务。排队或执行中的任务返回 409 TASK_BUSY，须等待完成或先停止。保留工作目录中的文件。其他用户的任务返回 404。",
)
async def delete_task(
    task_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    await asyncio.to_thread(tasks.delete, _services(server), user.id, task_id)
    from octop.api.routers.project_task_workbench import close_task_sessions

    await close_task_sessions(task_id, user.id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@task_router.post(
    "/{task_id}/forks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="从历史锚点分叉、编辑或重新生成",
    description="明确创建关联的新任务，原历史不变；只复制可见历史和已校验附件，不复制执行授权、审批、检查点或工作文件。edit/regenerate 或提供 prompt 时以稳定请求 ID 受理新轮次，工具可能再次执行；无新消息时仅创建空闲分支。",
)
async def fork_task(
    task_id: str,
    body: branches.TaskFork,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    services = _services(server)
    worker = _task_worker(server) if body.operation != "fork" or body.prompt else None
    result = await asyncio.to_thread(branches.create, services, user.id, task_id, body)
    if result.continuation_prompt is not None or result.continuation_attachment_ids:
        identifier = hashlib.sha256(f"{task_id}:{body.request_id}".encode()).hexdigest()
        detail = await task_submission.submit(
            services,
            user.id,
            tasks.TaskMessage(
                request_id="branch_" + identifier,
                prompt=result.continuation_prompt or "",
                attachment_ids=result.continuation_attachment_ids,
                expected_configuration_revision=1,
            ),
            task_id=result.task_id,
        )
        assert worker is not None
        worker.wake()
        return detail
    return await asyncio.to_thread(tasks.detail, services, user.id, result.task_id)


@task_router.get(
    "/{task_id}",
    response_model=tasks.TaskDetail,
    summary="读取任务对话和执行状态",
    description="返回最近一页轮次及任务摘要，默认 50 轮、最多 100；page_cursor 读取更早记录。每轮 artifacts 包含已发布附件元数据，内容通过鉴权 HTTP 下载。首页 event_cursor 可用于订阅实时状态，不返回凭据或附件字节。其他用户的任务返回 404。",
)
async def task_detail(
    task_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: str | None = Query(default=None, min_length=1, max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        tasks.detail, _services(server), user.id, task_id, limit=limit, page_cursor=page_cursor
    )


@task_router.get(
    "/{task_id}/context-usage",
    response_model=TaskContextUsage,
    summary="读取任务上下文占用与分解",
    description="读取本人任务最近模型请求的窗口占用；运行中、暂停及服务重启后均可读取持久快照。各组成部分为估算；压缩后占用也标记为估算。source=empty 表示尚无快照，不是已知零占用。此处输入输出为最近请求，累计计费见每轮 input_tokens/output_tokens。其他用户或不存在的任务返回 404。",
)
async def task_context_usage(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> TaskContextUsage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(read_usage, _services(server), user.id, task_id)


@task_router.get(
    "/{task_id}/expert-memory",
    response_model=expert_memory_management.ExpertMemoryEntries,
    summary="查看当前专家的通用、项目或旧版记忆",
    description="按本人任务解析当前专家和记忆范围。legacy 只读且须先显式启用；无项目任务的 scope 仅属于该任务。不会把历史会话的记忆合并到新范围。",
)
async def task_expert_memory(
    task_id: str,
    response: Response,
    source: expert_memory_management.MemorySource = "scope",
    q: str = Query(default="", max_length=4096),
    path: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=20, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> expert_memory_management.ExpertMemoryEntries:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        expert_memory_management.query_memory,
        _services(server),
        user.id,
        task_id,
        expert_memory_management.ExpertMemoryQuery(source=source, query=q, path=path, limit=limit),
    )


@task_router.post(
    "/{task_id}/expert-memory",
    response_model=expert_memory_management.ExpertMemorySaveResult,
    summary="明确保存一条专家记忆",
    description="用户明确选择通用或当前项目/任务范围。请求 ID 保证同一保存不重复执行；结果 uncertain 须先核对记忆，不能自动换 ID 重试。旧版范围不可写。",
)
async def save_task_expert_memory(
    task_id: str,
    body: expert_memory_management.ExpertMemorySave,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> expert_memory_management.ExpertMemorySaveResult:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        expert_memory_management.save_memory, _services(server), user.id, task_id, body
    )


@task_router.get(
    "/{task_id}/expert-memory/policy",
    response_model=expert_memory_management.ExpertMemoryPolicy,
    summary="读取当前专家的记忆策略",
)
async def task_expert_memory_policy(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> expert_memory_management.ExpertMemoryPolicy:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        expert_memory_management.memory_policy, _services(server), user.id, task_id
    )


@task_router.patch(
    "/{task_id}/expert-memory/policy",
    response_model=expert_memory_management.ExpertMemoryPolicy,
    summary="修改当前专家的记忆采集和旧版召回策略",
    description="使用专家 config_revision 做并发检查；修改影响该专家后续受理的轮次，已入队快照保留原策略，但现时权限或召回/采集授权的撤销优先。不自动导入或修改旧记忆。",
)
async def update_task_expert_memory_policy(
    task_id: str,
    body: expert_memory_management.ExpertMemoryPolicyPatch,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> expert_memory_management.ExpertMemoryPolicy:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        expert_memory_management.update_memory_policy, _services(server), user.id, task_id, body
    )


@task_router.get(
    "/{task_id}/artifacts/{artifact_id}/download",
    response_class=Response,
    summary="下载任务已发布的附件",
    description="需要当前任务所属账号鉴权。下载发布时保存的不可变快照，不读取工作目录，不依赖任务运行状态。不存在或他人附件返回 404；响应始终为 attachment，支持中文文件名。",
    responses={
        200: {
            "description": "附件原始字节，Content-Type 按已保存的 MIME 类型返回。",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def download_task_artifact(
    task_id: str,
    artifact_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    artifact, content = await asyncio.to_thread(
        artifacts.download, _services(server), user.id, task_id, artifact_id
    )
    return Response(
        content=content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": content_disposition(artifact.name),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@task_router.post(
    "/{task_id}/messages",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="继续任务对话",
    description="在同一任务中发送下一轮消息，保留成功历史和标记为未完成的失败/取消正文，使用最新项目配置和本轮 options；不修改项目。无附件且正文为 /compact 时仅压缩模型上下文，保留公开历史，不执行任务工具。归档任务须先恢复。任务执行中返回 409；同一 request_id 的相同消息返回原任务，不重复执行。",
)
async def send_task_message(
    task_id: str,
    body: tasks.TaskMessage,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await task_submission.submit(_services(server), user.id, body, task_id=task_id)
    worker.wake()
    return result


@task_router.post(
    "/{task_id}/cancel",
    response_model=tasks.TaskDetail,
    summary="停止当前回复",
    description="必须指定 turn_id，仅取消该轮排队或执行。该轮已结束时幂等返回，不会停止后续轮次；其他任务的轮次返回 404。已保存正文保留，工具操作不回滚。",
)
async def cancel_task(
    task_id: str,
    body: tasks.TaskCancel,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(tasks.cancel, _services(server), user.id, task_id, body.turn_id)


@task_router.post(
    "/{task_id}/pause",
    response_model=tasks.TaskDetail,
    summary="暂停指定轮次",
    description="记录幂等暂停意图，在模型或工具安全边界保存断点并释放执行槽；不回滚已开始的操作。必须指定 turn_id 和稳定 request_id。",
)
async def pause_task(
    task_id: str,
    body: TaskControl,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    await asyncio.to_thread(
        interactions.control, _services(server), user.id, task_id, body, paused=True
    )
    if server.project_task_worker is not None:
        server.project_task_worker.wake()
    return await asyncio.to_thread(tasks.detail, _services(server), user.id, task_id)


@task_router.post(
    "/{task_id}/resume",
    response_model=tasks.TaskDetail,
    summary="恢复指定轮次",
    description="解除用户暂停意图，继续同一轮次及其原始配置；待审批、待回答或客户端结果尚未齐全时继续等待。复用 request_id 可安全重发。",
)
async def resume_task(
    task_id: str,
    body: TaskControl,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    await asyncio.to_thread(
        interactions.control, _services(server), user.id, task_id, body, paused=False
    )
    if server.project_task_worker is not None:
        server.project_task_worker.wake()
    return await asyncio.to_thread(tasks.detail, _services(server), user.id, task_id)


@task_router.post(
    "/{task_id}/turns/{turn_id}/interactions/{interaction_id}/respond",
    response_model=tasks.TaskDetail,
    summary="回复审批或提问",
    description="本人任务内的持久交互。提交稳定 request_id、expected_revision 及明确类型的 response；审批必须回传 action_digest。冲突或迟到返回 409；同一请求相同内容重发返回当前状态。提问答案不能授予操作权限。",
)
async def respond_task_interaction(
    task_id: str,
    turn_id: str,
    interaction_id: str,
    body: InteractionResponse,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    await asyncio.to_thread(
        interactions.respond, _services(server), user.id, task_id, turn_id, interaction_id, body
    )
    if server.project_task_worker is not None:
        server.project_task_worker.wake()
    return await asyncio.to_thread(tasks.detail, _services(server), user.id, task_id)


@task_router.post(
    "/{task_id}/turns/{turn_id}/retry",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="重新尝试最后一个失败或停止的轮次",
    description="使用原消息和当前项目配置创建新一轮，保留原记录。仅允许重试最后一个失败/取消轮次；同一 request_id 重传不重复执行。之前工具操作不会回滚，重试可能再次执行操作。",
)
async def retry_task_turn(
    task_id: str,
    turn_id: str,
    body: tasks.TaskRetry,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await task_submission.submit(
        _services(server), user.id, body, task_id=task_id, retry_of_turn_id=turn_id
    )
    worker.wake()
    return result


async def _task_events(
    request: Request,
    services: SharedServices,
    owner_id: int,
    task_id: str,
    cursor: str,
    pending: list[tasks.TaskUpdate],
    snapshot: tasks.TaskDetail | None = None,
) -> AsyncIterator[str]:
    started = time.monotonic()
    heartbeat = started
    if snapshot is not None:
        yield f"id: {cursor}\nevent: snapshot\ndata: {snapshot.model_dump_json()}\n\n"
    # Rotate connections to recheck authentication and apply renewed access tokens.
    while time.monotonic() - started < _TASK_STREAM_SECONDS:
        if await request.is_disconnected():
            return
        for update in pending:
            cursor = update.cursor
            yield f"id: {cursor}\nevent: turn\ndata: {update.model_dump_json()}\n\n"
        if time.monotonic() - heartbeat >= 10:
            yield ": heartbeat\n\n"
            heartbeat = time.monotonic()
        await asyncio.sleep(0.5)
        try:
            pending = await asyncio.to_thread(tasks.updates, services, owner_id, task_id, cursor)
        except OctopError as exc:
            yield (
                "event: error\ndata: "
                + json.dumps(exc.to_envelope(locale=resolve_request_locale(request)))
                + "\n\n"
            )
            return


@task_router.get(
    "/{task_id}/events",
    response_class=StreamingResponse,
    summary="订阅任务轮次的实时状态",
    description="Bearer 鉴权的 SSE。使用首页 event_cursor 或 Last-Event-ID 续接；turn 事件的 data 为 {task: TaskSummary, turn: TaskTurn, cursor: string}，SSE id 与 cursor 一致；snapshot 为 TaskDetail。同一轮中间状态可合并。断线后补齐遗漏轮次的最新状态，不重放工具操作。无游标先返回最近一页 snapshot。游标失效返回 409 PROJECT_SYNC_RESET，需重建首页。连接约 25 秒后结束，可用最后游标重连。",
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def task_events(
    task_id: str,
    request: Request,
    cursor: str | None = Query(default=None, min_length=1, max_length=4096),
    last_event_id: str | None = Header(default=None, max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> StreamingResponse:
    services = _services(server)
    position = cursor or last_event_id
    snapshot = None
    if position is None:
        snapshot = await asyncio.to_thread(tasks.detail, services, user.id, task_id)
        position = snapshot.event_cursor
    assert position is not None
    pending = await asyncio.to_thread(tasks.updates, services, user.id, task_id, position)
    return StreamingResponse(
        _task_events(request, services, user.id, task_id, position, pending, snapshot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
