"""Server tool receipts fence dispatch and replay only durable, exact-call results."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolMessage, message_to_dict, messages_from_dict
from langchain_core.messages.tool import ToolCall
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.interactions import digest
from octop.infra.projects.recovery import TaskInterruption, diagnostic
from octop.infra.utils.ulid import new_ulid


class TaskReceipts:
    def __init__(self, services: SharedServices, owner: int, turn: ProjectTaskTurnRow):
        self.services, self.owner, self.turn = services, owner, turn

    @staticmethod
    def tracked(call: ToolCall) -> bool:
        return call["name"] not in {
            "planning",
            "message_notify_user",
            "ask_user_question",
        } and not call["name"].startswith("client__")

    def _uncertain(self, call_id: str) -> TaskInterruption:
        return TaskInterruption(
            diagnostic(
                "tool",
                RuntimeError(),
                can_resume=False,
                needs_reconciliation=True,
                resource_type="tool_call",
                resource_id=call_id,
            ),
            "graph",
        )

    def replay(self, call: ToolCall) -> ToolMessage | None:
        with self.services.db.transaction() as conn:
            if not self.services.project_task_repo.lock_owner(conn, self.owner):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            current = self.services.project_task_turn_repo.get_in_transaction(
                conn, self.turn.turn_id
            )
            if (
                current is None
                or current.worker_id != self.turn.worker_id
                or current.status != "running"
                or current.cancel_requested
            ):
                raise OctopError.localized(ErrorCode.TASK_INTERRUPTED)
            row = self.services.project_task_execution_receipt_repo.get(
                conn, self.turn.turn_id, str(call["id"])
            )
            if row is None:
                return None
            if (
                row.tool_name != call["name"]
                or row.arguments_digest != digest(call["args"])
                or row.state != "completed"
            ):
                raise self._uncertain(str(call["id"]))
            messages = messages_from_dict([json.loads(row.result_json)])
            message = messages[0]
            if not isinstance(message, ToolMessage) or message.tool_call_id != call["id"]:
                raise self._uncertain(str(call["id"]))
            return message

    def start(self, conn: Any, call: ToolCall) -> None:
        if not self.services.project_task_execution_receipt_repo.start(
            conn,
            "receipt_" + new_ulid(),
            self.turn.task_id,
            self.turn.turn_id,
            str(call["id"]),
            call["name"],
            digest(call["args"]),
            self.turn.worker_id,
            int(time.time() * 1000),
        ):
            raise self._uncertain(str(call["id"]))

    def _save(self, call_id: str, result: ToolMessage | Command[Any] | None) -> None:
        with self.services.db.transaction() as conn:
            if not self.services.project_task_repo.lock_owner(conn, self.owner):
                return
            current = self.services.project_task_turn_repo.get_in_transaction(
                conn, self.turn.turn_id
            )
            if (
                current is None
                or current.worker_id != self.turn.worker_id
                or current.status != "running"
            ):
                return
            repo = self.services.project_task_execution_receipt_repo
            now = int(time.time() * 1000)
            if isinstance(result, ToolMessage):
                repo.complete(
                    conn,
                    self.turn.turn_id,
                    call_id,
                    self.turn.worker_id,
                    json.dumps(message_to_dict(result), ensure_ascii=False),
                    now,
                )
            else:
                # Commands can mutate graph state beyond messages. Do not pretend a
                # message-only cache is sufficient to replay those state changes.
                repo.unknown(conn, self.turn.turn_id, call_id, self.turn.worker_id, now)

    async def run(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        import asyncio

        call_id = str(request.tool_call["id"])
        try:
            result = await handler(request)
        except BaseException:
            await asyncio.to_thread(self._save, call_id, None)
            raise
        await asyncio.to_thread(self._save, call_id, result)
        return result
