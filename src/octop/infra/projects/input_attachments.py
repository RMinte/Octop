"""Immutable task file inputs; storage never depends on a project directory."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
import time
import warnings
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError

from octop.i18n import tr
from octop.infra.db.repos.project_task_attachments import ProjectTaskAttachmentRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.media.attachment_hints import (
    VISION_MAX_BYTES,
    _reencode_image_to_fit,
    sniff_image_media_type,
)
from octop.infra.gateway.media.inbound_store import (
    INBOUND_EXTENSION_MEDIA_TYPES,
    sanitize_inbound_filename,
)
from octop.infra.projects.input_attachment_models import TaskInputAttachment
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    from octop_harness.backends.workspace import BackendWorkspace

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 9
PENDING_TTL_MS = 24 * 60 * 60 * 1000
IMAGE_SCHEME = "task-image://"
_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


def is_image(media_type: str) -> bool:
    return media_type in _MEDIA_TYPES.values()


def max_upload_bytes(services: SharedServices) -> int:
    return min(services.config.max_upload_bytes, MAX_ATTACHMENT_BYTES)


def view(row: ProjectTaskAttachmentRow) -> TaskInputAttachment:
    return TaskInputAttachment(
        attachment_id=row.attachment_id,
        name=row.name,
        media_type=row.media_type,
        byte_count=row.byte_count,
        sha256=row.sha256,
        preview_url=f"/api/project-tasks/attachments/{row.attachment_id}",
        workspace_path=(
            None
            if is_image(row.media_type)
            else f"inbound/{row.attachment_id}/{sanitize_inbound_filename(row.name)}"
        ),
        created_at=datetime.fromtimestamp(row.created_at / 1000, UTC),
    )


def model_image_bytes(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Use the ordinary chat image size gate without persisting encoded model bytes."""
    if len(data) <= VISION_MAX_BYTES:
        return data, media_type
    encoded = _reencode_image_to_fit(data)
    if encoded is None:
        raise OctopError.localized(ErrorCode.TASK_IMAGE_LIMIT)
    return encoded


def _validate_image(data: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                media_type = _MEDIA_TYPES.get(image.format or "")
                if media_type is None:
                    raise OctopError.localized(ErrorCode.TASK_IMAGE_INVALID)
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise OctopError.localized(ErrorCode.TASK_IMAGE_INVALID) from exc
    return media_type


def _media_type(name: str, data: bytes, content_type: str | None) -> str:
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    guessed = INBOUND_EXTENSION_MEDIA_TYPES.get(Path(name).suffix.lower())
    if guessed is None:
        guessed = mimetypes.guess_type(name)[0]
    if is_image(declared) or is_image(guessed or "") or sniff_image_media_type(data):
        media_type = _validate_image(data)
        model_image_bytes(data, media_type)
        return media_type
    if declared != "application/octet-stream" and re.fullmatch(
        r"[\w!#$&^_.+-]+/[\w!#$&^_.+-]+", declared, flags=re.ASCII
    ):
        return declared
    return guessed or "application/octet-stream"


def save(
    services: SharedServices,
    owner_id: int,
    name: str,
    data: bytes,
    content_type: str | None = None,
) -> TaskInputAttachment:
    if len(data) > max_upload_bytes(services):
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_LIMIT)
    name = sanitize_inbound_filename(name)
    media_type = _media_type(name, data, content_type)
    now = int(time.time() * 1000)
    row = ProjectTaskAttachmentRow(
        attachment_id="input_" + new_ulid(),
        owner_id=owner_id,
        task_id=None,
        name=name,
        media_type=media_type,
        byte_count=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at=now,
    )
    with services.db.transaction() as conn:
        if not services.project_task_repo.lock_owner(conn, owner_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        services.project_task_attachment_repo.delete_expired(conn, owner_id, now - PENDING_TTL_MS)
        services.project_task_attachment_repo.insert(conn, row, data)
    return view(row)


def _available(row: ProjectTaskAttachmentRow | None) -> ProjectTaskAttachmentRow:
    if row is None or (
        row.task_id is None and row.created_at <= int(time.time() * 1000) - PENDING_TTL_MS
    ):
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    return row


def download(
    services: SharedServices,
    owner_id: int,
    attachment_id: str,
    *,
    task_id: str | None = None,
) -> tuple[TaskInputAttachment, bytes]:
    with services.db.read_snapshot() as conn:
        stored = services.project_task_attachment_repo.download(conn, owner_id, attachment_id)
    row = _available(stored[0] if stored else None)
    if task_id is not None and row.task_id != task_id:
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    assert stored is not None
    data = stored[1]
    if len(data) != row.byte_count or hashlib.sha256(data).hexdigest() != row.sha256:
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    return view(row), data


def for_task(services: SharedServices, owner_id: int, task_id: str) -> list[TaskInputAttachment]:
    """List task-owned attachment metadata without reading file bytes."""
    with services.db.read_snapshot() as conn:
        if services.project_task_repo.get_in_transaction(conn, task_id, owner_id) is None:
            raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
        return [
            view(row)
            for row in services.project_task_attachment_repo.for_task(conn, owner_id, task_id)
        ]


def _validate_ids(ids: list[str]) -> None:
    if len(ids) > MAX_ATTACHMENTS:
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_LIMIT)
    if len(set(ids)) != len(ids):
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_INVALID)


def bind(
    services: SharedServices, conn: Any, owner_id: int, task_id: str, ids: list[str]
) -> list[TaskInputAttachment]:
    """Bind uploads inside the send transaction, after its owner lock is held."""
    _validate_ids(ids)
    if services.project_task_repo.get_in_transaction(conn, task_id, owner_id) is None:
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    rows = [
        _available(services.project_task_attachment_repo.get(conn, owner_id, attachment_id))
        for attachment_id in ids
    ]
    if any(row.task_id not in {None, task_id} for row in rows):
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    for row in rows:
        services.project_task_attachment_repo.bind(conn, owner_id, task_id, row.attachment_id)
    return [view(row) for row in rows]


async def materialize_files(
    services: SharedServices, owner_id: int, task_id: str, workspace: BackendWorkspace
) -> None:
    """Restore missing task files in the current workspace, preserving working edits."""
    attachments = await asyncio.to_thread(for_task, services, owner_id, task_id)
    for attachment in attachments:
        path = attachment.workspace_path
        if path is None or await workspace.aexists(path):
            continue
        _, data = await asyncio.to_thread(
            download, services, owner_id, attachment.attachment_id, task_id=task_id
        )
        await workspace.aupload_bytes(path, data)


def content(
    prompt: str,
    attachment_ids: list[str],
    attachments: list[TaskInputAttachment] | None = None,
    *,
    locale: str = "zh",
) -> str | list[dict[str, Any]]:
    if not attachment_ids:
        return prompt
    blocks: list[dict[str, Any]] = []
    if prompt:
        blocks.append({"type": "text", "text": prompt})
    metadata = {item.attachment_id: item for item in attachments or []}
    for attachment_id in attachment_ids:
        attachment = metadata.get(attachment_id)
        if attachments is not None and attachment is None:
            raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
        if attachment is None or is_image(attachment.media_type):
            blocks.append({"type": "image_url", "image_url": {"url": IMAGE_SCHEME + attachment_id}})
        else:
            blocks.append(
                {
                    "type": "text",
                    "text": tr(
                        "projects.task_file_hint",
                        locale,
                        name=attachment.name,
                        attachment_id=attachment_id,
                        path=attachment.workspace_path,
                    ),
                }
            )
    return blocks
