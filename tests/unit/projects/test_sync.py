"""Compact cursors retain list boundaries without storing server-side tokens."""

import base64
import re
from typing import Literal

import pytest

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.cursors import CursorCodec


@pytest.mark.parametrize(
    ("position", "length"),
    [
        (0, 12),
        (255, 12),
        (256, 14),
        (65536, 15),
        (2**24, 16),
        (2**32 - 1, 16),
        (2**32, 18),
        (2**63 - 1, 22),
    ],
)
def test_sync_cursor_fits_within_16_characters_until_32_bit_position(position, length):
    codec = CursorCodec(context="v2:box:1:1:tasks:project", key=b"test-key")
    token = codec.encode("sync", position=position)
    assert len(token) == length
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert codec.decode(token, "sync").position == position
    assert codec.encode("sync", position=position) == token


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        ("history", {"updated_at": 1_760_000_000_000, "after": "task_历史"}),
        ("delta", {"position": 125, "cutoff": 130, "after": "task_125"}),
    ],
)
def test_page_cursors_retain_their_boundaries(kind, values):
    codec = CursorCodec(context="v2:box:1:1:tasks:project", key=b"test-key")
    decoded = codec.decode(codec.encode(kind, **values), kind)
    for name, expected in values.items():
        assert getattr(decoded, name) == expected


@pytest.mark.parametrize(
    "context",
    [
        "v2:other-box:1:1:tasks:project",
        "v2:box:2:1:tasks:project",
        "v2:box:1:2:tasks:project",
        "v2:box:1:1:tasks:other-project",
    ],
)
def test_cursor_cannot_cross_instance_epoch_user_or_project(context):
    codec = CursorCodec(context="v2:box:1:1:tasks:project", key=b"test-key")
    token = codec.encode("sync", position=0)
    with pytest.raises(OctopError) as raised:
        CursorCodec(context=context, key=codec.key).decode(token, "sync")
    assert raised.value.code == ErrorCode.PROJECT_SYNC_RESET


@pytest.mark.parametrize("kind", ["history", "delta"])
def test_sync_cursor_cannot_be_used_for_pagination(kind: Literal["history", "delta"]):
    codec = CursorCodec(context="v2:box:1:1:tasks:project", key=b"test-key")
    with pytest.raises(OctopError):
        codec.decode(codec.encode("sync", position=0), kind)


def test_payload_and_signature_tampering_are_rejected():
    codec = CursorCodec(context="v2:box:1:1:tasks:project", key=b"test-key")
    token = codec.encode("sync", position=0)
    original = base64.urlsafe_b64decode(token)
    for index in (0, len(original) - 1):
        changed = bytearray(original)
        changed[index] ^= 1
        with pytest.raises(OctopError):
            codec.decode(base64.urlsafe_b64encode(changed).decode(), "sync")


@pytest.mark.parametrize("token", ["", "x", "bad.游标", "A" * 4097])
def test_malformed_cursors_require_a_reset(token):
    codec = CursorCodec(context="v2:box:1:1:tasks:project", key=b"test-key")
    with pytest.raises(OctopError) as raised:
        codec.decode(token, "sync")
    assert raised.value.code == ErrorCode.PROJECT_SYNC_RESET
