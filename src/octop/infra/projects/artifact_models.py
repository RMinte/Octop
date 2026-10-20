"""Public task attachment metadata and server tool arguments."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from octop.infra.projects.models import ProjectModel

ArtifactName = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class CreateArtifactArgs(ProjectModel):
    name: ArtifactName = Field(description="附件文件名，含文本格式扩展名，不含目录。")
    content: str = Field(description="完整 UTF-8 文本内容；不会写入工作目录。")
    media_type: str | None = Field(default=None, max_length=128, description="可选 MIME 类型。")

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return validate_name(value)

    @field_validator("media_type")
    @classmethod
    def valid_media_type(cls, value: str | None) -> str | None:
        if value is not None and (
            re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", value) is None
        ):
            raise ValueError("invalid media type")
        return value


class PublishWorkspaceFileArgs(ProjectModel):
    path: str = Field(min_length=1, max_length=4096, description="服务端工作目录内文件的相对路径。")
    name: ArtifactName | None = Field(
        default=None, description="可选附件文件名，省略沿用源文件名。"
    )

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or ":" in value
            or any(ord(char) < 32 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("a workspace-relative POSIX path is required")
        validate_name(PurePosixPath(value).name)
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        return validate_name(value) if value is not None else None


class PublishWorkspaceArchiveArgs(ProjectModel):
    paths: list[str] = Field(
        min_length=1,
        max_length=100,
        description="明确选定的服务端工作目录相对文件路径；不接受目录、通配符或自动扫描。",
    )
    name: ArtifactName = Field(description="交付 ZIP 文件名，必须以 .zip 结尾，不含目录。")

    @field_validator("paths")
    @classmethod
    def valid_paths(cls, values: list[str]) -> list[str]:
        paths = [PublishWorkspaceFileArgs(path=value).path for value in values]
        if len(paths) != len(set(paths)):
            raise ValueError("archive paths must be unique")
        return paths

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = validate_name(value)
        if PurePosixPath(value).suffix.lower() != ".zip":
            raise ValueError("a ZIP filename is required")
        return value


def validate_name(value: str) -> str:
    if (
        not 1 <= len(value) <= 240
        or value != value.strip()
        or value in {".", ".."}
        or any(char in value for char in "/\\:")
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("a single safe filename is required")
    return value


class TaskArtifact(ProjectModel):
    artifact_id: str
    task_id: str
    turn_id: str
    name: str
    media_type: str
    byte_count: int
    sha256: str
    source_path: str | None = Field(
        description="发布已有文件时的服务端工作目录相对路径；直接创建为 null。"
    )
    download_url: str = Field(description="需要当前账号鉴权的相对 HTTP 下载地址；不包含令牌。")
    created_at: datetime
