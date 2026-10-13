"""Trusted internal approval operations; no public HITL resume hook."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from octop.infra.agentcomputer.models import (
    ApprovalDecisionCommand,
    EngineInvocation,
    RunPauseContent,
    RunPauseDecisionReceipt,
    RunPauseIntent,
    RunPauseRecord,
)
from octop.infra.agentcomputer.pauses import _decision, open_pause
from octop.infra.db.services import SharedServices


def request_approval(
    services: SharedServices,
    invocation: EngineInvocation,
    *,
    action: RunPauseContent,
    expires_at: datetime,
) -> RunPauseRecord:
    return open_pause(services, invocation, kind="approval", content=action, expires_at=expires_at)


def decide_approval(
    services: SharedServices,
    *,
    owner_id: int,
    command: ApprovalDecisionCommand,
    current_action: RunPauseContent,
    authorize: Callable[[RunPauseIntent], bool],
) -> RunPauseDecisionReceipt:
    return _decision(
        services,
        owner_id=owner_id,
        command=command,
        kind="approval",
        content=current_action,
        authorize=authorize,
    )
