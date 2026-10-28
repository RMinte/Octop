"""Read immutable uploads bound to this private conversation without host file access."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.knowledge.parse import parse_document
from octop.infra.projects import input_attachments
from octop.infra.projects.models import Reference

_PARSED_DOCUMENT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".yaml",
        ".yml",
        ".jsonl",
        ".json",
        ".html",
        ".htm",
        ".csv",
        ".tsv",
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".xlsm",
        ".xls",
    }
)


def _text(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in _PARSED_DOCUMENT_SUFFIXES:
        text = data.decode("utf-8-sig")
        if any(
            (ord(char) < 32 and char not in "\t\n\r\f") or 127 <= ord(char) <= 159 for char in text
        ):
            raise ValueError("attachment is not plain text")
        return text
    # The path is created by the host solely to reuse the existing document parser.
    # It never points into the agent workspace or to a model-selected path.
    with tempfile.TemporaryDirectory(prefix="octop-resident-attachment-") as directory:
        path = Path(directory) / ("input" + suffix)
        path.write_bytes(data)
        return parse_document(path, ocr=None)


class ReadAttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: Reference | None = Field(
        default=None,
        description="An upload bound to this private conversation; omit to list files.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Character offset when reading a file; attachment index when listing files.",
    )
    limit: int = Field(
        default=12000, ge=1, le=16000, description="Maximum characters of text or listed metadata."
    )


def read_resident_attachment(
    services: SharedServices, owner_id: int, thread_id: str, arguments: ReadAttachmentInput
) -> dict[str, Any]:
    from octop.infra.projects.resident_tasks import task_id

    identifier = task_id(owner_id, thread_id)
    with services.db.read_snapshot() as conn:
        task = services.project_task_repo.get_internal_in_transaction(conn, identifier, owner_id)
        if task is not None and task.internal_kind != "resident":
            raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
        rows = (
            services.project_task_attachment_repo.for_task(conn, owner_id, identifier)
            if task is not None
            else []
        )
    files = [row for row in rows if not input_attachments.is_image(row.media_type)]
    if arguments.attachment_id is None:
        items: list[dict[str, Any]] = []
        used = 2  # JSON array delimiters.
        for row in files[arguments.offset :]:
            item = {
                "attachment_id": row.attachment_id,
                "name": row.name,
                "media_type": row.media_type,
                "byte_count": row.byte_count,
            }
            size = len(json.dumps(item, ensure_ascii=False)) + (2 if items else 0)
            if used + size > arguments.limit:
                break
            items.append(item)
            used += size
        end = min(arguments.offset + len(items), len(files))
        if end < len(files) and not items:
            raise OctopError.localized(ErrorCode.RESIDENT_TOOL_INPUT_INVALID)
        return {
            "items": items,
            "offset": arguments.offset,
            "next_offset": end if end < len(files) else None,
            "total_items": len(files),
        }
    if arguments.attachment_id not in {row.attachment_id for row in files}:
        raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE)
    metadata, data = input_attachments.download(
        services, owner_id, arguments.attachment_id, task_id=identifier
    )
    try:
        text = _text(metadata.name, data)
        if not text.strip():
            raise ValueError("attachment has no extractable text")
    except Exception as exc:
        raise OctopError.localized(ErrorCode.RESIDENT_ATTACHMENT_UNREADABLE) from exc
    end = min(arguments.offset + arguments.limit, len(text))
    return {
        "attachment_id": metadata.attachment_id,
        "name": metadata.name,
        "media_type": metadata.media_type,
        "text": text[arguments.offset : end],
        "offset": arguments.offset,
        "next_offset": end if end < len(text) else None,
        "total_characters": len(text),
    }
