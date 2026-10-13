"""Internal effect runner; no production tool hook or runtime is registered here.

Only the caller that prepares a new effect and wins its dispatch CAS can invoke
the trusted handler. Cancellation observes any pending database operation before
deciding whether an effect was rejected, completed or requires reconciliation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Literal

from octop.infra.agentcomputer import effects
from octop.infra.agentcomputer.commands import CommandAdmissionError
from octop.infra.agentcomputer.models import (
    EngineInvocation,
    ToolEffectAction,
    ToolEffectOutput,
    ToolEffectReceipt,
)
from octop.infra.db.services import SharedServices


async def _settled_database_call[T](operation: Callable[[], T]) -> tuple[T, bool]:
    future = asyncio.ensure_future(asyncio.get_running_loop().run_in_executor(None, operation))
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    return future.result(), cancelled


async def execute_effect(
    services: SharedServices,
    *,
    invocation: EngineInvocation,
    action: ToolEffectAction,
    authorize: Callable[[], Awaitable[bool]],
    handler: Callable[[], Awaitable[ToolEffectOutput]],
) -> ToolEffectReceipt:
    """Run one isolated trusted handler through the durable effect boundaries.

    Replayed unfinished effects never acquire execution permission. A handler
    exception or cancellation may leave partial effects, so it cannot establish
    a rollback or release the project's writer lane.
    """
    task = asyncio.current_task()
    cancellation_count = task.cancelling() if task is not None else 0

    def caller_cancelled() -> bool:
        return task is not None and task.cancelling() > cancellation_count

    preparation, cancelled = await _settled_database_call(
        partial(effects.prepare_effect, services, invocation, action)
    )
    record = preparation.record
    effect_id = record.intent.effectID
    if preparation.replayed:
        if cancelled:
            raise asyncio.CancelledError
        if record.phase == "terminal" and record.receipt is not None:
            return record.receipt
        raise CommandAdmissionError("reconciliationRequired")

    async def reject(
        reason: Literal["authorizationDenied", "callerCancelled"],
    ) -> ToolEffectReceipt:
        receipt, was_cancelled = await _settled_database_call(
            partial(effects.reject_effect, services, invocation, effect_id, reason)
        )
        if was_cancelled:
            raise asyncio.CancelledError
        return receipt

    async def interrupt() -> None:
        await _settled_database_call(
            partial(
                effects.interrupt_effect,
                services,
                owner_id=invocation.intent.ownerID,
                effect_id=effect_id,
            )
        )

    if cancelled or caller_cancelled():
        await reject("callerCancelled")
        raise asyncio.CancelledError
    try:
        authorized = await authorize()
    except BaseException as exc:
        await reject(
            "callerCancelled" if isinstance(exc, asyncio.CancelledError) else "authorizationDenied"
        )
        raise
    if caller_cancelled():
        await reject("callerCancelled")
        raise asyncio.CancelledError
    if authorized is not True:
        return await reject("authorizationDenied")

    try:
        _, cancelled = await _settled_database_call(
            partial(effects.begin_effect, services, invocation, effect_id, record.revision)
        )
    except CommandAdmissionError as exc:
        if exc.code not in {"revisionConflict", "activeRunConflict"}:
            await reject("authorizationDenied")
        raise
    except BaseException:
        await interrupt()
        raise
    try:
        if cancelled or caller_cancelled():
            raise asyncio.CancelledError
        output = await handler()
        receipt, cancelled = await _settled_database_call(
            partial(effects.complete_effect, services, invocation, effect_id, output)
        )
    except BaseException:
        await interrupt()
        raise
    if cancelled or caller_cancelled():
        raise asyncio.CancelledError
    return receipt
