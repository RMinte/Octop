"""Owner-scoped migration control; extraction and execution live in infra."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response

from octop.api.deps import current_user, get_server, require_database
from octop.infra.projects import conversation_migrations as domain
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(prefix="/project-task-migrations", tags=["projects"])


def _wake(server: OctopServer) -> None:
    worker = getattr(server, "conversation_migration_worker", None)
    if worker is not None:
        worker.wake()


@router.post(
    "/preflight",
    response_model=domain.MigrationPreflight,
    summary="预检本人旧会话迁移范围",
    description="按本人归属盘点各普通来源，私人管家、旧原型及远端来源明确列出专项原因；不调用模型、不复制历史或修改来源。",
)
async def preflight(
    body: domain.MigrationCreate,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> domain.MigrationPreflight:
    require_database(server)
    assert server.services is not None
    return await asyncio.to_thread(domain.preflight, server.services, user.id, body)


@router.post(
    "",
    response_model=domain.MigrationBatch,
    summary="创建可恢复的旧会话迁移批次",
    description="按本人request_id幂等受理；后台固定来源、复制可达附件、核验后原子发布Task与来源路由。原始消息、文件、审批和检查点不删除，不执行历史工具。",
)
async def create(
    body: domain.MigrationCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> domain.MigrationBatch:
    require_database(server)
    assert server.services is not None
    result = await asyncio.to_thread(domain.create, server.services, user.id, body)
    response.headers["Cache-Control"] = "no-store"
    _wake(server)
    return result


@router.get("", response_model=list[domain.MigrationBatch], summary="列出本人会话迁移批次")
async def list_batches(
    server: OctopServer = Depends(get_server), user: User = Depends(current_user)
) -> list[domain.MigrationBatch]:
    require_database(server)
    assert server.services is not None
    return await asyncio.to_thread(domain.batches, server.services, user.id)


@router.get(
    "/automatic", response_model=domain.MigrationAutomation, summary="读取本人旧会话自动迁移开关"
)
async def get_automation(
    server: OctopServer = Depends(get_server), user: User = Depends(current_user)
) -> domain.MigrationAutomation:
    require_database(server)
    assert server.services is not None
    return await asyncio.to_thread(domain.automation, server.services, user.id)


@router.put(
    "/automatic",
    response_model=domain.MigrationAutomation,
    summary="启用或停用本人旧会话自动排队",
    description="启用后服务启动及定期盘点会自动建立幂等批次；无需逐条确认。停用只停止新批次排队；现有批次可分别暂停。原始数据不会删除。",
)
async def set_automation(
    body: domain.MigrationAutomation,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> domain.MigrationAutomation:
    require_database(server)
    assert server.services is not None
    result = await asyncio.to_thread(domain.automation, server.services, user.id, body.enabled)
    worker = getattr(server, "conversation_migration_worker", None)
    if worker is not None and body.enabled:
        await asyncio.to_thread(worker.enqueue_automatic)
    _wake(server)
    return result


@router.get(
    "/legacy/{thread_id}",
    response_model=domain.LegacyTaskLink,
    summary="解析本人旧会话到统一Task的来源映射",
    description="仅解析本人普通thread；返回迁移状态与已发布Task链接。待迁、正在切换及用户已删除目标均明确标记，不自动复活目标，不执行消息。",
)
async def legacy_link(
    thread_id: str, server: OctopServer = Depends(get_server), user: User = Depends(current_user)
) -> domain.LegacyTaskLink:
    require_database(server)
    assert server.services is not None
    return await asyncio.to_thread(domain.legacy_link, server.services, user.id, thread_id)


@router.get(
    "/{batch_id}", response_model=domain.MigrationBatch, summary="读取本人迁移进度与核验报告"
)
async def get_batch(
    batch_id: str, server: OctopServer = Depends(get_server), user: User = Depends(current_user)
) -> domain.MigrationBatch:
    require_database(server)
    assert server.services is not None
    return await asyncio.to_thread(domain.detail, server.services, user.id, batch_id)


@router.post(
    "/{batch_id}/control",
    response_model=domain.MigrationBatch,
    summary="暂停或恢复本人迁移批次",
    description="暂停在安全发布边界生效；已发布Task不回退。恢复重试可恢复条目并核对来源映射，保持来源与目标唯一。",
)
async def control(
    batch_id: str,
    body: domain.MigrationControl,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> domain.MigrationBatch:
    require_database(server)
    assert server.services is not None
    result = await asyncio.to_thread(
        domain.control, server.services, user.id, batch_id, body.action
    )
    _wake(server)
    return result
