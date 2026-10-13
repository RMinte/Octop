"""Trusted internal question operations; answers do not resume an engine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from octop.infra.agentcomputer.models import (
    EngineInvocation,
    QuestionAnswerCommand,
    RunPauseContent,
    RunPauseDecisionReceipt,
    RunPauseIntent,
    RunPauseRecord,
)
from octop.infra.agentcomputer.pauses import _decision, open_pause
from octop.infra.db.services import SharedServices


def ask_question(
    services: SharedServices,
    invocation: EngineInvocation,
    *,
    question: RunPauseContent,
    expires_at: datetime,
) -> RunPauseRecord:
    return open_pause(
        services, invocation, kind="question", content=question, expires_at=expires_at
    )


def answer_question(
    services: SharedServices,
    *,
    owner_id: int,
    command: QuestionAnswerCommand,
    current_question: RunPauseContent,
    authorize: Callable[[RunPauseIntent], bool],
) -> RunPauseDecisionReceipt:
    return _decision(
        services,
        owner_id=owner_id,
        command=command,
        kind="question",
        content=current_question,
        authorize=authorize,
    )
