"""Keep host admission failures distinct from human approval decisions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import Decision, InterruptOnConfig
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall

HOST_ERROR = "octop_host_error"


class TaskHumanInTheLoopMiddleware(HumanInTheLoopMiddleware[Any, Any, Any]):
    def __init__(self, interrupts: Callable[[], dict[str, Any]]) -> None:
        self._interrupts = interrupts
        super().__init__(interrupt_on=interrupts())

    @property
    def name(self) -> str:
        # DeepAgents replaces this native slot by name, preserving checkpoint nodes.
        return HumanInTheLoopMiddleware.__name__

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Use the final agent config, including tools installed during construction.
        self.interrupt_on = HumanInTheLoopMiddleware(interrupt_on=self._interrupts()).interrupt_on
        return super().after_model(state, runtime)

    @staticmethod
    def _process_decision(
        decision: Decision, tool_call: ToolCall, config: InterruptOnConfig
    ) -> tuple[ToolCall | None, ToolMessage | None]:
        host = cast(dict[str, Any], decision)
        if host.get("type") != HOST_ERROR:
            return HumanInTheLoopMiddleware._process_decision(decision, tool_call, config)
        if (
            not {"reject", "respond"}.intersection(config["allowed_decisions"])
            or host.get("tool_call_id") != tool_call["id"]
            or host.get("tool_name") != tool_call["name"]
            or not isinstance(host.get("message"), str)
            or not host["message"].strip()
        ):
            raise ValueError("invalid host admission decision")
        return tool_call, ToolMessage(
            content=host["message"],
            name=tool_call["name"],
            tool_call_id=tool_call["id"],
            status="error",
        )
