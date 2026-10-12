"""Installation identity survives restart without accepting corrupted metadata."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from octop.infra.agentcomputer import identity as identity_module
from octop.infra.agentcomputer.identity import (
    ControlIdentity,
    IdentityError,
    get_or_create_identity,
)
from octop.infra.utils.paths import PathLayout

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")


def _existing_identity(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)


def test_restart_reuses_identity_and_epoch(tmp_path: Path) -> None:
    paths = PathLayout(tmp_path)
    first = get_or_create_identity(paths.agentcomputer_identity)
    original = paths.agentcomputer_identity.read_bytes()

    restored_layout = PathLayout(tmp_path)
    second = get_or_create_identity(restored_layout.agentcomputer_identity)

    assert first == second
    assert first.boxID.version == 4
    assert first.schemaVersion == 1
    assert first.restoreEpoch == 1
    assert paths.agentcomputer_identity.read_bytes() == original
    assert paths.agentcomputer_identity.parent != paths.agents_dir
    assert not paths.db.exists()


def test_different_installations_have_different_identity(tmp_path: Path) -> None:
    first = get_or_create_identity(PathLayout(tmp_path / "first").agentcomputer_identity)
    second = get_or_create_identity(PathLayout(tmp_path / "second").agentcomputer_identity)
    assert first.boxID != second.boxID


def test_concurrent_initializers_publish_one_complete_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    barrier = Barrier(8)
    original_reader = identity_module._read_identity

    def synchronized_reader(identity_path: Path) -> ControlIdentity:
        try:
            return original_reader(identity_path)
        except FileNotFoundError:
            barrier.wait(timeout=10)
            raise

    monkeypatch.setattr(identity_module, "_read_identity", synchronized_reader)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: get_or_create_identity(path), range(8)))

    assert len({value.boxID for value in results}) == 1
    assert all(value == results[0] for value in results)
    assert ControlIdentity.model_validate_json(path.read_bytes()) == results[0]
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        json.dumps({"boxID": "11111111-1111-4111-8111-111111111111"}),
        json.dumps({"schemaVersion": 1, "boxID": "11111111-1111-4111-8111-111111111111"}),
        json.dumps({"boxID": "11111111-1111-4111-8111-111111111111", "restoreEpoch": 1}),
        json.dumps(
            {"schemaVersion": 2, "boxID": "11111111-1111-4111-8111-111111111111", "restoreEpoch": 1}
        ),
        json.dumps(
            {
                "schemaVersion": True,
                "boxID": "11111111-1111-4111-8111-111111111111",
                "restoreEpoch": 1,
            }
        ),
        json.dumps({"schemaVersion": 1, "boxID": "not-a-uuid", "restoreEpoch": 1}),
        json.dumps(
            {"schemaVersion": 1, "boxID": "11111111-1111-4111-8111-111111111111", "restoreEpoch": 0}
        ),
        json.dumps(
            {
                "schemaVersion": 1,
                "boxID": "11111111-1111-4111-8111-111111111111",
                "restoreEpoch": True,
            }
        ),
        json.dumps(
            {
                "schemaVersion": 1,
                "boxID": "11111111-1111-4111-8111-111111111111",
                "restoreEpoch": "1",
            }
        ),
        json.dumps(
            {
                "schemaVersion": 1,
                "boxID": "11111111-1111-4111-8111-111111111111",
                "restoreEpoch": 1,
                "extra": 1,
            }
        ),
    ],
)
def test_invalid_existing_identity_is_preserved(tmp_path: Path, content: str) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    _existing_identity(path, content)
    original = path.read_bytes()

    with pytest.raises(IdentityError):
        get_or_create_identity(path)

    assert path.read_bytes() == original
    assert list(path.parent.glob("*.tmp")) == []


def test_existing_restore_epoch_is_not_reset_or_incremented(tmp_path: Path) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    box_id = uuid4()
    _existing_identity(
        path, json.dumps({"schemaVersion": 1, "boxID": str(box_id), "restoreEpoch": 7})
    )
    assert get_or_create_identity(path) == ControlIdentity(
        schemaVersion=1, boxID=box_id, restoreEpoch=7
    )


def test_failed_publication_leaves_no_partial_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity

    def fail_publication(source: Path, destination: Path) -> None:
        assert ControlIdentity.model_validate_json(source.read_bytes()).restoreEpoch == 1
        assert destination == path
        raise OSError("filesystem publication failed")

    monkeypatch.setattr(identity_module.os, "link", fail_publication)
    with pytest.raises(OSError, match="filesystem publication failed"):
        get_or_create_identity(path)
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_directory_cannot_be_used_as_identity(tmp_path: Path) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    path.mkdir(parents=True)

    with pytest.raises(IdentityError):
        get_or_create_identity(path)

    assert path.is_dir()


@posix_only
def test_fifo_identity_is_rejected_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    path.parent.mkdir()
    os.mkfifo(path)

    def forbid_open(*args: object, **kwargs: object) -> int:
        raise AssertionError("must not open an existing FIFO")

    monkeypatch.setattr(identity_module.os, "open", forbid_open)
    with pytest.raises(IdentityError):
        get_or_create_identity(path)

    assert stat.S_ISFIFO(path.lstat().st_mode)


@posix_only
def test_fifo_replacement_between_lstat_and_open_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    get_or_create_identity(path)
    original_open = os.open

    def replace_before_open(file_path: Path, flags: int) -> int:
        assert file_path == path
        assert flags & os.O_NONBLOCK
        path.unlink()
        os.mkfifo(path)
        return original_open(file_path, flags)

    monkeypatch.setattr(identity_module.os, "open", replace_before_open)
    with pytest.raises(IdentityError):
        get_or_create_identity(path)

    assert stat.S_ISFIFO(path.lstat().st_mode)


@posix_only
def test_identity_file_and_new_directory_are_private(tmp_path: Path) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    get_or_create_identity(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_readable_by_others_is_rejected_without_rewrite(tmp_path: Path) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    get_or_create_identity(path)
    original = path.read_bytes()
    path.chmod(0o644)

    with pytest.raises(IdentityError):
        get_or_create_identity(path)

    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@posix_only
def test_symlink_identity_is_not_followed(tmp_path: Path) -> None:
    path = PathLayout(tmp_path).agentcomputer_identity
    path.parent.mkdir()
    target = tmp_path / "target.json"
    _existing_identity(
        target, ControlIdentity(schemaVersion=1, boxID=uuid4(), restoreEpoch=1).model_dump_json()
    )
    path.symlink_to(target)
    original = target.read_bytes()

    with pytest.raises(IdentityError):
        get_or_create_identity(path)

    assert path.is_symlink()
    assert target.read_bytes() == original


def test_schema_and_epoch_reject_implicit_coercion() -> None:
    with pytest.raises(ValidationError):
        ControlIdentity(schemaVersion=True, boxID=UUID(int=1), restoreEpoch=1)  # type: ignore[arg-type]
