"""ACP transport and event projection for a task's native execution session."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from acp.task import (
    DefaultMessageDispatcher,
    InMemoryMessageQueue,
    MessageDispatcher,
    MessageQueue,
    MessageStateStore,
    NotificationRunner,
    RequestRunner,
    RpcTask,
    RpcTaskKind,
    TaskSupervisor,
)

from octop.infra.agents.acp_setup import _stop_process

PermissionHandler = Callable[[str, Any, list[Any]], Awaitable[str | None]]
_OPEN_TIMEOUT = 30.0
_CONTROL_TIMEOUT = 3.0


class _NotificationQueue(InMemoryMessageQueue):
    def __init__(self, barrier: _NotificationBarrier) -> None:
        super().__init__()
        self._barrier = barrier

    async def publish(self, task: RpcTask) -> None:
        if task.kind is RpcTaskKind.NOTIFICATION:
            self._barrier.received(task.message)
        await super().publish(task)


class _NotificationBarrier:
    """Acknowledge received notifications before crossing an RPC response boundary.

    The SDK reader resolves responses before its background notification handlers
    necessarily run. Its public queue/dispatcher hooks let us track receipt and
    completion without depending on scheduling delays or private SDK tasks.
    """

    def __init__(self) -> None:
        self.queue = _NotificationQueue(self)
        self._received = 0
        self._processed = 0
        self._sequences: dict[int, int] = {}
        self._serial = asyncio.Lock()
        self._completed = asyncio.Condition()

    def received(self, message: dict[str, Any]) -> None:
        self._received += 1
        self._sequences[id(message)] = self._received

    def dispatcher(
        self,
        queue: MessageQueue,
        supervisor: TaskSupervisor,
        state: MessageStateStore,
        request_runner: RequestRunner,
        notification_runner: NotificationRunner,
    ) -> MessageDispatcher:
        async def notification(message: dict[str, Any]) -> None:
            sequence = self._sequences.pop(id(message))
            async with self._serial:
                try:
                    await notification_runner(message)
                finally:
                    async with self._completed:
                        self._processed = sequence
                        self._completed.notify_all()

        return DefaultMessageDispatcher(
            queue=queue,
            supervisor=supervisor,
            store=state,
            request_runner=request_runner,
            notification_runner=notification,
        )

    async def drain(self) -> None:
        target = self._received
        async with self._completed:
            await self._completed.wait_for(lambda: self._processed >= target)


class ACPFeatureError(ValueError):
    """The requested feature was not advertised by this native session."""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(f"ACP feature unavailable: {feature}")


class ACPTaskError(RuntimeError):
    """A sanitized transport failure; raw vendor errors never escape this module."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(f"ACP operation failed: {phase}")


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    return (
        dict(dump(mode="json", by_alias=True, exclude_none=True, warnings=False))
        if callable(dump)
        else {}
    )


def _options(options: list[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in options:
        value = _payload(item)
        if isinstance(value.get("options"), list):
            output.extend(_options(value["options"]))
        elif isinstance(value.get("value"), str):
            output.append(value)
    return output


@dataclass
class SessionInfo:
    session_id: str
    resumed: bool
    capabilities: dict[str, Any] = field(default_factory=dict)
    modes: dict[str, Any] | None = None
    config_options: list[dict[str, Any]] = field(default_factory=list)

    @property
    def supports_load(self) -> bool:
        return self.capabilities.get("loadSession") is True

    @property
    def prompt_images(self) -> bool:
        return bool((self.capabilities.get("promptCapabilities") or {}).get("image"))

    @property
    def model_ids(self) -> list[str]:
        return [
            option["value"]
            for config in self.config_options
            if config.get("category") == "model"
            for option in _options(config.get("options") or [])
        ]


class _TaskClient:
    def __init__(self, session: ACPTaskSession) -> None:
        self.session = session

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        await self.session._update(session_id, _payload(update))

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **_: Any
    ) -> Any:
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        selected: str | None = None
        session = self.session
        if (
            session._accept_updates
            and session.on_permission is not None
            and session.info
            and session.info.session_id == session_id
        ):
            try:
                await session._notifications.drain()
                proposal = _payload(tool_call)
                previous = session._tools.get(str(proposal.get("toolCallId") or ""), {})
                # Some native permission RPCs omit details already advertised in
                # tool updates. Empty placeholders must not erase those facts.
                proposal = {
                    **{key: value for key, value in previous.items() if key != "sessionUpdate"},
                    **{
                        key: value
                        for key, value in proposal.items()
                        if value not in (None, "", {}, []) or key not in previous
                    },
                }
                selected = await session.on_permission(session_id, proposal, options)
            except Exception:
                session._callback_failure = ACPTaskError("permission")
        allowed = {_payload(option).get("optionId") for option in options}
        if selected is not None and selected in allowed:
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=selected)
            )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def read_text_file(self, **_: Any) -> None:
        from acp import RequestError

        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, **_: Any) -> None:
        from acp import RequestError

        raise RequestError.method_not_found("fs/write_text_file")


class ACPTaskSession:
    def __init__(
        self,
        launch: list[str],
        env: dict[str, str],
        cwd: str,
        mcp_servers: list[Any] | None = None,
        on_permission: PermissionHandler | None = None,
    ) -> None:
        self.launch, self.env, self.cwd = list(launch), dict(env), cwd
        self.mcp_servers = list(mcp_servers or [])
        self.on_permission = on_permission
        self.info: SessionInfo | None = None
        self._connection: Any = None
        self._process: asyncio.subprocess.Process | None = None
        self._prompt_task: asyncio.Task[Any] | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._accept_updates = False
        self._closed = False
        self._callback_failure: ACPTaskError | None = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._ended_tools: set[str] = set()
        self._started_tools: dict[str, datetime] = {}
        self._block = 0
        self._reserved = False
        self._cancel_requested = False
        self._notifications = _NotificationBarrier()

    async def open(self, session_id: str | None = None) -> SessionInfo:
        from acp import PROTOCOL_VERSION, Client, connect_to_agent
        from acp.schema import ClientCapabilities, Implementation

        if self._closed:
            raise ACPTaskError("closed")
        if self._connection is not None and self.info is not None:
            return self.info
        try:
            async with asyncio.timeout(_OPEN_TIMEOUT):
                self._process = await asyncio.create_subprocess_exec(
                    *self.launch,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                    limit=50 * 1024 * 1024,
                )
                self._connection = connect_to_agent(
                    cast(Client, _TaskClient(self)),
                    self._process.stdin,
                    self._process.stdout,
                    queue=self._notifications.queue,
                    dispatcher_factory=self._notifications.dispatcher,
                )
                initialized = await self._connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="octop-task", version="1"),
                )
                if initialized.protocol_version != PROTOCOL_VERSION:
                    raise ACPTaskError("protocol")
                capabilities = _payload(initialized.agent_capabilities)
                result, resumed = None, False
                if session_id and capabilities.get("loadSession") is True:
                    try:
                        result = await asyncio.wait_for(
                            self._connection.load_session(
                                session_id=session_id, cwd=self.cwd, mcp_servers=self.mcp_servers
                            ),
                            _OPEN_TIMEOUT / 2,
                        )
                        resumed = True
                    except Exception:
                        result = None
                if not resumed:
                    result = await self._connection.new_session(
                        cwd=self.cwd, mcp_servers=self.mcp_servers
                    )
                    session_id = result.session_id
                if not session_id:
                    raise ACPTaskError("session")
                response = _payload(result)
                self.info = SessionInfo(
                    session_id,
                    resumed,
                    capabilities,
                    response.get("modes"),
                    list(response.get("configOptions") or []),
                )
                # Keep replay suppressed until every notification received before
                # the load/new response has finished its asynchronous handler.
                await self._notifications.drain()
                return self.info
        except BaseException as exc:
            await self._abort_transport()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ACPTaskError("open") from None

    async def _configure(self, model_id: str | None, mode_id: str | None) -> None:
        assert self.info is not None
        if mode_id is not None and mode_id not in [
            mode.get("id") for mode in (self.info.modes or {}).get("availableModes", [])
        ]:
            raise ACPFeatureError("mode")
        if model_id is not None:
            config = next(
                (
                    item
                    for item in self.info.config_options
                    if item.get("category") == "model"
                    and model_id
                    in [option["value"] for option in _options(item.get("options") or [])]
                ),
                None,
            )
            if config is None:
                raise ACPFeatureError("model")
            if config.get("currentValue") != model_id:
                response = await asyncio.wait_for(
                    self._connection.set_config_option(
                        config_id=config["id"], session_id=self.info.session_id, value=model_id
                    ),
                    _CONTROL_TIMEOUT,
                )
                options = _payload(response).get("configOptions")
                if isinstance(options, list):
                    self.info.config_options = options
        if mode_id is not None:
            modes = self.info.modes or {}
            if mode_id not in [mode.get("id") for mode in modes.get("availableModes", [])]:
                raise ACPFeatureError("mode")
            if modes.get("currentModeId") != mode_id:
                await asyncio.wait_for(
                    self._connection.set_session_mode(
                        session_id=self.info.session_id, mode_id=mode_id
                    ),
                    _CONTROL_TIMEOUT,
                )
                modes["currentModeId"] = mode_id

    async def stream(
        self,
        prompt: list[dict[str, Any]],
        *,
        model_id: str | None = None,
        mode_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        from acp.schema import PromptRequest
        from pydantic import ValidationError

        if self.info is None or self._connection is None or self._closed:
            raise ACPTaskError("not_open")
        if self._reserved:
            raise ACPTaskError("busy")
        self._reserved, self._cancel_requested = True, False
        try:
            capabilities = self.info.capabilities.get("promptCapabilities") or {}
            for item in prompt:
                if item.get("type") in {"image", "audio"} and not capabilities.get(
                    str(item["type"])
                ):
                    raise ACPFeatureError(str(item["type"]))
            request = PromptRequest.model_validate(
                {"sessionId": self.info.session_id, "prompt": prompt}
            )
            await self._configure(model_id, mode_id)
        except ValidationError:
            self._reserved = False
            raise ACPFeatureError("prompt") from None
        except ACPFeatureError:
            self._reserved = False
            raise
        except asyncio.CancelledError:
            self._reserved = False
            raise
        except Exception:
            self._reserved = False
            raise ACPTaskError("configure") from None
        if self._cancel_requested or self._closed:
            self._reserved = False
            raise asyncio.CancelledError
        self._queue = asyncio.Queue(maxsize=1024)
        self._tools, self._ended_tools = {}, set()
        self._started_tools = {}
        self._callback_failure = None
        self._accept_updates = True
        self._block += 1

        async def dispatch_prompt() -> Any:
            try:
                response = await self._connection.prompt(
                    session_id=request.session_id, prompt=request.prompt
                )
            except Exception:
                await self._notifications.drain()
                raise
            # Leave the streaming consumer running while queued tail handlers
            # complete, including when they need queue backpressure to clear.
            await self._notifications.drain()
            return response

        self._prompt_task = asyncio.create_task(dispatch_prompt())
        get: asyncio.Task[dict[str, Any]] | None = None
        try:
            while True:
                if not self._queue.empty():
                    yield self._queue.get_nowait()
                    continue
                if self._prompt_task.done():
                    break
                get = asyncio.create_task(self._queue.get())
                done, _ = await asyncio.wait(
                    {get, self._prompt_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if get in done:
                    yield get.result()
                else:
                    get.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get
                get = None
            try:
                response = await self._prompt_task
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ACPTaskError("prompt") from None
            if self._callback_failure:
                raise self._callback_failure
            response_data = _payload(response)
            if response_data.get("stopReason") == "cancelled" or self._cancel_requested:
                raise asyncio.CancelledError
            if "usage" in response_data:
                yield {"type": "usage", "usage": response_data["usage"], "scope": "native_session"}
            yield {"type": "message_end"}
        finally:
            self._accept_updates = False
            if get is not None:
                get.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await get
            if self._prompt_task is not None and not self._prompt_task.done():
                await self.cancel()
            self._prompt_task = None
            self._reserved = False

    async def _update(self, session_id: str, update: dict[str, Any]) -> None:
        if self.info is None or session_id != self.info.session_id:
            return
        kind = update.get("sessionUpdate")
        if kind == "config_option_update":
            self.info.config_options = list(update.get("configOptions") or [])
        elif kind == "current_mode_update" and self.info.modes is not None:
            self.info.modes["currentModeId"] = update.get("currentModeId")
        if not self._accept_updates:
            return
        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            content = update.get("content") or {}
            if content.get("type") == "text" and content.get("text"):
                await self._queue.put(
                    {
                        "type": "token" if kind == "agent_message_chunk" else "reasoning",
                        "content": str(content["text"]),
                        "block_key": f"acp:{self._block}:{kind}",
                    }
                )
        elif kind in {"tool_call", "tool_call_update"}:
            await self._tool_update(update)
        elif kind == "usage_update":
            await self._queue.put(
                {
                    "type": "usage_context",
                    "used": update.get("used"),
                    "size": update.get("size"),
                    **({"cost": update["cost"]} if "cost" in update else {}),
                }
            )
        elif kind == "plan":
            for entry in update.get("entries") or []:
                if entry.get("status") == "in_progress" and entry.get("content"):
                    await self._queue.put(
                        {"type": "progress", "call_id": "", "content": str(entry["content"])}
                    )

    async def _tool_update(self, update: dict[str, Any]) -> None:
        identifier = str(update.get("toolCallId") or "")
        if not identifier or identifier in self._ended_tools:
            return
        first = identifier not in self._tools
        state = self._tools.setdefault(identifier, {})
        previous_arguments = state.get("rawInput")
        state.update({key: value for key, value in update.items() if value is not None})
        running = state.get("status") == "in_progress" and identifier not in self._started_tools
        if running:
            self._started_tools[identifier] = datetime.now(UTC)
        started = self._started_tools.get(identifier)
        if first or running or state.get("rawInput") != previous_arguments:
            await self._queue.put(
                {
                    "type": "tool_start",
                    "name": str(state.get("kind") or "external_agent"),
                    "label": str(state.get("title") or ""),
                    "call_id": identifier,
                    "arguments": state.get("rawInput")
                    if isinstance(state.get("rawInput"), dict)
                    else {},
                    **({"started_at": started.isoformat()} if started else {}),
                }
            )
        if state.get("status") in {"completed", "failed"}:
            finished = datetime.now(UTC)
            output = state.get("rawOutput")
            if output is None:
                text = "\n".join(
                    str((item.get("content") or {}).get("text") or "")
                    for item in state.get("content") or []
                    if item.get("type") == "content"
                )
                output = text if text else state.get("content")
            await self._queue.put(
                {
                    "type": "tool_end",
                    "call_id": identifier,
                    "failed": state.get("status") == "failed",
                    "output": output,
                    "finished_at": finished.isoformat(),
                    **(
                        {"duration_ms": max(0.0, (finished - started).total_seconds() * 1000)}
                        if started
                        else {}
                    ),
                }
            )
            self._ended_tools.add(identifier)
        elif not first and update.get("title"):
            await self._queue.put(
                {"type": "progress", "call_id": identifier, "content": str(update["title"])}
            )

    async def cancel(self) -> None:
        self._cancel_requested = True
        task = self._prompt_task
        if task is None or task.done():
            return
        try:
            if self.info is not None and self._connection is not None:
                await asyncio.wait_for(
                    self._connection.cancel(session_id=self.info.session_id), _CONTROL_TIMEOUT
                )
            await asyncio.wait_for(asyncio.shield(task), _CONTROL_TIMEOUT)
        except (Exception, asyncio.CancelledError):
            await self._abort_transport()

    async def _abort_transport(self) -> None:
        self._accept_updates = False
        process, self._process = self._process, None
        connection, self._connection = self._connection, None
        if process is not None:
            await _stop_process(process)
        if connection is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.close(), _CONTROL_TIMEOUT)
        if self._prompt_task is not None and not self._prompt_task.done():
            self._prompt_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._prompt_task

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.cancel()
            if (
                self.info is not None
                and self._connection is not None
                and (self.info.capabilities.get("sessionCapabilities") or {}).get("close")
                is not None
            ):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self._connection.close_session(session_id=self.info.session_id),
                        _CONTROL_TIMEOUT,
                    )
        finally:
            await self._abort_transport()

    async def __aenter__(self) -> ACPTaskSession:
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
