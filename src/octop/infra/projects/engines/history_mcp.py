"""Read one exported task journal over stdio, without database credentials."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


def create_server(path: Path) -> FastMCP:
    # The parent exports this owner's single task. No caller-supplied path or
    # task ID is accepted by either tool, and the server has no write operation.
    rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    server = FastMCP("octop_task_history")

    @server.tool()
    def get_task_history(after_sequence: int = 0, limit: int = 10) -> list[dict[str, Any]]:
        """Read task turns in order, including public tool results; paginate by sequence."""
        return [row for row in rows if row["sequence"] > after_sequence][: max(1, min(limit, 20))]

    @server.tool()
    def search_task_history(query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Find original task turns and tool results containing all search terms."""
        words = query.casefold().split()
        if not words:
            return []
        return [
            row
            for row in rows
            if all(word in json.dumps(row, ensure_ascii=False).casefold() for word in words)
        ][: max(1, min(limit, 20))]

    return server


if __name__ == "__main__":
    create_server(Path(sys.argv[1])).run(transport="stdio")
