"""Channel-scoped temporary ingress media, independent of expert workspaces."""

from __future__ import annotations

import asyncio
from pathlib import Path

from octop_gateway.media import MediaBackend

from octop.infra.gateway.media.inbound_store import inbound_rel_path, validate_inbound_size


class SourceMediaBackend(MediaBackend):
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root, self.max_bytes = root, max_bytes

    def _path(self, key: str) -> Path:
        path = (self.root / inbound_rel_path(key)).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("invalid source media path")
        return path

    async def save(self, data: bytes, key: str) -> None:
        validate_inbound_size(data, max_bytes=self.max_bytes)
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(write)

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    def get_local_path(self, key: str) -> Path | None:
        return self._path(key)

    @property
    def inbound_dir(self) -> str:
        return "/inbound"
