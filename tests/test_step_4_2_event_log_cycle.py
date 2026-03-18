"""Tests for EventLog persistence, Direct emit depth cycle detection, and per-scope event history (C11)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope


class LogNavEvent(BaseEvent[str]):
    __registry_key__ = 'log_test_nav'
    url: str = 'https://example.com'


class LogPingEvent(BaseEvent[None]):
    __registry_key__ = 'log_test_ping'


# ── EventLogWriter: JSONL persistence ──


class TestEventLogWriteJsonlFormat:
    async def test_event_log_write_jsonl_format(self, tmp_path: Path) -> None:
        """Write a LogNavEvent, verify exactly 1 line, parseable JSON with event_type and event_id."""
        from agent_cdp.advanced.event_log import EventLogWriter

        writer = EventLogWriter(tmp_path / 'log.jsonl')
        event = LogNavEvent()
        await writer.write(event)

        lines = (tmp_path / 'log.jsonl').read_text().strip().splitlines()
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert 'event_type' in data
        assert 'event_id' in data
        assert data['event_type'] == 'log_test_nav'
        assert data['event_id'] == event.event_id


class TestEventLogReadAllDeserializes:
    async def test_event_log_read_all_deserializes(self, tmp_path: Path) -> None:
        """Write a LogNavEvent and a LogPingEvent, read_all() returns 2 correctly typed events."""
        from agent_cdp.advanced.event_log import EventLogWriter

        writer = EventLogWriter(tmp_path / 'log.jsonl')
        await writer.write(LogNavEvent(url='https://a.com'))
        await writer.write(LogPingEvent())

        events = await writer.read_all()
        assert len(events) == 2
        assert isinstance(events[0], LogNavEvent)
        assert isinstance(events[1], LogPingEvent)
        assert events[0].url == 'https://a.com'


class TestEventLogPerScopeIsolation:
    async def test_event_log_per_scope_isolation(self, tmp_path: Path) -> None:
        """Two writers with different paths, each returns only its own events."""
        from agent_cdp.advanced.event_log import EventLogWriter

        w1 = EventLogWriter(tmp_path / 'scope1.jsonl')
        w2 = EventLogWriter(tmp_path / 'scope2.jsonl')

        await w1.write(LogNavEvent(url='https://one.com'))
        await w2.write(LogPingEvent())

        events1 = await w1.read_all()
        events2 = await w2.read_all()

        assert len(events1) == 1
        assert len(events2) == 1
        assert isinstance(events1[0], LogNavEvent)
        assert isinstance(events2[0], LogPingEvent)


class TestEventLogEmptyFile:
    async def test_event_log_empty_file(self, tmp_path: Path) -> None:
        """read_all() on nonexistent path returns []."""
        from agent_cdp.advanced.event_log import EventLogWriter

        writer = EventLogWriter(tmp_path / 'nonexistent.jsonl')
        events = await writer.read_all()
        assert events == []


# ── Direct emit cycle detection ──


class TestDirectCycleRaisesRecursionError:
    def test_direct_cycle_raises_recursion_error(self) -> None:
        """Direct handler that re-emits the same event type triggers RecursionError."""
        scope = EventScope('cycle')

        def recursive_handler(event: LogNavEvent) -> None:
            scope.emit(LogNavEvent())

        scope.connect(LogNavEvent, recursive_handler, mode=ConnectionType.DIRECT)

        with pytest.raises(RecursionError, match='depth'):
            scope.emit(LogNavEvent())


class TestQueuedDoesNotTriggerCycleCheck:
    def test_queued_does_not_trigger_cycle_check(self) -> None:
        """Queued handlers don't increment Direct emit depth — no RecursionError.

        Verifies that the _emit_depth ContextVar defaults to 0 and that
        enqueueing to a QUEUED handler does not increment the depth counter.
        """
        from agent_cdp._context import _emit_depth

        # Verify default is 0
        assert _emit_depth.get() == 0

        scope = EventScope('queued')
        enqueued: list[str] = []

        def queued_handler(event: LogNavEvent) -> None:
            enqueued.append('called')

        scope.connect(LogNavEvent, queued_handler, mode=ConnectionType.QUEUED)
        scope.emit(LogNavEvent())

        # After emit, depth should have been reset back to 0
        assert _emit_depth.get() == 0


class TestDepthLimitConfigurable:
    def test_depth_limit_configurable(self) -> None:
        """Patching _MAX_DIRECT_DEPTH to 3 causes RecursionError at depth 3."""
        scope = EventScope('depth_cfg')
        depth_reached: list[int] = []

        def chain_handler(event: LogNavEvent) -> None:
            from agent_cdp._context import _emit_depth

            depth_reached.append(_emit_depth.get())
            scope.emit(LogNavEvent())

        scope.connect(LogNavEvent, chain_handler, mode=ConnectionType.DIRECT)

        with patch('agent_cdp.scope.scope._MAX_DIRECT_DEPTH', 3):
            with pytest.raises(RecursionError, match='depth'):
                scope.emit(LogNavEvent())

        # Should have reached depths 1, 2, 3 before error at depth 3
        assert len(depth_reached) == 3


# ── Per-scope event history ──


class TestScopeEventHistoryRecords:
    def test_scope_event_history_records(self) -> None:
        """Emit 3 events, scope.event_history returns list of 3 with matching event_ids."""
        scope = EventScope('hist')
        events = [LogNavEvent(), LogNavEvent(), LogNavEvent()]

        for e in events:
            scope.emit(e)

        history = scope.event_history
        assert len(history) == 3
        assert [h.event_id for h in history] == [e.event_id for e in events]


class TestEventHistoryMaxSize:
    def test_event_history_max_size(self) -> None:
        """max_history_size=5, emit 8 events, history has only the last 5."""
        scope = EventScope('hist_max', max_history_size=5)
        events = [LogNavEvent() for _ in range(8)]

        for e in events:
            scope.emit(e)

        history = scope.event_history
        assert len(history) == 5
        assert [h.event_id for h in history] == [e.event_id for e in events[-5:]]
