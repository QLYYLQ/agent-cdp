"""Tests for M3 — Filter exception safety.

Verifies that:
1. Filter exception skips that connection, remaining handlers still execute
2. Filter exception produces warning log
"""

from __future__ import annotations

import logging

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope


class FilterSafetyEvent(BaseEvent[str]):
    __registry_key__ = 'm3_test_filter_safety'


class TestFilterExceptionSafety:
    def test_filter_exception_skips_connection_others_run(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_with_bad_filter(event: FilterSafetyEvent) -> str:
            calls.append('bad_filter')
            return 'bad'

        def handler_normal(event: FilterSafetyEvent) -> str:
            calls.append('normal')
            return 'normal'

        # Bad filter raises
        scope.connect(
            FilterSafetyEvent,
            handler_with_bad_filter,
            mode=ConnectionType.DIRECT,
            priority=10,
            filter=lambda e: 1 / 0,  # ZeroDivisionError  # type: ignore[truthy-function]
        )
        # Good handler, no filter
        scope.connect(
            FilterSafetyEvent,
            handler_normal,
            mode=ConnectionType.DIRECT,
            priority=5,
        )

        event = FilterSafetyEvent()
        scope.emit(event)

        assert calls == ['normal'], 'Bad filter handler should be skipped, normal should run'

    def test_filter_exception_produces_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        scope = EventScope('s1')

        def handler(event: FilterSafetyEvent) -> str:
            return 'ok'

        scope.connect(
            FilterSafetyEvent,
            handler,
            mode=ConnectionType.DIRECT,
            filter=lambda e: (_ for _ in ()).throw(RuntimeError('filter broke')),  # type: ignore[truthy-function]
        )

        with caplog.at_level(logging.WARNING, logger='agent_cdp.scope.scope'):
            scope.emit(FilterSafetyEvent())

        assert any('filter' in r.message.lower() for r in caplog.records), (
            f'Expected WARNING log about filter, got: {[r.message for r in caplog.records]}'
        )
