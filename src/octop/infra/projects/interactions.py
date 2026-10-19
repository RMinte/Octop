"""Persist human decisions and exact-turn pause intents under the task owner lock."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from octop.i18n import tr
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.interaction_models import (
    ApprovalPayload,
    ApprovalResponse,
    InteractionResolution,
    InteractionResponse,
    QuestionAnswer,
    QuestionPayload,
    TaskControl,
    TaskInteraction,
)
from octop.infra.utils.ulid import new_ulid

WAIT_SECONDS = 600


def encode(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def load(turn: ProjectTaskTurnRow) -> list[TaskInteraction]:
    return [TaskInteraction.model_validate(item) for item in json.loads(turn.interactions_json)]


def dump(items: list[TaskInteraction]) -> str:
    return encode([item.model_dump(mode="json") for item in items])


def new(
    turn: ProjectTaskTurnRow, payload: ApprovalPayload | QuestionPayload, tool_call_id: str
) -> TaskInteraction:
    now = datetime.now(UTC)
    return TaskInteraction(
        interaction_id="interaction_" + new_ulid(),
        task_id=turn.task_id,
        turn_id=turn.turn_id,
        kind="approval" if isinstance(payload, ApprovalPayload) else "question",
        tool_call_id=tool_call_id,
        revision=1,
        status="pending",
        payload=payload,
        created_at=now,
        expires_at=now + timedelta(seconds=WAIT_SECONDS),
    )


def resolve_question(
    item: TaskInteraction, action: str, source: str, answer: str | None = None
) -> None:
    assert isinstance(item.payload, QuestionPayload)
    outcome = "answered" if action == "answer" else "skipped" if action == "skip" else "unanswered"
    if action == "use_default" and item.payload.options:
        outcome, answer = "defaulted", item.payload.options[0].label
    item.resolution = InteractionResolution.model_validate(
        {"outcome": outcome, "source": source, "answer": answer}
    )
    item.status, item.resolved_at = "resolved", datetime.now(UTC)
    item.revision += 1


def expire(items: list[TaskInteraction]) -> bool:
    now, changed = datetime.now(UTC), False
    for item in items:
        if item.status not in {"pending", "reviewing"} or item.expires_at > now:
            continue
        if item.kind == "question":
            resolve_question(item, "use_default", "timeout")
        else:
            item.status, item.resolved_at = "expired", now
            item.resolution = InteractionResolution(outcome="deny", source="timeout")
            item.revision += 1
        changed = True
    return changed


def close(services: SharedServices, conn: Any, turn: ProjectTaskTurnRow, now: int) -> None:
    services.project_task_review_repo.cancel(conn, turn.turn_id, now)
    items = load(turn)
    changed = False
    for item in items:
        if item.status in {"pending", "reviewing"}:
            item.status, item.resolved_at = "cancelled", datetime.fromtimestamp(now / 1000, UTC)
            item.revision += 1
            changed = True
    if changed:
        services.project_task_turn_repo.save_interactions(conn, turn.turn_id, dump(items), now)


def _load_owned(
    services: SharedServices, conn: Any, owner: int, task_id: str, turn_id: str
) -> ProjectTaskTurnRow:
    if not services.project_task_repo.lock_owner(conn, owner):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    task = services.project_task_repo.get_in_transaction(conn, task_id, owner)
    turn = services.project_task_turn_repo.get_in_transaction(conn, turn_id) if task else None
    if turn is None or turn.task_id != task_id:
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    return turn


def _request(turn: ProjectTaskTurnRow, request_id: str, payload: Any) -> list[Any] | None:
    requests = json.loads(turn.requests_json)
    fingerprint = digest(payload)
    previous = next((r for r in requests if r["request_id"] == request_id), None)
    if previous:
        if previous["digest"] != fingerprint:
            raise OctopError.localized(ErrorCode.TASK_REQUEST_CONFLICT, status=409)
        return None
    return [*requests, {"request_id": request_id, "digest": fingerprint}]


def respond(
    services: SharedServices,
    owner: int,
    task_id: str,
    turn_id: str,
    interaction_id: str,
    body: InteractionResponse,
) -> None:
    now, error = int(time.time() * 1000), None
    with services.db.transaction() as conn:
        turn = _load_owned(services, conn, owner, task_id, turn_id)
        requests = _request(
            turn,
            body.request_id,
            {
                "operation": "respond",
                "interaction_id": interaction_id,
                **body.model_dump(mode="json"),
            },
        )
        if requests is None:
            return
        items = load(turn)
        changed = expire(items)
        item = next((item for item in items if item.interaction_id == interaction_id), None)
        if item is None:
            error = ErrorCode.NOT_FOUND
        elif item.status != "pending" or not turn.is_waiting or turn.cancel_requested:
            error = ErrorCode.TASK_INTERACTION_CLOSED
        elif item.revision != body.expected_revision:
            error = ErrorCode.TASK_INTERACTION_CONFLICT
        elif isinstance(body.response, ApprovalResponse):
            if (
                not isinstance(item.payload, ApprovalPayload)
                or body.response.action_digest != item.payload.action_digest
                or body.response.decision not in item.payload.allowed_decisions
            ):
                error = ErrorCode.TASK_INTERACTION_CONFLICT
            else:
                item.status, item.resolved_at = "resolved", datetime.now(UTC)
                item.resolution = InteractionResolution(
                    outcome=body.response.decision, source="user"
                )
                item.revision += 1
                changed = True
        elif not isinstance(item.payload, QuestionPayload):
            error = ErrorCode.TASK_INTERACTION_CONFLICT
        else:
            resolve_question(
                item,
                body.response.action,
                "user" if body.response.action != "use_default" else "user_default",
                body.response.answer if isinstance(body.response, QuestionAnswer) else None,
            )
            changed = True
        if changed:
            services.project_task_turn_repo.save_interactions(conn, turn_id, dump(items), now)
        if error is None:
            services.project_task_turn_repo.save_request(
                conn, turn_id, encode(requests), turn.pause_requested
            )
    if error:
        raise OctopError.localized(error)


def control(
    services: SharedServices, owner: int, task_id: str, body: TaskControl, *, paused: bool
) -> None:
    now = int(time.time() * 1000)
    with services.db.transaction() as conn:
        turn = _load_owned(services, conn, owner, task_id, body.turn_id)
        requests = _request(
            turn,
            body.request_id,
            {"operation": "pause" if paused else "resume", **body.model_dump()},
        )
        if requests is None:
            return
        if turn.status not in {"queued", "running"} or turn.cancel_requested:
            raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
        if turn.wait_kind == "interrupted" and not paused:
            from octop.infra.projects import approval_runtime

            recovery = json.loads(turn.recovery_json)
            if services.project_task_execution_receipt_repo.unfinished(conn, turn.turn_id):
                raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
            if recovery.get("mode") == "wait":
                wait = json.loads(turn.pause_json)
                calls = [
                    services.project_task_tool_call_repo.get(conn, identifier)
                    for identifier in wait["calls"]
                ]
                if any(row is None or row.status != "completed" for row in calls):
                    raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
                items = load(turn)
                approval_runtime.rearm_expired(services, conn, owner, turn, items, wait, now)
                kind = approval_runtime.wait_kind(items, clients=False, user_paused=False)
                services.project_task_turn_repo.pause(
                    conn,
                    turn.turn_id,
                    "",
                    turn.checkpoint_id,
                    encode(wait),
                    turn.reply,
                    turn.reasoning,
                    turn.content_blocks_json,
                    turn.content_revision,
                    now,
                    wait_kind=kind,
                    interactions_json=dump(items),
                )
                services.project_task_repo.wait(conn, task_id, turn.turn_id, kind, now)
            elif not recovery.get(
                "can_resume"
            ) or not services.project_task_turn_repo.resume_recovery(conn, turn.turn_id, now):
                raise OctopError.localized(ErrorCode.TASK_CONTROL_INVALID)
            services.project_task_turn_repo.save_request(
                conn, turn.turn_id, encode(requests), False
            )
            return
        services.project_task_turn_repo.save_request(conn, turn.turn_id, encode(requests), paused)
        if turn.is_waiting:
            services.project_task_repo.wait(
                conn, task_id, turn.turn_id, "user" if paused else turn.wait_kind, now
            )


def native_decision(item: TaskInteraction, locale: str) -> dict[str, Any]:
    assert item.resolution is not None
    if item.kind == "approval":
        return (
            {"type": "approve"}
            if item.resolution.outcome == "allow_once"
            else {
                "type": "reject",
                "message": tr("projects.approval_not_granted", locale),
            }
        )
    return {
        "type": "respond",
        "message": encode(
            {
                "question": item.payload.model_dump(mode="json"),
                "result": item.resolution.model_dump(mode="json"),
                "authorization": False,
            }
        ),
    }
