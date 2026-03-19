"""Tests for M7 — emit_and_wait() convenience method.

Verifies that:
1. Basic emit_and_wait returns completed event
2. timeout= override triggers EventTimeoutError
3. Direct-only handlers return immediately
"""

from __future__ import annotations

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent, EventTimeoutError
from agent_cdp.scope import EventScope


class EmitWaitEvent(BaseEvent[str]):
    __registry_key__ = 'm7_test_emit_wait'


class TestEmitAndWait:
    @pytest.mark.asyncio
    async def test_basic_emit_and_wait_returns_event(self) -> None:
        scope = EventScope('s1')

        async def handler(event: EmitWaitEvent) -> str:
            return 'result'

        conn = scope.connect(EmitWaitEvent, handler, mode=ConnectionType.QUEUED)
        await scope._event_loop.start()

        try:
            event = EmitWaitEvent()
            returned = await scope.emit_and_wait(event)
            assert returned is event
            assert conn.id in event.event_results
            assert event.event_results[conn.id].result == 'result'
            assert event.has_pending is False
        finally:
            await scope._event_loop.stop()

    @pytest.mark.asyncio
    async def test_timeout_override_triggers_error(self) -> None:
        import asyncio

        scope = EventScope('s1')

        async def slow_handler(event: EmitWaitEvent) -> str:
            await asyncio.sleep(100)
            return 'never'

        scope.connect(EmitWaitEvent, slow_handler, mode=ConnectionType.QUEUED)
        await scope._event_loop.start()

        try:
            event = EmitWaitEvent()
            with pytest.raises(EventTimeoutError):
                await scope.emit_and_wait(event, timeout=0.05)
        finally:
            await scope._event_loop.stop()

    @pytest.mark.asyncio
    async def test_direct_only_returns_immediately(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: EmitWaitEvent) -> str:
            calls.append('direct')
            return 'ok'

        scope.connect(EmitWaitEvent, handler, mode=ConnectionType.DIRECT)
        event = EmitWaitEvent()
        returned = await scope.emit_and_wait(event)
        assert returned is event
        assert calls == ['direct']
        assert event.has_pending is False
