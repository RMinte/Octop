"""Explicit legacy import request and authenticated links to unchanged source files."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from octop.infra.projects.models import ProjectModel, Reference


class LegacyConversationCreate(ProjectModel):
    request_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")] = Field(
        description="显式接入的幂等标识；重复请求复用同一工作记录，不持续同步旧会话。"
    )
    agent_id: Reference = Field(description="本人本地专家、本人团队或已授权共享专家的 agent_id。")
    thread_id: Reference = Field(description="该专家下属于当前用户的旧会话。")
    project_id: Reference | None = Field(description="目标项目；显式 null 为无项目。")
    selected_expert_id: Reference | None = Field(
        default=None, description="省略继承来源专家；显式 null 使用通用助手。"
    )


class LegacySourceAttachment(ProjectModel):
    name: str
    media_type: str = "application/octet-stream"
    source_agent_id: str
    workspace_path: str | None = None
    download_url: str | None = Field(
        default=None, description="原专家工作区的鉴权下载地址；未复制到新任务目录。"
    )
    available: bool = Field(description="接入时可在原工作区读取；不可达资源明确保留为缺失引用。")


class LegacyTrajectoryEvent(ProjectModel):
    source_event_id: str
    source_sequence: int
    source_turn_id: str | None = None
    source_agent_id: str
    kind: str
    created_at: datetime | None = None
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    speaker_agent_id: str | None = None
    speaker_name: str | None = None
    source_status: Literal["recorded", "incomplete", "redacted"] = "recorded"
    mapping: Literal["tool_call", "message", "source_thread"] = "source_thread"
    history_only: Literal[True] = True
    restorable: Literal[False] = False
