"""Expand task image references for a model request without changing durable history."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, ToolMessage

from octop.infra.db.services import SharedServices
from octop.infra.gateway.media.attachment_hints import make_image_url_block
from octop.infra.projects.image_tools import TASK_IMAGE_KEY
from octop.infra.projects.input_attachments import IMAGE_SCHEME, download, model_image_bytes


def _attachment_id(block: Any) -> str | None:
    if not isinstance(block, dict) or block.get("type") != "image_url":
        return None
    field = block.get("image_url")
    url = field.get("url") if isinstance(field, dict) else field
    if isinstance(url, str) and url.startswith(IMAGE_SCHEME):
        return url[len(IMAGE_SCHEME) :]
    return None


class TaskImageMaterializeMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, services: SharedServices, owner_id: int, task_id: str) -> None:
        self._services, self._owner_id, self._task_id = services, owner_id, task_id

    def _expand_messages(self, messages: Sequence[Any]) -> list[Any]:
        out: list[Any] = []
        expanded: dict[str, dict[str, Any]] = {}
        pending_images: dict[str, None] = {}

        def image(attachment_id: str) -> dict[str, Any]:
            if attachment_id not in expanded:
                metadata, data = download(
                    self._services, self._owner_id, attachment_id, task_id=self._task_id
                )
                data, media_type = model_image_bytes(data, metadata.media_type)
                expanded[attachment_id] = make_image_url_block(
                    base64.b64encode(data).decode("ascii"), media_type
                )
            return expanded[attachment_id]

        for index, message in enumerate(messages):
            if isinstance(message, ToolMessage):
                attachment_id = message.additional_kwargs.get(TASK_IMAGE_KEY)
                if isinstance(attachment_id, str):
                    pending_images[attachment_id] = None
                out.append(message)
                if pending_images and (
                    index + 1 == len(messages) or not isinstance(messages[index + 1], ToolMessage)
                ):
                    tool_blocks: list[Any] = []
                    for attachment_id in pending_images:
                        tool_blocks.extend(
                            [
                                {"type": "text", "text": IMAGE_SCHEME + attachment_id},
                                image(attachment_id),
                            ]
                        )
                    out.append(HumanMessage(content=tool_blocks))
                    pending_images.clear()
                continue
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                out.append(message)
                continue
            blocks: list[Any] = []
            changed = False
            for block in content:
                attachment_id = _attachment_id(block)
                if attachment_id is None:
                    blocks.append(block)
                    continue
                changed = True
                blocks.append(image(attachment_id))
            out.append(message.model_copy(update={"content": blocks}) if changed else message)
        return out

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        messages = self._expand_messages(request.messages)
        return handler(request.override(messages=messages))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        messages = await asyncio.to_thread(self._expand_messages, request.messages)
        return await handler(request.override(messages=messages))
