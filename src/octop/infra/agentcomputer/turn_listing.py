"""Read one owned Task's original messages and verified Run results."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import Field, StrictStr, ValidationError

from octop.infra.agentcomputer.commands import (
    StorageDataError,
    StorageUnavailableError,
    canonical_json,
)
from octop.infra.agentcomputer.identity import IdentityError, get_or_create_identity
from octop.infra.agentcomputer.models import ControlModel, RunView, TurnSnapshot
from octop.infra.agentcomputer.results import RunResult, get_run_result
from octop.infra.agentcomputer.runs import get_run_in_transaction
from octop.infra.agentcomputer.tasks import _get_task
from octop.infra.db.services import SharedServices

_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_CURSOR_FIELDS = {
    "v",
    "boxID",
    "ownerID",
    "taskID",
    "restoreEpoch",
    "afterID",
    "turnID",
}


class InvalidTurnListCursor(ValueError):
    """The page cursor is malformed or belongs to another scope."""


class TurnListItem(ControlModel):
    turnID: UUID
    userMessageID: UUID
    prompt: StrictStr = Field(description="The exact accepted user text for this Turn.")
    run: RunView
    result: RunResult = Field(
        description="Current Run state and verified final answer, or null text until success."
    )


class TurnListResponse(ControlModel):
    items: list[TurnListItem]
    nextCursor: str | None = Field(
        description="Opaque cursor for older Turns, or null when the Task has no older Turn."
    )


def _encode_cursor(
    *,
    box_id: UUID,
    owner_id: int,
    task_id: UUID,
    restore_epoch: int,
    after_id: int,
    turn_id: UUID,
) -> str:
    payload = canonical_json(
        {
            "v": 1,
            "boxID": str(box_id),
            "ownerID": owner_id,
            "taskID": str(task_id),
            "restoreEpoch": restore_epoch,
            "afterID": after_id,
            "turnID": str(turn_id),
        }
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(
    cursor: str,
    *,
    box_id: UUID,
    owner_id: int,
    task_id: UUID,
    restore_epoch: int,
) -> tuple[int, str]:
    if not 1 <= len(cursor) <= 512 or _CURSOR_PATTERN.fullmatch(cursor) is None:
        raise InvalidTurnListCursor("invalid cursor encoding")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != cursor:
            raise InvalidTurnListCursor("non-canonical cursor encoding")
        payload = json.loads(raw.decode("utf-8"))
        if type(payload) is not dict or set(payload) != _CURSOR_FIELDS:
            raise InvalidTurnListCursor("invalid cursor fields")
        if canonical_json(payload).encode("utf-8") != raw:
            raise InvalidTurnListCursor("non-canonical cursor payload")
        turn_id = payload["turnID"]
        if (
            type(payload["v"]) is not int
            or payload["v"] != 1
            or type(payload["ownerID"]) is not int
            or payload["ownerID"] != owner_id
            or type(payload["restoreEpoch"]) is not int
            or payload["restoreEpoch"] != restore_epoch
            or type(payload["boxID"]) is not str
            or payload["boxID"] != str(box_id)
            or type(payload["taskID"]) is not str
            or payload["taskID"] != str(task_id)
            or type(payload["afterID"]) is not int
            or not 1 <= payload["afterID"] <= 2**63 - 1
            or type(turn_id) is not str
            or str(UUID(turn_id)) != turn_id
        ):
            raise InvalidTurnListCursor("cursor scope or position mismatch")
        return payload["afterID"], turn_id
    except (binascii.Error, UnicodeError, ValueError, TypeError) as exc:
        raise InvalidTurnListCursor("invalid cursor") from exc


def list_turns(
    services: SharedServices,
    owner_id: int,
    task_id: UUID,
    limit: int,
    cursor: str | None,
) -> TurnListResponse | None:
    """List original user prompts, current Runs and separately proven answers."""
    try:
        identity = get_or_create_identity(services.paths.agentcomputer_identity)
        with services.db.transaction() as conn:
            task = _get_task(services, conn, owner_id, task_id, identity.boxID)
            if task is None:
                return None
            after_id = None
            if cursor is not None:
                after_id, anchor_id = _decode_cursor(
                    cursor,
                    box_id=identity.boxID,
                    owner_id=owner_id,
                    task_id=task_id,
                    restore_epoch=identity.restoreEpoch,
                )
                anchor = services.ac_turn_repo.get_by_pk_for_task_in_transaction(
                    conn, str(task_id), owner_id, after_id
                )
                if anchor is None or anchor.turn_id != anchor_id:
                    raise InvalidTurnListCursor("cursor does not identify this Task's Turn")
            rows = services.ac_turn_repo.list_page_for_task_in_transaction(
                conn, str(task_id), owner_id, after_id=after_id, limit=limit + 1
            )
            accepted: list[tuple[UUID, UUID, str, RunView]] = []
            for row in rows[:limit]:
                if (
                    row.pk < 1
                    or row.task_id != str(task_id)
                    or row.project_id != str(task.projectID)
                    or row.owner_id != owner_id
                    or row.schema_version != 1
                    or row.context_mode != "newMessage"
                    or row.created_at < 0
                ):
                    raise StorageDataError("invalid Task Turn row")
                turn_id = UUID(row.turn_id)
                user_message_id = UUID(row.user_message_id)
                if str(turn_id) != row.turn_id or str(user_message_id) != row.user_message_id:
                    raise StorageDataError("non-canonical Turn identity")
                run_row = services.ac_run_repo.get_by_turn_in_transaction(
                    conn, row.turn_id, str(task_id), owner_id
                )
                if run_row is None:
                    raise StorageDataError("Task Turn has no Run")
                run_id = UUID(run_row.run_id)
                if str(run_id) != run_row.run_id:
                    raise StorageDataError("non-canonical Run identity")
                run = get_run_in_transaction(
                    services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
                )
                if (
                    run is None
                    or run.turnID != turn_id
                    or run.taskID != task_id
                    or run.projectID != task.projectID
                ):
                    raise StorageDataError("Task Turn Run binding mismatch")
                snapshot = TurnSnapshot.model_validate_json(row.snapshot_json)
                if snapshot.request.userMessageID != user_message_id:
                    raise StorageDataError("Task Turn message binding mismatch")
                accepted.append((turn_id, user_message_id, snapshot.request.prompt, run))
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = _encode_cursor(
                    box_id=identity.boxID,
                    owner_id=owner_id,
                    task_id=task_id,
                    restore_epoch=identity.restoreEpoch,
                    after_id=last.pk,
                    turn_id=UUID(last.turn_id),
                )

        items: list[TurnListItem] = []
        for turn_id, user_message_id, prompt, run in accepted:
            result = get_run_result(services, owner_id=owner_id, run_id=run.runID)
            if result is None:
                raise StorageDataError("Task Turn result binding disappeared")
            items.append(
                TurnListItem(
                    turnID=turn_id,
                    userMessageID=user_message_id,
                    prompt=prompt,
                    run=run,
                    result=result,
                )
            )
        return TurnListResponse(items=items, nextCursor=next_cursor)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("Task Turn list storage unavailable") from exc
    except (IdentityError, InvalidTurnListCursor, StorageDataError, StorageUnavailableError):
        raise
    except (ValidationError, ValueError, TypeError) as exc:
        raise StorageDataError("invalid stored Task Turn list value") from exc
