"""Stable installation identity outside the rollbackable control-plane database."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class IdentityError(ValueError):
    """Existing control identity cannot be trusted; never silently replace it."""


class ControlIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schemaVersion: Literal[1]
    boxID: UUID
    restoreEpoch: Annotated[int, Field(strict=True, ge=1)]

    @field_validator("schemaVersion", mode="before")
    @classmethod
    def _supported_schema(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported identity schema")
        return value


def _read_identity(path: Path) -> ControlIdentity:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise IdentityError("control identity must be a regular private file")
    # A non-regular replacement after lstat must not block the worker on a FIFO.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as identity_file:
        metadata = os.fstat(identity_file.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise IdentityError("control identity must be a regular private file")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise IdentityError("control identity must be a regular private file")
        content = identity_file.read()
    try:
        return ControlIdentity.model_validate_json(content)
    except ValidationError as exc:
        raise IdentityError("invalid control identity") from exc


def read_identity(path: Path) -> ControlIdentity:
    """Read an existing identity; recovery never initializes a new installation."""
    try:
        return _read_identity(path)
    except FileNotFoundError as exc:
        raise IdentityError("control identity is missing") from exc


def _sync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def get_or_create_identity(path: Path) -> ControlIdentity:
    """Atomically initialize once, or validate the existing identity.

    Publishing a completed private temporary file with a hard link cannot
    overwrite another initializer. Only FileNotFoundError permits creation.
    Restore fencing is not implemented: the epoch is initialized, not bumped.
    """
    try:
        return _read_identity(path)
    except FileNotFoundError:
        pass

    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = ControlIdentity(schemaVersion=1, boxID=uuid4(), restoreEpoch=1)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix="identity-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(identity.model_dump_json() + "\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        # Another initializer may have published a complete file; validate it below.
        with suppress(FileExistsError):
            os.link(temporary_path, path)
        _sync_directory(directory)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return _read_identity(path)
