"""Read task-owned images again after older visual input has been compacted."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import Field

from octop.i18n import tr
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.input_attachments import IMAGE_SCHEME, download, for_task, is_image
from octop.infra.projects.models import ProjectModel, Reference

READ_TASK_IMAGE = "read_task_image"
TASK_IMAGE_KEY = "octop_task_image"


class _ReadImageArgs(ProjectModel):
    attachment_id: Reference | None = Field(default=None)
    call_id: Annotated[str, InjectedToolCallId]


def read_image_tool(
    services: SharedServices, owner_id: int, task_id: str, locale: str
) -> StructuredTool:
    async def read(call_id: str, attachment_id: str | None = None) -> ToolMessage:
        try:
            if attachment_id is None:
                attachments = await asyncio.to_thread(for_task, services, owner_id, task_id)
                return ToolMessage(
                    content=json.dumps(
                        [
                            item.model_dump(mode="json")
                            for item in attachments
                            if is_image(item.media_type)
                        ],
                        ensure_ascii=False,
                    ),
                    tool_call_id=call_id,
                    name=READ_TASK_IMAGE,
                )
            metadata, _ = await asyncio.to_thread(
                download, services, owner_id, attachment_id, task_id=task_id
            )
            if not is_image(metadata.media_type):
                raise OctopError.localized(ErrorCode.TASK_IMAGE_INVALID)
            return ToolMessage(
                content=tr(
                    "projects.task_image_loaded",
                    locale,
                    name=metadata.name,
                    attachment_id=attachment_id,
                )
                + "\n"
                + json.dumps(
                    {
                        "attachment_id": attachment_id,
                        "name": metadata.name,
                        "source": IMAGE_SCHEME + attachment_id,
                    },
                    ensure_ascii=False,
                ),
                additional_kwargs={TASK_IMAGE_KEY: attachment_id},
                tool_call_id=call_id,
                name=READ_TASK_IMAGE,
            )
        except OctopError as exc:
            return ToolMessage(
                content=json.dumps(
                    {"error_code": exc.code.value, "message": exc.localized_message(locale)},
                    ensure_ascii=False,
                ),
                tool_call_id=call_id,
                name=READ_TASK_IMAGE,
                status="error",
            )

    return StructuredTool.from_function(
        name=READ_TASK_IMAGE,
        description=tr("projects.read_task_image_description", locale),
        args_schema=_ReadImageArgs,
        coroutine=read,
    )
