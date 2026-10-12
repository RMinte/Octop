"""Startup tightening preserves accepted facts without replay or process claims."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_dispatch import seed
from tests.unit.agentcomputer.test_submission_runs import control as control
from tests.unit.agentcomputer.test_submission_runs import request

from octop.config import OctopConfig
from octop.infra.agentcomputer import dispatch, recovery
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    admit_new_message,
)
from octop.infra.agentcomputer.identity import IdentityError, read_identity
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def empty_services(tmp_path):
    paths = PathLayout(tmp_path)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    try:
        yield build_shared_services(db=pool, paths=paths, config=OctopConfig())
    finally:
        pool.close()


def _facts(services):
    with services.db.connect() as conn:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "ac_projects",
                "ac_project_preparations",
                "ac_tasks",
                "ac_turns",
                "ac_runs",
                "ac_commands",
                "ac_events",
                "ac_run_dispatches",
                "threads",
                "sessions",
            )
        }


def _phase(control, monkeypatch, phase):
    services, receipt, claim = seed(control, monkeypatch)
    invocation = None
    if phase != "claimed":
        invocation = dispatch._begin_dispatch(services, claim)
    if phase == "engineAccepted":
        dispatch.accept_engine_entry(services, invocation)
    return services, receipt, claim, invocation


def test_zero_candidates_does_not_read_or_create_identity(empty_services, monkeypatch):
    before = _facts(empty_services)
    reader = Mock(side_effect=AssertionError("zero candidates must not read identity"))
    monkeypatch.setattr(recovery, "read_identity", reader)
    assert recovery.recover_dispatches(empty_services) == recovery.RecoveryReport()
    assert _facts(empty_services) == before
    reader.assert_not_called()
    assert not empty_services.paths.agentcomputer_identity.parent.exists()


def test_read_identity_never_initializes_missing_parent(tmp_path):
    path = PathLayout(tmp_path).agentcomputer_identity
    with pytest.raises(IdentityError, match="missing"):
        read_identity(path)
    assert not path.parent.exists()


def test_queued_not_dispatched_has_no_recovery_write(control, monkeypatch):
    services, _, task_id = control
    admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    before = _facts(services)
    monkeypatch.setattr(recovery, "read_identity", Mock(side_effect=AssertionError("no candidate")))
    assert recovery.recover_dispatches(services) == recovery.RecoveryReport()
    assert _facts(services) == before


@pytest.mark.parametrize("phase", ["claimed", "dispatching", "engineAccepted"])
def test_startup_tightens_each_phase_and_preserves_original_facts(control, monkeypatch, phase):
    services, receipt, _, _ = _phase(control, monkeypatch, phase)
    before = _facts(services)
    identity_bytes = services.paths.agentcomputer_identity.read_bytes()
    monkeypatch.setattr(
        dispatch, "_worker_identity", Mock(side_effect=AssertionError("no execution gate"))
    )
    monkeypatch.setattr(
        dispatch, "_begin_dispatch", Mock(side_effect=AssertionError("no dispatch"))
    )
    monkeypatch.setattr(
        dispatch, "accept_engine_entry", Mock(side_effect=AssertionError("no engine entry"))
    )
    report = recovery.recover_dispatches(services)
    assert report == recovery.RecoveryReport(scanned=1, tightened=1)
    after = _facts(services)
    for table in before.keys() - {"ac_runs", "ac_tasks", "ac_events"}:
        assert after[table] == before[table]
    run = after["ac_runs"][0]
    assert run["state"] == "interrupted" and run["reconciliation_required"] == 1
    assert run["dispatch_phase"] == phase and run["execution_epoch"] == 1
    assert run["revision"] == before["ac_runs"][0]["revision"] + 1
    assert run["restore_epoch"] == before["ac_runs"][0]["restore_epoch"]
    task = after["ac_tasks"][0]
    assert task["active_run_id"] == str(receipt.runID)
    assert task["revision"] == before["ac_tasks"][0]["revision"] + 1
    assert task["next_sequence"] == before["ac_tasks"][0]["next_sequence"] + 1
    assert after["ac_projects"][0]["active_run_id"] == str(receipt.runID)
    assert after["ac_events"][:-1] == before["ac_events"]
    event = after["ac_events"][-1]
    assert event["event_type"] == "run.interrupted"
    payload = json.loads(event["payload_json"])
    assert payload["reasonCode"] == "restartExecutionOwnerUnknown"
    assert payload["reconciliationRequired"] is True and payload["executionEpoch"] == 1
    assert services.paths.agentcomputer_identity.read_bytes() == identity_bytes
    assert recovery.recover_dispatches(services) == recovery.RecoveryReport()
    assert _facts(services) == after


def test_old_interrupt_keeps_claimed_rejection_and_startup_has_explicit_entry(control, monkeypatch):
    services, receipt, claim, _ = _phase(control, monkeypatch, "claimed")
    before = _facts(services)
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        dispatch.interrupt_uncertain_run(services, claim=claim)
    assert _facts(services) == before
    assert dispatch.interrupt_startup_run(
        services, owner_id=1, run_id=receipt.runID, box_id=claim.intent.boxID
    )
    assert not dispatch.interrupt_startup_run(
        services, owner_id=1, run_id=receipt.runID, box_id=claim.intent.boxID
    )


@pytest.mark.parametrize("state", ["succeeded", "failed", "stopped"])
def test_terminal_run_is_not_rewritten_even_after_candidate_was_listed(control, monkeypatch, state):
    services, receipt, claim, _ = _phase(control, monkeypatch, "engineAccepted")
    with services.db.transaction() as conn:
        conn.execute("UPDATE ac_runs SET state=? WHERE run_id=?", (state, str(receipt.runID)))
        conn.execute("UPDATE ac_tasks SET active_run_id=NULL")
        conn.execute("UPDATE ac_projects SET active_run_id=NULL")
    before = _facts(services)
    assert not dispatch.interrupt_startup_run(
        services, owner_id=1, run_id=receipt.runID, box_id=claim.intent.boxID
    )
    assert recovery.recover_dispatches(services) == recovery.RecoveryReport()
    assert _facts(services) == before


def test_disabled_owner_and_older_restore_epoch_are_still_tightened(control, monkeypatch):
    services, _, _, _ = _phase(control, monkeypatch, "engineAccepted")
    identity = read_identity(services.paths.agentcomputer_identity)
    services.paths.agentcomputer_identity.write_text(
        identity.model_copy(update={"restoreEpoch": 7}).model_dump_json(), encoding="utf-8"
    )
    identity_bytes = services.paths.agentcomputer_identity.read_bytes()
    with services.db.transaction() as conn:
        conn.execute("UPDATE users SET disabled=1 WHERE id=1")
    before = _facts(services)
    assert recovery.recover_dispatches(services) == recovery.RecoveryReport(scanned=1, tightened=1)
    after = _facts(services)
    assert after["ac_runs"][0]["restore_epoch"] == 1
    assert after["ac_runs"][0]["reconciliation_required"] == 1
    for table in ("ac_commands", "ac_turns", "ac_run_dispatches", "ac_projects"):
        assert after[table] == before[table]
    assert services.paths.agentcomputer_identity.read_bytes() == identity_bytes


@pytest.mark.parametrize("method", ["startup", "handoff", "probe"])
def test_missing_identity_never_initializes_during_recovery(control, monkeypatch, method):
    services, receipt, claim, _ = _phase(control, monkeypatch, "dispatching")
    services.paths.agentcomputer_identity.unlink()
    before = _facts(services)
    with pytest.raises(StorageDataError):
        if method == "startup":
            recovery.recover_dispatches(services)
        elif method == "handoff":
            dispatch.interrupt_uncertain_run(services, claim=claim)
        else:
            dispatch.get_claim(services, owner_id=1, run_id=receipt.runID)
    assert _facts(services) == before
    assert not services.paths.agentcomputer_identity.exists()
    assert list(services.paths.agentcomputer_identity.parent.glob("*.tmp")) == []


def test_corrupt_identity_is_preserved_and_stops_startup(control, monkeypatch):
    services, _, _, _ = _phase(control, monkeypatch, "claimed")
    services.paths.agentcomputer_identity.write_text("{", encoding="utf-8")
    before = _facts(services)
    with pytest.raises(StorageDataError):
        recovery.recover_dispatches(services)
    assert _facts(services) == before
    assert services.paths.agentcomputer_identity.read_bytes() == b"{"


@pytest.mark.parametrize(
    "damage",
    [
        "journal",
        "intent_digest",
        "snapshot",
        "receipt",
        "task_lane",
        "project_lane",
        "session",
        "fractional_revision",
    ],
)
def test_invalid_candidate_aborts_without_repair_or_partial_write(control, monkeypatch, damage):
    services, _, _, _ = _phase(control, monkeypatch, "engineAccepted")
    with services.db.transaction() as conn:
        if damage == "journal":
            conn.execute("DELETE FROM ac_run_dispatches")
        elif damage == "intent_digest":
            conn.execute("UPDATE ac_run_dispatches SET intent_digest='sha256:bad'")
        elif damage == "snapshot":
            conn.execute("UPDATE ac_turns SET snapshot_json='{}'")
        elif damage == "receipt":
            conn.execute("UPDATE ac_run_dispatches SET engine_receipt_json='{}'")
        elif damage == "task_lane":
            conn.execute("UPDATE ac_tasks SET active_run_id=NULL")
        elif damage == "project_lane":
            conn.execute("UPDATE ac_projects SET active_run_id=NULL")
        elif damage == "session":
            conn.execute("UPDATE sessions SET channel_metadata=NULL")
        else:
            conn.execute("UPDATE ac_runs SET revision=4.5")
    before = _facts(services)
    with pytest.raises(StorageDataError):
        recovery.recover_dispatches(services)
    assert _facts(services) == before


def test_event_storage_failure_rolls_back_current_run_sequence_and_revision(control, monkeypatch):
    services, _, _, _ = _phase(control, monkeypatch, "claimed")
    before = _facts(services)
    original = services.ac_event_repo.insert

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("injected recovery event failure")

    monkeypatch.setattr(services.ac_event_repo, "insert", fail)
    with pytest.raises(StorageUnavailableError):
        recovery.recover_dispatches(services)
    assert _facts(services) == before


def test_late_receipt_before_row_lock_is_preserved_while_run_is_tightened(control, monkeypatch):
    services, receipt, _, invocation = _phase(control, monkeypatch, "dispatching")
    original = recovery.interrupt_startup_run
    entries = []

    def after_entry(target, **kwargs):
        entries.append(dispatch.accept_engine_entry(target, invocation).receipt)
        return original(target, **kwargs)

    monkeypatch.setattr(recovery, "interrupt_startup_run", after_entry)
    assert recovery.recover_dispatches(services) == recovery.RecoveryReport(scanned=1, tightened=1)
    claim = dispatch.get_claim(services, owner_id=1, run_id=receipt.runID)
    assert claim.engineReceipt == entries[0] and claim.runRevision == 5
    with services.db.connect() as conn:
        row = conn.execute("SELECT * FROM ac_runs").fetchone()
        assert row["state"] == "interrupted" and row["dispatch_phase"] == "engineAccepted"


def test_two_recovery_connections_tighten_once_without_duplicate_event(control, monkeypatch):
    services, _, _, _ = _phase(control, monkeypatch, "claimed")
    pool = SqlitePool(services.paths.db)
    other = build_shared_services(db=pool, paths=services.paths, config=OctopConfig())
    barrier = Barrier(2)
    original = recovery.interrupt_startup_run

    def simultaneous(target, **kwargs):
        barrier.wait(timeout=10)
        return original(target, **kwargs)

    monkeypatch.setattr(recovery, "interrupt_startup_run", simultaneous)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            reports = list(executor.map(recovery.recover_dispatches, (services, other)))
        assert sum(report.scanned for report in reports) == 2
        assert sum(report.tightened for report in reports) == 1
        assert sum(report.unchanged for report in reports) == 1
        with services.db.connect() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM ac_events WHERE event_type='run.interrupted'"
                ).fetchone()[0]
                == 1
            )
    finally:
        pool.close()


def test_reopened_database_observes_same_uncertainty_without_replay(control, monkeypatch):
    services, _, _, _ = _phase(control, monkeypatch, "dispatching")
    pool = SqlitePool(services.paths.db)
    other = build_shared_services(db=pool, paths=services.paths, config=OctopConfig())
    try:
        assert recovery.recover_dispatches(other) == recovery.RecoveryReport(scanned=1, tightened=1)
        before = _facts(other)
        assert recovery.recover_dispatches(services) == recovery.RecoveryReport()
        assert _facts(services) == before
    finally:
        pool.close()


def test_wrong_box_identity_has_no_authority_to_tighten_candidate(control, monkeypatch):
    services, receipt, claim, _ = _phase(control, monkeypatch, "claimed")
    before = _facts(services)
    with pytest.raises(StorageDataError, match="installation mismatch"):
        dispatch.interrupt_startup_run(services, owner_id=1, run_id=receipt.runID, box_id=uuid4())
    assert _facts(services) == before
    assert read_identity(services.paths.agentcomputer_identity).boxID == claim.intent.boxID
