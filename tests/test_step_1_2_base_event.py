"""Tests for BaseEvent[T] — the generic event base class (C3).

TDD: These tests are written BEFORE the implementation.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from agent_cdp._registry import EventRegistrar
from agent_cdp.events import BaseEvent, EmitPolicy
from agent_cdp.events.result import EventResult, ResultStatus

# ── Test event subclasses ──


class SampleEvent(BaseEvent[str]):
    payload: str = 'default'


class AnotherEvent(BaseEvent[int]):
    value: int = 0


class AbstractMiddleEvent(BaseEvent[None]):
    __abstract__ = True


class ConcreteChildEvent(AbstractMiddleEvent):
    tag: str = 'child'


# ── Test groups ──


class TestCreateEventDefaultFields:
    """Default field values: event_id auto-generated, consumed=False, timeout=300, etc."""

    def test_event_id_auto_generated(self) -> None:
        e = SampleEvent()
        assert e.event_id
        assert isinstance(e.event_id, str)

    def test_event_ids_are_unique(self) -> None:
        e1 = SampleEvent()
        e2 = SampleEvent()
        assert e1.event_id != e2.event_id

    def test_consumed_default_false(self) -> None:
        e = SampleEvent()
        assert e.consumed is False

    def test_event_results_empty(self) -> None:
        e = SampleEvent()
        assert e.event_results == {}
        assert isinstance(e.event_results, dict)

    def test_timeout_default_300(self) -> None:
        e = SampleEvent()
        assert e.event_timeout == 300.0

    def test_parent_id_default_none(self) -> None:
        e = SampleEvent()
        assert e.event_parent_id is None

    def test_has_pending_default_false(self) -> None:
        e = SampleEvent()
        assert e.has_pending is False

    def test_emit_policy_default_fail_fast(self) -> None:
        assert SampleEvent.emit_policy is EmitPolicy.FAIL_FAST


class TestConsumeSetsFlag:
    """consume() sets consumed=True; idempotent."""

    def test_consume_sets_true(self) -> None:
        e = SampleEvent()
        e.consume()
        assert e.consumed is True

    def test_consume_is_idempotent(self) -> None:
        e = SampleEvent()
        e.consume()
        e.consume()
        assert e.consumed is True


class TestRecordResultStoresInDict:
    """record_result stores by connection_id; multiple records; status=COMPLETED."""

    def test_stores_by_connection_id(self) -> None:
        e = SampleEvent()
        e.record_result(connection_id='c1', handler_name='handler_a', result='ok')
        assert 'c1' in e.event_results
        r = e.event_results['c1']
        assert isinstance(r, EventResult)
        assert r.status is ResultStatus.COMPLETED
        assert r.result == 'ok'
        assert r.handler_name == 'handler_a'

    def test_multiple_records(self) -> None:
        e = SampleEvent()
        e.record_result(connection_id='c1', handler_name='h1', result='r1')
        e.record_result(connection_id='c2', handler_name='h2', result='r2')
        assert len(e.event_results) == 2
        assert e.event_results['c1'].result == 'r1'
        assert e.event_results['c2'].result == 'r2'

    def test_connection_id_stored(self) -> None:
        e = SampleEvent()
        e.record_result(connection_id='conn-xyz', handler_name='h', result='v')
        assert e.event_results['conn-xyz'].connection_id == 'conn-xyz'


class TestRecordResultWithError:
    """error → FAILED; TimeoutError → TIMEOUT."""

    def test_error_records_failed_status(self) -> None:
        e = SampleEvent()
        err = ValueError('boom')
        e.record_result(connection_id='c1', handler_name='h', error=err)
        r = e.event_results['c1']
        assert r.status is ResultStatus.FAILED
        assert r.error is err

    def test_timeout_error_records_timeout_status(self) -> None:
        e = SampleEvent()
        err = TimeoutError('too slow')
        e.record_result(connection_id='c1', handler_name='h', error=err)
        r = e.event_results['c1']
        assert r.status is ResultStatus.TIMEOUT
        assert r.error is err


class TestAwaitCompletesWhenNoPending:
    """pending=0 → await returns immediately."""

    @pytest.mark.asyncio
    async def test_await_immediate_when_no_pending(self) -> None:
        e = SampleEvent()
        # Should return immediately, not hang
        await asyncio.wait_for(e, timeout=0.1)


class TestAwaitBlocksUntilPendingZero:
    """pending=2 → decrement twice → completion; increment clears completion; decrement-to-zero sets completion."""

    @pytest.mark.asyncio
    async def test_increment_blocks_await(self) -> None:
        e = SampleEvent()
        e._increment_pending()
        assert e.has_pending is True

        # Should not complete within timeout
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(e, timeout=0.05)

    @pytest.mark.asyncio
    async def test_decrement_to_zero_completes(self) -> None:
        e = SampleEvent()
        e._increment_pending()
        e._increment_pending()
        assert e._pending_count == 2

        e._decrement_pending()
        assert e._pending_count == 1
        assert e.has_pending is True

        e._decrement_pending()
        assert e._pending_count == 0
        assert e.has_pending is False

        # Now await should complete
        await asyncio.wait_for(e, timeout=0.1)

    def test_decrement_clamps_at_zero(self) -> None:
        e = SampleEvent()
        e._decrement_pending()  # should not go below 0
        assert e._pending_count == 0

    @pytest.mark.asyncio
    async def test_deepcopy_has_independent_completion(self) -> None:
        e = SampleEvent()
        e._increment_pending()  # blocked

        e2 = copy.deepcopy(e)
        # Deepcopy should get an independent, set completion event
        assert e2._pending_count == 0
        await asyncio.wait_for(e2, timeout=0.1)

        # Original still blocked
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(e, timeout=0.05)


class TestParentIdTracking:
    """Manual parent_id setting; default None."""

    def test_default_parent_id_none(self) -> None:
        e = SampleEvent()
        assert e.event_parent_id is None

    def test_set_parent_id(self) -> None:
        parent = SampleEvent()
        child = SampleEvent(event_parent_id=parent.event_id)
        assert child.event_parent_id == parent.event_id


class TestEventSubclassAutoRegisters:
    """Concrete classes registered; abstract classes not; abstract's concrete children registered."""

    def test_concrete_class_registered(self) -> None:
        keys = EventRegistrar.keys()
        assert 'sample' in keys
        assert 'another' in keys

    def test_abstract_class_not_registered(self) -> None:
        keys = EventRegistrar.keys()
        assert 'abstract_middle' not in keys

    def test_concrete_child_of_abstract_registered(self) -> None:
        keys = EventRegistrar.keys()
        assert 'concrete_child' in keys


class TestEventRegistrarLookup:
    """get('sample') → SampleEvent; get('another') → AnotherEvent; get(unknown) raises."""

    def test_lookup_sample(self) -> None:
        assert EventRegistrar.get('sample') is SampleEvent

    def test_lookup_another(self) -> None:
        assert EventRegistrar.get('another') is AnotherEvent

    def test_lookup_unknown_raises(self) -> None:
        with pytest.raises(KeyError):
            EventRegistrar.get('nonexistent_event_type')
