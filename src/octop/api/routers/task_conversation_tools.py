"""Task export, independent memory and draft editing HTTP adapters."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from octop.api.common.content_disposition import content_disposition
from octop.api.deps import current_user, get_server
from octop.infra.projects import conversation_tools as tools
from octop.infra.projects.source_routes import local_instance
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(prefix="/project-tasks")


class TaskCapabilityFlags(BaseModel):
    tasks: bool
    projects: bool
    task_ws: bool
    files: bool
    browser: bool
    terminal: bool
    recording: bool
    tool_ui: bool
    memory: bool
    migrations: bool


class TaskCapabilitiesResponse(BaseModel):
    contract: Literal["unified-task-conversations"]
    version: Literal[1]
    protocol_version: Literal[3]
    instance_id: str
    account_id: str
    capabilities: TaskCapabilityFlags


@router.get(
    "/capabilities", response_model=TaskCapabilitiesResponse, summary="读取统一会话协议能力"
)
async def capabilities(
    server: OctopServer = Depends(get_server), user: User = Depends(current_user)
) -> dict[str, object]:
    assert server.services is not None
    can_browser = (
        user.role == "admin" or "browser" in user.permissions
    ) and importlib.util.find_spec("octop_browser") is not None
    can_terminal = (user.role == "admin" or "terminal" in user.permissions) and os.name == "posix"
    instance = await asyncio.to_thread(local_instance, server.services)
    return {
        "contract": "unified-task-conversations",
        "version": 1,
        "protocol_version": 3,
        "instance_id": instance,
        "account_id": hashlib.sha256(f"{instance}:{user.id}".encode()).hexdigest(),
        "capabilities": {
            "tasks": True,
            "projects": True,
            "task_ws": True,
            "files": True,
            "browser": can_browser,
            "terminal": can_terminal,
            "recording": can_browser,
            "tool_ui": True,
            "memory": True,
            "migrations": True,
        },
    }


@router.post("/polish", response_model=tools.PolishedPrompt, summary="润色任务草稿")
async def polish(
    body: tools.PromptPolish,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tools.PolishedPrompt:
    """只生成改写后的草稿，不创建 Task、Turn 或运行工具。"""
    assert server.services is not None
    return await tools.polish(server.services, user.id, body)


@router.get("/{task_id}/export", summary="导出全部任务会话及公开执行记录")
async def export(
    task_id: str,
    format: Literal["json", "markdown"] = "json",
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    assert server.services is not None
    payload = await asyncio.to_thread(tools.export_task, server.services, user.id, task_id)
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2)
        if format == "json"
        else tools.export_markdown(payload)
    )
    extension = "json" if format == "json" else "md"
    return Response(
        content,
        media_type="application/json" if format == "json" else "text/markdown",
        headers={
            "Content-Disposition": content_disposition(f"{task_id}.{extension}"),
            "Cache-Control": "no-store",
        },
    )


@router.get("/{task_id}/memory", response_model=tools.TaskMemoryEntries, summary="查看任务独立记忆")
async def memory(
    task_id: str,
    q: str = Query(default="", max_length=4096),
    limit: int = Query(default=50, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tools.TaskMemoryEntries:
    assert server.services is not None
    return await asyncio.to_thread(
        tools.query_task_memory, server.services, user.id, task_id, q, limit
    )


@router.post(
    "/{task_id}/memory", response_model=tools.TaskMemoryEntry, summary="保存或修正任务独立记忆"
)
async def save_memory(
    task_id: str,
    body: tools.TaskMemorySave,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tools.TaskMemoryEntry:
    assert server.services is not None
    entry = await asyncio.to_thread(
        tools.mutate_task_memory,
        server.services,
        user.id,
        task_id,
        body.request_id,
        content=body.content,
        identifier=body.id,
    )
    assert entry is not None
    return entry


@router.delete("/{task_id}/memory", status_code=204, summary="遗忘一条任务独立记忆")
async def delete_memory(
    task_id: str,
    id: str = Query(min_length=1, max_length=256),
    request_id: str = Query(pattern=r"^[A-Za-z0-9_-]{8,128}$"),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    assert server.services is not None
    await asyncio.to_thread(
        tools.mutate_task_memory, server.services, user.id, task_id, request_id, identifier=id
    )
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
