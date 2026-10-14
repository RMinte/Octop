"""Live text is visible only from the exact bound Turn, never as final proof."""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from tests.unit.agentcomputer.test_checkpoint_evidence import persisted as persisted
from tests.unit.agentcomputer.test_engine_entry import entry as entry
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.commands import StorageDataError, content_digest
from octop.infra.agentcomputer.output import get_run_output


@pytest.fixture
async def accepted(entry):
    invocation = dispatch._begin_dispatch(entry.services, entry.claim)
    dispatch.accept_engine_entry(entry.services, invocation)
    intent = entry.claim.intent
    store = entry.history_archive.store
    with store.transaction() as conn:
        segment = conn.execute(
            "INSERT INTO segments (thread_id,agent_id,format,legacy_start,trajectory_start) "
            "VALUES (?,?,'v2',0,0)",
            (intent.snapshot.octopThreadID, intent.snapshot.agentID),
        )
        conn.execute(
            "INSERT INTO turns (id,segment_id,status,started_at) VALUES (?,?,'active',1)",
            (str(intent.turnID), segment.lastrowid),
        )
        store.put_document(
            conn,
            doc_id=f"message:{intent.turnID}:0",
            thread_id=intent.snapshot.octopThreadID,
            turn_id=str(intent.turnID),
            kind="message",
            seq=1,
            value=message_to_dict(
                HumanMessage(content=intent.snapshot.request.prompt, id=str(intent.userMessageID))
            ),
        )
    return entry


def _write_answer(entry, answer) -> None:
    intent = entry.claim.intent
    with entry.history_archive.store.transaction() as conn:
        entry.history_archive.store.put_document(
            conn,
            doc_id=f"message:{intent.turnID}:1",
            thread_id=intent.snapshot.octopThreadID,
            turn_id=str(intent.turnID),
            kind="message",
            seq=2,
            value=message_to_dict(answer),
        )


async def test_queued_run_has_no_output_before_engine_acceptance(entry) -> None:
    run_id = entry.claim.intent.runID
    snapshot = get_run_output(entry.services, owner_id=1, run_id=run_id)
    assert snapshot is not None
    assert snapshot.model_dump(mode="json") == {
        "runID": str(run_id),
        "state": "queued",
        "text": None,
        "digest": None,
        "provisional": False,
    }
    assert get_run_output(entry.services, owner_id=2, run_id=run_id) is None
    assert get_run_output(entry.services, owner_id=1, run_id=uuid4()) is None


async def test_bound_live_text_is_explicitly_provisional(accepted) -> None:
    run_id = accepted.claim.intent.runID
    before = get_run_output(accepted.services, owner_id=1, run_id=run_id)
    assert before is not None and before.text is None and not before.provisional
    _write_answer(accepted, AIMessage(content="第一段\n还在生成", id=f"{run_id}:stream:1"))
    snapshot = get_run_output(accepted.services, owner_id=1, run_id=run_id)
    assert snapshot is not None
    assert snapshot.state == "queued"  # Engine acceptance does not mean Run success.
    assert snapshot.text == "第一段\n还在生成"
    assert snapshot.digest == content_digest(snapshot.text)
    assert snapshot.provisional
    assert get_run_output(accepted.services, owner_id=2, run_id=run_id) is None


async def test_complete_archive_without_answer_is_invalid(accepted) -> None:
    intent = accepted.claim.intent
    with accepted.history_archive.store.transaction() as conn:
        conn.execute(
            "UPDATE turns SET status='complete',finished_at=2 WHERE id=?",
            (str(intent.turnID),),
        )
    with pytest.raises(StorageDataError):
        get_run_output(accepted.services, owner_id=1, run_id=intent.runID)


async def test_success_uses_verified_result_receipt(persisted) -> None:
    fixture, _, _ = persisted
    snapshot = get_run_output(fixture.services, owner_id=1, run_id=fixture.claim.intent.runID)
    assert snapshot is not None
    assert snapshot.state == "succeeded"
    assert snapshot.text == "fixture response"
    assert snapshot.digest == content_digest("fixture response")
    assert not snapshot.provisional


@pytest.mark.parametrize("corruption", ["archiveIdentity", "seedText", "turnThread", "toolResult"])
async def test_partial_output_rejects_unbound_or_tool_history(accepted, corruption) -> None:
    _write_answer(accepted, AIMessage(content="would leak", id="partial-id"))
    intent = accepted.claim.intent
    store = accepted.history_archive.store
    with store.transaction() as conn:
        if corruption == "archiveIdentity":
            conn.execute("UPDATE archive_meta SET identity='other-control'")
        elif corruption == "seedText":
            store.put_document(
                conn,
                doc_id=f"message:{intent.turnID}:0",
                thread_id=intent.snapshot.octopThreadID,
                turn_id=str(intent.turnID),
                kind="message",
                seq=1,
                value=message_to_dict(
                    HumanMessage(content="another prompt", id=str(intent.userMessageID))
                ),
            )
        elif corruption == "turnThread":
            conn.execute(
                "UPDATE segments SET thread_id='other-thread' WHERE thread_id=?",
                (intent.snapshot.octopThreadID,),
            )
        else:
            store.put_document(
                conn,
                doc_id=f"message:{intent.turnID}:2",
                thread_id=intent.snapshot.octopThreadID,
                turn_id=str(intent.turnID),
                kind="message",
                seq=3,
                value=message_to_dict(ToolMessage(content="side effect", tool_call_id="call-1")),
            )
    with pytest.raises(StorageDataError):
        get_run_output(accepted.services, owner_id=1, run_id=intent.runID)


@pytest.mark.parametrize(
    "answer",
    [
        AIMessage(
            content="tool output",
            id="partial-id",
            tool_calls=[{"name": "write", "args": {}, "id": "call-1"}],
        ),
        AIMessage(
            content="error body", id="partial-id", additional_kwargs={"octop_stream_error": True}
        ),
    ],
)
async def test_partial_output_rejects_tool_calls_and_stream_errors(accepted, answer) -> None:
    _write_answer(accepted, answer)
    with pytest.raises(StorageDataError):
        get_run_output(accepted.services, owner_id=1, run_id=accepted.claim.intent.runID)


async def test_pending_document_call_without_journal_has_no_partial_text(accepted) -> None:
    _write_answer(
        accepted,
        AIMessage(
            content="",
            id="model-document-call",
            tool_calls=[
                {"name": "read_project_document", "args": {"path": "brief.md"}, "id": "call-1"}
            ],
            response_metadata={"finish_reason": "tool_calls"},
        ),
    )
    snapshot = get_run_output(accepted.services, owner_id=1, run_id=accepted.claim.intent.runID)
    assert snapshot is not None
    assert snapshot.state == "queued"
    assert snapshot.text is None and snapshot.digest is None and not snapshot.provisional


@pytest.mark.parametrize("path", ["../secret.md", "documents/brief.md", "brief.pdf"])
async def test_pending_document_call_rejects_invalid_path(accepted, path: str) -> None:
    _write_answer(
        accepted,
        AIMessage(
            content="",
            id="model-document-call",
            tool_calls=[{"name": "read_project_document", "args": {"path": path}, "id": "call-1"}],
            response_metadata={"finish_reason": "tool_calls"},
        ),
    )
    with pytest.raises(StorageDataError):
        get_run_output(accepted.services, owner_id=1, run_id=accepted.claim.intent.runID)
