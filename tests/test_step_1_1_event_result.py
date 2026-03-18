"""Tests for EventResult[T] — the immutable result container (C1).

TDD: These tests are written BEFORE the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_cdp.events.result import EventResult, ResultStatus


class TestCreatePendingResult:
    """Default construction produces a PENDING result with no data."""

    def test_default_status_is_pending(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        assert r.status is ResultStatus.PENDING

    def test_result_is_none(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        assert r.result is None

    def test_error_is_none(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        assert r.error is None

    def test_children_empty_tuple(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        assert r.event_children == ()

    def test_timestamps_none(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        assert r.started_at is None
        assert r.completed_at is None


class TestMarkCompleted:
    """mark_completed transitions PENDING → COMPLETED with result and timestamp."""

    def test_status_becomes_completed(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        c = r.mark_completed(result='ok')
        assert c.status is ResultStatus.COMPLETED

    def test_result_stored(self) -> None:
        r = EventResult[str](handler_name='h', connection_id='c')
        c = r.mark_completed(result='payload')
        assert c.result == 'payload'

    def test_completed_at_set(self) -> None:
        before = datetime.now(UTC)
        r = EventResult(handler_name='h', connection_id='c')
        c = r.mark_completed(result=42)
        assert c.completed_at is not None
        assert c.completed_at >= before

    def test_completed_without_result(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        c = r.mark_completed()
        assert c.status is ResultStatus.COMPLETED
        assert c.result is None


class TestMarkFailed:
    """mark_failed transitions PENDING → FAILED with error stored."""

    def test_status_becomes_failed(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        err = ValueError('boom')
        f = r.mark_failed(err)
        assert f.status is ResultStatus.FAILED

    def test_error_stored(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        err = ValueError('boom')
        f = r.mark_failed(err)
        assert f.error is err

    def test_completed_at_set(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        f = r.mark_failed(RuntimeError('x'))
        assert f.completed_at is not None


class TestMarkTimeout:
    """mark_timeout transitions PENDING → TIMEOUT with TimeoutError."""

    def test_status_becomes_timeout(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        err = TimeoutError('too slow')
        t = r.mark_timeout(err)
        assert t.status is ResultStatus.TIMEOUT

    def test_error_is_timeout_error(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        err = TimeoutError('too slow')
        t = r.mark_timeout(err)
        assert t.error is err
        assert isinstance(t.error, TimeoutError)


class TestImmutability:
    """mark_* methods return new instances; originals are unchanged; direct mutation raises."""

    def test_mark_completed_returns_new_instance(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        c = r.mark_completed(result='x')
        assert r is not c
        assert r.status is ResultStatus.PENDING
        assert c.status is ResultStatus.COMPLETED

    def test_mark_failed_returns_new_instance(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        f = r.mark_failed(ValueError('x'))
        assert r is not f
        assert r.status is ResultStatus.PENDING

    def test_direct_mutation_raises(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        with pytest.raises(AttributeError):
            r.status = ResultStatus.COMPLETED  # type: ignore[misc]


class TestTransitionGuards:
    """Only PENDING results can transition; terminal states reject mark_* calls."""

    def test_completed_cannot_mark_failed(self) -> None:
        r = EventResult(handler_name='h', connection_id='c').mark_completed(result='ok')
        with pytest.raises(ValueError, match='Cannot transition from completed to failed'):
            r.mark_failed(ValueError('nope'))

    def test_completed_cannot_mark_completed(self) -> None:
        r = EventResult(handler_name='h', connection_id='c').mark_completed()
        with pytest.raises(ValueError, match='Cannot transition from completed to completed'):
            r.mark_completed(result='again')

    def test_failed_cannot_mark_completed(self) -> None:
        r = EventResult(handler_name='h', connection_id='c').mark_failed(ValueError('x'))
        with pytest.raises(ValueError, match='Cannot transition from failed to completed'):
            r.mark_completed()

    def test_timeout_cannot_mark_failed(self) -> None:
        r = EventResult(handler_name='h', connection_id='c').mark_timeout(TimeoutError('x'))
        with pytest.raises(ValueError, match='Cannot transition from timeout to failed'):
            r.mark_failed(ValueError('y'))


class TestIsSuccessProperty:
    """is_success is True only when COMPLETED with no error."""

    def test_completed_no_error_is_success(self) -> None:
        r = EventResult(handler_name='h', connection_id='c').mark_completed(result='ok')
        assert r.is_success is True

    def test_pending_not_success(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        assert r.is_success is False

    def test_failed_not_success(self) -> None:
        r = EventResult(handler_name='h', connection_id='c').mark_failed(ValueError('x'))
        assert r.is_success is False

    def test_timeout_not_success(self) -> None:
        r = EventResult(handler_name='h', connection_id='c').mark_timeout(TimeoutError('x'))
        assert r.is_success is False


class TestAddChildEvent:
    """event_children is a tuple; adding children preserves immutability."""

    def test_default_children_empty(self) -> None:
        r = EventResult(handler_name='h', connection_id='c')
        assert r.event_children == ()
        assert isinstance(r.event_children, tuple)

    def test_children_via_construction(self) -> None:
        # Use a sentinel object to simulate a BaseEvent (forward-ref safe)
        sentinel = object()
        r = EventResult(handler_name='h', connection_id='c', event_children=(sentinel,))  # type: ignore[arg-type]
        assert len(r.event_children) == 1
        assert r.event_children[0] is sentinel

    def test_tuple_concat_preserves_original(self) -> None:
        r = EventResult(handler_name='h', connection_id='c', event_children=())
        sentinel = object()
        # Simulate adding a child via tuple concat (the pattern consumers will use)
        from dataclasses import replace

        r2 = replace(r, event_children=r.event_children + (sentinel,))  # type: ignore[arg-type]
        assert len(r.event_children) == 0  # original unchanged
        assert len(r2.event_children) == 1
