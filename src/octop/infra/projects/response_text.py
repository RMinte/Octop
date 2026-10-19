"""Classify ordinary model text only after its response's tool intent is known."""

from __future__ import annotations

from typing import Any


class TaskResponseText:
    def __init__(self, *, executing: bool = False) -> None:
        self._kind: str | None = "reasoning" if executing else None
        self._buffer: list[dict[str, Any]] = []

    def begin(self, *, executing: bool) -> list[dict[str, Any]]:
        # An unresolved previous response is process text, never a final answer.
        pending = self.complete(has_tools=True)
        self._kind = "reasoning" if executing else None
        return pending

    def append(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if event["type"] == "token":
            if self._kind is not None:
                return [{**event, "type": self._kind}]
            self._buffer.append(event)
            return []
        if self._buffer:
            self._buffer.append(event)
            return []
        return [event]

    def complete(self, *, has_tools: bool) -> list[dict[str, Any]]:
        if has_tools:
            self._kind = "reasoning"
        elif self._kind is None:
            self._kind = "token"
        pending, self._buffer = self._buffer, []
        return [
            {**event, "type": self._kind} if event["type"] == "token" else event
            for event in pending
        ]
