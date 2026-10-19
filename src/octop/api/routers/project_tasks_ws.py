"""Multiplexed task conversations over one authenticated WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from octop.api.deps import extract_raw_token, maybe_sliding_renew_token, resolve_user_from_token
from octop.api.routers.projects import _services, _task_worker
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import (
    approval_policy,
    client_reviews,
    client_tools,
    interactions,
    tasks,
    title,
)
from octop.infra.projects.approval_models import ApprovalPolicyUpdate
from octop.infra.projects.client_tool_models import (
    ClientID,
    ClientToolCall,
    ClientToolClaim,
    ClientToolResult,
    ClientToolReviewRequest,
)
from octop.infra.projects.interaction_models import InteractionResponse, TaskControl
from octop.infra.projects.models import ProjectModel, Reference
from octop.infra.utils.locale import resolve_locale, resolve_request_locale

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/project-tasks")
_AUTH_CHECK_SECONDS = 10.0
_UPDATE_SECONDS = 0.5
_MAX_SUBSCRIPTIONS = 32
_MAX_FRAME_BYTES = 512 * 1024


class AuthenticateFrame(ProjectModel):
    type: Literal["authenticate"]
    token: Annotated[str, StringConstraints(min_length=1, max_length=16384)]
    locale: Literal["zh", "en"] | None = None


class CommandFrame(ProjectModel):
    id: tasks.RequestID = Field(description="关联本次命令响应；网络重发使用新的命令 ID。")


class CreateFrame(CommandFrame):
    type: Literal["task.create"]
    data: tasks.TaskCreate


class MessageFrame(CommandFrame):
    type: Literal["task.message"]
    task_id: Reference
    data: tasks.TaskMessage


class RetryFrame(CommandFrame):
    type: Literal["task.retry"]
    task_id: Reference
    turn_id: Reference
    data: tasks.TaskRetry


class CancelFrame(CommandFrame):
    type: Literal["task.cancel"]
    task_id: Reference
    data: tasks.TaskCancel


class PauseFrame(CommandFrame):
    type: Literal["task.pause", "task.resume"]
    task_id: Reference
    data: TaskControl


class ApprovalPolicyFrame(CommandFrame):
    type: Literal["task.approval.set"]
    task_id: Reference
    data: ApprovalPolicyUpdate


class InteractionFrame(CommandFrame):
    type: Literal["task.interaction.respond"]
    task_id: Reference
    turn_id: Reference
    interaction_id: Reference
    data: InteractionResponse


class SubscribeFrame(CommandFrame):
    type: Literal["task.subscribe"]
    task_id: Reference
    cursor: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None


class OpenFrame(CommandFrame):
    type: Literal["task.open"]
    task_id: Reference = Field(description="用户明确进入详情的任务；按需补生成缺少的语义标题。")


class UnsubscribeFrame(CommandFrame):
    type: Literal["task.unsubscribe"]
    task_id: Reference


class PingFrame(CommandFrame):
    type: Literal["ping"]


class AttachFrame(CommandFrame):
    type: Literal["client.attach"]
    client_id: ClientID = Field(description="稳定的客户端安装标识；连接内不可切换。")


class ToolIdentityFrame(CommandFrame):
    task_id: Reference
    turn_id: Reference
    call_id: Reference


class ToolGetFrame(ToolIdentityFrame):
    type: Literal["tool.get"]


class ToolClaimFrame(ToolIdentityFrame):
    type: Literal["tool.claim"]
    request_id: tasks.RequestID = Field(description="本次领取的稳定标识；恢复领取必须复用。")


class ToolResultFrame(ToolIdentityFrame):
    type: Literal["tool.result"]
    data: ClientToolResult


class ToolReviewFrame(ToolIdentityFrame):
    type: Literal["tool.review"]
    data: ClientToolReviewRequest


TaskCommand = Annotated[
    CreateFrame
    | MessageFrame
    | RetryFrame
    | CancelFrame
    | PauseFrame
    | ApprovalPolicyFrame
    | InteractionFrame
    | OpenFrame
    | SubscribeFrame
    | UnsubscribeFrame
    | PingFrame
    | AttachFrame
    | ToolGetFrame
    | ToolClaimFrame
    | ToolResultFrame
    | ToolReviewFrame,
    Field(discriminator="type"),
]
_command: TypeAdapter[TaskCommand] = TypeAdapter(TaskCommand)
_command_id: TypeAdapter[tasks.RequestID] = TypeAdapter(tasks.RequestID)


@router.websocket("/ws")
async def task_websocket(websocket: WebSocket) -> None:
    """Task commands and resumable state pushes, independent of worker lifetime."""
    server = websocket.app.state.octop_server
    try:
        services = _services(server)
    except OctopError as exc:
        await websocket.close(code=1013, reason=exc.code.value)
        return
    await websocket.accept()
    locale = resolve_request_locale(websocket)
    token = extract_raw_token(authorization=websocket.headers.get("authorization"))
    subscriptions: dict[str, str] = {}
    client_id: str | None = None
    delivered_calls: dict[str, ClientToolCall] = {}
    # Commands and pushes share ordering, including subscription cursor changes.
    lock = asyncio.Lock()
    pump: asyncio.Task[None] | None = None

    async def error(exc: OctopError, **identity: str) -> None:
        await websocket.send_json(
            {"type": "error", **identity, "status": exc.status, **exc.to_envelope(locale=locale)}
        )

    async def authenticate() -> None:
        nonlocal token
        assert token is not None
        current = await asyncio.to_thread(resolve_user_from_token, server, token)
        if current.id != user.id:
            raise OctopError.localized(ErrorCode.AUTH_FAILED)
        renewed = await asyncio.to_thread(maybe_sliding_renew_token, server, token, current)
        if renewed:
            token = renewed
            await websocket.send_json({"type": "session", "access_token": renewed})

    async def push_updates() -> None:
        checked = time.monotonic()
        delivered_policy = None
        try:
            while True:
                await asyncio.sleep(_UPDATE_SECONDS)
                async with lock:
                    if time.monotonic() - checked >= _AUTH_CHECK_SECONDS:
                        await authenticate()
                        await websocket.send_json({"type": "heartbeat"})
                        checked = time.monotonic()
                    policy = await asyncio.to_thread(approval_policy.current, services, user.id)
                    if policy != delivered_policy:
                        await websocket.send_json(
                            {"type": "approval.policy", "data": policy.model_dump(mode="json")}
                        )
                        delivered_policy = policy
                    for task_id, cursor in list(subscriptions.items()):
                        try:
                            updates = await asyncio.to_thread(
                                tasks.updates, services, user.id, task_id, cursor
                            )
                        except OctopError as exc:
                            subscriptions.pop(task_id, None)
                            await error(exc, task_id=task_id)
                            continue
                        for update in updates:
                            await websocket.send_json(
                                {"type": "turn", "data": update.model_dump(mode="json")}
                            )
                            subscriptions[task_id] = update.cursor
                    if client_id is not None:
                        calls = await asyncio.to_thread(
                            client_tools.open_calls, services, user.id, client_id
                        )
                        current_ids = {call.call_id for call in calls}
                        for call_id, previous in list(delivered_calls.items()):
                            if call_id not in current_ids:
                                calls.append(
                                    await asyncio.to_thread(
                                        client_tools.get_call,
                                        services,
                                        user.id,
                                        client_id,
                                        previous.task_id,
                                        previous.turn_id,
                                        call_id,
                                    )
                                )
                        for call in calls:
                            if delivered_calls.get(call.call_id) == call:
                                continue
                            active = call.status in {"pending", "claimed"}
                            await websocket.send_json(
                                {
                                    "type": "tool.call" if active else "tool.closed",
                                    "data": call.model_dump(mode="json"),
                                }
                            )
                            if active:
                                delivered_calls[call.call_id] = call
                            else:
                                delivered_calls.pop(call.call_id, None)
        except OctopError as exc:
            await error(exc)
            await websocket.close(code=4401, reason=exc.code.value)
        except (WebSocketDisconnect, OSError):
            pass
        except Exception:
            logger.exception("Task WebSocket subscription failed")
            with contextlib.suppress(Exception):
                await error(OctopError.localized(ErrorCode.INTERNAL_ERROR))
                await websocket.close(code=1011)

    try:
        if token is None:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
                auth = AuthenticateFrame.model_validate_json(raw)
                token = auth.token
                locale = auth.locale or locale
            except (TimeoutError, ValidationError):
                raise OctopError.localized(ErrorCode.AUTH_FAILED) from None
        user = await asyncio.to_thread(resolve_user_from_token, server, token)
        locale = resolve_locale(user_locale=user.locale, explicit=locale)
        await websocket.send_json({"type": "ready", "protocol_version": 3})
        pump = asyncio.create_task(push_updates())
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > _MAX_FRAME_BYTES:
                await websocket.close(code=1009)
                return
            async with lock:
                await authenticate()
                try:
                    frame = _command.validate_json(raw)
                except ValidationError:
                    # Never echo invalid inputs: they may contain prompts or tokens.
                    identity = {}
                    try:
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            identity["id"] = _command_id.validate_python(payload.get("id"))
                    except (ValueError, ValidationError):
                        pass
                    await error(OctopError.localized(ErrorCode.TASK_FRAME_INVALID), **identity)
                    continue
                try:
                    result: Any = None
                    if isinstance(frame, AttachFrame):
                        if client_id is not None and frame.client_id != client_id:
                            raise OctopError.localized(ErrorCode.TASK_FRAME_INVALID)
                        client_id = frame.client_id
                    elif isinstance(frame, ToolIdentityFrame):
                        if client_id is None:
                            raise OctopError.localized(ErrorCode.TASK_FRAME_INVALID)
                        args = (
                            services,
                            user.id,
                            client_id,
                            frame.task_id,
                            frame.turn_id,
                            frame.call_id,
                        )
                        receipt: ClientToolCall | ClientToolClaim
                        if isinstance(frame, ToolReviewFrame):
                            receipt = await asyncio.to_thread(
                                client_reviews.request_review, *args, frame.data
                            )
                            if server.project_task_worker is not None:
                                server.project_task_worker.review_worker.wake()
                        elif isinstance(frame, ToolClaimFrame):
                            receipt = await asyncio.to_thread(
                                client_tools.claim_call, *args, frame.request_id
                            )
                        elif isinstance(frame, ToolResultFrame):
                            receipt = await asyncio.to_thread(
                                client_tools.submit_result, *args, frame.data
                            )
                        else:
                            receipt = await asyncio.to_thread(client_tools.get_call, *args)
                        result = receipt.model_dump(mode="json")
                    elif isinstance(frame, OpenFrame):
                        worker = _task_worker(server)
                        detail = await asyncio.to_thread(
                            title.open_task, services, user.id, frame.task_id
                        )
                        worker.title_worker.wake()
                        result = detail.model_dump(mode="json")
                    elif isinstance(frame, SubscribeFrame):
                        if (
                            frame.task_id not in subscriptions
                            and len(subscriptions) >= _MAX_SUBSCRIPTIONS
                        ):
                            raise OctopError.localized(ErrorCode.TASK_FRAME_INVALID)
                        cursor = frame.cursor
                        if cursor is None:
                            snapshot = await asyncio.to_thread(
                                tasks.detail, services, user.id, frame.task_id
                            )
                            await websocket.send_json(
                                {"type": "snapshot", "data": snapshot.model_dump(mode="json")}
                            )
                            cursor = snapshot.event_cursor
                        assert cursor is not None
                        updates = await asyncio.to_thread(
                            tasks.updates, services, user.id, frame.task_id, cursor
                        )
                        for update in updates:
                            await websocket.send_json(
                                {"type": "turn", "data": update.model_dump(mode="json")}
                            )
                            cursor = update.cursor
                        subscriptions[frame.task_id] = cursor
                    elif isinstance(frame, UnsubscribeFrame):
                        subscriptions.pop(frame.task_id, None)
                    elif isinstance(frame, ApprovalPolicyFrame):
                        detail = await asyncio.to_thread(
                            approval_policy.set_policy,
                            services,
                            user.id,
                            frame.task_id,
                            frame.data,
                        )
                        result = detail.model_dump(mode="json")
                    elif isinstance(frame, (PauseFrame, InteractionFrame)):
                        if isinstance(frame, PauseFrame):
                            await asyncio.to_thread(
                                interactions.control,
                                services,
                                user.id,
                                frame.task_id,
                                frame.data,
                                paused=frame.type == "task.pause",
                            )
                        else:
                            await asyncio.to_thread(
                                interactions.respond,
                                services,
                                user.id,
                                frame.task_id,
                                frame.turn_id,
                                frame.interaction_id,
                                frame.data,
                            )
                        if server.project_task_worker is not None:
                            server.project_task_worker.wake()
                        detail = await asyncio.to_thread(
                            tasks.detail, services, user.id, frame.task_id
                        )
                        result = detail.model_dump(mode="json")
                    elif isinstance(frame, CancelFrame):
                        detail = await asyncio.to_thread(
                            tasks.cancel, services, user.id, frame.task_id, frame.data.turn_id
                        )
                        result = detail.model_dump(mode="json")
                    elif not isinstance(frame, PingFrame):
                        worker = _task_worker(server)
                        kwargs: dict[str, Any] = {}
                        if isinstance(frame, CreateFrame):
                            kwargs["project_id"] = frame.data.project_id
                        else:
                            kwargs["task_id"] = frame.task_id
                        if isinstance(frame, RetryFrame):
                            kwargs["retry_of_turn_id"] = frame.turn_id
                        detail = await asyncio.to_thread(
                            tasks.send, services, user.id, frame.data, **kwargs
                        )
                        worker.wake()
                        result = detail.model_dump(mode="json")
                    await websocket.send_json({"type": "result", "id": frame.id, "data": result})
                except OctopError as exc:
                    await error(exc, id=frame.id)
    except OctopError as exc:
        await error(exc)
        await websocket.close(code=4401, reason=exc.code.value)
    except (WebSocketDisconnect, OSError):
        pass
    except Exception:
        logger.exception("Task WebSocket command failed")
        with contextlib.suppress(Exception):
            await error(OctopError.localized(ErrorCode.INTERNAL_ERROR))
            await websocket.close(code=1011)
    finally:
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
