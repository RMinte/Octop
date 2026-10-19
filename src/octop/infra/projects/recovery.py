"""Durable interruption diagnostics, separate from terminal failures and retries."""

from __future__ import annotations

import json
from typing import Literal
from urllib.error import HTTPError, URLError

import httpx
from pydantic import Field

from octop.infra.projects.models import ProjectModel
from octop.infra.skills.skillhub_market import SkillHubMarketTimeout, SkillHubPackageError
from octop.infra.utils.ulid import new_ulid

RecoveryMode = Literal["prepare", "graph", "wait"]


class TaskRecovery(ProjectModel):
    can_resume: bool = Field(
        description="服务端是否允许尝试从原轮断点继续；执行前仍校验断点和权限。"
    )
    needs_reconciliation: bool = Field(
        default=False, description="有已派发但结果未知的操作，禁止自动重放。"
    )
    phase: str = Field(description="中断发生的具体阶段。")
    reason_code: str
    cause_type: str = Field(description="底层异常类型，不包含异常正文、请求参数或凭据。")
    diagnostic_id: str
    resource_type: str | None = None
    resource_id: str | None = None
    status_code: int | None = None


class TaskInterruption(Exception):
    def __init__(self, recovery: TaskRecovery, mode: RecoveryMode, checkpoint_id: str = ""):
        super().__init__(recovery.reason_code)
        self.recovery, self.mode, self.checkpoint_id = recovery, mode, checkpoint_id

    def encode(self) -> str:
        return json.dumps({**self.recovery.model_dump(), "mode": self.mode})


def diagnostic(
    phase: str,
    exc: BaseException,
    *,
    can_resume: bool,
    needs_reconciliation: bool = False,
    reason_code: str = "TASK_INTERRUPTED",
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> TaskRecovery:
    cause = exc
    seen: set[int] = set()
    while id(cause) not in seen:
        seen.add(id(cause))
        nested = cause.__cause__
        if nested is None:
            break
        cause = nested
    status = getattr(cause, "status_code", None) or getattr(
        getattr(cause, "response", None), "status_code", None
    )
    if isinstance(cause, HTTPError):
        status = cause.code
    return TaskRecovery(
        can_resume=can_resume,
        needs_reconciliation=needs_reconciliation,
        phase=phase,
        reason_code=reason_code,
        cause_type=type(cause).__name__,
        diagnostic_id="diag_" + new_ulid(),
        resource_type=resource_type,
        resource_id=resource_id,
        status_code=status if isinstance(status, int) else None,
    )


def transient(exc: BaseException) -> bool:
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, SkillHubPackageError):
            return False
        if isinstance(exc, HTTPError):
            return exc.code == 429 or exc.code >= 500
        if isinstance(exc, BaseExceptionGroup):
            return all(transient(child) for child in exc.exceptions)
        if isinstance(
            exc,
            (TimeoutError, ConnectionError, httpx.TransportError, URLError, SkillHubMarketTimeout),
        ):
            return True
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        if isinstance(status, int) and (status == 429 or status >= 500):
            return True
        if exc.__cause__ is None:
            return False
        exc = exc.__cause__
    return False
