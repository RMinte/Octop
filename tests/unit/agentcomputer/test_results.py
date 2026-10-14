"""Only a bound, durable no-tool completion exposes an AgentComputer answer."""

from __future__ import annotations

import sqlite3

import pytest
from tests.unit.agentcomputer.test_checkpoint_evidence import persisted as persisted
from tests.unit.agentcomputer.test_engine_entry import entry as entry
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.infra.agentcomputer.commands import StorageDataError
from octop.infra.agentcomputer.results import get_run_result


async def test_completed_run_returns_exact_archived_answer(persisted) -> None:
    fixture, _, _ = persisted
    run_id = fixture.claim.intent.runID
    result = get_run_result(fixture.services, owner_id=1, run_id=run_id)
    assert result is not None
    assert result.state == "succeeded"
    assert result.runID == run_id
    assert result.assistantMessageID == str(fixture.claim.intent.assistantMessageID)
    assert result.text == "fixture response"
    assert get_run_result(fixture.services, owner_id=2, run_id=run_id) is None


async def test_completed_run_rejects_missing_history_answer(persisted) -> None:
    fixture, _, _ = persisted
    run_id = fixture.claim.intent.runID
    history = fixture.services.paths.root / "history_v2.sqlite"
    with sqlite3.connect(history) as conn:
        conn.execute(
            "DELETE FROM documents WHERE id=?",
            (f"message:{fixture.claim.intent.turnID}:1",),
        )
    with pytest.raises(StorageDataError, match="final history evidence"):
        get_run_result(fixture.services, owner_id=1, run_id=run_id)
