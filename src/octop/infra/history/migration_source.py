"""Pinned legacy reads without starting an agent or modifying source stores."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from langgraph.channels.delta import DeltaChannel
from langgraph.graph.message import _messages_delta_reducer

from octop.infra.agents.memory.backend import open_memory_kwargs
from octop.infra.agents.workspace.dir import workspace_dir_from_config
from octop.infra.db.repos.threads import ThreadRow
from octop.infra.db.services import SharedServices
from octop.infra.history.projection import _role, message_inputs


class SourceUnavailable(ValueError):
    """Stable, non-secret diagnostic for a source that cannot safely migrate."""


@dataclass(frozen=True)
class SourceSnapshot:
    wires: list[dict[str, Any]]
    digest: str
    anchor: dict[str, Any]
    warnings: list[str]
    trajectory: list[dict[str, Any]]


def _wires(raw: list[Any]) -> list[dict[str, Any]]:
    normalized = message_inputs(raw)
    if len(normalized) != len([item for item in raw if _role(item) != "system"]):
        raise SourceUnavailable("invalid_message")
    return [json.loads(item.message_json) for item in normalized]


def _checkpoint_values(
    saver: Any, thread_id: str, checkpoint_id: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    saved = saver.get_tuple(config)
    if saved is None:
        if checkpoint_id:
            raise SourceUnavailable("checkpoint_missing")
        return [], {}
    values = saved.checkpoint.get("channel_values", {})
    # A branch or interrupt is executable state, not a completed history boundary.
    if any(
        key.startswith("branch:to:") and value is not None for key, value in values.items()
    ) or any(write[1] == "__interrupt__" for write in saved.pending_writes or []):
        raise SourceUnavailable("source_active")
    history = saver.get_delta_channel_history(config=saved.config, channels=["messages"])[
        "messages"
    ]
    channel: DeltaChannel[Any] = DeltaChannel(cast(Any, _messages_delta_reducer))
    channel = channel.from_checkpoint(history.get("seed", []))
    channel.replay_writes(history["writes"])
    raw = channel.get() if channel.is_available() else []
    # Pending message writes belong to an unfinished superstep; never synthesize a completion.
    if any(write[1] == "messages" for write in saved.pending_writes or []):
        raise SourceUnavailable("source_active")
    return _wires(raw), dict(saved.config.get("configurable", {}))


def checkpoint_snapshot(
    services: SharedServices,
    thread: ThreadRow,
    config: dict[str, Any],
    checkpoint_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if config.get("_migration_checkpoint_unavailable"):
        raise SourceUnavailable("checkpoint_backup_unavailable")
    workspace = workspace_dir_from_config(
        config, paths=services.paths, agent_id=thread.agent_id, ensure=False
    )
    _, backend, options = open_memory_kwargs(
        agent_id=thread.agent_id, cfg=config, octop_config=services.config, workspace_dir=workspace
    )
    if backend == "postgres":
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        with psycopg.connect(
            str((options or {})["dsn"]), row_factory=dict_row, prepare_threshold=0
        ) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return _checkpoint_values(PostgresSaver(conn), thread.thread_id, checkpoint_id)
    path = Path(str((options or {})["db_path"]))
    if not path.exists():
        return [], {}
    from langgraph.checkpoint.sqlite import SqliteSaver
    from octop_memory.storage.backends.sqlite_checkpoint import CompactSqliteSaver

    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
            ).fetchone()
            is None
        ):
            return [], {}
        # CompactSqliteSaver.cursor starts its own transaction. Use its codec
        # with the base reader so every checkpoint/blob query shares this
        # explicit read snapshot without nested transactions or setup writes.
        saver = SqliteSaver(conn, serde=CompactSqliteSaver(conn).codec)
        saver.is_setup = True
        return _checkpoint_values(saver, thread.thread_id, checkpoint_id)


def _decode(conn: sqlite3.Connection, node: Any) -> Any:
    kind, value = node
    if kind == "body":
        row = conn.execute("SELECT value FROM bodies WHERE digest=?", (value,)).fetchone()
        if row is None:
            raise SourceUnavailable("archive_body_missing")
        if hashlib.sha256(str(row[0]).encode()).hexdigest() != value:
            raise SourceUnavailable("archive_body_corrupt")
        return json.loads(row[0])
    if kind == "text":
        return "".join(_decode(conn, ["body", digest]) for digest in value)
    if kind == "dict":
        return {key: _decode(conn, child) for key, child in value}
    if kind == "list":
        return [_decode(conn, child) for child in value]
    if kind != "value":
        raise SourceUnavailable("archive_format_unknown")
    return value


def _archive(
    services: SharedServices, thread: ThreadRow, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]] | None:
    path = services.paths.root / "history_v2.sqlite"
    if not path.exists():
        if path.with_suffix(".required").exists():
            raise SourceUnavailable("archive_missing")
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        meta = conn.execute("SELECT version,identity FROM archive_meta").fetchone()
        expected = str(services.config.database.resolve_sqlite_path(services.paths.root).resolve())
        if meta is None or meta["version"] != 1 or meta["identity"] != expected:
            raise SourceUnavailable("archive_identity_mismatch")
        segments = conn.execute(
            "SELECT * FROM segments WHERE thread_id=? ORDER BY id", (thread.thread_id,)
        ).fetchall()
        if not segments:
            return None
        if conn.execute(
            "SELECT 1 FROM turns t JOIN segments s ON s.id=t.segment_id WHERE s.thread_id=? AND t.status IN ('active','paused')",
            (thread.thread_id,),
        ).fetchone():
            raise SourceUnavailable("source_active")
        wires: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        anchors = []
        for segment in segments:
            if segment["agent_id"] != thread.agent_id:
                raise SourceUnavailable("source_owner_mismatch")
            if segment["format"] == "v2":
                rows = conn.execute(
                    "SELECT d.data FROM documents d JOIN turns t ON t.id=d.turn_id WHERE t.segment_id=? AND d.kind='message' ORDER BY d.seq",
                    (segment["id"],),
                ).fetchall()
                wires.extend(_decode(conn, json.loads(row[0])) for row in rows)
                rows = conn.execute(
                    "SELECT d.data FROM documents d JOIN turns t ON t.id=d.turn_id WHERE t.segment_id=? AND d.kind='event' ORDER BY d.seq",
                    (segment["id"],),
                ).fetchall()
                events.extend(_decode(conn, json.loads(row[0])) for row in rows)
            elif segment["anchor"]:
                anchor = json.loads(segment["anchor"])["checkpoint_config"]["configurable"]
                messages, _ = checkpoint_snapshot(services, thread, config, anchor["checkpoint_id"])
                wires.extend(messages)
            else:
                end = segment["legacy_end"]
                if end is None:
                    end = services.thread_message_repo.head(thread.thread_id)
                wires.extend(
                    json.loads(row.message_json)
                    for row in services.thread_message_repo.range_rows(
                        thread.thread_id, segment["legacy_start"], end
                    )
                )
            anchors.append(
                {
                    "segment": segment["id"],
                    "format": segment["format"],
                    "legacy_end": segment["legacy_end"],
                    "trajectory_end": segment["trajectory_end"],
                }
            )
        return wires, {"archive_segments": anchors}, events


async def read_source(
    services: SharedServices, thread: ThreadRow, config: dict[str, Any], workspace: Any
) -> SourceSnapshot:
    archived = await asyncio.to_thread(_archive, services, thread, config)
    checkpoint, checkpoint_anchor = await asyncio.to_thread(
        checkpoint_snapshot, services, thread, config
    )
    warnings: list[str] = []
    events = await asyncio.to_thread(
        lambda: [
            asdict(event)
            for event in services.trajectory_event_repo.iter_for_export(thread.thread_id)
        ]
    )
    if archived is not None:
        wires, anchor, archived_events = archived
        events.extend(archived_events)
    elif (
        await asyncio.to_thread(services.thread_message_repo.projection_status, thread.thread_id)
        == "ready"
    ):
        head = await asyncio.to_thread(services.thread_message_repo.head, thread.thread_id)
        rows = await asyncio.to_thread(
            services.thread_message_repo.range_rows, thread.thread_id, 0, head
        )
        wires, anchor = [json.loads(row.message_json) for row in rows], {"projection_head": head}
    else:
        wires, anchor = checkpoint, {"checkpoint": checkpoint_anchor}
    if not wires:
        # Only explicitly identified log entries are eligible. Time-window matching is ambiguous.
        entries = await asyncio.to_thread(workspace.list_dir, "sessions")
        raw = []
        ambiguous = False
        for entry in entries or []:
            path = str(
                entry.get("path", "") if isinstance(entry, dict) else getattr(entry, "path", "")
            )
            if not path.endswith(".jsonl"):
                continue
            text = await workspace.aread_text(
                path if path.startswith("sessions/") else "sessions/" + Path(path).name
            )
            for line in (text or "").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    warnings.append("invalid_jsonl_line")
                    continue
                if not isinstance(item, dict):
                    continue
                if not item.get("thread_id"):
                    ambiguous = True
                if item.get("thread_id") == thread.thread_id and item.get("role") in {
                    "user",
                    "assistant",
                }:
                    raw.append(item)
        if raw:
            wires, anchor = _wires(raw), {"jsonl": True}
        elif ambiguous:
            raise SourceUnavailable("jsonl_owner_ambiguous")
    if not wires:
        raise SourceUnavailable("empty_history")
    anchor["latest_checkpoint"] = checkpoint_anchor
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("thread_id") != thread.thread_id or event.get("agent_id") != thread.agent_id:
            raise SourceUnavailable("trajectory_owner_mismatch")
        identifier = str(event.get("event_id") or "")
        if not identifier or not isinstance(event.get("seq"), int):
            raise SourceUnavailable("trajectory_format_unknown")
        if identifier in by_id and by_id[identifier] != event:
            raise SourceUnavailable("trajectory_conflict")
        by_id[identifier] = event
    events = sorted(by_id.values(), key=lambda event: (event["seq"], event["event_id"]))
    anchor["trajectory_head"] = events[-1]["seq"] if events else None
    digest = hashlib.sha256(
        json.dumps(
            [wires, anchor, events, thread.artifacts],
            sort_keys=True,
            default=lambda value: value.as_ref(),
        ).encode()
    ).hexdigest()
    return SourceSnapshot(wires, digest, anchor, warnings, events)
