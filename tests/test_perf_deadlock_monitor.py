"""Tests for scope-level deadlock monitor (replaces per-handler task creation).

Covers:
- Warning logged for long-running handlers
- No warning for fast handlers
- Single warning per handler (not repeated)
- Monitor lifecycle (stops with loop)
- Handler register/unregister internals
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope.event_loop import ScopeEventLoop

# ── Test event ──


class MonitorTestEvent(BaseEvent[str]):
    __registry_key__ = 'deadlock_monitor_test'


# ── Tests ──


class TestMonitorWarnsLongHandler:
    @pytest.mark.asyncio
    async def test_monitor_warns_long_handler(self, caplog: pytest.LogCaptureFixture) -> None:
        """Handler exceeding threshold triggers a deadlock warning."""
        loop = ScopeEventLoop(deadlock_scan_interval=0.05, deadlock_threshold=0.1)
        await loop.start()

        from agent_cdp.scope import EventScope

        scope = EventScope('s1')
        scope._event_loop = loop

        async def slow_handler(event: MonitorTestEvent) -> str:
            await asyncio.sleep(0.3)
            return 'done'

        scope.connect(MonitorTestEvent, slow_handler, mode=ConnectionType.QUEUED, target_scope=scope)

        with caplog.at_level(logging.WARNING):
            event = MonitorTestEvent(event_timeout=5.0)
            scope.emit(event)
            await event

        await loop.stop()

        assert any(
            'has been running' in r.message and 'slow_handler' in r.message
            for r in caplog.records
        )


class TestMonitorNoWarnFastHandler:
    @pytest.mark.asyncio
    async def test_monitor_no_warn_fast_handler(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fast handler completes before threshold — no warning."""
        loop = ScopeEventLoop(deadlock_scan_interval=0.05, deadlock_threshold=0.5)
        await loop.start()

        from agent_cdp.scope import EventScope

        scope = EventScope('s1')
        scope._event_loop = loop

        async def fast_handler(event: MonitorTestEvent) -> str:
            await asyncio.sleep(0.01)
            return 'fast'

        scope.connect(MonitorTestEvent, fast_handler, mode=ConnectionType.QUEUED, target_scope=scope)

        with caplog.at_level(logging.WARNING):
            event = MonitorTestEvent(event_timeout=5.0)
            scope.emit(event)
            await event

        await loop.stop()

        deadlock_warnings = [
            r for r in caplog.records
            if 'has been running' in r.message
        ]
        assert len(deadlock_warnings) == 0


class TestMonitorWarnsOnce:
    @pytest.mark.asyncio
    async def test_monitor_warns_once_per_handler(self, caplog: pytest.LogCaptureFixture) -> None:
        """Handler running long enough for 3+ scans gets warned only once."""
        loop = ScopeEventLoop(deadlock_scan_interval=0.05, deadlock_threshold=0.1)
        await loop.start()

        from agent_cdp.scope import EventScope

        scope = EventScope('s1')
        scope._event_loop = loop

        async def very_slow_handler(event: MonitorTestEvent) -> str:
            # Long enough for ~6 scan intervals after threshold
            await asyncio.sleep(0.5)
            return 'done'

        scope.connect(
            MonitorTestEvent, very_slow_handler, mode=ConnectionType.QUEUED, target_scope=scope,
        )

        with caplog.at_level(logging.WARNING):
            event = MonitorTestEvent(event_timeout=5.0)
            scope.emit(event)
            await event

        await loop.stop()

        deadlock_warnings = [
            r for r in caplog.records
            if 'has been running' in r.message and 'very_slow_handler' in r.message
        ]
        assert len(deadlock_warnings) == 1


class TestMonitorStopsWithLoop:
    @pytest.mark.asyncio
    async def test_monitor_stops_with_loop(self) -> None:
        """Stopping the loop also cancels the monitor task."""
        loop = ScopeEventLoop(deadlock_scan_interval=0.05, deadlock_threshold=0.1)
        await loop.start()

        assert loop._monitor_task is not None
        assert not loop._monitor_task.done()

        await loop.stop()

        assert loop._monitor_task is None


class TestHandlerRegisterUnregister:
    def test_register_unregister(self) -> None:
        """_register_handler and _unregister_handler manage the active dict."""
        loop = ScopeEventLoop()

        hid1 = loop._register_handler('handler_a')
        hid2 = loop._register_handler('handler_b')

        assert hid1 in loop._active_handlers
        assert hid2 in loop._active_handlers
        assert loop._active_handlers[hid1][0] == 'handler_a'
        assert loop._active_handlers[hid2][0] == 'handler_b'

        # Simulate one warning for hid1
        loop._warned_handlers.add(hid1)

        loop._unregister_handler(hid1)
        assert hid1 not in loop._active_handlers
        assert hid1 not in loop._warned_handlers  # cleaned up
        assert hid2 in loop._active_handlers

        loop._unregister_handler(hid2)
        assert len(loop._active_handlers) == 0

    def test_unregister_idempotent(self) -> None:
        """Unregistering a non-existent handler is a no-op."""
        loop = ScopeEventLoop()
        loop._unregister_handler(999)  # should not raise
