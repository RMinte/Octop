"""Observe logical model invocations without retaining requests or response text."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.callbacks.base import BaseCallbackManager
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.base import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import RunnableConfig
from octop_harness.config import ModelConfig, ProviderConfig

from octop.infra.db.repos.model_calls import (
    TOKEN_FIELDS,
    ModelCallRepo,
    ModelCallRow,
    ModelCallSource,
    ModelCallStatus,
)

logger = logging.getLogger(__name__)
_VERSION = 1
UsageSource = Literal[
    "usage_metadata", "response_metadata", "llm_output", "mixed", "missing", "derived"
]


class ModelCallServices(Protocol):
    @property
    def model_call_repo(self) -> ModelCallRepo: ...


@dataclass(frozen=True)
class ModelCallScope:
    services: ModelCallServices
    user_id: int | None
    purpose: str
    agent_id: str = ""
    project_id: str = ""
    task_id: str = ""
    turn_id: str = ""
    submission_id: str = ""
    thread_id: str = ""


_SCOPE: ContextVar[ModelCallScope | None] = ContextVar("model_call_scope", default=None)


@dataclass
class _Invocation:
    calls: list[tuple[ModelCallObserver, str]] = dataclass_field(default_factory=list)
    writes: list[asyncio.Task[None]] = dataclass_field(default_factory=list)

    async def finish_pending(self, status: ModelCallStatus) -> None:
        for observer, key in self.calls:
            await observer._finish(key, status, None)
        # A shielded LangChain error callback may already have begun its write.
        # Wait for our invocation's writes without touching unrelated child calls.
        for operation in self.writes:
            await asyncio.shield(operation)


_INVOCATION: ContextVar[_Invocation | None] = ContextVar("model_invocation", default=None)


@asynccontextmanager
async def _invocation_boundary() -> AsyncIterator[None]:
    invocation = _Invocation()
    token = _INVOCATION.set(invocation)
    try:
        yield
    except (asyncio.CancelledError, GeneratorExit):
        await invocation.finish_pending("cancelled")
        raise
    except BaseException:
        await invocation.finish_pending("failed")
        raise
    finally:
        _INVOCATION.reset(token)


class _ObservedModelLifecycle(BaseChatModel):
    """Keep a terminal receipt when LangChain skips callbacks on outer cancellation."""

    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        async with _invocation_boundary():
            return await super().ainvoke(input, config=config, stop=stop, **kwargs)

    async def astream(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        invocation = _Invocation()
        stream = cast(
            AsyncGenerator[AIMessageChunk, None],
            super().astream(input, config=config, stop=stop, **kwargs),
        )
        status: ModelCallStatus | None = None
        try:
            while True:
                token = _INVOCATION.set(invocation)
                try:
                    chunk = await anext(stream)
                except StopAsyncIteration:
                    return
                finally:
                    _INVOCATION.reset(token)
                # Never keep a ContextVar token across yield: consumers may
                # close this iterator from a different task/context.
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        except BaseException:
            status = "failed"
            raise
        finally:
            token = _INVOCATION.set(invocation)
            try:
                await stream.aclose()
            finally:
                _INVOCATION.reset(token)
                if status is not None:
                    await invocation.finish_pending(status)


_OBSERVED_CLASSES: dict[type[BaseChatModel], type[BaseChatModel]] = {}


def _with_lifecycle(model: BaseChatModel) -> BaseChatModel:
    if isinstance(model, _ObservedModelLifecycle):
        return model
    original = type(model)
    observed = _OBSERVED_CLASSES.get(original)
    if observed is None:
        observed = type(
            f"Observed{original.__name__}",
            (_ObservedModelLifecycle, original),
            {"__module__": __name__},
        )
        _OBSERVED_CLASSES[original] = observed
    # Preserve the exact instance configuration, clients, validators and private
    # harness stamps. No global vendor methods or other instances are patched.
    result = model.model_copy()
    object.__setattr__(result, "__class__", observed)
    return result


def current_model_call_scope() -> ModelCallScope | None:
    return _SCOPE.get()


@contextmanager
def model_call_scope(
    services: ModelCallServices,
    *,
    user_id: int | None,
    purpose: str,
    agent_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    turn_id: str | None = None,
    submission_id: str | None = None,
    thread_id: str | None = None,
) -> Iterator[ModelCallScope]:
    """Bind an explicit instance and owner; child tasks inherit the same scope."""
    scope = ModelCallScope(
        services=services,
        user_id=user_id,
        purpose=purpose,
        agent_id=agent_id or "",
        project_id=project_id or "",
        task_id=task_id or "",
        turn_id=turn_id or "",
        submission_id=submission_id or "",
        thread_id=thread_id or "",
    )
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


@contextmanager
def override_model_call_scope(
    *, purpose: str | None = None, agent_id: str | None = None
) -> Iterator[ModelCallScope | None]:
    """Refine an existing call scope without inventing an owner or global sink."""
    current = current_model_call_scope()
    if current is None:
        yield None
        return
    scope = replace(
        current,
        purpose=purpose if purpose is not None else current.purpose,
        agent_id=agent_id if agent_id is not None else current.agent_id,
    )
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _first_count(data: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        value = _count(data.get(name))
        if value is not None:
            return value
    return None


def _wire_usage(data: object) -> Mapping[str, object]:
    source = _mapping(data)
    return _mapping(source.get("token_usage")) or _mapping(source.get("usage"))


def _has_wire_usage(data: object) -> bool:
    source = _mapping(data)
    return isinstance(source.get("token_usage"), Mapping) or isinstance(
        source.get("usage"), Mapping
    )


def _trace_id(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", value)
        else None
    )


def _field_count(data: Mapping[str, object], field: str) -> int | None:
    direct = _first_count(data, field)
    if direct is not None:
        return direct
    input_details = (
        _mapping(data.get("input_token_details"))
        or _mapping(data.get("prompt_tokens_details"))
        or _mapping(data.get("input_tokens_details"))
    )
    output_details = (
        _mapping(data.get("output_token_details"))
        or _mapping(data.get("completion_tokens_details"))
        or _mapping(data.get("output_tokens_details"))
    )
    if field == "input_tokens":
        return _first_count(data, "prompt_tokens")
    if field == "output_tokens":
        return _first_count(data, "completion_tokens")
    if field == "cache_read_tokens":
        return (
            _first_count(input_details, "cache_read", "cached_tokens")
            if any(
                _first_count(input_details, key) is not None
                for key in ("cache_read", "cached_tokens")
            )
            else _first_count(data, "cache_read_input_tokens", "prompt_cache_hit_tokens")
        )
    if field == "cache_write_tokens":
        detail = _first_count(input_details, "cache_creation", "cache_write")
        return detail if detail is not None else _first_count(data, "cache_creation_input_tokens")
    if field == "reasoning_tokens":
        return _first_count(output_details, "reasoning", "reasoning_tokens")
    return None


def normalize_model_usage(
    *,
    usage_metadata: object = None,
    response_metadata: object = None,
    llm_output: object = None,
    protocol: str = "openai",
    openai_adapter: bool = False,
) -> tuple[dict[str, object], UsageSource]:
    """Retain reported zero and unknown fields; derive only exact known arithmetic.

    Cache buckets are subdivisions of input; reasoning is a subdivision of output.
    Presence describes an explicitly reported field, rather than an inferred zero.
    """
    wire_sources = (
        ("response_metadata", _wire_usage(response_metadata)),
        ("llm_output", _wire_usage(llm_output)),
    )
    # Some SDK adapters synthesize absent core counters as zero. If raw usage
    # exists, only its actual keys establish presence; canonical defaults cannot.
    sources = (
        wire_sources
        if _has_wire_usage(response_metadata) or _has_wire_usage(llm_output)
        else (("usage_metadata", _mapping(usage_metadata)),)
    )
    values: dict[str, int | None] = dict.fromkeys(TOKEN_FIELDS)
    presence = dict.fromkeys(TOKEN_FIELDS, False)
    origins: set[str] = set()
    for field in TOKEN_FIELDS:
        for source, raw in sources:
            value = _field_count(raw, field)
            if value is None:
                continue
            # Without raw stream evidence, ChatOpenAI's default core zeros
            # and synthesized total cannot establish reported presence.
            if (
                source == "usage_metadata"
                and openai_adapter
                and (
                    field == "total_tokens"
                    or (field in {"input_tokens", "output_tokens"} and value == 0)
                )
            ):
                continue
            # Anthropic wire input excludes cache buckets; LangChain input includes them.
            if protocol == "anthropic" and source != "usage_metadata" and field == "input_tokens":
                if values["uncached_input_tokens"] is None:
                    values["uncached_input_tokens"] = value
                    presence["uncached_input_tokens"] = True
                origins.add(source)
                break
            values[field], presence[field] = value, True
            origins.add(source)
            break
    input_tokens = values["input_tokens"]
    output_tokens = values["output_tokens"]
    cache_read, cache_write = values["cache_read_tokens"], values["cache_write_tokens"]
    uncached = values["uncached_input_tokens"]
    if input_tokens is None and all(
        value is not None for value in (uncached, cache_read, cache_write)
    ):
        input_tokens = cast(int, uncached) + cast(int, cache_read) + cast(int, cache_write)
        values["input_tokens"] = input_tokens
    if uncached is None and all(
        value is not None for value in (input_tokens, cache_read, cache_write)
    ):
        remainder = cast(int, input_tokens) - cast(int, cache_read) - cast(int, cache_write)
        if remainder >= 0:
            values["uncached_input_tokens"] = remainder
    if values["total_tokens"] is None and input_tokens is not None and output_tokens is not None:
        values["total_tokens"] = input_tokens + output_tokens
    source_name: UsageSource = (
        "missing"
        if not origins
        else cast(UsageSource, next(iter(origins)))
        if len(origins) == 1
        else "mixed"
    )
    return {**values, "presence": presence}, source_name


def _result_usage(
    response: LLMResult | None, protocol: str, openai_adapter: bool
) -> tuple[dict[str, object], UsageSource]:
    message = (
        next(
            (
                generation.message
                for group in response.generations
                for generation in group
                if hasattr(generation, "message")
            ),
            None,
        )
        if response is not None
        else None
    )
    return normalize_model_usage(
        usage_metadata=getattr(message, "usage_metadata", None),
        response_metadata=getattr(message, "response_metadata", None),
        llm_output=response.llm_output if response is not None else None,
        protocol=protocol,
        openai_adapter=openai_adapter,
    )


async def _persist(method: Callable[[ModelCallRow], ModelCallRow], row: ModelCallRow) -> None:
    operation = asyncio.create_task(asyncio.to_thread(method, row))
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        operation.result()
    except Exception as exc:
        # Database exception text can include values; keep only the exception class.
        logger.warning("Model invocation receipt write failed (%s)", type(exc).__name__)
    if cancelled:
        raise asyncio.CancelledError


class ModelCallObserver(AsyncCallbackHandler):
    """One receipt per LangChain logical invocation, including opaque SDK retries."""

    run_inline = True

    def __init__(
        self,
        *,
        provider: str,
        model_ref: str,
        source: ModelCallSource,
        protocol: str,
        openai_adapter: bool = False,
    ) -> None:
        self.provider = provider
        self.model_ref = model_ref
        self.source = source
        self.protocol = protocol
        self.openai_adapter = openai_adapter
        self._runs: dict[str, tuple[ModelCallScope, ModelCallRow, _Invocation | None]] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        scope = current_model_call_scope()
        key = str(run_id)
        if scope is None or key in self._runs:
            return
        purpose = scope.purpose
        if purpose != "compaction" and metadata:
            if metadata.get("lc_source") == "summarization":
                purpose = "summarization"
            elif metadata.get("ls_agent_type") == "subagent":
                purpose = "subagent"
        usage, usage_source = normalize_model_usage()
        row = ModelCallRow(
            call_id=key,
            started_at=int(time.time() * 1_000),
            provider=self.provider,
            model_ref=self.model_ref,
            source=self.source,
            purpose=purpose,
            # LangChain parents are usually graph/chain runs, not model calls.
            # Preserve that trace edge in metadata without inventing a ledger FK.
            parent_call_id="",
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            project_id=scope.project_id,
            task_id=scope.task_id,
            turn_id=scope.turn_id,
            submission_id=scope.submission_id,
            thread_id=scope.thread_id,
            usage_json=json.dumps(usage),
            metadata_json=json.dumps(
                {
                    "version": _VERSION,
                    "run_id": key,
                    "parent_run_id": str(parent_run_id) if parent_run_id else "",
                    "usage_source": usage_source,
                    **(
                        {"request_id": scope.submission_id}
                        if _trace_id(scope.submission_id)
                        else {}
                    ),
                }
            ),
        )
        invocation = _INVOCATION.get()
        self._runs[key] = scope, row, invocation
        if invocation is not None:
            invocation.calls.append((self, key))
        try:
            await _persist(scope.services.model_call_repo.start, row)
        except asyncio.CancelledError:
            await self._finish(key, "cancelled", None)
            raise

    async def _finish(self, key: str, status: ModelCallStatus, response: LLMResult | None) -> None:
        pending = self._runs.pop(key, None)
        if pending is None:
            return
        scope, row, invocation = pending
        usage, usage_source = _result_usage(response, self.protocol, self.openai_adapter)
        metadata = json.loads(row.metadata_json)
        metadata["usage_source"] = usage_source
        if response is not None:
            for group in response.generations:
                for generation in group:
                    message = getattr(generation, "message", None)
                    raw = _mapping(getattr(message, "response_metadata", None))
                    request_id = _trace_id(raw.get("request_id") or raw.get("provider_request_id"))
                    if request_id:
                        metadata["provider_request_id"] = request_id
        terminal = replace(
            row,
            status=status,
            finished_at=max(row.started_at, int(time.time() * 1_000)),
            usage_json=json.dumps(usage),
            metadata_json=json.dumps(metadata),
        )
        operation = asyncio.create_task(_persist(scope.services.model_call_repo.finish, terminal))
        if invocation is not None:
            invocation.writes.append(operation)
        await asyncio.shield(operation)

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        await self._finish(str(run_id), "succeeded", response)

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        response = kwargs.get("response")
        await self._finish(
            str(run_id),
            "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "failed",
            response if isinstance(response, LLMResult) else None,
        )


def attach_model_observer(
    model: BaseChatModel, provider: ProviderConfig, modelconfig: ModelConfig
) -> BaseChatModel:
    """Attach at the factory boundary so seed, summary and subagent copies are observed."""
    from langchain_openai.chat_models.base import BaseChatOpenAI

    from octop.infra.agents.providers.box import BoxProviderConfig

    current = model.callbacks
    handlers = current.handlers if isinstance(current, BaseCallbackManager) else current or []
    if any(isinstance(handler, ModelCallObserver) for handler in handlers):
        return model
    source: ModelCallSource = "box" if isinstance(provider, BoxProviderConfig) else "third_party"
    observer = ModelCallObserver(
        provider=provider.id,
        model_ref=f"{provider.id}/{modelconfig.id}",
        source=source,
        protocol=provider.protocol,
        openai_adapter=isinstance(model, BaseChatOpenAI),
    )
    if isinstance(current, BaseCallbackManager):
        callbacks = current.copy()
        callbacks.add_handler(observer, inherit=True)
        return _with_lifecycle(model.model_copy(update={"callbacks": callbacks}))
    return _with_lifecycle(model.model_copy(update={"callbacks": [*(current or []), observer]}))
