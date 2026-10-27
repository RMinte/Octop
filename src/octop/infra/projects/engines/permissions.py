"""Persist native permission decisions while retaining the live ACP connection."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from octop.i18n import tr
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_runtime, interactions
from octop.infra.projects.engines.base import EngineRequest
from octop.infra.projects.interaction_models import ApprovalPayload
from octop.infra.projects.tasks import now_ms


def payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else value.model_dump(mode="json", by_alias=True)


class _LeaseLost(Exception):
    """Roll back synchronous SQL before propagating cancellation asynchronously."""


class EnginePermissions:
    def __init__(self, request: EngineRequest, locale: str) -> None:
        self.request, self.locale = request, locale
        self._lock = asyncio.Lock()

    def _owned(self, services: SharedServices, conn: Any) -> Any:
        request = self.request
        if not services.project_task_repo.lock_owner(conn, request.owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        task = services.project_task_repo.get_in_transaction(
            conn, request.turn.task_id, request.owner_id
        )
        turn = services.project_task_turn_repo.get_in_transaction(conn, request.turn.turn_id)
        if (
            task is None
            or task.active_turn_id != request.turn.turn_id
            or turn is None
            or turn.worker_id != request.turn.worker_id
            or turn.status != "running"
            or turn.cancel_requested
        ):
            raise _LeaseLost
        return turn

    def _begin(self, tool: dict[str, Any], allow: bool) -> str:
        request, services = self.request, self.request.services
        raw = tool.get("rawInput")
        arguments = (
            raw
            if isinstance(raw, dict) and raw
            else {
                key: tool[key]
                for key in ("title", "kind", "locations", "content", "rawInput")
                if tool.get(key) not in (None, "", {}, [])
            }
        )
        name = (
            "execute" if tool.get("kind") == "execute" else "acp_" + str(tool.get("kind") or "tool")
        )
        proposal = ApprovalPayload(
            tool_name=name,
            arguments=arguments,
            reason=tr("projects.engine_permission_reason", self.locale),
            action_digest=interactions.digest(tool),
            allowed_decisions=["allow_once", "deny"] if allow else ["deny"],
        )
        with services.db.transaction() as conn:
            current = self._owned(services, conn)
            items = interactions.load(current)
            item = interactions.new(current, proposal, str(tool.get("toolCallId") or ""))
            approval_runtime.freeze_server(
                services, conn, request.owner_id, current, item, items, now_ms()
            )
            services.project_task_turn_repo.save_interactions(
                conn, current.turn_id, interactions.dump([*items, item]), now_ms()
            )
            if not services.project_task_turn_repo.engine_wait(conn, current, True, now_ms()):
                raise _LeaseLost
            services.project_task_repo.wait(
                conn, current.task_id, current.turn_id, "approval", now_ms()
            )
            return item.interaction_id

    def _poll(self, identifier: str) -> str | None:
        services = self.request.services
        with services.db.transaction() as conn:
            current = self._owned(services, conn)
            items = interactions.load(current)
            if interactions.expire(items):
                services.project_task_turn_repo.save_interactions(
                    conn, current.turn_id, interactions.dump(items), now_ms()
                )
            item = next(item for item in items if item.interaction_id == identifier)
            if item.status in {"pending", "reviewing"}:
                return None
            return item.resolution.outcome if item.resolution else "deny"

    def _end(self) -> None:
        services = self.request.services
        with services.db.transaction() as conn:
            current = self._owned(services, conn)
            if services.project_task_turn_repo.engine_wait(conn, current, False, now_ms()):
                services.project_task_repo.wait(
                    conn, current.task_id, current.turn_id, "", now_ms()
                )

    async def __call__(self, session_id: str, tool_call: Any, options: list[Any]) -> str | None:
        try:
            return await self._decide(session_id, tool_call, options)
        except _LeaseLost:
            raise asyncio.CancelledError from None

    async def _decide(self, session_id: str, tool_call: Any, options: list[Any]) -> str | None:
        del session_id
        async with self._lock:
            normalized = [payload(option) for option in options]
            allow = next(
                (item["optionId"] for item in normalized if item.get("kind") == "allow_once"), None
            )
            deny = next(
                (item["optionId"] for item in normalized if item.get("kind") == "reject_once"), None
            )
            # Only the exact native proposal is approved; permanent native grants
            # are never synthesized from Octop's one-action decision.
            tool = json.loads(json.dumps(payload(tool_call), allow_nan=False))
            identifier = await asyncio.to_thread(self._begin, tool, allow is not None)
            try:
                while (decision := await asyncio.to_thread(self._poll, identifier)) is None:
                    await asyncio.sleep(0.2)
                return allow if decision == "allow_once" else deny
            finally:
                with contextlib.suppress(_LeaseLost):
                    await asyncio.to_thread(self._end)
