"""Independent SQLite archive. No control-plane schema migrations or backfill.

Message and trajectory documents share content-addressed bodies. Replacing a
live document releases only its own obsolete bodies, in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octop.infra.db.pool import SqlitePool

_BODY_KEYS = frozenset(
    {"content", "text", "reasoning_content", "thinking", "summary", "args", "result"}
)
# Stable boundaries let an appended token reuse every completed text block.
# Count Unicode characters so splitting and rejoining never damages UTF-8.
TEXT_BLOCK_SIZE = 1024
_BOUND_STATUSES = frozenset({"active", "paused", "interrupted", "failed", "partial", "complete"})
_SCHEMA = """
CREATE TABLE archive_meta (version INTEGER NOT NULL, identity TEXT NOT NULL);
CREATE TABLE segments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 format TEXT NOT NULL CHECK(format IN ('legacy','v2')),
 legacy_start INTEGER NOT NULL, legacy_end INTEGER, anchor TEXT,
 trajectory_start INTEGER NOT NULL, trajectory_end INTEGER
);
CREATE INDEX segments_thread ON segments(thread_id,id);
CREATE TABLE turns (
 id TEXT PRIMARY KEY, segment_id INTEGER NOT NULL REFERENCES segments(id),
 status TEXT NOT NULL, error TEXT, started_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE bodies (digest TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL REFERENCES turns(id),
 kind TEXT NOT NULL CHECK(kind IN ('message','event')), seq INTEGER NOT NULL,
 data TEXT NOT NULL, UNIQUE(thread_id,kind,seq)
);
CREATE INDEX documents_turn ON documents(turn_id,kind,seq);
CREATE TABLE body_refs (
 owner TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 digest TEXT NOT NULL REFERENCES bodies(digest), PRIMARY KEY(owner,digest)
);
CREATE INDEX refs_digest ON body_refs(digest);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_document(conn: sqlite3.Connection, node: Any) -> Any:
    tag, value = node
    if tag == "body":
        row = conn.execute("SELECT value FROM bodies WHERE digest=?", (value,)).fetchone()
        if row is None:
            raise ValueError("History body reference is missing")
        return json.loads(row[0])
    if tag == "text":
        return "".join(_decode_document(conn, ["body", digest]) for digest in value)
    if tag == "dict":
        return {key: _decode_document(conn, item) for key, item in value}
    if tag == "list":
        return [_decode_document(conn, item) for item in value]
    if tag == "value":
        return value
    raise ValueError("Unknown history encoding")


@dataclass(frozen=True)
class BoundTurnProbe:
    status: str
    initial_message_saved: bool = False


@dataclass(frozen=True)
class BoundCompletionProbe:
    """Exact archived answer evidence; it is not a control-plane Run receipt."""

    status: str
    assistant_message_id: str | None = None
    assistant_content_digest: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_digest: str | None = None
    tool_message_id: str | None = None
    tool_content_digest: str | None = None


@dataclass(frozen=True)
class BoundToolExpectation:
    """The exact single call and result authorized by the Run's tool journal."""

    tool_call_id: str
    tool_name: str
    tool_arguments_digest: str
    tool_content_digest: str


def _bound_final_answer(answer: Any, turn_id: str) -> tuple[str, str] | None:
    if not isinstance(answer, dict) or answer.get("type") != "ai":
        return None
    data = answer.get("data")
    if not isinstance(data, dict):
        return None
    answer_id = data.get("id")
    content = data.get("content")
    extra = data.get("additional_kwargs")
    metadata = data.get("response_metadata")
    if (
        not isinstance(answer_id, str)
        or not answer_id.strip()
        or answer_id.startswith(f"{turn_id}:stream:")
        or data.get("type") != "ai"
        or not isinstance(content, str)
        or not content.strip()
        or not isinstance(extra, dict)
        or not isinstance(metadata, dict)
        or any(data.get(key) for key in ("tool_calls", "invalid_tool_calls"))
        or any(extra.get(key) for key in ("tool_calls", "function_call", "history_tool_args"))
        or extra.get("octop_stream_error")
        or metadata.get("finish_reason") in {"length", "content_filter", "tool_calls"}
    ):
        return None
    digest = "sha256:" + hashlib.sha256(dumps(content).encode("utf-8")).hexdigest()
    return answer_id, digest


def _bound_tool_exchange(
    call_message: Any, result_message: Any, turn_id: str, expected: BoundToolExpectation
) -> tuple[str, str, str, str, str] | None:
    if (
        not isinstance(call_message, dict)
        or call_message.get("type") != "ai"
        or not isinstance(result_message, dict)
        or result_message.get("type") != "tool"
    ):
        return None
    call_data = call_message.get("data")
    result_data = result_message.get("data")
    if not isinstance(call_data, dict) or not isinstance(result_data, dict):
        return None
    call_id = call_data.get("id")
    call_content = call_data.get("content")
    call_extra = call_data.get("additional_kwargs")
    call_metadata = call_data.get("response_metadata")
    calls = call_data.get("tool_calls")
    if (
        call_data.get("type") != "ai"
        or not isinstance(call_id, str)
        or not call_id.strip()
        or call_id.startswith(f"{turn_id}:stream:")
        or not isinstance(call_content, str)
        or not isinstance(call_extra, dict)
        or not isinstance(call_metadata, dict)
        or call_data.get("invalid_tool_calls")
        or any(call_extra.get(key) for key in ("tool_calls", "function_call", "history_tool_args"))
        or call_extra.get("octop_stream_error")
        or call_metadata.get("finish_reason") in {"length", "content_filter"}
        or not isinstance(calls, list)
        or len(calls) != 1
        or not isinstance(calls[0], dict)
    ):
        return None
    call = calls[0]
    arguments = call.get("args")
    if (
        call.get("type") != "tool_call"
        or call.get("id") != expected.tool_call_id
        or call.get("name") != expected.tool_name
        or not isinstance(arguments, dict)
    ):
        return None
    try:
        arguments_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )
    except (ValueError, TypeError):
        return None
    result_id = result_data.get("id")
    result_content = result_data.get("content")
    result_extra = result_data.get("additional_kwargs")
    if (
        arguments_digest != expected.tool_arguments_digest
        or result_data.get("type") != "tool"
        or not isinstance(result_id, str)
        or not result_id.strip()
        or result_data.get("tool_call_id") != expected.tool_call_id
        or result_data.get("name") != expected.tool_name
        or result_data.get("status") != "success"
        or not isinstance(result_content, str)
        or not result_content
        or not isinstance(result_extra, dict)
        or result_extra.get("octop_stream_error")
    ):
        return None
    result_digest = "sha256:" + hashlib.sha256(dumps(result_content).encode("utf-8")).hexdigest()
    if result_digest != expected.tool_content_digest:
        return None
    return call_id, expected.tool_call_id, expected.tool_name, arguments_digest, result_id


def probe_bound_completion(
    path: Path,
    *,
    identity: str,
    turn_id: str,
    thread_id: str,
    agent_id: str,
    user_message_id: str,
    prompt: str,
    expected_tool: BoundToolExpectation | None = None,
) -> BoundCompletionProbe:
    """Read one complete bound Turn from a consistent archive snapshot.

    A generic history ``complete`` flag alone is insufficient. This validates
    the fixed input, an optional single journal-bound tool exchange, and one
    persisted final answer. It does not prove that the engine stopped, nor that
    the control-plane Run may release its lanes.
    """
    try:
        if not path.exists():
            marker = path.with_suffix(".required")
            return BoundCompletionProbe(
                "archiveUnavailable" if marker.exists() else "archiveMissing"
            )
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            meta = conn.execute("SELECT version, identity FROM archive_meta").fetchall()
            if len(meta) != 1 or meta[0]["version"] != 1 or meta[0]["identity"] != identity:
                return BoundCompletionProbe("archiveIdentityMismatch")
            turn = conn.execute(
                "SELECT t.status,t.error,t.finished_at,s.thread_id,s.agent_id,s.format "
                "FROM turns t JOIN segments s ON s.id=t.segment_id WHERE t.id=?",
                (turn_id,),
            ).fetchone()
            if turn is None:
                return BoundCompletionProbe("turnMissing")
            if (
                turn["thread_id"] != thread_id
                or turn["agent_id"] != agent_id
                or turn["format"] != "v2"
                or turn["status"] not in _BOUND_STATUSES
            ):
                return BoundCompletionProbe("turnBindingMismatch")
            if turn["status"] != "complete":
                return BoundCompletionProbe("turnNotComplete")
            if turn["error"] is not None or turn["finished_at"] is None:
                return BoundCompletionProbe("turnIncomplete")
            rows = conn.execute(
                "SELECT id,thread_id,turn_id,kind,seq,data FROM documents "
                "WHERE turn_id=? ORDER BY seq,id",
                (turn_id,),
            ).fetchall()
            expected_count = 4 if expected_tool is not None else 2
            if len(rows) != expected_count or any(
                row["id"] != f"message:{turn_id}:{index}"
                or row["thread_id"] != thread_id
                or row["turn_id"] != turn_id
                or row["kind"] != "message"
                or (index > 0 and rows[index - 1]["seq"] >= row["seq"])
                for index, row in enumerate(rows)
            ):
                return BoundCompletionProbe("messagesIncomplete")
            seed = _decode_document(conn, json.loads(rows[0]["data"]))
            if not isinstance(seed, dict) or seed.get("type") != "human":
                return BoundCompletionProbe("inputMismatch")
            seed_data = seed.get("data")
            if (
                not isinstance(seed_data, dict)
                or seed_data.get("type") != "human"
                or seed_data.get("id") != user_message_id
                or seed_data.get("content") != prompt
            ):
                return BoundCompletionProbe("inputMismatch")
            tool_exchange = None
            if expected_tool is not None:
                call_message = _decode_document(conn, json.loads(rows[1]["data"]))
                result_message = _decode_document(conn, json.loads(rows[2]["data"]))
                tool_exchange = _bound_tool_exchange(
                    call_message, result_message, turn_id, expected_tool
                )
                if tool_exchange is None:
                    return BoundCompletionProbe("toolMismatch")
            answer = _decode_document(conn, json.loads(rows[-1]["data"]))
            final = _bound_final_answer(answer, turn_id)
            if final is None:
                return BoundCompletionProbe("answerIncomplete")
            answer_id, digest = final
            if tool_exchange is None:
                return BoundCompletionProbe("complete", answer_id, digest)
            assert expected_tool is not None
            call_message_id, tool_call_id, tool_name, arguments_digest, tool_message_id = (
                tool_exchange
            )
            if len({user_message_id, call_message_id, tool_message_id, answer_id}) != 4:
                return BoundCompletionProbe("toolMismatch")
            return BoundCompletionProbe(
                "complete",
                answer_id,
                digest,
                tool_call_id,
                tool_name,
                arguments_digest,
                tool_message_id,
                expected_tool.tool_content_digest,
            )
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError, KeyError):
        return BoundCompletionProbe("archiveUnavailable")


def probe_bound_turn(
    path: Path, *, identity: str, turn_id: str, thread_id: str, agent_id: str
) -> BoundTurnProbe:
    """Read an exact external Turn without creating or repairing the archive.

    This is diagnostic evidence only. A complete history Turn is not a Run
    terminal receipt and says nothing about whether the old worker stopped.
    """
    try:
        if not path.exists():
            marker = path.with_suffix(".required")
            return BoundTurnProbe("archiveUnavailable" if marker.exists() else "archiveMissing")
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            meta = conn.execute("SELECT version, identity FROM archive_meta").fetchall()
            if len(meta) != 1 or meta[0]["version"] != 1 or meta[0]["identity"] != identity:
                return BoundTurnProbe("archiveIdentityMismatch")
            row = conn.execute(
                "SELECT t.status,t.error,s.thread_id,s.agent_id,s.format FROM turns t "
                "JOIN segments s ON s.id=t.segment_id WHERE t.id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return BoundTurnProbe("turnMissing")
            if (
                row["thread_id"] != thread_id
                or row["agent_id"] != agent_id
                or row["format"] != "v2"
                or row["status"] not in _BOUND_STATUSES
                or (row["status"] == "complete" and row["error"] is not None)
            ):
                return BoundTurnProbe("turnBindingMismatch")
            initial = conn.execute(
                "SELECT 1 FROM documents WHERE id=? AND thread_id=? AND turn_id=? "
                "AND kind='message'",
                (f"message:{turn_id}:0", thread_id, turn_id),
            ).fetchone()
            return BoundTurnProbe(str(row["status"]), initial is not None)
    except (sqlite3.DatabaseError, OSError):
        return BoundTurnProbe("archiveUnavailable")


class HistoryStore:
    def __init__(self, path: Path, *, identity: str) -> None:
        existed = path.exists()
        marker = path.with_suffix(".required")
        if not existed and marker.exists():
            raise FileNotFoundError("The required history archive is missing: " + str(path))
        self.path = path
        self.db = SqlitePool(path)
        try:
            with self.db.connect() as conn:
                conn.execute("PRAGMA synchronous = FULL")
                conn.execute("PRAGMA busy_timeout = 5000")
                if not existed:
                    conn.executescript("BEGIN IMMEDIATE;" + _SCHEMA)
                    conn.execute("INSERT INTO archive_meta VALUES (1, ?)", (identity,))
                    conn.commit()
                row = conn.execute("SELECT version, identity FROM archive_meta").fetchone()
                if row is None or row["version"] != 1 or row["identity"] != identity:
                    raise ValueError("History archive version or control-plane identity mismatch")
            marker.write_text(
                "History archive required; do not remove independently.\n", encoding="utf-8"
            )
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # Refuse to recreate a missing archive: absence is not an empty history.
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.db.transaction() as conn:
            yield conn

    def segments(self, thread_id: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.db.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM segments WHERE thread_id = ? ORDER BY id", (thread_id,)
                )
            ]

    def turn(self, thread_id: str, *, unfinished: bool = False) -> dict[str, Any] | None:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT t.*, s.format, s.thread_id, s.agent_id FROM turns t "
                "JOIN segments s ON s.id=t.segment_id WHERE s.thread_id=? "
                + ("AND t.status IN ('active','paused') " if unfinished else "")
                + "ORDER BY t.started_at DESC, t.id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def put_document(
        self,
        conn: sqlite3.Connection,
        *,
        doc_id: str,
        thread_id: str,
        turn_id: str,
        kind: str,
        seq: int,
        value: dict[str, Any],
    ) -> None:
        old = conn.execute("SELECT data FROM documents WHERE id=?", (doc_id,)).fetchone()
        old_refs = {
            str(r[0]) for r in conn.execute("SELECT digest FROM body_refs WHERE owner=?", (doc_id,))
        }
        refs: set[str] = set()

        def intern(item: Any) -> str:
            body = dumps(item)
            digest = hashlib.sha256(body.encode()).hexdigest()
            if digest not in old_refs and digest not in refs:
                conn.execute("INSERT OR IGNORE INTO bodies VALUES (?,?)", (digest, body))
            refs.add(digest)
            return digest

        def encode(item: Any, key: str = "") -> Any:
            if isinstance(item, str) and len(item) > TEXT_BLOCK_SIZE:
                return [
                    "text",
                    [
                        intern(item[start : start + TEXT_BLOCK_SIZE])
                        for start in range(0, len(item), TEXT_BLOCK_SIZE)
                    ],
                ]
            if isinstance(item, dict):
                return ["dict", [[k, encode(v, k)] for k, v in item.items()]]
            if isinstance(item, list):
                return ["list", [encode(v) for v in item]]
            if key in _BODY_KEYS and item not in (None, ""):
                return ["body", intern(item)]
            return ["value", item]

        encoded = dumps(encode(value))
        if old is not None and old["data"] == encoded:
            return
        conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "data=excluded.data",
            (doc_id, thread_id, turn_id, kind, seq, encoded),
        )
        conn.executemany(
            "DELETE FROM body_refs WHERE owner=? AND digest=?",
            [(doc_id, ref) for ref in old_refs - refs],
        )
        conn.executemany(
            "INSERT INTO body_refs VALUES (?,?)", [(doc_id, ref) for ref in refs - old_refs]
        )
        for digest in old_refs - refs:
            conn.execute(
                "DELETE FROM bodies WHERE digest=? AND NOT EXISTS "
                "(SELECT 1 FROM body_refs WHERE digest=?)",
                (digest, digest),
            )

    def _decode(self, conn: sqlite3.Connection, node: Any) -> Any:
        return _decode_document(conn, node)

    def documents(
        self,
        thread_id: str,
        kind: str,
        *,
        segment_id: int | None = None,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        conditions = ["d.thread_id=?", "d.kind=?"]
        args: list[Any] = [thread_id, kind]
        if segment_id is not None:
            conditions.append("t.segment_id=?")
            args.append(segment_id)
        if turn_id is not None:
            conditions.append("d.turn_id=?")
            args.append(turn_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT d.*,t.status FROM documents d JOIN turns t ON t.id=d.turn_id WHERE "
                + " AND ".join(conditions)
                + " ORDER BY d.seq",
                args,
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "seq": row["seq"],
                    "turn_id": row["turn_id"],
                    "status": row["status"],
                    "value": self._decode(conn, json.loads(row["data"])),
                }
                for row in rows
            ]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM documents WHERE id=? AND kind='event'", ("event:" + event_id,)
            ).fetchone()
            if row is None:
                return None
            value: dict[str, Any] = self._decode(conn, json.loads(row[0]))
            return value

    def message_page(
        self, thread_id: str, segment_id: int, before: int | None, limit: int
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT d.seq,d.data FROM documents d JOIN turns t ON t.id=d.turn_id "
                "WHERE d.thread_id=? AND t.segment_id=? AND d.kind='message' "
                "AND (? IS NULL OR d.seq<?) ORDER BY d.seq DESC LIMIT ?",
                (thread_id, segment_id, before, before, limit),
            ).fetchall()
            return [
                {"seq": row["seq"], "value": self._decode(conn, json.loads(row["data"]))}
                for row in rows
            ]

    def event_page(self, thread_id: str, before: int | None, limit: int) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT data FROM documents WHERE thread_id=? AND kind='event' "
                "AND (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT ?",
                (thread_id, before, before, limit),
            ).fetchall()
            return [self._decode(conn, json.loads(row[0])) for row in rows]

    def close(self) -> None:
        self.db.close()
