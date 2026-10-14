"""A Task timeline uses the same bound history proof as the Run result API."""

from __future__ import annotations

import json
import sqlite3

import pytest
from tests.unit.agentcomputer.test_checkpoint_evidence import persisted as persisted
from tests.unit.agentcomputer.test_engine_entry import entry as entry
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.infra.agentcomputer.commands import StorageDataError
from octop.infra.agentcomputer.turn_listing import list_turns


async def test_completed_turn_list_returns_its_verified_answer(persisted) -> None:
    fixture, _, _ = persisted
    intent = fixture.claim.intent
    listing = list_turns(fixture.services, 1, intent.taskID, 20, None)
    assert listing is not None and listing.nextCursor is None
    assert len(listing.items) == 1
    item = listing.items[0]
    assert item.turnID == intent.turnID
    assert item.userMessageID == intent.userMessageID
    assert item.prompt == intent.snapshot.request.prompt
    assert item.run.runID == item.result.runID == intent.runID
    assert item.run.state == item.result.state == "succeeded"
    assert item.result.text == "fixture response"

    identity_path = fixture.services.paths.agentcomputer_identity
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["restoreEpoch"] += 1
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    historical = list_turns(fixture.services, 1, intent.taskID, 20, None)
    assert historical is not None
    assert historical.items[0].result.text == "fixture response"


async def test_completed_turn_list_rejects_unverified_history(persisted) -> None:
    fixture, _, _ = persisted
    intent = fixture.claim.intent
    history = fixture.services.paths.root / "history_v2.sqlite"
    with sqlite3.connect(history) as conn:
        conn.execute(
            "DELETE FROM documents WHERE id=?",
            (f"message:{intent.turnID}:1",),
        )
    with pytest.raises(StorageDataError, match="final history evidence"):
        list_turns(fixture.services, 1, intent.taskID, 20, None)
