"""Safe, owned summaries of the one managed Project document read tool."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import Field, StrictStr, field_validator

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
)
from octop.infra.agentcomputer.dispatch import validate_dispatch_binding
from octop.infra.agentcomputer.effects import _record
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import ControlModel, EngineInvocation, ToolEffectRecord
from octop.infra.agentcomputer.project_document_tool import (
    CAPABILITY_VERSION,
    POLICY_VERSION,
    TOOL_NAME,
    validate_document_filename,
)
from octop.infra.agentcomputer.runs import get_run_in_transaction, load_bound_run
from octop.infra.db.services import SharedServices

DocumentStatus = Literal[
    "ok", "notFound", "tooLarge", "unreadable", "pending", "unknown", "unavailable"
]
EffectOutcome = Literal["succeeded", "failed", "notDispatched", "pending", "unknown"]


class ToolActivityItem(ControlModel):
    toolName: Literal["read_project_document"]
    path: StrictStr = Field(description="Safe Project-relative documents/<filename> path.")
    status: DocumentStatus = Field(
        description="Document read status, pending/unknown effect, or unavailable failed read."
    )
    outcome: EffectOutcome = Field(description="Bound effect outcome without its private receipt.")

    @field_validator("path")
    @classmethod
    def managed_document_path(cls, value: str) -> str:
        prefix = "documents/"
        if not value.startswith(prefix):
            raise ValueError("tool activity path must name a managed Project document")
        try:
            validate_document_filename(value[len(prefix) :])
        except CommandAdmissionError as exc:
            raise ValueError("tool activity path must name a managed Project document") from exc
        return value


class RunToolActivity(ControlModel):
    runID: UUID
    items: list[ToolActivityItem] = Field(
        max_length=1,
        description="At most one validated read_project_document activity for this Run.",
    )


def _item(record: ToolEffectRecord) -> ToolActivityItem:
    action = record.intent.action
    if (
        action.toolName != TOOL_NAME
        or action.capabilityVersion != CAPABILITY_VERSION
        or action.policyVersion != POLICY_VERSION
    ):
        raise StorageDataError("unsupported project document tool activity")
    try:
        arguments = json.loads(action.argumentsJSON)
        if type(arguments) is not dict or set(arguments) != {"path"}:
            raise ValueError("unexpected tool arguments")
        filename = validate_document_filename(arguments["path"])
    except (CommandAdmissionError, ValueError, TypeError, KeyError) as exc:
        raise StorageDataError("invalid project document tool arguments") from exc
    path = f"documents/{filename}"
    if record.phase == "outcomeUnknown":
        return ToolActivityItem(
            toolName="read_project_document", path=path, status="unknown", outcome="unknown"
        )
    if record.phase != "terminal":
        return ToolActivityItem(
            toolName="read_project_document", path=path, status="pending", outcome="pending"
        )
    receipt = record.receipt
    if receipt is None:
        raise StorageDataError("missing project document tool receipt")
    if receipt.outcome != "succeeded":
        return ToolActivityItem(
            toolName="read_project_document",
            path=path,
            status="unavailable",
            outcome=receipt.outcome,
        )
    output = receipt.output
    if (
        output is None
        or output.effects != "reported"
        or output.fileChangeIDs
        or output.artifactIDs
        or output.exitCode is not None
    ):
        raise StorageDataError("invalid project document read result")
    try:
        result = json.loads(output.resultJSON)
    except (TypeError, ValueError) as exc:
        raise StorageDataError("invalid project document read result") from exc
    if (
        type(result) is not dict
        or set(result) != {"path", "status", "content"}
        or result["path"] != path
        or result["status"] not in {"ok", "notFound", "tooLarge", "unreadable"}
        or (result["status"] == "ok") != isinstance(result["content"], str)
        or (result["status"] != "ok" and result["content"] is not None)
    ):
        raise StorageDataError("invalid project document read result")
    return ToolActivityItem(
        toolName="read_project_document", path=path, status=result["status"], outcome="succeeded"
    )


def get_run_tool_activity(
    services: SharedServices, *, owner_id: int, run_id: UUID
) -> RunToolActivity | None:
    """Validate the entire owned Run graph and journal in one read-only transaction."""
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    try:
        with services.db.transaction() as conn:
            current = get_run_in_transaction(
                services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
            )
            if current is None:
                return None
            rows = services.ac_tool_effect_repo.list_for_run_in_transaction(
                conn, str(run_id), owner_id
            )
            if len(rows) > 1:
                raise StorageDataError("multiple project document reads in one Run")
            if not rows:
                return RunToolActivity(runID=run_id, items=[])
            bound = load_bound_run(
                services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
            )
            if bound is None:
                raise StorageDataError("Run binding disappeared")
            claim = validate_dispatch_binding(services, conn, bound)
            if claim is None or claim.engineReceipt is None:
                raise StorageDataError("tool effect without engine acceptance")
            invocation = EngineInvocation(
                intent=claim.intent,
                claimantID=claim.claimantID,
                expectedRunRevision=claim.runRevision,
            )
            return RunToolActivity(runID=run_id, items=[_item(_record(rows[0], invocation))])
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("Run tool activity storage unavailable") from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, StorageDataError):
            raise
        raise StorageDataError("invalid persisted Run tool activity") from exc
