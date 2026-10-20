"""Authenticated imports of explicitly selected external data into Octop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from starlette.types import Message

from octop.api.deps import current_user, get_server, require_database
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import conversation_imports, imports
from octop.infra.projects.conversation_import_models import ConversationImport
from octop.infra.projects.conversation_imports import ConversationImportResult
from octop.infra.projects.import_models import (
    ConfigurationCategory,
    ConfigurationDetail,
    ConfigurationImport,
    ConfigurationImportResult,
    ConfigurationView,
    ImportSource,
    ProjectImport,
    ProjectImportResult,
)
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

_MAX_HISTORY_BODY = 16 * 1024 * 1024
_MAX_CONFIGURATION_BODY = 32 * 1024 * 1024


class _ImportRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            if request.method != "POST":
                return await handler(request)
            maximum = (
                _MAX_CONFIGURATION_BODY
                if request.url.path.endswith("/configurations")
                else _MAX_HISTORY_BODY
            )
            received = 0

            async def receive() -> Message:
                nonlocal received
                message = await request.receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > maximum:
                        # FastAPI preserves HTTPException during body parsing, but
                        # wraps other exceptions as 400 before the domain handler.
                        raise HTTPException(status_code=413)
                return message

            try:
                return await handler(Request(request.scope, receive=receive))
            except HTTPException:
                if received > maximum:
                    raise OctopError.localized(
                        ErrorCode.ATTACHMENT_TOO_LARGE,
                        max_mb=maximum // (1024 * 1024),
                        details={"max_bytes": maximum, "max_mb": maximum // (1024 * 1024)},
                    ) from None
                raise

        return bounded


router = APIRouter(prefix="/projects/import", route_class=_ImportRoute)
task_router = APIRouter(prefix="/project-tasks/import", route_class=_ImportRoute)


def _services(server: OctopServer) -> SharedServices:
    require_database(server)
    assert server.services is not None
    return server.services


@task_router.post(
    "",
    response_model=ConversationImportResult,
    summary="导入一个 Codex 或 Claude 会话为任务历史",
    description="接收客户端转换后的可见历史，按本人、来源及来源会话 ID 去重，无需模型配置。保存为已完成轮次，不排队、不执行工具、不读取路径，也不接收私有推理。附件须先上传到 Octop。重导可更新来源回答和追加来源轮次；既有用户正文、顺序、时间或来源活动被破坏时返回 409。任务有活动轮次时返回 409；已有 Octop 追问后只允许重放相同来源内容，防止改写后续上下文。之后追问使用当前项目和模型配置。每次最多 1000 轮、16 MiB，详情按现有历史分页读取。",
)
async def import_conversation(
    body: ConversationImport,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ConversationImportResult:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        conversation_imports.import_conversation, _services(server), user.id, body
    )


@router.post(
    "",
    response_model=ProjectImportResult,
    summary="导入一个外部项目",
    description="按本人、来源及外部项目 ID 去重。首次可明确关联本人已有项目；同名不会自动认领。新项目仅保存客户端目录，服务端目录为空；不创建专家或读取来源文件，重复导入不覆盖项目编辑。",
)
async def import_project(
    body: ProjectImport,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectImportResult:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(imports.import_project, _services(server), user.id, body)


@router.post(
    "/configurations",
    response_model=ConfigurationImportResult,
    summary="导入一个待启用配置条目",
    description="独立导入设置、行动指南、插件、Skill 或 MCP 条目及其文件副本。文件使用相对路径和 Base64 字节，单文件最多 2 MiB、条目最多 20 MiB/1000 个文件；不读取来源路径、不安装或激活任何配置。相同来源及内容重复导入返回原条目。",
)
async def import_configuration(
    body: ConfigurationImport,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ConfigurationImportResult:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(imports.import_configuration, _services(server), user.id, body)


@router.get(
    "/configurations",
    response_model=list[ConfigurationView],
    summary="列出本人导入的待启用配置",
    description="按来源和类别筛选，每个条目单独展示来源、名称、摘要和 pendingActivation 状态；列表不返回文件字节。",
)
async def list_configurations(
    response: Response,
    source: ImportSource | None = None,
    category: ConfigurationCategory | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> list[ConfigurationView]:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        imports.list_configurations,
        _services(server),
        user.id,
        limit=limit,
        offset=offset,
        source=source,
        category=category,
    )


@router.get(
    "/configurations/{configuration_id}",
    response_model=ConfigurationDetail,
    summary="读取一个导入配置条目的完整副本",
    description="只返回本人的待启用副本及文件 Base64 内容；其他用户或不存在的 ID 返回相同的 404，不激活条目。",
)
async def get_configuration(
    configuration_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ConfigurationDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        imports.get_configuration, _services(server), user.id, configuration_id
    )
