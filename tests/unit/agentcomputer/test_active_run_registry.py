"""A durable Run request can signal only its exact in-process invocation."""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_submission_runs import allow_fixture, request
from tests.unit.agentcomputer.test_submission_runs import control as control

from octop.infra.agentcomputer import cancellations, dispatch, engine
from octop.infra.agentcomputer.active_runs import ActiveRunRegistry
from octop.infra.agentcomputer.commands import CommandAdmissionError, admit_new_message
from octop.infra.agentcomputer.models import EngineInvocation


@pytest.fixture
def prepared(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    accepted = admit_new_message(services, owner_id=1, task_id=task_id, command=request())
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=accepted.runID, claimant_id=uuid4(), expected_revision=1
    )
    invocation = dispatch._begin_dispatch(services, claim)
    assert isinstance(invocation, EngineInvocation)
    return services, invocation


async def _park(registry: ActiveRunRegistry, invocation: EngineInvocation, ready: asyncio.Event):
    async with registry.track(invocation):
        ready.set()
        await asyncio.Future()


async def test_only_exact_committed_receipt_signals_active_run(prepared):
    services, invocation = prepared
    registry = ActiveRunRegistry()
    ready = asyncio.Event()
    task = asyncio.create_task(_park(registry, invocation, ready))
    await ready.wait()
    receipt = await asyncio.to_thread(
        cancellations.cancel_run,
        services,
        owner_id=1,
        run_id=invocation.intent.runID,
        command=cancellations.CancelRunCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=invocation.intent.restoreEpoch,
            expectedRevision=invocation.expectedRunRevision,
        ),
    )
    assert receipt.status == "cancelRequested"
    assert not registry.signal_cancel(owner_id=2, receipt=receipt)
    assert not registry.signal_cancel(
        owner_id=1, receipt=receipt.model_copy(update={"status": "stopped"})
    )
    for field in ("boxID", "runID", "turnID", "taskID", "projectID"):
        assert not registry.signal_cancel(
            owner_id=1, receipt=receipt.model_copy(update={field: uuid4()})
        )
    for field in ("restoreEpoch", "executionEpoch"):
        assert not registry.signal_cancel(
            owner_id=1, receipt=receipt.model_copy(update={field: getattr(receipt, field) + 1})
        )
    assert not task.done()
    assert registry.signal_cancel(owner_id=1, receipt=receipt)
    assert not registry.signal_cancel(
        owner_id=1, receipt=receipt.model_copy(update={"replayed": True})
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.signal_cancel(owner_id=1, receipt=receipt)

    # A later Run can use the same Task thread without receiving the old signal.
    later_intent = invocation.intent.model_copy(update={"runID": uuid4()})
    later = invocation.model_copy(update={"intent": later_intent})
    assert later.intent.snapshot.octopThreadID == invocation.intent.snapshot.octopThreadID
    later_ready = asyncio.Event()
    later_task = asyncio.create_task(_park(registry, later, later_ready))
    await later_ready.wait()
    assert not registry.signal_cancel(owner_id=1, receipt=receipt)
    assert not later_task.done()
    later_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await later_task


async def test_duplicate_or_nested_registration_is_rejected(prepared):
    _, invocation = prepared
    registry = ActiveRunRegistry()
    async with registry.track(invocation):
        with pytest.raises(CommandAdmissionError, match="revisionConflict"):
            async with registry.track(invocation):
                pytest.fail("duplicate active Run registered")
        another = invocation.model_copy(
            update={"intent": invocation.intent.model_copy(update={"runID": uuid4()})}
        )
        with pytest.raises(CommandAdmissionError, match="revisionConflict"):
            async with registry.track(another):
                pytest.fail("one task owns two active Runs")

        async def duplicate():
            async with registry.track(invocation):
                pytest.fail("another task registered the same Run")

        with pytest.raises(CommandAdmissionError, match="revisionConflict"):
            await asyncio.create_task(duplicate())


async def test_engine_entry_registers_while_stream_is_active(prepared, monkeypatch):
    services, invocation = prepared
    registry = ActiveRunRegistry()
    ready = asyncio.Event()
    monkeypatch.setattr(engine, "_validate_registered_workspace", lambda *_: None)
    snapshot = invocation.intent.snapshot

    class Agent:
        agent_id = snapshot.agentID
        config = SimpleNamespace(
            default_model=snapshot.projectConfiguration.modelRef,
            system_prompt=snapshot.projectConfiguration.instructions,
            middleware=[engine.EngineEntryMiddleware(services, invocation)],
        )

        async def stream(self, request, *, protocol):
            assert protocol == "langgraph" and request.thread_id == snapshot.octopThreadID
            ready.set()
            await asyncio.Future()
            yield None

    task = asyncio.create_task(
        engine.invoke_engine_entry(Agent(), invocation, active_runs=registry)
    )
    await ready.wait()
    receipt = await asyncio.to_thread(
        cancellations.cancel_run,
        services,
        owner_id=1,
        run_id=invocation.intent.runID,
        command=cancellations.CancelRunCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=invocation.intent.restoreEpoch,
            expectedRevision=invocation.expectedRunRevision,
        ),
    )
    assert registry.signal_cancel(owner_id=1, receipt=receipt)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.signal_cancel(owner_id=1, receipt=receipt)


def test_completed_registration_releases_loop_affinity(prepared):
    _, invocation = prepared
    registry = ActiveRunRegistry()

    async def once():
        async with registry.track(invocation):
            pass

    asyncio.run(once())
    asyncio.run(once())


async def test_drain_closes_admission_and_waits_for_dispatch_scope(prepared):
    services, invocation = prepared
    claim = dispatch.get_claim(services, owner_id=1, run_id=invocation.intent.runID)
    assert claim is not None
    registry = ActiveRunRegistry()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def running_dispatch():
        async with registry.track_dispatch(claim):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise

    task = asyncio.create_task(running_dispatch())
    await asyncio.wait_for(entered.wait(), timeout=5)
    draining = asyncio.create_task(registry.drain())
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        assert not draining.done() and not task.done()
        # An unrelated Run cannot enter the registry after shutdown starts.
        another = claim.model_copy(
            update={"intent": claim.intent.model_copy(update={"runID": uuid4()})}
        )
        with pytest.raises(CommandAdmissionError):
            async with registry.track_dispatch(another):
                pytest.fail("shutdown admitted a new dispatch")
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(draining, timeout=5)
    await registry.drain()
    with pytest.raises(CommandAdmissionError):
        async with registry.track_dispatch(claim):
            pytest.fail("completed shutdown admitted a new dispatch")


async def test_drain_waits_for_workspace_validation_thread_before_stream(prepared, monkeypatch):
    services, invocation = prepared
    registry = ActiveRunRegistry()
    started, release, finished = Event(), Event(), Event()
    streamed = []

    def delayed_validation(_agent, _middleware):
        started.set()
        try:
            assert release.wait(timeout=10)
        finally:
            finished.set()

    monkeypatch.setattr(engine, "_validate_registered_workspace", delayed_validation)
    snapshot = invocation.intent.snapshot

    class Agent:
        agent_id = snapshot.agentID
        config = SimpleNamespace(
            default_model=snapshot.projectConfiguration.modelRef,
            system_prompt=snapshot.projectConfiguration.instructions,
            middleware=[engine.EngineEntryMiddleware(services, invocation)],
        )

        async def stream(self, request, *, protocol):
            streamed.append((request, protocol))
            yield None

    task = asyncio.create_task(
        engine.invoke_engine_entry(Agent(), invocation, active_runs=registry)
    )
    try:
        assert await asyncio.to_thread(started.wait, 10)
        draining = asyncio.create_task(registry.drain())

        async def wait_for_closing():
            while not registry.closing:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_closing(), timeout=5)
        assert not draining.done() and not task.done()
        assert not finished.is_set() and not streamed
        with pytest.raises(CommandAdmissionError):
            async with registry.track(invocation):
                pytest.fail("shutdown admitted another engine entry")
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(draining, timeout=5)
    assert finished.is_set() and not streamed
