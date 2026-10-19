"""Shared task approval policy contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, StringConstraints, model_validator

from octop.infra.projects.models import ProjectModel

ApprovalMode = Literal["ask_for_approval", "approve_for_me", "full_access"]
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("ask_for_approval", "approve_for_me", "full_access")
RequestID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]


class TaskApprovalPolicy(ProjectModel):
    mode: ApprovalMode
    revision: int = Field(ge=1, description="当前账号全部任务共用的审批模式版本；切换时递增。")
    updated_at: datetime


class ApprovalPolicyUpdate(ProjectModel):
    request_id: RequestID = Field(description="本次切换的稳定标识；网络重试必须复用。")
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    mode: ApprovalMode


class TaskApprovalSettings(ProjectModel):
    require_human_tools: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    ] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_tools(self) -> Self:
        if len(self.require_human_tools) != len(set(self.require_human_tools)):
            raise ValueError("duplicate tool names")
        return self


class ApprovalCapabilities(ProjectModel):
    allowed_modes: list[ApprovalMode] = Field(
        description="固定支持全部三种任务审批模式，无需管理员开启。"
    )
    reviewer_ready: bool = Field(
        description="所选或默认任务模型当前可供独立 Reviewer 使用；仅报告状态，不限制模式选择。"
    )
    execution_profile: Literal["host_process"] = Field(
        default="host_process", description="服务端工具在 Octop 主机进程内执行。"
    )
