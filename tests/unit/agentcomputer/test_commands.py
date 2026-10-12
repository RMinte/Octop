"""Acceptance durability without an engine, real workspace or model service."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from octop.config import OctopConfig
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    admit_new_message,
    canonical_json,
    command_digest,
    content_digest,
    get_command_receipt,
)
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import CommandReceipt, NewMessageCommand
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import SharedServices, build_shared_services
from octop.infra.utils.paths import PathLayout

BOX = UUID("11111111-1111-4111-8111-111111111111")
TASK = UUID("22222222-2222-4222-8222-222222222222")
GOLDEN = (
    '{"boxID":"11111111-1111-4111-8111-111111111111","digestVersion":1,'
    '"operation":"turn.newMessage","payload":{"contextMode":"newMessage",'
    '"expectedProjectRevision":7,"expectedRestoreEpoch":3,"inputArtifactIDs":[], '
    '"permissionMode":"askForApproval","prompt":"你好\\nWorld","selectedSkillIDs":[], '
    '"userMessageID":"44444444-4444-4444-8444-444444444444"},"protocolVersion":1,'
    '"taskID":"22222222-2222-4222-8222-222222222222"}'
).replace(", ", ",")


def _request(**changes: Any) -> NewMessageCommand:
    fields = {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "contextMode": "newMessage",
        "userMessageID": str(uuid4()),
        "expectedProjectRevision": 1,
        "prompt": "你好\nWorld",
        "permissionMode": "askForApproval",
    }
    fields.update(changes)
    return NewMessageCommand.model_validate(fields)


def _mapping(services: SharedServices, *, owner: int = 1, project: str | None = None) -> UUID:
    """Only tests create ready mappings; no production preparation is implied."""
    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    project_id, task_id, agent_id, thread_id = (
        project or str(uuid4()),
        uuid4(),
        str(uuid4()),
        str(uuid4()),
    )
    with services.db.transaction() as conn:
        if project is None:
            conn.execute(
                "INSERT INTO agents(agent_id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                (agent_id, owner, agent_id, 1, 1),
            )
            config = {
                "schemaVersion": 1,
                "instructions": "使用中文回答",
                "modelRef": "fixture/no-model",
            }
            conn.execute(
                "INSERT INTO ac_projects(project_id,owner_id,box_id,workspace_id,default_agent_id,"
                "preparation_state,revision,config_json,config_digest,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'ready',1,?,?,1,1)",
                (
                    project_id,
                    owner,
                    str(identity.boxID),
                    str(uuid4()),
                    agent_id,
                    canonical_json(config),
                    content_digest(config),
                ),
            )
        else:
            row = conn.execute(
                "SELECT default_agent_id FROM ac_projects WHERE project_id=?", (project,)
            ).fetchone()
            assert row is not None
            agent_id = row[0]
        conn.execute(
            "INSERT INTO threads(thread_id,agent_id,user_id,channel_type,session_key,last_active,created_at) "
            "VALUES(?,?,?,'agentcomputer',?,1,1)",
            (thread_id, agent_id, owner, str(task_id)),
        )
        conn.execute(
            "INSERT INTO ac_tasks(task_id,project_id,owner_id,session_id,octop_thread_id,"
            "preparation_state,revision,next_sequence,title,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'ready',1,1,'Fixture',1,1)",
            (str(task_id), project_id, owner, str(uuid4()), thread_id),
        )
    return task_id


@pytest.fixture
def control(tmp_path: Path):
    paths = PathLayout(tmp_path)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(db=pool, paths=paths, config=OctopConfig())
    with pool.transaction() as conn:
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) VALUES(1,'one','fixture','admin',1)"
        )
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) VALUES(2,'two','fixture','user',1)"
        )
    task = _mapping(services)
    yield services, task
    pool.close()


def _counts(services: SharedServices) -> dict[str, int]:
    with services.db.connect() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) FROM ac_{name}").fetchone()[0]
            for name in ("commands", "turns", "runs", "events")
        }


def _finish_fixture(
    services: SharedServices, receipt: CommandReceipt, *, reconcile: bool = False
) -> None:
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_runs SET state='interrupted',reconciliation_required=? WHERE run_id=?",
            (int(reconcile), str(receipt.runID)),
        )
        if not reconcile:
            conn.execute(
                "UPDATE ac_tasks SET active_run_id=NULL WHERE task_id=?", (str(receipt.taskID),)
            )
            conn.execute(
                "UPDATE ac_projects SET active_run_id=NULL WHERE active_run_id=?",
                (str(receipt.runID),),
            )


def test_digest_golden_bytes_and_semantics() -> None:
    command = _request(
        expectedRestoreEpoch=3,
        expectedProjectRevision=7,
        userMessageID="44444444-4444-4444-8444-444444444444",
    )
    assert canonical_json(json.loads(GOLDEN)) == GOLDEN
    assert (
        command_digest(box_id=BOX, task_id=TASK, command=command)
        == "sha256:20add4b780534e89fd2af9c4bbe1185379b6abede8023eaea605930de7687ef0"
    )
    raw = command.model_dump(mode="json")
    reordered = NewMessageCommand.model_validate_json(json.dumps(dict(reversed(list(raw.items())))))
    assert command_digest(box_id=BOX, task_id=TASK, command=reordered) == command_digest(
        box_id=BOX, task_id=TASK, command=command
    )
    retry = NewMessageCommand.model_validate(
        {**raw, "commandID": str(uuid4()), "idempotencyKey": "another-key"}
    )
    assert command_digest(box_id=BOX, task_id=TASK, command=retry) == command_digest(
        box_id=BOX, task_id=TASK, command=command
    )
    assert content_digest({"text": "é"}) != content_digest({"text": "e\u0301"})
    assert content_digest(["a", "b"]) != content_digest(["b", "a"])
    assert content_digest({"text": "a\nb"}) != content_digest({"text": "a\r\nb"})
    assert content_digest({"text": " a "}) != content_digest({"text": "a"})
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


@pytest.mark.parametrize(
    "change",
    [
        {"expectedRestoreEpoch": True},
        {"expectedProjectRevision": "1"},
        {"prompt": 123},
        {"contextMode": "continueOriginalMessage"},
        {"ownerID": 2},
        {"inputArtifactIDs": None},
    ],
)
def test_invalid_command_types_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _request(**change)


def test_acceptance_commits_all_facts_and_immutable_snapshot(control) -> None:
    services, task = control
    command = _request()
    receipt = admit_new_message(services, owner_id=1, task_id=task, command=command)
    assert receipt.accepted and receipt.runState == "queued" and not receipt.replayed
    assert receipt.taskRevision == 2 and receipt.acceptedSequence == 1
    assert _counts(services) == {"commands": 1, "turns": 1, "runs": 1, "events": 1}
    with services.db.connect() as conn:
        run = conn.execute("SELECT * FROM ac_runs").fetchone()
        turn = conn.execute("SELECT * FROM ac_turns").fetchone()
        assert run["dispatch_phase"] == "notDispatched"
        assert run["execution_epoch"] == 0
        assert run["restore_epoch"] == 1
        snapshot = json.loads(turn["snapshot_json"])
        assert snapshot["request"]["prompt"] == command.prompt
        assert turn["snapshot_digest"] == content_digest(snapshot)
        event = conn.execute("SELECT * FROM ac_events").fetchone()
        assert event["event_type"] == "run.accepted"
        assert json.loads(event["payload_json"])["inputSnapshotDigest"] == turn["snapshot_digest"]
        conn.execute("UPDATE ac_projects SET config_json='{}',revision=2")
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert get_command_receipt(services, 2, receipt.commandID) is None
    assert admit_new_message(services, owner_id=1, task_id=task, command=command).replayed


def test_new_command_id_same_key_returns_original_without_alias(control) -> None:
    services, task = control
    command = _request()
    first = admit_new_message(services, owner_id=1, task_id=task, command=command)
    retry = _request(**{**command.model_dump(mode="json"), "commandID": str(uuid4())})
    repeated = admit_new_message(services, owner_id=1, task_id=task, command=retry)
    assert repeated == first.model_copy(update={"replayed": True})
    assert get_command_receipt(services, 1, retry.commandID) is None
    assert _counts(services) == {"commands": 1, "turns": 1, "runs": 1, "events": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "changed"},
        {"expectedProjectRevision": 2},
        {"permissionMode": "fullAccess"},
        {"idempotencyKey": "different-key"},
    ],
)
def test_retry_conflicts_do_not_change_facts(control, change: dict[str, Any]) -> None:
    services, task = control
    command = _request()
    first = admit_new_message(services, owner_id=1, task_id=task, command=command)
    retry = _request(**{**command.model_dump(mode="json"), **change})
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        admit_new_message(services, owner_id=1, task_id=task, command=retry)
    assert get_command_receipt(services, 1, first.commandID) == first
    assert _counts(services) == {"commands": 1, "turns": 1, "runs": 1, "events": 1}


def test_epoch_checked_before_replay_but_old_receipt_remains_readable(control) -> None:
    services, task = control
    command = _request()
    first = admit_new_message(services, owner_id=1, task_id=task, command=command)
    path = services.paths.agentcomputer_identity
    identity = json.loads(path.read_text())
    identity["restoreEpoch"] = 3
    path.write_text(json.dumps(identity))
    with pytest.raises(CommandAdmissionError, match="restoreEpochConflict"):
        admit_new_message(services, owner_id=1, task_id=task, command=command)
    assert get_command_receipt(services, 1, first.commandID) == first
    assert get_or_create_identity(path).restoreEpoch == 3
    assert first.restoreEpoch == 1


def test_receipt_survives_closed_database(control) -> None:
    services, task = control
    first = admit_new_message(services, owner_id=1, task_id=task, command=_request())
    services.db.close()
    reopened = SqlitePool(services.paths.db)
    try:
        restarted = build_shared_services(db=reopened, paths=services.paths, config=services.config)
        assert get_command_receipt(restarted, 1, first.commandID) == first
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "point", ["ac_turn_repo", "ac_run_repo", "ac_event_repo", "ac_command_repo"]
)
def test_failures_roll_back_receipt_run_lanes_and_sequence(
    control, monkeypatch, point: str
) -> None:
    services, task = control
    command = _request()
    repo = getattr(services, point)
    original = repo.insert

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "insert", fail_after_insert)
        with pytest.raises(RuntimeError, match="injected"):
            admit_new_message(services, owner_id=1, task_id=task, command=command)
    assert all(value == 0 for value in _counts(services).values())
    with services.db.connect() as conn:
        row = conn.execute("SELECT active_run_id,next_sequence,revision FROM ac_tasks").fetchone()
        assert tuple(row) == (None, 1, 1)
        assert conn.execute("SELECT active_run_id FROM ac_projects").fetchone()[0] is None
    retry = admit_new_message(services, owner_id=1, task_id=task, command=command)
    assert retry.acceptedSequence == 1 and not retry.replayed


def test_active_and_unreconciled_runs_block_new_turn(control) -> None:
    services, task = control
    first = admit_new_message(services, owner_id=1, task_id=task, command=_request())
    with pytest.raises(CommandAdmissionError, match="activeRunConflict"):
        admit_new_message(services, owner_id=1, task_id=task, command=_request())
    _finish_fixture(services, first, reconcile=True)
    with pytest.raises(CommandAdmissionError, match="reconciliationRequired"):
        admit_new_message(services, owner_id=1, task_id=task, command=_request())
    _finish_fixture(services, first)
    second = admit_new_message(services, owner_id=1, task_id=task, command=_request())
    assert second.acceptedSequence == 2 and second.turnID != first.turnID


def test_duplicate_user_message_cannot_be_recreated_with_new_key(control) -> None:
    services, task = control
    command = _request()
    first = admit_new_message(services, owner_id=1, task_id=task, command=command)
    _finish_fixture(services, first)
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        admit_new_message(
            services,
            owner_id=1,
            task_id=task,
            command=_request(userMessageID=command.userMessageID),
        )
    assert _counts(services)["turns"] == 1


def test_owner_isolation_and_same_key_in_two_owners(control) -> None:
    services, task = control
    first = admit_new_message(
        services, owner_id=1, task_id=task, command=_request(idempotencyKey="shared-key")
    )
    task_two = _mapping(services, owner=2)
    with pytest.raises(CommandAdmissionError, match="resourceNotFound"):
        admit_new_message(services, owner_id=2, task_id=task, command=_request())
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        admit_new_message(
            services, owner_id=2, task_id=task_two, command=_request(commandID=first.commandID)
        )
    second = admit_new_message(
        services, owner_id=2, task_id=task_two, command=_request(idempotencyKey="shared-key")
    )
    assert first.commandID != second.commandID
    assert get_command_receipt(services, 1, second.commandID) is None


@pytest.mark.parametrize(
    "change,code",
    [
        ({"expectedProjectRevision": 9}, "revisionConflict"),
        ({"permissionMode": "fullAccess"}, "capabilityUnsupported"),
        ({"selectedSkillIDs": ["unresolved"]}, "capabilityUnsupported"),
        ({"inputArtifactIDs": ["55555555-5555-4555-8555-555555555555"]}, "capabilityUnsupported"),
    ],
)
def test_unavailable_or_stale_input_not_accepted(control, change, code) -> None:
    services, task = control
    with pytest.raises(CommandAdmissionError, match=code):
        admit_new_message(services, owner_id=1, task_id=task, command=_request(**change))
    assert all(value == 0 for value in _counts(services).values())


def test_corrupt_configuration_or_receipt_is_not_repaired(control) -> None:
    services, task = control
    with services.db.connect() as conn:
        conn.execute("UPDATE ac_projects SET config_digest='sha256:corrupt'")
    with pytest.raises(StorageDataError, match="digest"):
        admit_new_message(services, owner_id=1, task_id=task, command=_request())
    assert all(value == 0 for value in _counts(services).values())


@pytest.mark.parametrize(
    "missing", ["protocolVersion", "receiptRevision", "accepted", "replayed", "runState"]
)
def test_incomplete_stored_receipts_do_not_acquire_model_defaults(control, missing: str) -> None:
    services, task = control
    first = admit_new_message(services, owner_id=1, task_id=task, command=_request())
    damaged = first.model_dump(mode="json")
    del damaged[missing]
    raw = canonical_json(damaged)
    with services.db.connect() as conn:
        conn.execute("UPDATE ac_commands SET receipt_json=?", (raw,))
    with pytest.raises(StorageDataError):
        get_command_receipt(services, 1, first.commandID)
    assert services.ac_command_repo.get(str(first.commandID), 1).receipt_json == raw


@pytest.mark.parametrize("mismatch", ["userMessageID", "restoreEpoch"])
def test_replay_rejects_structurally_valid_receipt_for_different_input(
    control, mismatch: str
) -> None:
    services, task = control
    command = _request()
    first = admit_new_message(services, owner_id=1, task_id=task, command=command)
    damaged = first.model_dump(mode="json")
    damaged[mismatch] = str(uuid4()) if mismatch == "userMessageID" else 9
    with services.db.connect() as conn:
        conn.execute(
            "UPDATE ac_commands SET receipt_json=?,restore_epoch=?",
            (canonical_json(damaged), damaged["restoreEpoch"]),
        )
    with pytest.raises(StorageDataError):
        admit_new_message(services, owner_id=1, task_id=task, command=command)
    assert _counts(services) == {"commands": 1, "turns": 1, "runs": 1, "events": 1}


@pytest.mark.parametrize("same_key", [True, False])
def test_two_connections_cannot_register_duplicate_or_overlapping_runs(
    control, same_key: bool
) -> None:
    services, task = control
    pool_two = SqlitePool(services.paths.db)
    second = build_shared_services(db=pool_two, paths=services.paths, config=services.config)
    barrier = Barrier(2)
    first_command = _request()
    other = first_command if same_key else _request()

    def submit(pair):
        service, command = pair
        barrier.wait(timeout=10)
        try:
            return admit_new_message(service, owner_id=1, task_id=task, command=command)
        except CommandAdmissionError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, [(services, first_command), (second, other)]))
        assert _counts(services) == {"commands": 1, "turns": 1, "runs": 1, "events": 1}
        receipts = [item for item in results if isinstance(item, CommandReceipt)]
        if same_key:
            assert len(receipts) == 2 and receipts[0].commandID == receipts[1].commandID
            assert sorted(item.replayed for item in receipts) == [False, True]
        else:
            assert len(receipts) == 1 and "activeRunConflict" in results
    finally:
        pool_two.close()


def test_project_writer_lane_blocks_a_different_task(control) -> None:
    services, first_task = control
    with services.db.connect() as conn:
        project = conn.execute(
            "SELECT project_id FROM ac_tasks WHERE task_id=?", (str(first_task),)
        ).fetchone()[0]
    second_task = _mapping(services, project=project)
    first = admit_new_message(services, owner_id=1, task_id=first_task, command=_request())
    with pytest.raises(CommandAdmissionError, match="activeRunConflict"):
        admit_new_message(services, owner_id=1, task_id=second_task, command=_request())
    _finish_fixture(services, first)
    assert admit_new_message(services, owner_id=1, task_id=second_task, command=_request()).accepted
