"""Tests for M1 — AsyncHandlerError sentinel replaces broad except TypeError.

Verifies that:
1. User TypeError collected under COLLECT_ERRORS (not re-raised)
2. User TypeError still raises under FAIL_FAST
3. AsyncHandlerError from async detection still raises
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import AsyncHandlerError, BaseEvent, EmitPolicy
from agent_cdp.scope import EventScope


class UserTypeErrorEvent(BaseEvent[str]):
    __registry_key__ = 'm1_test_user_type_error'
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.COLLECT_ERRORS


class FailFastEvent(BaseEvent[str]):
    __registry_key__ = 'm1_test_fail_fast'


class TestAsyncHandlerError:
    def test_user_type_error_collected_under_collect_errors(self) -> None:
        """A user handler raising TypeError should be collected, not re-raised."""
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_a(event: UserTypeErrorEvent) -> str:
            calls.append('a')
            msg = 'user type error'
            raise TypeError(msg)

        def handler_b(event: UserTypeErrorEvent) -> str:
            calls.append('b')
            return 'b-result'

        conn_a = scope.connect(UserTypeErrorEvent, handler_a, mode=ConnectionType.DIRECT, priority=10)
        conn_b = scope.connect(UserTypeErrorEvent, handler_b, mode=ConnectionType.DIRECT, priority=5)

        event = UserTypeErrorEvent()
        scope.emit(event)  # should NOT raise — COLLECT_ERRORS policy

        assert calls == ['a', 'b'], 'Both handlers should have run'
        assert conn_a.id in event.event_results
        assert event.event_results[conn_a.id].error is not None
        assert isinstance(event.event_results[conn_a.id].error, TypeError)
        assert conn_b.id in event.event_results
        assert event.event_results[conn_b.id].result == 'b-result'

    def test_user_type_error_raises_under_fail_fast(self) -> None:
        """A user handler TypeError should still raise under FAIL_FAST."""
        scope = EventScope('s1')

        def handler(event: FailFastEvent) -> str:
            msg = 'user type error'
            raise TypeError(msg)

        scope.connect(FailFastEvent, handler, mode=ConnectionType.DIRECT)

        with pytest.raises(TypeError, match='user type error'):
            scope.emit(FailFastEvent())

    def test_async_handler_error_from_coroutine_still_raises(self) -> None:
        """AsyncHandlerError from async detection (coroutine returned) still raises."""
        scope = EventScope('s1')

        async def async_handler(event: FailFastEvent) -> str:
            return 'oops'

        scope.connect(FailFastEvent, async_handler, mode=ConnectionType.DIRECT)

        with pytest.raises(AsyncHandlerError, match='coroutine'):
            scope.emit(FailFastEvent())

    def test_async_handler_error_is_type_error_subclass(self) -> None:
        """AsyncHandlerError inherits from TypeError."""
        assert issubclass(AsyncHandlerError, TypeError)
