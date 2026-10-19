"""Durable handoff to an authenticated client's local tool executor."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool

from octop.infra.db.repos.project_task_tool_calls import ProjectTaskToolCallRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import approval_policy, interactions
from octop.infra.projects.approval_runtime import review_view
from octop.infra.projects.client_tool_models import (
    CallStatus,
    ClientApprovalContext,
    ClientToolCall,
    ClientToolClaim,
    ClientToolDefinition,
    ClientToolResult,
)
from octop.infra.utils.ulid import new_ulid

CALL_TIMEOUT_MS = 600_000
_POLL_SECONDS = 0.2


def _now() -> int:
    return int(time.time() * 1000)


def _previous_denial(
    services: SharedServices,
    conn: Any,
    turn_id: str,
    client_id: str,
    name: str,
    arguments: dict[str, Any],
) -> bool:
    return any(
        json.loads(encoded) == arguments
        for encoded in services.project_task_review_repo.denied_client_arguments(
            conn, turn_id, client_id, name
        )
    )


def _dispatch_allowed(turn: ProjectTaskTurnRow | None, row: ProjectTaskToolCallRow) -> bool:
    now = _now()
    return bool(
        turn
        and turn.status in {"queued", "running"}
        and turn.wait_kind != "interrupted"
        and not turn.pause_requested
        and not turn.cancel_requested
        and row.status in {"pending", "claimed"}
        and row.expires_at > now
        and not (
            turn.status == "running" and not turn.is_waiting and turn.updated_at < now - 90_000
        )
    )


def view(row: ProjectTaskToolCallRow, services: SharedServices, conn: Any) -> ClientToolCall:
    context = json.loads(row.approval_context_json)
    if not context:
        context = {
            "mode": "ask_for_approval",
            "revision": 1,
            "action_digest": interactions.digest(
                {"call_id": row.call_id, "arguments": row.arguments_json}
            ),
        }
    turn = services.project_task_turn_repo.get_in_transaction(conn, row.turn_id)
    review = services.project_task_review_repo.get(conn, "client", row.call_id)
    settings = approval_policy.settings(services)
    return ClientToolCall(
        call_id=row.call_id,
        task_id=row.task_id,
        turn_id=row.turn_id,
        client_id=row.client_id,
        name=row.name,
        arguments=json.loads(row.arguments_json),
        approval_context=ClientApprovalContext.model_validate(context),
        dispatch_allowed=_dispatch_allowed(turn, row),
        manual_review_required=bool(
            context.get("requires_user")
            or row.name in settings.require_human_tools
            or "client__" + row.name in settings.require_human_tools
        ),
        review=review_view(review) if review else None,
        status=cast(CallStatus, row.status),
        error_code=row.error_code,
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
        updated_at=datetime.fromtimestamp(row.updated_at / 1000, UTC),
        expires_at=datetime.fromtimestamp(row.expires_at / 1000, UTC),
    )


def _normalize(
    services: SharedServices, conn: Any, row: ProjectTaskToolCallRow
) -> ProjectTaskToolCallRow:
    if row.status not in {"pending", "claimed"}:
        return row
    turn = services.project_task_turn_repo.get_in_transaction(conn, row.turn_id)
    now, status, error = _now(), "", ""
    if turn is None or turn.cancel_requested or turn.status == "cancelled":
        status = "cancelled"
    elif turn.status not in {"running", "queued"}:
        status, error = "interrupted", "TASK_INTERRUPTED"
    elif row.expires_at <= now:
        # A claimed action may already have run. Preserve its original token so
        # the executor can reconcile the result without granting another dispatch.
        status = "claimed" if row.status == "claimed" else "expired"
        error = "TASK_CLIENT_TOOL_TIMEOUT"
        if row.status == status and row.error_code == error:
            return row
    if status:
        services.project_task_tool_call_repo.close(conn, row.call_id, status, error, now)
        return replace(row, status=status, error_code=error, updated_at=now)
    return row


def _load(
    services: SharedServices,
    conn: Any,
    owner_id: int,
    client_id: str,
    task_id: str,
    turn_id: str,
    call_id: str,
) -> ProjectTaskToolCallRow:
    if not services.project_task_repo.lock_owner(conn, owner_id):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    task = services.project_task_repo.get_in_transaction(conn, task_id, owner_id)
    row = services.project_task_tool_call_repo.get(conn, call_id) if task else None
    if row is None or row.task_id != task_id or row.turn_id != turn_id:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    if row.client_id != client_id:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    return _normalize(services, conn, row)


def get_call(
    services: SharedServices,
    owner_id: int,
    client_id: str,
    task_id: str,
    turn_id: str,
    call_id: str,
) -> ClientToolCall:
    with services.db.transaction() as conn:
        return view(
            _load(services, conn, owner_id, client_id, task_id, turn_id, call_id), services, conn
        )


def open_calls(services: SharedServices, owner_id: int, client_id: str) -> list[ClientToolCall]:
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        return [
            view(_normalize(services, conn, row), services, conn)
            for row in services.project_task_tool_call_repo.open_for_client(
                conn, owner_id, client_id
            )
        ]


def claim_call(
    services: SharedServices,
    owner_id: int,
    client_id: str,
    task_id: str,
    turn_id: str,
    call_id: str,
    request_id: str,
) -> ClientToolClaim:
    error = None
    with services.db.transaction() as conn:
        row = _load(services, conn, owner_id, client_id, task_id, turn_id, call_id)
        if row.status == "pending":
            turn = services.project_task_turn_repo.get_in_transaction(conn, turn_id)
            if not _dispatch_allowed(turn, row):
                error = ErrorCode.TASK_CLIENT_TOOL_CLOSED
            else:
                now, token = _now(), secrets.token_urlsafe(32)
                services.project_task_tool_call_repo.claim(conn, call_id, request_id, token, now)
                row = replace(
                    row,
                    status="claimed",
                    claim_request_id=request_id,
                    claim_token=token,
                    updated_at=now,
                )
        elif row.status != "claimed":
            error = ErrorCode.TASK_CLIENT_TOOL_CLOSED
        elif row.claim_request_id != request_id:
            error = ErrorCode.TASK_CLIENT_TOOL_CLAIMED
    if error:
        raise OctopError.localized(error)
    return ClientToolClaim(
        call=get_call(services, owner_id, client_id, task_id, turn_id, call_id),
        claim_token=row.claim_token,
    )


def _reconcile_completed_wait(
    services: SharedServices, conn: Any, row: ProjectTaskToolCallRow, now: int
) -> None:
    turn = services.project_task_turn_repo.get_in_transaction(conn, row.turn_id)
    if (
        turn is None
        or turn.status != "running"
        or turn.wait_kind != "interrupted"
        or turn.cancel_requested
        or not turn.recovery_json
    ):
        return
    recovery = json.loads(turn.recovery_json)
    if recovery.get("mode") != "wait" or not recovery.get("needs_reconciliation"):
        return
    calls = json.loads(turn.pause_json)["calls"]
    if row.call_id not in calls or any(
        (pending := services.project_task_tool_call_repo.get(conn, call_id)) is None
        or pending.status != "completed"
        for call_id in calls
    ):
        return
    if services.project_task_execution_receipt_repo.unfinished(conn, turn.turn_id):
        return
    recovery.update(can_resume=True, needs_reconciliation=False)
    services.project_task_turn_repo.update_recovery(conn, turn.turn_id, json.dumps(recovery), now)


def submit_result(
    services: SharedServices,
    owner_id: int,
    client_id: str,
    task_id: str,
    turn_id: str,
    call_id: str,
    result: ClientToolResult,
) -> ClientToolCall:
    # Canonical JSON makes object key order irrelevant to an idempotent retry.
    encoded = json.dumps(
        {"output": result.output, "is_error": result.is_error},
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    error = None
    with services.db.transaction() as conn:
        row = _load(services, conn, owner_id, client_id, task_id, turn_id, call_id)
        if not row.claim_token or not hmac.compare_digest(row.claim_token, result.claim_token):
            error = ErrorCode.FORBIDDEN
        elif row.status == "completed":
            if row.result_json != encoded:
                error = ErrorCode.TASK_CLIENT_TOOL_RESULT_CONFLICT
        elif row.status != "claimed":
            error = ErrorCode.TASK_CLIENT_TOOL_CLOSED
        else:
            now = _now()
            services.project_task_tool_call_repo.complete(conn, call_id, encoded, now)
            row = replace(
                row, status="completed", result_json=encoded, error_code="", updated_at=now
            )
            _reconcile_completed_wait(services, conn, row, now)
    if error:
        raise OctopError.localized(error)
    return get_call(services, owner_id, client_id, task_id, turn_id, call_id)


class ClientToolBroker:
    """One broker per runtime turn. The harness resumes with the actual tool result."""

    def __init__(
        self,
        services: SharedServices,
        owner_id: int,
        turn: ProjectTaskTurnRow,
        client_id: str,
        definitions: list[ClientToolDefinition],
    ) -> None:
        self.services, self.owner_id, self.turn = services, owner_id, turn
        self.client_id, self.definitions = client_id, definitions
        self.failure: ErrorCode | None = None

    def _create(
        self,
        definition: ClientToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str = "",
    ) -> str:
        Draft202012Validator(definition.input_schema).validate(arguments)
        if len(json.dumps(arguments, allow_nan=False).encode()) > 128 * 1024:
            raise OctopError.localized(ErrorCode.TASK_CLIENT_TOOL_ARGUMENTS_INVALID)
        now = _now()
        identifier = (
            hashlib.sha256(f"{self.turn.turn_id}:{tool_call_id}".encode()).hexdigest()
            if tool_call_id
            else new_ulid()
        )
        row = ProjectTaskToolCallRow(
            "call_" + identifier,
            self.turn.task_id,
            self.turn.turn_id,
            self.owner_id,
            self.client_id,
            definition.name,
            json.dumps(arguments, allow_nan=False),
            "pending",
            "",
            "",
            "",
            "",
            now,
            now,
            now + CALL_TIMEOUT_MS,
        )
        with self.services.db.transaction() as conn:
            if not self.services.project_task_repo.lock_owner(conn, self.owner_id):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            current = self.services.project_task_turn_repo.get_in_transaction(conn, row.turn_id)
            if (
                current is None
                or current.status != "running"
                or current.cancel_requested
                or current.worker_id != self.turn.worker_id
            ):
                raise OctopError.localized(ErrorCode.TASK_CLIENT_TOOL_CLOSED)
            existing = self.services.project_task_tool_call_repo.get(conn, row.call_id)
            if existing is not None:
                if (
                    existing.turn_id != row.turn_id
                    or existing.client_id != row.client_id
                    or existing.name != row.name
                    or json.loads(existing.arguments_json) != arguments
                ):
                    raise OctopError.localized(ErrorCode.TASK_CLIENT_TOOL_CLOSED)
                return existing.call_id
            if _previous_denial(
                self.services, conn, row.turn_id, row.client_id, row.name, arguments
            ):
                # Changing native call ids or Task modes cannot retry a denied
                # proposal in this turn, including a switch to full access.
                raise OctopError.localized(ErrorCode.TASK_CLIENT_TOOL_CLOSED)
            policy = approval_policy.snapshot(conn, self.services, self.turn.task_id, self.owner_id)
            settings = approval_policy.settings(self.services)
            context = ClientApprovalContext(
                mode=policy.mode,
                revision=policy.revision,
                action_digest=interactions.digest(
                    {
                        "call_id": row.call_id,
                        "owner": self.owner_id,
                        "task_id": row.task_id,
                        "turn_id": row.turn_id,
                        "client_id": row.client_id,
                        "name": row.name,
                        "arguments": arguments,
                        "configuration": self.turn.configuration_json,
                        "policy": policy.model_dump(mode="json"),
                    }
                ),
                requires_user=row.name in settings.require_human_tools
                or "client__" + row.name in settings.require_human_tools,
            )
            row = replace(row, approval_context_json=context.model_dump_json())
            self.services.project_task_tool_call_repo.insert(conn, row)
        return row.call_id

    def _poll(self, call_id: str) -> ProjectTaskToolCallRow:
        with self.services.db.transaction() as conn:
            return _load(
                self.services,
                conn,
                self.owner_id,
                self.client_id,
                self.turn.task_id,
                self.turn.turn_id,
                call_id,
            )

    async def invoke(self, definition: ClientToolDefinition, arguments: dict[str, Any]) -> str:
        try:
            Draft202012Validator(definition.input_schema).validate(arguments)
            if len(json.dumps(arguments, allow_nan=False).encode()) > 128 * 1024:
                raise ValueError("arguments exceed 128 KiB")
        except Exception:
            # Invalid model arguments never reach the client, nor leak schema details.
            return json.dumps(
                {"is_error": True, "error_code": "TASK_CLIENT_TOOL_ARGUMENTS_INVALID"}
            )
        call_id = await asyncio.to_thread(self._create, definition, arguments)
        while True:
            row = await asyncio.to_thread(self._poll, call_id)
            if row.status == "completed":
                return row.result_json
            if row.status == "claimed" and row.error_code == "TASK_CLIENT_TOOL_TIMEOUT":
                self.failure = ErrorCode.TASK_CLIENT_TOOL_TIMEOUT
                raise OctopError.localized(self.failure)
            if row.status not in {"pending", "claimed"}:
                self.failure = (
                    ErrorCode.TASK_CLIENT_TOOL_TIMEOUT
                    if row.status == "expired"
                    else ErrorCode.TASK_CLIENT_TOOL_CLOSED
                )
                raise OctopError.localized(self.failure)
            await asyncio.sleep(_POLL_SECONDS)

    def tools(self) -> list[StructuredTool]:
        def build(definition: ClientToolDefinition) -> StructuredTool:
            async def execute(**arguments: Any) -> str:
                return await self.invoke(definition, arguments)

            return StructuredTool.from_function(
                coroutine=execute,
                name="client__" + definition.name,
                description=definition.description,
                args_schema=definition.input_schema,
            )

        return [build(definition) for definition in self.definitions]
