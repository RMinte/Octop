"""Strict private journal facts and conservative startup recovery, without tools."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tests.unit.agentcomputer.test_effect_runner import effect as effect
from tests.unit.agentcomputer.test_effect_runner import execute, output
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.infra.agentcomputer import effects
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.identity import read_identity
from octop.infra.agentcomputer.models import ToolEffectAction, ToolEffectOutput
from octop.infra.agentcomputer.recovery import recover_dispatches
from octop.infra.agentcomputer.runs import get_run


@pytest.mark.parametrize("field", ["executionAttempt", "schemaVersion"])
@pytest.mark.parametrize("value", [True, 1.0, "1", 2, 0])
async def test_action_versions_are_strict_and_attempt_two_is_not_a_retry(effect, field, value):
    data = effect.action.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        ToolEffectAction.model_validate(data)


@pytest.mark.parametrize(
    "text",
    ["[]", '{"z":1,"a":2}', '{"same":1,"same":1}', '{"x":NaN}', "broken", "{} "],
)
async def test_noncanonical_arguments_are_rejected_before_journal(effect, text):
    action = effect.action.model_copy(update={"argumentsJSON": text})
    with pytest.raises(StorageDataError):
        effects.prepare_effect(effect.services, effect.invocation, action)
    with effect.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0


async def test_utf8_size_and_payload_digest_are_enforced(effect):
    text = canonical_json({"x": "千" * 22000})
    assert len(text) < 65536 < len(text.encode())
    action = ToolEffectAction.model_validate(
        {
            **effect.action.model_dump(mode="json"),
            "argumentsJSON": text,
            "argumentsDigest": content_digest(json.loads(text)),
        }
    )
    with pytest.raises(StorageDataError):
        effects.prepare_effect(effect.services, effect.invocation, action)
    with pytest.raises(StorageDataError):
        effects.prepare_effect(
            effect.services,
            effect.invocation,
            effect.action.model_copy(update={"argumentsDigest": content_digest({})}),
        )


async def test_prepare_and_event_failure_roll_back_together(effect, monkeypatch):
    before = [tuple(row) for row in effect.events()]
    insert = effect.services.ac_event_repo.insert

    def fail(*args, **kwargs):
        insert(*args, **kwargs)
        raise RuntimeError("injected prepared event failure")

    monkeypatch.setattr(effect.services.ac_event_repo, "insert", fail)
    with pytest.raises(RuntimeError, match="prepared event"):
        effects.prepare_effect(effect.services, effect.invocation, effect.action)
    with effect.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0
    assert [tuple(row) for row in effect.events()] == before


@pytest.mark.parametrize(
    "mutation",
    [
        "unknownSchema",
        "changedAssistant",
        "changedArguments",
        "missingActionVersion",
        "badTimestamp",
    ],
)
async def test_bad_stored_intent_is_rejected_by_effect_and_public_run_queries(effect, mutation):
    prepared = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    with effect.services.db.transaction() as conn:
        if mutation == "badTimestamp":
            conn.execute(
                "UPDATE ac_tool_effects SET created_at=9223372036854775807,updated_at=9223372036854775807"
            )
        elif mutation == "unknownSchema":
            conn.execute("UPDATE ac_tool_effects SET schema_version=2")
        else:
            data = prepared.intent.model_dump(mode="json")
            if mutation == "changedAssistant":
                data["assistantMessageID"] = str(uuid4())
            elif mutation == "changedArguments":
                data["action"]["argumentsJSON"] = canonical_json({"changed": True})
            else:
                del data["action"]["schemaVersion"]
            conn.execute(
                "UPDATE ac_tool_effects SET intent_json=?,intent_digest=?",
                (canonical_json(data), content_digest(data)),
            )
    with pytest.raises(StorageDataError):
        effects.get_effect(effect.services, owner_id=1, effect_id=prepared.intent.effectID)
    with pytest.raises(StorageDataError):
        get_run(effect.services, owner_id=1, run_id=effect.invocation.intent.runID)


@pytest.mark.parametrize(
    "field", ["revision", "execution_attempt", "schema_version", "terminal_at"]
)
async def test_invalid_numeric_storage_is_not_coerced(effect, field):
    receipt = await execute(effect)
    with effect.services.db.transaction() as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(f"UPDATE ac_tool_effects SET {field}=?", (1.25,))
        conn.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(StorageDataError):
        effects.get_effect(effect.services, owner_id=1, effect_id=receipt.effectID)


@pytest.mark.parametrize(
    "mutation", ["resultDigest", "receiptDigest", "receiptBinding", "missingOutputField"]
)
async def test_corrupt_terminal_is_not_exposed_as_an_original_result(effect, mutation):
    receipt = await execute(effect)
    data = receipt.model_dump(mode="json")
    if mutation == "resultDigest":
        data["output"]["resultDigest"] = content_digest({})
    elif mutation == "receiptBinding":
        data["runID"] = str(uuid4())
    elif mutation == "missingOutputField":
        del data["output"]["effects"]
    with effect.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_tool_effects SET terminal_json=?,terminal_digest=?",
            (
                canonical_json(data),
                content_digest({}) if mutation == "receiptDigest" else content_digest(data),
            ),
        )
    with pytest.raises(StorageDataError):
        effects.get_effect(effect.services, owner_id=1, effect_id=receipt.effectID)
    with pytest.raises(StorageDataError):
        get_run(effect.services, owner_id=1, run_id=effect.invocation.intent.runID)


@pytest.mark.parametrize("phase", ["prepared", "dispatching"])
@pytest.mark.parametrize("already_interrupted", [False, True])
async def test_startup_recovers_effects_even_when_run_scan_skips_the_run(
    effect, phase, already_interrupted
):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    if phase == "dispatching":
        record = effects.begin_effect(effect.services, effect.invocation, record.intent.effectID, 1)
    if already_interrupted:
        with effect.services.db.transaction() as conn:
            conn.execute(
                "UPDATE ac_runs SET state='interrupted',reconciliation_required=1,revision=revision+1"
            )
    report = recover_dispatches(effect.services)
    assert report.effects_scanned == report.effects_tightened == 1
    assert report.scanned == (0 if already_interrupted else 1)
    current = effects.get_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    assert current.phase == "outcomeUnknown" and current.receipt is None
    assert current.dispatchStartedAt == record.dispatchStartedAt
    effect.assert_lane(reconciliation=True)
    events = [tuple(row) for row in effect.events()]
    again = recover_dispatches(effect.services)
    assert again.scanned == again.effects_scanned == 0
    assert [tuple(row) for row in effect.events()] == events
    with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
        effects.complete_effect(
            effect.services, effect.invocation, record.intent.effectID, output()
        )


async def test_old_epoch_and_disabled_owner_can_be_tightened_without_execution(effect):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    path = effect.services.paths.agentcomputer_identity
    identity = read_identity(path)
    path.write_text(
        identity.model_copy(update={"restoreEpoch": identity.restoreEpoch + 1}).model_dump_json()
    )
    with effect.services.db.transaction() as conn:
        conn.execute("UPDATE users SET disabled=1 WHERE id=1")
    report = recover_dispatches(effect.services)
    assert report.effects_tightened == 1
    assert (
        effects.get_effect(effect.services, owner_id=1, effect_id=record.intent.effectID).phase
        == "outcomeUnknown"
    )
    effect.assert_lane(reconciliation=True)


async def test_missing_identity_does_not_create_a_new_box_or_effect(effect):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    path = effect.services.paths.agentcomputer_identity
    path.unlink()
    with pytest.raises(StorageDataError):
        recover_dispatches(effect.services)
    assert not path.exists()
    with pytest.raises(StorageDataError):
        effects.get_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    assert not path.exists()
    assert effects.get_effect(effect.services, owner_id=2, effect_id=record.intent.effectID) is None


async def test_failed_or_unknown_adapter_result_fences_run_without_releasing_lanes(effect):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    effects.begin_effect(
        effect.services, effect.invocation, record.intent.effectID, record.revision
    )
    receipt = effects.complete_effect(
        effect.services, effect.invocation, record.intent.effectID, output(reported="unknown")
    )
    assert receipt.outcome == "succeeded" and receipt.reconciliationRequired
    effect.assert_lane(reconciliation=True)
    with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
        effects.prepare_effect(
            effect.services,
            effect.invocation,
            effect.action.model_copy(update={"toolCallID": "other"}),
        )


async def test_invalid_output_digest_does_not_commit_a_fake_receipt(effect):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    effects.begin_effect(effect.services, effect.invocation, record.intent.effectID, 1)
    bad = output().model_copy(update={"resultDigest": content_digest({})})
    with pytest.raises(StorageDataError):
        effects.complete_effect(effect.services, effect.invocation, record.intent.effectID, bad)
    current = effects.get_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    assert current.phase == "dispatching" and current.receipt is None
    with pytest.raises(ValidationError):
        ToolEffectOutput.model_validate({**output().model_dump(mode="json"), "exitCode": True})


@pytest.mark.parametrize("state", ["succeeded", "failed", "stopped"])
async def test_unresolved_effect_cannot_be_committed_under_a_terminal_run(effect, state):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    effects.begin_effect(effect.services, effect.invocation, record.intent.effectID, 1)
    with effect.services.db.transaction() as conn:
        conn.execute("UPDATE ac_runs SET state=?", (state,))
    before = [tuple(row) for row in effect.events()]
    with pytest.raises(StorageDataError, match="terminal Run"):
        effects.interrupt_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    with pytest.raises(StorageDataError, match="terminal Run"):
        effects.complete_effect(
            effect.services, effect.invocation, record.intent.effectID, output(outcome="failed")
        )
    with effect.services.db.connect() as conn:
        row = conn.execute("SELECT phase,terminal_json FROM ac_tool_effects").fetchone()
        assert tuple(row) == ("dispatching", None)
        assert (
            conn.execute("SELECT state,reconciliation_required FROM ac_runs").fetchone()[0] == state
        )
    assert [tuple(row) for row in effect.events()] == before


@pytest.mark.parametrize("table", ["ac_tasks", "ac_projects"])
async def test_unknown_effect_with_a_missing_lane_is_rejected_without_repair(effect, table):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    effects.interrupt_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    with effect.services.db.transaction() as conn:
        conn.execute(f"UPDATE {table} SET active_run_id=NULL")
    before = [tuple(row) for row in effect.events()]
    with pytest.raises(StorageDataError, match="active lanes"):
        effects.get_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    with pytest.raises(StorageDataError, match="active lanes"):
        get_run(effect.services, owner_id=1, run_id=effect.invocation.intent.runID)
    with pytest.raises(StorageDataError, match="active lanes"):
        effects.interrupt_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    assert [tuple(row) for row in effect.events()] == before


@pytest.mark.parametrize("phase", ["prepared", "dispatching", "terminal", "outcomeUnknown"])
async def test_missing_phase_event_does_not_repair_the_journal(effect, phase):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    if phase in {"dispatching", "terminal"}:
        effects.begin_effect(effect.services, effect.invocation, record.intent.effectID, 1)
    if phase == "terminal":
        effects.complete_effect(
            effect.services, effect.invocation, record.intent.effectID, output()
        )
    elif phase == "outcomeUnknown":
        effects.interrupt_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    with effect.services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_events WHERE event_type=?", ("tool." + phase,))
    before = [tuple(row) for row in effect.events()]
    with pytest.raises(StorageDataError, match="event binding"):
        get_run(effect.services, owner_id=1, run_id=effect.invocation.intent.runID)
    with pytest.raises(StorageDataError, match="event binding"):
        effects.get_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    assert [tuple(row) for row in effect.events()] == before


@pytest.mark.parametrize(
    "mutation", ["version", "booleanRevision", "operation", "timestamp", "duplicate"]
)
async def test_invalid_phase_event_is_not_an_authority_to_replay(effect, mutation):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    with effect.services.db.transaction() as conn:
        row = conn.execute("SELECT * FROM ac_events WHERE event_type='tool.prepared'").fetchone()
        if mutation == "version":
            conn.execute("UPDATE ac_events SET payload_version=2 WHERE event_type='tool.prepared'")
        elif mutation == "timestamp":
            conn.execute(
                "UPDATE ac_events SET created_at=created_at+1 WHERE event_type='tool.prepared'"
            )
        elif mutation == "duplicate":
            conn.execute(
                "INSERT INTO ac_events (event_id,task_id,owner_id,sequence,run_id,turn_id,event_type,payload_version,schema_version,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    row["task_id"],
                    1,
                    row["sequence"] + 1,
                    row["run_id"],
                    row["turn_id"],
                    row["event_type"],
                    1,
                    1,
                    row["payload_json"],
                    row["created_at"],
                ),
            )
        else:
            payload = json.loads(row["payload_json"])
            payload["effectRevision" if mutation == "booleanRevision" else "operationDigest"] = (
                True if mutation == "booleanRevision" else content_digest({})
            )
            conn.execute(
                "UPDATE ac_events SET payload_json=? WHERE event_type='tool.prepared'",
                (canonical_json(payload),),
            )
    with pytest.raises(StorageDataError):
        effects.get_effect(effect.services, owner_id=1, effect_id=record.intent.effectID)
    with pytest.raises(StorageDataError):
        effects.prepare_effect(effect.services, effect.invocation, effect.action)


async def test_validly_rehashed_result_must_still_match_original_terminal_event(effect):
    receipt = await execute(effect)
    data = receipt.model_dump(mode="json")
    data["output"]["resultJSON"] = canonical_json({"different": "result"})
    data["output"]["resultDigest"] = content_digest({"different": "result"})
    with effect.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_tool_effects SET terminal_json=?,terminal_digest=?",
            (canonical_json(data), content_digest(data)),
        )
    with pytest.raises(StorageDataError, match="event binding"):
        effects.get_effect(effect.services, owner_id=1, effect_id=receipt.effectID)


async def test_missing_effect_table_repair_does_not_erase_orphan_event_evidence(effect):
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool

    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    with effect.services.db.transaction() as conn:
        conn.execute("DROP TABLE ac_tool_effects")
    other = SqlitePool(effect.services.paths.db)
    try:
        run_migrations(other)
        with effect.services.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM ac_tool_effects").fetchone()[0] == 0
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM ac_events WHERE event_type='tool.prepared'"
                ).fetchone()[0]
                == 1
            )
        with pytest.raises(StorageDataError, match="original journal"):
            get_run(effect.services, owner_id=1, run_id=record.intent.runID)
        with pytest.raises(StorageDataError, match="original journal"):
            effects.prepare_effect(effect.services, effect.invocation, effect.action)
    finally:
        other.close()


async def test_dispatch_event_without_prepared_or_journal_is_also_rejected(effect):
    record = effects.prepare_effect(effect.services, effect.invocation, effect.action).record
    effects.begin_effect(effect.services, effect.invocation, record.intent.effectID, 1)
    with effect.services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_tool_effects")
        conn.execute("DELETE FROM ac_events WHERE event_type='tool.prepared'")
    with pytest.raises(StorageDataError, match="original journal"):
        effects.prepare_effect(effect.services, effect.invocation, effect.action)
