"""Typed human interaction and pause commands for a single task turn."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictInt, StringConstraints

from octop.infra.projects.approval_models import ApprovalMode
from octop.infra.projects.models import ProjectModel
from octop.infra.projects.review_models import ApprovalReview

RequestID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
Answer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class QuestionOption(ProjectModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: str = Field(default="", max_length=200)


class QuestionPayload(ProjectModel):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    header: str | None = Field(default=None, max_length=24)
    options: list[QuestionOption] = Field(default_factory=list, max_length=3)
    multi_select: Literal[False] = False


class AskQuestions(ProjectModel):
    questions: list[QuestionPayload] = Field(min_length=1, max_length=3)


class ApprovalPayload(ProjectModel):
    approval_mode: ApprovalMode = "ask_for_approval"
    approval_revision: int = 1
    action_signature: str = ""
    tool_name: str
    arguments: dict[str, JsonValue]
    execution_host: Literal["server"] = "server"
    reason: str
    action_digest: str
    allowed_decisions: list[Literal["allow_once", "deny"]]


class InteractionResolution(ProjectModel):
    outcome: Literal["allow_once", "deny", "answered", "skipped", "defaulted", "unanswered"]
    source: Literal[
        "user", "user_default", "timeout", "auto_reviewer", "full_access_mode", "previous_denial"
    ]
    answer: str | None = None


class TaskInteraction(ProjectModel):
    interaction_id: str
    task_id: str
    turn_id: str
    kind: Literal["approval", "question"]
    tool_call_id: str
    supersedes_interaction_id: str | None = Field(
        default=None, description="用户明确恢复后新审批所续接的原超时交互；原记录保留。"
    )
    dispatch_started: bool = False
    revision: int
    status: Literal["reviewing", "pending", "resolved", "cancelled", "expired"]
    review: ApprovalReview | None = None
    payload: ApprovalPayload | QuestionPayload
    resolution: InteractionResolution | None = None
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None


class ApprovalResponse(ProjectModel):
    kind: Literal["approval"]
    decision: Literal["allow_once", "deny"]
    action_digest: str = Field(min_length=1, max_length=128)


class QuestionAnswer(ProjectModel):
    kind: Literal["question"]
    action: Literal["answer"]
    answer: Answer


class QuestionDefault(ProjectModel):
    kind: Literal["question"]
    action: Literal["skip", "use_default"]


class InteractionResponse(ProjectModel):
    request_id: RequestID = Field(description="原轮次内的持久幂等标识；重发保持同一请求体。")
    expected_revision: Annotated[StrictInt, Field(ge=1)] = Field(
        description="读取的单项交互版本，不匹配返回 409。"
    )
    response: ApprovalResponse | QuestionAnswer | QuestionDefault


class TaskControl(ProjectModel):
    request_id: RequestID = Field(description="该控制意图的持久幂等标识；重发不再次应用旧意图。")
    turn_id: str = Field(min_length=1, max_length=256)


WAIT_STATUSES = {
    "approval": "waiting_approval",
    "engine_approval": "waiting_approval",
    "answer": "waiting_answer",
    "client_tool": "waiting_client_tool",
    "user": "paused",
    "interrupted": "interrupted",
}


def public_status(status: str, wait_kind: str) -> str:
    return WAIT_STATUSES.get(wait_kind, status) if status == "running" else status
