"""A complete archive status alone cannot prove a bound, tool-free answer."""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict

from octop.infra.agentcomputer.commands import content_digest
from octop.infra.history.store import (
    BoundCompletionProbe,
    HistoryStore,
    probe_bound_completion,
)


@pytest.fixture
def complete_turn(tmp_path):
    path = tmp_path / "history.sqlite"
    store = HistoryStore(path, identity="control-db")
    turn_id = str(uuid4())
    user_message_id = str(uuid4())
    kwargs = {
        "identity": "control-db",
        "turn_id": turn_id,
        "thread_id": "managed-thread",
        "agent_id": "managed-agent",
        "user_message_id": user_message_id,
        "prompt": "  原文\n第二行  ",
    }
    with store.transaction() as conn:
        segment = conn.execute(
            "INSERT INTO segments (thread_id,agent_id,format,legacy_start,trajectory_start) "
            "VALUES (?,?,'v2',0,0)",
            (kwargs["thread_id"], kwargs["agent_id"]),
        )
        conn.execute(
            "INSERT INTO turns (id,segment_id,status,started_at,finished_at) "
            "VALUES (?,?,'complete',1,2)",
            (turn_id, segment.lastrowid),
        )
        store.put_document(
            conn,
            doc_id=f"message:{turn_id}:0",
            thread_id=kwargs["thread_id"],
            turn_id=turn_id,
            kind="message",
            seq=1,
            value=message_to_dict(HumanMessage(content=kwargs["prompt"], id=user_message_id)),
        )
        store.put_document(
            conn,
            doc_id=f"message:{turn_id}:1",
            thread_id=kwargs["thread_id"],
            turn_id=turn_id,
            kind="message",
            seq=2,
            value=message_to_dict(AIMessage(content="可核对的回答", id="model-answer-1")),
        )
    try:
        yield store, kwargs
    finally:
        store.close()


def _replace(store, kwargs, index, wire):
    with store.transaction() as conn:
        store.put_document(
            conn,
            doc_id=f"message:{kwargs['turn_id']}:{index}",
            thread_id=kwargs["thread_id"],
            turn_id=kwargs["turn_id"],
            kind="message",
            seq=index + 1,
            value=wire,
        )


def test_exact_completed_answer_is_read_without_changing_archive(complete_turn):
    store, kwargs = complete_turn
    with store.db.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM turns")]
    result = probe_bound_completion(store.path, **kwargs)
    assert result.status == "complete"
    assert result.assistant_message_id == "model-answer-1"
    assert result.assistant_content_digest == content_digest("可核对的回答")
    with store.db.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM turns")] == before


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("wrongIdentity", "archiveIdentityMismatch"),
        ("wrongTurn", "turnMissing"),
        ("wrongThread", "turnBindingMismatch"),
        ("wrongAgent", "turnBindingMismatch"),
        ("wrongUserID", "inputMismatch"),
        ("wrongPrompt", "inputMismatch"),
    ],
)
def test_fixed_binding_and_input_cannot_be_inferred(complete_turn, change, expected):
    store, kwargs = complete_turn
    altered = dict(kwargs)
    field = {
        "wrongIdentity": "identity",
        "wrongTurn": "turn_id",
        "wrongThread": "thread_id",
        "wrongAgent": "agent_id",
        "wrongUserID": "user_message_id",
        "wrongPrompt": "prompt",
    }[change]
    altered[field] = "other"
    assert probe_bound_completion(store.path, **altered) == BoundCompletionProbe(expected)


@pytest.mark.parametrize("status", ["active", "paused", "interrupted", "failed", "partial"])
def test_noncomplete_turn_never_proves_completion(complete_turn, status):
    store, kwargs = complete_turn
    with store.transaction() as conn:
        conn.execute("UPDATE turns SET status=? WHERE id=?", (status, kwargs["turn_id"]))
    assert probe_bound_completion(store.path, **kwargs) == BoundCompletionProbe("turnNotComplete")


def test_complete_flag_with_error_or_missing_finish_is_rejected(complete_turn):
    store, kwargs = complete_turn
    with store.transaction() as conn:
        conn.execute("UPDATE turns SET error='capture_incomplete' WHERE id=?", (kwargs["turn_id"],))
    assert probe_bound_completion(store.path, **kwargs) == BoundCompletionProbe("turnIncomplete")
    with store.transaction() as conn:
        conn.execute(
            "UPDATE turns SET error=NULL,finished_at=NULL WHERE id=?", (kwargs["turn_id"],)
        )
    assert probe_bound_completion(store.path, **kwargs) == BoundCompletionProbe("turnIncomplete")


@pytest.mark.parametrize(
    "change", ["missing", "wrongID", "wrongText", "wrongRole", "forgedInnerRole"]
)
def test_complete_flag_cannot_substitute_for_exact_seed(complete_turn, change):
    store, kwargs = complete_turn
    if change == "missing":
        with store.transaction() as conn:
            conn.execute("DELETE FROM documents WHERE id=?", (f"message:{kwargs['turn_id']}:0",))
        expected = "messagesIncomplete"
    else:
        seed = HumanMessage(
            content="changed" if change == "wrongText" else kwargs["prompt"],
            id="wrong-id" if change == "wrongID" else kwargs["user_message_id"],
        )
        wire = message_to_dict(seed)
        if change == "wrongRole":
            wire["type"] = "ai"
        elif change == "forgedInnerRole":
            wire["data"]["type"] = "ai"
        _replace(store, kwargs, 0, wire)
        expected = "inputMismatch"
    assert probe_bound_completion(store.path, **kwargs) == BoundCompletionProbe(expected)


@pytest.mark.parametrize(
    "answer",
    [
        AIMessage(content="", id="answer"),
        AIMessage(content="answer", id=None),
        AIMessage(content="answer", id="synthetic"),
        AIMessage(
            content="answer",
            id="answer",
            tool_calls=[{"name": "write", "args": {}, "id": "call-1"}],
        ),
        AIMessage(content="answer", id="answer", additional_kwargs={"octop_stream_error": True}),
        AIMessage(content="answer", id="answer", response_metadata={"finish_reason": "length"}),
    ],
)
def test_partial_or_tool_answer_is_not_completion(complete_turn, answer):
    store, kwargs = complete_turn
    if answer.id == "synthetic":
        answer.id = f"{kwargs['turn_id']}:stream:1"
    _replace(store, kwargs, 1, message_to_dict(answer))
    assert probe_bound_completion(store.path, **kwargs) == BoundCompletionProbe("answerIncomplete")


def test_extra_tool_result_or_extra_answer_is_not_tool_free_completion(complete_turn):
    store, kwargs = complete_turn
    _replace(
        store,
        kwargs,
        2,
        message_to_dict(ToolMessage(content="side effect", tool_call_id="call-1")),
    )
    assert probe_bound_completion(store.path, **kwargs) == BoundCompletionProbe(
        "messagesIncomplete"
    )


def test_missing_archive_is_never_created_by_completion_probe(tmp_path):
    path = tmp_path / "history.sqlite"
    kwargs = {
        "identity": "control-db",
        "turn_id": str(uuid4()),
        "thread_id": "thread",
        "agent_id": "agent",
        "user_message_id": str(uuid4()),
        "prompt": "hello",
    }
    assert probe_bound_completion(path, **kwargs) == BoundCompletionProbe("archiveMissing")
    assert not path.exists()
    path.with_suffix(".required").write_text("required")
    assert probe_bound_completion(path, **kwargs) == BoundCompletionProbe("archiveUnavailable")
    assert not path.exists()
