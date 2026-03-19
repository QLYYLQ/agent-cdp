"""Tests for M2 — Queued handler exception logging.

Verifies that queued handler exceptions produce WARNING log entries.
"""

from __future__ import annotations

import logging

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope


class LogTestEvent(BaseEvent[str]):
    __registry_key__ = 'm2_test_log'


class TestQueuedHandlerExceptionLogging:
    @pytest.mark.asyncio
    async def test_queued_handler_exception_produces_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        scope = EventScope('s1')

        async def failing_handler(event: LogTestEvent) -> str:
            msg = 'handler boom'
            raise ValueError(msg)

        scope.connect(LogTestEvent, failing_handler, mode=ConnectionType.QUEUED)
        event = LogTestEvent()
        scope.emit(event)

        with caplog.at_level(logging.WARNING, logger='agent_cdp.scope.event_loop'):
            try:
                await scope._event_loop.start()
                await event
            finally:
                await scope._event_loop.stop()

        assert any('handler boom' in r.message for r in caplog.records), (
            f'Expected WARNING log with "handler boom", got: {[r.message for r in caplog.records]}'
        )
        assert any(r.levelno == logging.WARNING for r in caplog.records)
