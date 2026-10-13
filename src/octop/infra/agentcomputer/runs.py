"""Owned current Run projections from strictly bound acceptance facts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import ValidationError

from octop.infra.agentcomputer.commands import (
    StorageDataError,
    StorageUnavailableError,
    decode_turn_receipt,
    validate_turn_receipt_binding,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import CommandReceipt, RunView, TurnSnapshot
from octop.infra.agentcomputer.tasks import _get_task
from octop.infra.db.services import SharedServices

if TYPE_CHECKING:
    from octop.infra.db.repos.ac_runs import AcRunRow


@dataclass(frozen=True)
class BoundRun:
    run: AcRunRow
    receipt: CommandReceipt
    snapshot: TurnSnapshot


def load_bound_run(
    services: SharedServices, conn: Any, *, owner_id: int, run_id: UUID, box_id: UUID
) -> BoundRun | None:
    """One consistent connection for the original facts and managed Task mapping."""
    run = services.ac_run_repo.get_in_transaction(conn, str(run_id), owner_id)
    if run is None:
        return None
    events = services.ac_event_repo.list_for_run_in_transaction(
        conn, str(run_id), owner_id, event_type="run.accepted"
    )
    if len(events) != 1:
        raise StorageDataError("missing or duplicated run acceptance")
    try:
        payload = json.loads(events[0].payload_json)
        if not isinstance(payload, dict) or type(payload.get("commandID")) is not str:
            raise StorageDataError("invalid run acceptance identity")
        command_id = UUID(payload["commandID"])
    except (TypeError, ValueError, KeyError) as exc:
        raise StorageDataError("invalid run acceptance identity") from exc
    command = services.ac_command_repo.get_in_transaction(conn, str(command_id), owner_id)
    if command is None:
        raise StorageDataError("missing run receipt")
    receipt = decode_turn_receipt(
        command.receipt_json, command.command_id, box_id, command.restore_epoch
    )
    validate_turn_receipt_binding(services, receipt, command, owner_id, conn=conn)
    if (
        receipt.runID != run_id
        or _get_task(services, conn, owner_id, receipt.taskID, box_id) is None
    ):
        raise StorageDataError("run task binding mismatch")
    turn = services.ac_turn_repo.get_in_transaction(conn, str(receipt.turnID), owner_id)
    if turn is None:
        raise StorageDataError("missing original turn")
    snapshot = TurnSnapshot.model_validate_json(turn.snapshot_json)
    if run.schema_version != 1 or run.updated_at < run.created_at:
        raise StorageDataError("invalid run schema or timestamps")
    # Validate row fields before the worker acts; get_run supplies the journal-derived flag.
    run_view(run, cancel_requested=False)
    return BoundRun(run=run, receipt=receipt, snapshot=snapshot)


def run_view(run: AcRunRow, *, cancel_requested: bool) -> RunView:
    try:
        return RunView.model_validate(
            {
                "runID": run.run_id,
                "turnID": run.turn_id,
                "taskID": run.task_id,
                "projectID": run.project_id,
                "state": run.state,
                "dispatchPhase": run.dispatch_phase,
                "executionEpoch": run.execution_epoch,
                "restoreEpoch": run.restore_epoch,
                "revision": run.revision,
                "reconciliationRequired": run.reconciliation_required,
                "cancelRequested": cancel_requested,
                "createdAt": datetime.fromtimestamp(run.created_at / 1000, UTC),
                "updatedAt": datetime.fromtimestamp(run.updated_at / 1000, UTC),
            }
        )
    except (ValidationError, ValueError, OverflowError, OSError) as exc:
        raise StorageDataError("invalid run projection") from exc


def get_run(services: SharedServices, *, owner_id: int, run_id: UUID) -> RunView | None:
    """Read current stored state without dispatch, repair, or rewriting receipts."""
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    try:
        with services.db.transaction() as conn:
            bound = load_bound_run(
                services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
            )
            if bound is None:
                return None
            from octop.infra.agentcomputer.dispatch import validate_dispatch_binding

            claim = validate_dispatch_binding(services, conn, bound)
            from octop.infra.agentcomputer.effects import validate_effect_bindings

            validate_effect_bindings(services, conn, bound, claim)
            from octop.infra.agentcomputer.pauses import validate_pause_bindings

            validate_pause_bindings(services, conn, bound, claim)
            from octop.infra.agentcomputer.cancellations import validate_cancellation_binding

            cancellation = validate_cancellation_binding(services, conn, bound, claim)
            return run_view(bound.run, cancel_requested=cancellation is not None)
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("run storage unavailable") from exc
    except StorageDataError:
        raise
    except (ValueError, TypeError) as exc:
        # SQLite affinity/checks do not guarantee numeric storage types. Repo
        # decoders must not turn malformed persisted rows into an HTTP 500.
        raise StorageDataError("invalid persisted run binding") from exc
