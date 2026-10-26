"""Authenticated exports and expiring downloads for dashboard WebViews."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from octop.api.common.content_disposition import content_disposition
from octop.api.common.upload_limit import read_upload_capped
from octop.api.deps import current_user, get_server
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.download_links import DownloadLinkStore
from octop.infra.utils.user_downloads import (
    is_local_dashboard_request,
    reveal_download,
    sanitize_download_filename,
    save_user_download,
)

router = APIRouter()


class LocalDownloadResponse(BaseModel):
    path: str = Field(description="Absolute path written under the host Downloads folder.")
    filename: str = Field(description="Final filename, unique if the name already existed.")


@router.post(
    "/downloads/local",
    summary="Save a file to the host Downloads folder",
    response_model=LocalDownloadResponse,
)
async def save_local_download(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> LocalDownloadResponse:
    """Write an already-fetched blob to ~/Downloads. Desktop WebViews cannot use <a download>."""
    _ = user
    if not is_local_dashboard_request(request):
        raise OctopError(
            ErrorCode.LOCAL_DOWNLOAD_UNAVAILABLE,
            "local download is only available on this computer",
        )
    data = await read_upload_capped(file, max_bytes=server.services.config.max_upload_bytes)
    filename = sanitize_download_filename(file.filename)
    path = await asyncio.to_thread(save_user_download, filename, data)
    await asyncio.to_thread(reveal_download, path)
    return LocalDownloadResponse(path=str(path), filename=path.name)


_download_links = DownloadLinkStore()


class DownloadLinkResponse(BaseModel):
    url: str = Field(description="Relative URL that serves the file without credentials.")
    filename: str = Field(description="Filename the browser will save the file as.")
    expires_in: int = Field(description="Seconds until the link stops working.")


@router.post(
    "/downloads/link",
    summary="Create a short-lived download link",
    response_model=DownloadLinkResponse,
)
async def create_download_link(
    file: UploadFile = File(...),  # noqa: B008
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> DownloadLinkResponse:
    """Keep an already-fetched blob behind a credential-free link for a few minutes.

    In-app browsers such as WeChat cannot save ``blob:`` downloads, and the
    system browser they hand off to has no Octop session.
    """
    data = await read_upload_capped(file, max_bytes=server.services.config.max_upload_bytes)
    filename = sanitize_download_filename(file.filename)
    link = await asyncio.to_thread(_download_links.create, filename, data, owner_id=user.id)
    return DownloadLinkResponse(
        url=f"/api/downloads/link/{link.token}",
        filename=filename,
        expires_in=_download_links.ttl_seconds,
    )


@router.get(
    "/downloads/link/{token}",
    summary="Download a file through a short-lived link",
    response_class=FileResponse,
)
async def open_download_link(
    token: str,
    server: Any = Depends(get_server),
) -> FileResponse:
    """Serve a file created by ``POST /downloads/link``. The token is the only credential."""
    link = await asyncio.to_thread(_download_links.get, token)
    owner = await asyncio.to_thread(server.services.user_repo.get, link.owner_id) if link else None
    if link is None or owner is None or owner.disabled:
        raise OctopError(ErrorCode.NOT_FOUND, "download link expired or not found")
    return FileResponse(
        link.path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(link.filename),
            "Cache-Control": "no-store",
        },
    )
