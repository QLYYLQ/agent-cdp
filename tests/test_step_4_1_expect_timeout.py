"""Tests for expect() + per-handler timeout + deadlock detection (C10 / Step 4.1).

Covers:
- expect() with matching, include/exclude filters, timeout, auto-disconnect
- Per-handler timeout recording errors without crashing the loop
- Deadlock warning logging after 15s
- Direct handler exemption from framework-level timeout
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agent_cdp.advanced.expect import expect
from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.events.result import ResultStatus
from agent_cdp.scope import EventScope

# ── Test event subclasses ──


class ExpectNavEvent(BaseEvent[str]):
    __registry_key__ = 'expect_test_nav'
    url: str = 'https://example.com'


class ExpectOtherEvent(BaseEvent[str]):
    __registry_key__ = 'expect_test_other'


# ── expect tests ──


class TestExpectReturnsMatchingEvent:
    @pytest.mark.asyncio
    async def test_expect_returns_matching_event(self) -> None:
        """Emit an event, expect catches it and returns the event."""
        scope = EventScope('s1')

        async def _emit_after_delay() -> None:
            await asyncio.sleep(0.05)
            scope.emit(ExpectNavEvent(url='https://example.com'))

        asyncio.create_task(_emit_after_delay())
        result = await expect(scope, ExpectNavEvent, timeout=2.0)
        assert isinstance(result, ExpectNavEvent)
        assert result.url == 'https://example.com'


class TestExpectWithIncludeFilter:
    @pytest.mark.asyncio
    async def test_expect_with_include_filter(self) -> None:
        """Include predicate filters correctly — only matching events are returned."""
        scope = EventScope('s1')

        async def _emit_events() -> None:
            await asyncio.sleep(0.05)
            # First event should NOT match the include filter
            scope.emit(ExpectNavEvent(url='https://other.com'))
            await asyncio.sleep(0.05)
            # Second event SHOULD match
            scope.emit(ExpectNavEvent(url='https://target.com'))

        asyncio.create_task(_emit_events())
        result = await expect(
            scope,
            ExpectNavEvent,
            include=lambda e: e.url == 'https://target.com',
            timeout=2.0,
        )
        assert result.url == 'https://target.com'


class TestExpectWithExcludeFilter:
    @pytest.mark.asyncio
    async def test_expect_with_exclude_filter(self) -> None:
        """Exclude predicate filters correctly — excluded events are skipped."""
        scope = EventScope('s1')

        async def _emit_events() -> None:
            await asyncio.sleep(0.05)
            # First event should be excluded
            scope.emit(ExpectNavEvent(url='https://excluded.com'))
            await asyncio.sleep(0.05)
            # Second event should NOT be excluded
            scope.emit(ExpectNavEvent(url='https://ok.com'))

        asyncio.create_task(_emit_events())
        result = await expect(
            scope,
            ExpectNavEvent,
            exclude=lambda e: e.url == 'https://excluded.com',
            timeout=2.0,
        )
        assert result.url == 'https://ok.com'


class TestExpectTimeoutRaises:
    @pytest.mark.asyncio
    async def test_expect_timeout_raises(self) -> None:
        """No matching event within timeout raises TimeoutError."""
        scope = EventScope('s1')
        with pytest.raises(TimeoutError):
            await expect(scope, ExpectNavEvent, timeout=0.1)


class TestExpectAutoDisconnects:
    @pytest.mark.asyncio
    async def test_expect_auto_disconnects(self) -> None:
        """After expect returns, the temporary connection is removed from scope."""
        scope = EventScope('s1')

        # Count connections before
        conns_before = sum(len(v) for v in scope._connections_by_type.values())

        async def _emit_after_delay() -> None:
            await asyncio.sleep(0.05)
            scope.emit(ExpectNavEvent(url='https://example.com'))

        asyncio.create_task(_emit_after_delay())
        await expect(scope, ExpectNavEvent, timeout=2.0)

        # After expect returns, temporary connection should be gone
        conns_after = sum(len(v) for v in scope._connections_by_type.values())
        assert conns_after == conns_before

    @pytest.mark.asyncio
    async def test_expect_auto_disconnects_on_timeout(self) -> None:
        """After expect times out, the temporary connection is also removed."""
        scope = EventScope('s1')
        conns_before = sum(len(v) for v in scope._connections_by_type.values())

        with pytest.raises(TimeoutError):
            await expect(scope, ExpectNavEvent, timeout=0.1)

        conns_after = sum(len(v) for v in scope._connections_by_type.values())
        assert conns_after == conns_before


# ── per-handler timeout tests ──


class TestHandlerTimeoutRecordsError:
    @pytest.mark.asyncio
    async def test_handler_timeout_records_error(self) -> None:
        """Slow handler exceeding event_timeout records TimeoutError in EventResult.

        With H4 enforcement, `await event` itself raises EventTimeoutError
        when event_timeout expires. The per-handler timeout in ScopeEventLoop
        records the TIMEOUT result on the EventResult before that deadline.
        """
        from agent_cdp.events.base import EventTimeoutError

        scope = EventScope('s1')

        async def slow_handler(event: ExpectNavEvent) -> str:
            await asyncio.sleep(10)  # Way longer than timeout
            return 'should not reach'

        conn = scope.connect(ExpectNavEvent, slow_handler, mode=ConnectionType.QUEUED)

        # Use a very short timeout event
        event = ExpectNavEvent(event_timeout=0.1)
        scope.emit(event)

        try:
            await scope._event_loop.start()
            # The event-level timeout (0.1s) fires before the handler completes.
            # EventTimeoutError is the expected outcome.
            try:
                await asyncio.wait_for(event, timeout=5.0)
            except EventTimeoutError:
                pass  # Expected: event_timeout enforced by __await__
        finally:
            await scope._event_loop.stop()

        assert conn.id in event.event_results
        er = event.event_results[conn.id]
        assert er.status == ResultStatus.TIMEOUT
        assert er.error is not None
        assert isinstance(er.error, TimeoutError)


class TestHandlerTimeoutDoesNotCrashLoop:
    @pytest.mark.asyncio
    async def test_handler_timeout_does_not_crash_loop(self) -> None:
        """Loop survives after a handler timeout and processes the next event."""
        scope = EventScope('s1')
        results: list[str] = []

        async def slow_handler(event: ExpectNavEvent) -> str:
            if event.url == 'https://slow.com':
                await asyncio.sleep(10)
            results.append(event.url)
            return event.url

        conn = scope.connect(ExpectNavEvent, slow_handler, mode=ConnectionType.QUEUED)

        # First event will timeout
        event1 = ExpectNavEvent(url='https://slow.com', event_timeout=0.1)
        scope.emit(event1)

        # Second event should process normally
        event2 = ExpectNavEvent(url='https://fast.com', event_timeout=5.0)
        scope.emit(event2)

        from agent_cdp.events.base import EventTimeoutError

        try:
            await scope._event_loop.start()
            # event1's event_timeout=0.1 fires via __await__ → EventTimeoutError
            try:
                await asyncio.wait_for(event1, timeout=5.0)
            except EventTimeoutError:
                pass  # Expected
            await asyncio.wait_for(event2, timeout=5.0)
        finally:
            await scope._event_loop.stop()

        # event1 timed out
        assert event1.event_results[conn.id].status == ResultStatus.TIMEOUT

        # event2 processed normally — loop survived
        assert conn.id in event2.event_results
        assert event2.event_results[conn.id].status == ResultStatus.COMPLETED
        assert event2.event_results[conn.id].result == 'https://fast.com'
        assert 'https://fast.com' in results


class TestDeadlockWarningLogged:
    @pytest.mark.asyncio
    async def test_deadlock_warning_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Deadlock warning function logs after the specified delay."""
        from agent_cdp.advanced.timeout import _deadlock_warning

        with caplog.at_level(logging.WARNING):
            # Run the warning with a very short delay for fast testing
            task = asyncio.create_task(_deadlock_warning('test_handler', delay=0.05))
            await asyncio.sleep(0.1)

            assert any('has been running' in r.message and 'test_handler' in r.message for r in caplog.records)
            # Task has already completed (delay elapsed), but cancel for cleanup
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ── Direct handler no timeout enforcement ──


class TestDirectHandlerNoTimeoutEnforcement:
    def test_direct_handler_no_timeout_enforcement(self) -> None:
        """Direct handler runs without framework-level timeout enforcement.

        Direct handlers execute synchronously in the emit() call stack.
        They cannot be interrupted by asyncio.wait_for, and the framework
        intentionally does not wrap them in timeout logic.
        """
        scope = EventScope('s1')
        calls: list[str] = []

        def direct_handler(event: ExpectNavEvent) -> str:
            calls.append('direct-ran')
            return 'direct-result'

        conn = scope.connect(ExpectNavEvent, direct_handler, mode=ConnectionType.DIRECT)

        # Even with a very short event_timeout, Direct handler executes fully
        event = ExpectNavEvent(event_timeout=0.001)
        scope.emit(event)

        assert calls == ['direct-ran']
        assert conn.id in event.event_results
        er = event.event_results[conn.id]
        assert er.status == ResultStatus.COMPLETED
        assert er.result == 'direct-result'
