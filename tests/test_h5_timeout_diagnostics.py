"""Tests for H5 — enhanced EventTimeoutError diagnostics."""

from __future__ import annotations

import asyncio

import pytest

from agent_cdp import ConnectionType, EventScope
from agent_cdp.events import BaseEvent, EventTimeoutError


class DiagSlowEvent(BaseEvent[str]):
    event_timeout: float | None = 0.1


# ── EventTimeoutError message tests ──


class TestEventTimeoutErrorDiagnostics:
    """EventTimeoutError should include handler completion details."""

    def test_pending_count_in_error(self) -> None:
        err = EventTimeoutError(
            'NavEvent', 'abc123456789', 5.0,
            pending_count=3,
        )
        assert err.pending_count == 3
        assert '3 handler(s) still pending' in str(err)

    def test_completed_handlers_in_error(self) -> None:
        err = EventTimeoutError(
            'NavEvent', 'abc123456789', 5.0,
            pending_count=1,
            completed_handlers=['handler_a', 'handler_b'],
        )
        assert err.completed_handlers == ['handler_a', 'handler_b']
        msg = str(err)
        assert 'handler_a' in msg
        assert 'handler_b' in msg
        assert 'completed' in msg

    def test_failed_handlers_in_error(self) -> None:
        err = EventTimeoutError(
            'NavEvent', 'abc123456789', 5.0,
            pending_count=1,
            failed_handlers=['broken_handler'],
        )
        assert 'broken_handler' in str(err)
        assert 'failed' in str(err)

    def test_timed_out_handlers_in_error(self) -> None:
        err = EventTimeoutError(
            'NavEvent', 'abc123456789', 5.0,
            pending_count=0,
            timed_out_handlers=['slow_handler'],
        )
        assert 'slow_handler' in str(err)
        assert 'timed_out' in str(err)

    def test_no_handlers_responded_message(self) -> None:
        err = EventTimeoutError(
            'NavEvent', 'abc123456789', 5.0,
            pending_count=2,
        )
        assert 'no handlers responded' in str(err)

    def test_backward_compatible_construction(self) -> None:
        """Old 3-arg construction still works."""
        err = EventTimeoutError('NavEvent', 'abc123', 5.0)
        assert err.event_type == 'NavEvent'
        assert err.event_id == 'abc123'
        assert err.timeout == 5.0
        assert err.pending_count == 0
        assert err.completed_handlers == []
        assert err.failed_handlers == []
        assert err.timed_out_handlers == []
        assert isinstance(err, TimeoutError)


# ── Integration: timeout diagnostics in real scope dispatch ──


class TestTimeoutDiagnosticsIntegration:
    """When await event times out, the error should contain real handler info."""

    @pytest.mark.asyncio
    async def test_timeout_reports_pending_count(self) -> None:
        """A hanging handler should show pending_count=1 in the timeout error."""
        scope = EventScope('test-scope')
        await scope._event_loop.start()

        async def hanging_handler(event: DiagSlowEvent) -> str:
            await asyncio.sleep(999)
            return 'never'

        scope.connect(DiagSlowEvent, hanging_handler, mode=ConnectionType.QUEUED, target_scope=scope)

        event = DiagSlowEvent(event_timeout=0.05)
        scope.emit(event)

        with pytest.raises(EventTimeoutError) as exc_info:
            await event

        err = exc_info.value
        assert err.pending_count >= 1
        assert 'handler(s) still pending' in str(err)

        await scope.close()

    @pytest.mark.asyncio
    async def test_timeout_reports_completed_and_timed_out_handlers(self) -> None:
        """fast_handler completes, slow_handler hits per-handler timeout → both reported."""
        scope = EventScope('test-scope')
        await scope._event_loop.start()

        async def fast_handler(event: DiagSlowEvent) -> str:
            return 'done'

        async def slow_handler(event: DiagSlowEvent) -> str:
            await asyncio.sleep(999)
            return 'never'

        # Both queued — processed sequentially. fast finishes, slow times out per-handler.
        scope.connect(DiagSlowEvent, fast_handler, mode=ConnectionType.QUEUED, target_scope=scope, priority=100)
        scope.connect(DiagSlowEvent, slow_handler, mode=ConnectionType.QUEUED, target_scope=scope, priority=0)

        # event_timeout applies both per-handler and to await. fast_handler finishes fast,
        # slow_handler hits per-handler timeout and gets recorded as TIMEOUT.
        # Then await completes because pending reaches 0 (both processed).
        # So we check event_results directly instead of catching EventTimeoutError.
        event = DiagSlowEvent(event_timeout=0.1)
        scope.emit(event)

        # Wait for both handlers to be processed (fast completes, slow times out)
        await asyncio.sleep(0.3)
        await event  # should not raise — both handlers processed (one completed, one timed out)

        # Verify diagnostics are in event_results
        # handler_name includes the full qualified path (e.g. 'Test...locals>.fast_handler')
        results = event.event_results
        names_statuses = {r.handler_name: r.status for r in results.values()}
        fast_name = [n for n in names_statuses if 'fast_handler' in n]
        slow_name = [n for n in names_statuses if 'slow_handler' in n]
        assert len(fast_name) == 1
        assert len(slow_name) == 1
        assert names_statuses[fast_name[0]] == 'completed'
        assert names_statuses[slow_name[0]] == 'timeout'

        await scope.close()
