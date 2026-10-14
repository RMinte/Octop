"""Read an owned Run's verified final answer from fixed v2 history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import Field, StrictStr, model_validator

from octop.infra.agentcomputer.commands import (
    StorageDataError,
    StorageUnavailableError,
    content_digest,
)
from octop.infra.agentcomputer.completions import (
    ToolCompletionEvidence,
    validate_completion_binding,
)
from octop.infra.agentcomputer.dispatch import validate_dispatch_binding
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import ControlModel
from octop.infra.agentcomputer.runs import get_run, load_bound_run
from octop.infra.db.services import SharedServices
from octop.infra.history.store import (
    BoundToolExpectation,
    _decode_document,
    probe_bound_completion,
)


class RunResult(ControlModel):
    """Current Run state and, only after verified success, its final text."""

    runID: UUID
    state: Literal[
        "queued",
        "running",
        "waitingApproval",
        "waitingInput",
        "succeeded",
        "failed",
        "stopped",
        "interrupted",
    ]
    assistantMessageID: StrictStr | None = Field(
        description="Fixed final assistant message ID; null until verified success."
    )
    text: StrictStr | None = Field(
        description="Exact final answer from bound v2 history; null until verified success."
    )

    @model_validator(mode="after")
    def _result_matches_state(self) -> Self:
        if self.state == "succeeded":
            if not self.assistantMessageID or not self.text:
                raise ValueError("a successful Run requires a final answer")
        elif self.assistantMessageID is not None or self.text is not None:
            raise ValueError("an unfinished Run cannot expose a final answer")
        return self


def _read_final_text(
    path: Path, turn_id: UUID, message_id: str, digest: str, *, message_index: int
) -> str:
    """Re-read one archived answer without creating or modifying the archive."""
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT d.data FROM documents d WHERE d.id=? AND d.turn_id=? AND d.kind='message'",
                (f"message:{turn_id}:{message_index}", str(turn_id)),
            ).fetchone()
            if row is None:
                raise StorageDataError("missing final history message")
            answer = _decode_document(conn, json.loads(row["data"]))
            if not isinstance(answer, dict) or answer.get("type") != "ai":
                raise StorageDataError("invalid final history message")
            data = answer.get("data")
            if not isinstance(data, dict):
                raise StorageDataError("invalid final history message")
            text = data.get("content")
            if (
                data.get("id") != message_id
                or not isinstance(text, str)
                or not text.strip()
                or content_digest(text) != digest
            ):
                raise StorageDataError("final history answer differs from completion evidence")
            return text
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, StorageDataError):
            raise
        raise StorageDataError("final history answer is unavailable") from exc


def get_run_result(services: SharedServices, *, owner_id: int, run_id: UUID) -> RunResult | None:
    """Return no answer before success; revalidate both durable proof sources."""
    current = get_run(services, owner_id=owner_id, run_id=run_id)
    if current is None:
        return None
    if current.state != "succeeded":
        return RunResult(runID=run_id, state=current.state, assistantMessageID=None, text=None)
    if not services.config.database.is_sqlite:
        raise StorageDataError("successful Run has no supported history archive")
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    try:
        with services.db.transaction() as conn:
            bound = load_bound_run(
                services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
            )
            if bound is None or bound.run.state != "succeeded":
                raise StorageDataError("successful Run binding changed")
            claim = validate_dispatch_binding(services, conn, bound)
            receipt = validate_completion_binding(services, conn, bound, claim)
            if receipt is None:
                raise StorageDataError("successful Run has no completion receipt")
            snapshot = bound.snapshot
            turn_id = bound.receipt.turnID
            user_message_id = bound.receipt.userMessageID
            evidence = receipt.evidence
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("Run result storage unavailable") from exc
    path = services.paths.root / "history_v2.sqlite"
    expected_tool = (
        BoundToolExpectation(
            tool_call_id=evidence.toolCallID,
            tool_name=evidence.toolName,
            tool_arguments_digest=evidence.toolArgumentsDigest,
            tool_content_digest=evidence.toolContentDigest,
        )
        if isinstance(evidence, ToolCompletionEvidence)
        else None
    )
    probe = probe_bound_completion(
        path,
        identity=str(services.config.database.resolve_sqlite_path(services.paths.root).resolve()),
        turn_id=str(turn_id),
        thread_id=snapshot.octopThreadID,
        agent_id=snapshot.agentID,
        user_message_id=str(user_message_id),
        prompt=snapshot.request.prompt,
        expected_tool=expected_tool,
    )
    if (
        probe.status != "complete"
        or probe.assistant_message_id != evidence.historyAssistantMessageID
        or probe.assistant_content_digest != evidence.historyAssistantContentDigest
        or (
            isinstance(evidence, ToolCompletionEvidence)
            and probe.tool_message_id != evidence.toolMessageID
        )
    ):
        raise StorageDataError("final history evidence differs from Run completion")
    text = _read_final_text(
        path,
        turn_id,
        evidence.historyAssistantMessageID,
        evidence.historyAssistantContentDigest,
        message_index=3 if expected_tool is not None else 1,
    )
    return RunResult(
        runID=run_id,
        state="succeeded",
        assistantMessageID=evidence.historyAssistantMessageID,
        text=text,
    )
