"""Tests for H4 — event_timeout enforcement in __await__."""

from __future__ import annotations

import asyncio

import pytest

from agent_cdp.events import BaseEvent, EventTimeoutError
from agent_cdp.events.aggregation import event_result


class SlowEvent(BaseEvent[str]):
    pass


class TestAwaitTimeoutRaises:
    """await on a pending event with event_timeout should raise EventTimeoutError."""

    @pytest.mark.asyncio
    async def test_await_timeout_raises_event_timeout_error(self) -> None:
        e = SlowEvent(event_timeout=0.05)
        e._increment_pending()  # simulate queued handler not completing

        with pytest.raises(EventTimeoutError):
            await e

    @pytest.mark.asyncio
    async def test_await_timeout_none_waits_indefinitely(self) -> None:
        """event_timeout=None → no timeout; event completes when pending reaches zero."""
        e = SlowEvent(event_timeout=None)
        e._increment_pending()

        async def release() -> None:
            await asyncio.sleep(0.02)
            e._decrement_pending()

        asyncio.create_task(release())
        # Should not raise — waits indefinitely until release
        await asyncio.wait_for(e, timeout=1.0)

    @pytest.mark.asyncio
    async def test_await_no_pending_returns_immediately(self) -> None:
        """No pending handlers → await returns immediately regardless of timeout."""
        e = SlowEvent(event_timeout=0.05)
        # _pending_count == 0 → fast path
        await asyncio.wait_for(e, timeout=0.1)


class TestEventTimeoutErrorProperties:
    """EventTimeoutError is a TimeoutError subclass with diagnostic attributes."""

    def test_is_timeout_error_subclass(self) -> None:
        err = EventTimeoutError('MyEvent', 'abc123456789extra', 5.0)
        assert isinstance(err, TimeoutError)

    def test_caught_by_except_timeout_error(self) -> None:
        try:
            raise EventTimeoutError('MyEvent', 'abc123', 5.0)
        except TimeoutError:
            pass  # should be caught

    def test_contains_diagnostics(self) -> None:
        err = EventTimeoutError('NavEvent', 'event-id-full', 10.0)
        assert err.event_type == 'NavEvent'
        assert err.event_id == 'event-id-full'
        assert err.timeout == 10.0

    def test_message_includes_context(self) -> None:
        err = EventTimeoutError('NavEvent', 'abcdef123456xyz', 2.5)
        msg = str(err)
        assert 'NavEvent' in msg
        assert 'abcdef123456' in msg
        assert '2.5' in msg


class TestAggregationUsesOwnTimeout:
    """event_result() uses its own timeout parameter, not event_timeout."""

    @pytest.mark.asyncio
    async def test_aggregation_still_uses_own_timeout(self) -> None:
        e = SlowEvent(event_timeout=300.0)  # very long event_timeout
        e._increment_pending()

        # aggregation timeout should trigger independently
        with pytest.raises(asyncio.TimeoutError):
            await event_result(e, timeout=0.05)
