"""Tests for ScopeEventLoop full asyncio Task implementation (C7).

Covers queued handler execution, ordering, result/error recording,
stop semantics (drain/no-drain), mixed Direct+Queued, and ContextVar parent tracking.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.events.result import ResultStatus
from agent_cdp.scope import EventScope


class LoopNavEvent(BaseEvent[str]):
    __registry_key__ = 'loop_test_nav'
    url: str = 'https://example.com'


class LoopChildEvent(BaseEvent[str]):
    __registry_key__ = 'loop_test_child'


# ── Queued handler execution ──


class TestQueuedHandlerExecution:
    @pytest.mark.asyncio
    async def test_queued_handler_executes_async(self) -> None:
        """Connect an async QUEUED handler. Emit returns synchronously without executing.
        After starting the loop and awaiting the event, the handler has run."""
        scope = EventScope('s1')
        calls: list[str] = []

        async def handler(event: LoopNavEvent) -> str:
            calls.append('called')
            return 'ok'

        scope.connect(LoopNavEvent, handler, mode=ConnectionType.QUEUED)
        event = LoopNavEvent()
        scope.emit(event)

        # Handler has NOT run yet — emit returns synchronously
        assert calls == []
        assert event.has_pending is True

        try:
            await scope._event_loop.start()
            await event
        finally:
            await scope._event_loop.stop()

        # Handler has now run
        assert calls == ['called']
        assert event.has_pending is False

    @pytest.mark.asyncio
    async def test_queued_handlers_execute_in_order(self) -> None:
        """Connect 3 async QUEUED handlers with same priority. Verify FIFO order."""
        scope = EventScope('s1')
        order: list[str] = []

        async def handler_a(event: LoopNavEvent) -> str:
            order.append('a')
            return 'a'

        async def handler_b(event: LoopNavEvent) -> str:
            order.append('b')
            return 'b'

        async def handler_c(event: LoopNavEvent) -> str:
            order.append('c')
            return 'c'

        scope.connect(LoopNavEvent, handler_a, mode=ConnectionType.QUEUED, priority=0)
        scope.connect(LoopNavEvent, handler_b, mode=ConnectionType.QUEUED, priority=0)
        scope.connect(LoopNavEvent, handler_c, mode=ConnectionType.QUEUED, priority=0)

        event = LoopNavEvent()
        scope.emit(event)

        try:
            await scope._event_loop.start()
            await event
        finally:
            await scope._event_loop.stop()

        # FIFO order: a, b, c (same priority, registration order preserved by emit)
        assert order == ['a', 'b', 'c']


# ── Result/error recording ──


class TestQueuedResultRecording:
    @pytest.mark.asyncio
    async def test_queued_handler_result_recorded(self) -> None:
        """Connect async QUEUED handler returning 'ok'. Verify result recorded."""
        scope = EventScope('s1')

        async def handler(event: LoopNavEvent) -> str:
            return 'ok'

        conn = scope.connect(LoopNavEvent, handler, mode=ConnectionType.QUEUED)
        event = LoopNavEvent()
        scope.emit(event)

        try:
            await scope._event_loop.start()
            await event
        finally:
            await scope._event_loop.stop()

        assert conn.id in event.event_results
        er = event.event_results[conn.id]
        assert er.status == ResultStatus.COMPLETED
        assert er.result == 'ok'

    @pytest.mark.asyncio
    async def test_queued_handler_exception_recorded(self) -> None:
        """Connect async QUEUED handler that raises ValueError. Verify error recorded
        and loop survives to process another event."""
        scope = EventScope('s1')

        async def failing_handler(event: LoopNavEvent) -> str:
            msg = 'handler boom'
            raise ValueError(msg)

        conn = scope.connect(LoopNavEvent, failing_handler, mode=ConnectionType.QUEUED)
        event1 = LoopNavEvent()
        scope.emit(event1)

        try:
            await scope._event_loop.start()
            await event1

            # Verify error recorded
            assert conn.id in event1.event_results
            er = event1.event_results[conn.id]
            assert er.status == ResultStatus.FAILED
            assert er.error is not None
            assert 'handler boom' in str(er.error)

            # Loop should still be alive — emit and process another event
            event2 = LoopNavEvent()
            scope.emit(event2)
            await event2
            assert conn.id in event2.event_results
        finally:
            await scope._event_loop.stop()


# ── Await / pending semantics ──


class TestAwaitPending:
    @pytest.mark.asyncio
    async def test_await_event_waits_for_queued(self) -> None:
        """Emit event with QUEUED handler. has_pending is True.
        After starting loop and awaiting, has_pending is False."""
        scope = EventScope('s1')

        async def handler(event: LoopNavEvent) -> str:
            return 'done'

        scope.connect(LoopNavEvent, handler, mode=ConnectionType.QUEUED)
        event = LoopNavEvent()
        scope.emit(event)

        assert event.has_pending is True

        try:
            await scope._event_loop.start()
            await event
        finally:
            await scope._event_loop.stop()

        assert event.has_pending is False


# ── Stop semantics ──


class TestStopSemantics:
    @pytest.mark.asyncio
    async def test_stop_drain_processes_remaining(self) -> None:
        """Start loop. Emit 3 events. stop(drain=True) processes all."""
        scope = EventScope('s1')
        calls: list[str] = []

        async def handler(event: LoopNavEvent) -> str:
            calls.append(event.url)
            return event.url

        scope.connect(LoopNavEvent, handler, mode=ConnectionType.QUEUED)

        await scope._event_loop.start()

        events = []
        for i in range(3):
            e = LoopNavEvent(url=f'https://example.com/{i}')
            scope.emit(e)
            events.append(e)

        await scope._event_loop.stop(drain=True)

        # All 3 handlers should have run
        assert len(calls) == 3
        for e in events:
            assert e.has_pending is False

    @pytest.mark.asyncio
    async def test_stop_no_drain_discards_remaining(self) -> None:
        """Connect a slow async handler. Emit several events.
        stop(drain=False) discards remaining. Events are awaitable without hanging."""
        scope = EventScope('s1')
        calls: list[str] = []

        async def slow_handler(event: LoopNavEvent) -> str:
            await asyncio.sleep(10)  # Very slow — should be cancelled
            calls.append('done')
            return 'done'

        scope.connect(LoopNavEvent, slow_handler, mode=ConnectionType.QUEUED)

        await scope._event_loop.start()

        events = []
        for i in range(3):
            e = LoopNavEvent(url=f'https://example.com/{i}')
            scope.emit(e)
            events.append(e)

        # Give the loop a moment to pick up at least the first item
        await asyncio.sleep(0.05)

        await scope._event_loop.stop(drain=False)

        # Pending counts should be decremented so await doesn't hang
        for e in events:
            # Each event should be resolvable (no hanging)
            await asyncio.wait_for(e, timeout=1.0)


# ── Mixed Direct+Queued ──


class TestMixedDirectQueued:
    @pytest.mark.asyncio
    async def test_mixed_direct_queued(self) -> None:
        """Connect a DIRECT handler and a QUEUED handler. Emit event.
        Direct result is immediate. Queued result appears after loop starts."""
        scope = EventScope('s1')

        def direct_handler(event: LoopNavEvent) -> str:
            return 'direct-result'

        async def queued_handler(event: LoopNavEvent) -> str:
            return 'queued-result'

        conn_direct = scope.connect(LoopNavEvent, direct_handler, mode=ConnectionType.DIRECT, priority=10)
        conn_queued = scope.connect(LoopNavEvent, queued_handler, mode=ConnectionType.QUEUED, priority=5)

        event = LoopNavEvent()
        scope.emit(event)

        # Direct result is immediately available
        assert conn_direct.id in event.event_results
        assert event.event_results[conn_direct.id].result == 'direct-result'

        # Queued result is NOT yet available
        assert conn_queued.id not in event.event_results

        try:
            await scope._event_loop.start()
            await event
        finally:
            await scope._event_loop.stop()

        # Now both results are present
        assert conn_queued.id in event.event_results
        assert event.event_results[conn_queued.id].result == 'queued-result'


# ── ContextVar parent tracking in queued handlers ──


class TestContextVarParentTrackingQueued:
    @pytest.mark.asyncio
    async def test_context_var_parent_tracking_in_queued(self) -> None:
        """Connect a QUEUED handler that emits a child event. Start loop, await parent.
        Check child event's event_parent_id == parent.event_id."""
        scope = EventScope('s1')
        child_events: list[LoopChildEvent] = []

        async def parent_handler(event: LoopNavEvent) -> str:
            child = LoopChildEvent()
            scope.emit(child)
            child_events.append(child)
            return 'parent-done'

        async def child_handler(event: LoopChildEvent) -> str:
            return 'child-done'

        scope.connect(LoopNavEvent, parent_handler, mode=ConnectionType.QUEUED)
        scope.connect(LoopChildEvent, child_handler, mode=ConnectionType.QUEUED)

        parent = LoopNavEvent()
        scope.emit(parent)

        try:
            await scope._event_loop.start()
            await parent

            # Parent handler emitted a child event
            assert len(child_events) == 1
            child = child_events[0]
            assert child.event_parent_id == parent.event_id

            # Await child event too
            await child
        finally:
            await scope._event_loop.stop()
