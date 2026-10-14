"""Read a bound Run's text-only output without treating live text as completion."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from psycopg import Error as PostgresError
from pydantic import Field, StrictBool, StrictStr, model_validator

from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    content_digest,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import ControlModel
from octop.infra.agentcomputer.project_document_tool import (
    TOOL_NAME,
    validate_document_filename,
)
from octop.infra.agentcomputer.results import get_run_result
from octop.infra.agentcomputer.runs import get_run_in_transaction, load_bound_run
from octop.infra.db.services import SharedServices
from octop.infra.history.store import _decode_document


class RunOutputSnapshot(ControlModel):
    """A point-in-time text snapshot; only receipt-verified success is final."""

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
    text: StrictStr | None = Field(
        description="Exact bound text so far, or null when no trustworthy text is available."
    )
    digest: StrictStr | None = Field(
        description="SHA-256 digest of the canonical JSON text, or null with text."
    )
    provisional: StrictBool = Field(
        description="True for archived text lacking a verified successful Run completion."
    )

    @model_validator(mode="after")
    def _text_consistency(self) -> Self:
        if (self.text is None) != (self.digest is None):
            raise ValueError("text and digest must be present together")
        if self.text is None and self.provisional:
            raise ValueError("missing text cannot be provisional")
        if self.state == "succeeded" and (self.text is None or self.provisional):
            raise ValueError("successful output requires verified final text")
        if self.text is not None and self.state != "succeeded" and not self.provisional:
            raise ValueError("unfinished output must be provisional")
        return self


def _read_partial_text(
    path: Path,
    *,
    identity: str,
    turn_id: UUID,
    thread_id: str,
    agent_id: str,
    user_message_id: UUID,
    prompt: str,
) -> str | None:
    """Read only the exact original v2 Turn and reject mixed/tool/error documents."""
    if not path.exists():
        if path.with_suffix(".required").exists():
            raise StorageDataError("required history archive is missing")
        return None
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            meta = conn.execute("SELECT version,identity FROM archive_meta").fetchall()
            if len(meta) != 1 or meta[0]["version"] != 1 or meta[0]["identity"] != identity:
                raise StorageDataError("history archive identity differs from control database")
            turn = conn.execute(
                "SELECT t.status,t.error,s.thread_id,s.agent_id,s.format "
                "FROM turns t JOIN segments s ON s.id=t.segment_id WHERE t.id=?",
                (str(turn_id),),
            ).fetchone()
            if turn is None:
                return None
            if (
                turn["thread_id"] != thread_id
                or turn["agent_id"] != agent_id
                or turn["format"] != "v2"
                or turn["status"]
                not in {"active", "paused", "interrupted", "failed", "partial", "complete"}
                or turn["error"] is not None
            ):
                raise StorageDataError("history Turn binding is invalid")
            rows = conn.execute(
                "SELECT id,thread_id,turn_id,kind,seq,data FROM documents "
                "WHERE turn_id=? ORDER BY seq,id",
                (str(turn_id),),
            ).fetchall()
            if not rows:
                if turn["status"] == "complete":
                    raise StorageDataError("complete history Turn has no messages")
                return None
            if (
                len(rows) > 2
                or any(
                    row["thread_id"] != thread_id
                    or row["turn_id"] != str(turn_id)
                    or row["kind"] != "message"
                    or row["id"] != f"message:{turn_id}:{index}"
                    for index, row in enumerate(rows)
                )
                or len(rows) == 2
                and rows[0]["seq"] >= rows[1]["seq"]
            ):
                raise StorageDataError("history Turn contains unexpected documents")
            seed = _decode_document(conn, json.loads(rows[0]["data"]))
            if not isinstance(seed, dict) or seed.get("type") != "human":
                raise StorageDataError("history Turn input is invalid")
            seed_data = seed.get("data")
            if (
                not isinstance(seed_data, dict)
                or seed_data.get("type") != "human"
                or seed_data.get("id") != str(user_message_id)
                or seed_data.get("content") != prompt
            ):
                raise StorageDataError("history Turn input differs from accepted command")
            if len(rows) == 1:
                if turn["status"] == "complete":
                    raise StorageDataError("complete history Turn has no answer")
                return None
            answer = _decode_document(conn, json.loads(rows[1]["data"]))
            if not isinstance(answer, dict) or answer.get("type") != "ai":
                raise StorageDataError("history Turn output is invalid")
            data = answer.get("data")
            if not isinstance(data, dict):
                raise StorageDataError("history Turn output is invalid")
            text = data.get("content")
            extra = data.get("additional_kwargs")
            metadata = data.get("response_metadata")
            calls = data.get("tool_calls")
            if calls:
                call = calls[0] if isinstance(calls, list) and len(calls) == 1 else None
                arguments = call.get("args") if isinstance(call, dict) else None
                try:
                    valid_document_call = (
                        turn["status"] != "complete"
                        and data.get("type") == "ai"
                        and isinstance(data.get("id"), str)
                        and bool(data["id"].strip())
                        and isinstance(text, str)
                        and isinstance(extra, dict)
                        and isinstance(metadata, dict)
                        and not data.get("invalid_tool_calls")
                        and not any(
                            extra.get(key)
                            for key in ("tool_calls", "function_call", "history_tool_args")
                        )
                        and not extra.get("octop_stream_error")
                        and metadata.get("finish_reason") not in {"length", "content_filter"}
                        and isinstance(call, dict)
                        and call.get("type") == "tool_call"
                        and call.get("name") == TOOL_NAME
                        and isinstance(call.get("id"), str)
                        and 1 <= len(call["id"]) <= 256
                        and isinstance(arguments, dict)
                        and set(arguments) == {"path"}
                        and validate_document_filename(arguments["path"]) == arguments["path"]
                    )
                except (CommandAdmissionError, ValueError, TypeError):
                    valid_document_call = False
                if not valid_document_call:
                    raise StorageDataError("history Turn tool call is invalid")
                # The model call may reach history before the effect journal is
                # prepared. Do not publish its text or mislabel it as corruption.
                return None
            if (
                data.get("type") != "ai"
                or not isinstance(data.get("id"), str)
                or not data["id"].strip()
                or not isinstance(text, str)
                or not isinstance(extra, dict)
                or not isinstance(metadata, dict)
                or any(data.get(key) for key in ("tool_calls", "invalid_tool_calls"))
                or any(
                    extra.get(key) for key in ("tool_calls", "function_call", "history_tool_args")
                )
                or extra.get("octop_stream_error")
                or metadata.get("finish_reason") in {"length", "content_filter", "tool_calls"}
            ):
                raise StorageDataError("history Turn output is not safe text")
            return text if text.strip() else None
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, StorageDataError):
            raise
        raise StorageDataError("history Turn output is unavailable") from exc


def get_run_output(
    services: SharedServices, *, owner_id: int, run_id: UUID
) -> RunOutputSnapshot | None:
    """Return a safe point-in-time text snapshot for one owned, fully bound Run."""
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    try:
        with services.db.transaction() as conn:
            current = get_run_in_transaction(
                services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
            )
            if current is None:
                return None
            bound = load_bound_run(
                services, conn, owner_id=owner_id, run_id=run_id, box_id=identity.boxID
            )
            if bound is None:
                raise StorageDataError("Run binding disappeared")
            snapshot = bound.snapshot
            turn_id = bound.receipt.turnID
            user_message_id = bound.receipt.userMessageID
            has_tool_effect = bool(
                services.ac_tool_effect_repo.list_for_run_in_transaction(
                    conn, bound.run.run_id, owner_id
                )
            )
    except (sqlite3.DatabaseError, PostgresError) as exc:
        raise StorageUnavailableError("Run output storage unavailable") from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, StorageDataError):
            raise
        raise StorageDataError("invalid persisted Run output binding") from exc
    if current.state == "succeeded":
        result = get_run_result(services, owner_id=owner_id, run_id=run_id)
        if result is None or result.state != "succeeded" or result.text is None:
            raise StorageDataError("successful Run result changed during output read")
        return RunOutputSnapshot(
            runID=run_id,
            state="succeeded",
            text=result.text,
            digest=content_digest(result.text),
            provisional=False,
        )
    if current.dispatchPhase != "engineAccepted":
        return RunOutputSnapshot(
            runID=run_id,
            state=current.state,
            text=None,
            digest=None,
            provisional=False,
        )
    if has_tool_effect:
        return RunOutputSnapshot(
            runID=run_id,
            state=current.state,
            text=None,
            digest=None,
            provisional=False,
        )
    if not services.config.database.is_sqlite:
        raise StorageDataError("Run has no supported history archive")
    text = _read_partial_text(
        services.paths.root / "history_v2.sqlite",
        identity=str(services.config.database.resolve_sqlite_path(services.paths.root).resolve()),
        turn_id=turn_id,
        thread_id=snapshot.octopThreadID,
        agent_id=snapshot.agentID,
        user_message_id=user_message_id,
        prompt=snapshot.request.prompt,
    )
    return RunOutputSnapshot(
        runID=run_id,
        state=current.state,
        text=text,
        digest=content_digest(text) if text is not None else None,
        provisional=text is not None,
    )
