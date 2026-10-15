"""Receiver contracts, independent observation and per-delivery facts."""

from __future__ import annotations

import asyncio
import copy

import pytest

from agent_cdp import ConnectionType, EventScope
from agent_cdp.events import (
    AsyncHandlerError,
    BaseEvent,
    EventExecutionError,
    EventTimeoutError,
    HandlerError,
    HandlerTimeoutError,
    Participation,
    ReceiverContractError,
    RequestContract,
    ResultContract,
    ResultContractError,
    ResultStatus,
    SelfWaitError,
    event_result,
    event_results_list,
)


class ContractEvent(BaseEvent[object]):
    pass


def test_observer_and_filtered_handler_do_not_satisfy_request_before_effects() -> None:
    scope = EventScope('contracts')
    calls = []
    scope.connect(ContractEvent, lambda event: calls.append('log'), participation=Participation.OBSERVER)
    scope.connect(
        ContractEvent, lambda event: calls.append('execute'), receiver_key='execute', filter=lambda event: False
    )
    event = ContractEvent(request_contract=RequestContract(min_receivers=1, required_receivers=('execute',)))
    with pytest.raises(ReceiverContractError) as caught:
        scope.emit(event)
    assert caught.value.event_id == event.event_id
    assert caught.value.missing_receivers == ('execute',)
    assert calls == []
    with pytest.raises(ReceiverContractError):
        event.check_success()


def test_notifications_may_have_no_receivers() -> None:
    event = EventScope('notifications').emit(ContractEvent())
    assert event.check_success() == {}


def test_all_required_connections_are_checked() -> None:
    scope = EventScope('business')
    good = scope.connect(ContractEvent, lambda event: {'ok': True}, receiver_key='first')
    bad = scope.connect(
        ContractEvent,
        lambda event: {'ok': False},
        receiver_key='second',
        result_contract=ResultContract(lambda value: value['ok']),
    )
    event = scope.emit(ContractEvent(request_contract=RequestContract(required_receivers=('first', 'second'))))
    assert event.results[good.id].is_success
    assert event.results[bad.id].status is ResultStatus.COMPLETED
    assert isinstance(event.results[bad.id].business_error, ResultContractError)
    with pytest.raises(EventExecutionError) as caught:
        event.check_success()
    assert caught.value.failures == (event.results[bad.id],)
    assert caught.value.causes == (event.results[bad.id].business_error,)


@pytest.mark.asyncio
async def test_none_business_contract_and_original_error() -> None:
    scope = EventScope('rules')
    accepts_none = scope.connect(
        ContractEvent,
        lambda event: None,
        result_contract=ResultContract(lambda value: value is None),
    )
    event = scope.emit(ContractEvent())
    assert event.results[accepts_none.id].is_success
    event.raise_for_status()
    assert await event_result(event, raise_if_none=False) is None
    accepts_none.disconnect()
    original = ValueError('business reason')

    def validate(value: object) -> bool:
        raise original

    bad = scope.connect(ContractEvent, lambda event: 'value', result_contract=ResultContract(validate))
    event = scope.emit(ContractEvent())
    with pytest.raises(HandlerError) as caught:
        await event_results_list(event)
    assert caught.value.original is original
    assert event.results[bad.id].result == 'value'
    assert event.results[bad.id].business_error is original
    assert await event_results_list(event, raise_if_any=False) == []


def test_request_and_connection_validators_both_apply() -> None:
    scope = EventScope('two-rules')
    conn = scope.connect(ContractEvent, lambda event: 4, result_contract=ResultContract(lambda value: value > 0))
    event = scope.emit(
        ContractEvent(request_contract=RequestContract(result_contract=ResultContract(lambda value: value > 5)))
    )
    assert not event.results[conn.id].is_success


def test_contract_callable_is_excluded_from_serialization_and_copied() -> None:
    event = ContractEvent(request_contract=RequestContract(result_contract=ResultContract(lambda value: True)))
    assert 'request_contract' not in event.model_dump_json()
    copied = copy.deepcopy(event)
    assert copied.request_contract == event.request_contract
    assert copied._completion is not event._completion
    assert copied._cancel_callbacks == {}


@pytest.mark.asyncio
async def test_wait_budgets_and_waiter_cancel_do_not_touch_execution() -> None:
    scope = EventScope('independent')
    await scope.start()
    gate = asyncio.Event()
    started = asyncio.Event()

    async def handler(event: ContractEvent) -> str:
        started.set()
        await gate.wait()
        return 'done'

    conn = scope.connect(ContractEvent, handler, mode=ConnectionType.QUEUED)
    event = scope.emit(ContractEvent(event_timeout=1.0, execution_timeout=2.0))
    try:
        await started.wait()
        longer = asyncio.create_task(event_results_list(event, timeout=1))
        cancelled = asyncio.create_task(event.wait(timeout=None))
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        with pytest.raises(EventTimeoutError) as caught:
            await event_result(event, timeout=0.005)
        diagnostic = caught.value.pending[0]
        assert caught.value.phase == 'wait'
        assert diagnostic.connection_id == conn.id
        assert diagnostic.event_id == event.event_id
        assert diagnostic.scope_id == 'independent'
        assert diagnostic.status is ResultStatus.RUNNING
        assert diagnostic.execution_duration > 0
        assert event.execution_timeout == 2
        assert event.event_timeout == 1
        assert not event.results[conn.id].cancel_requested
        gate.set()
        assert await longer == ['done']
    finally:
        gate.set()
        await scope.close(graceful=False)


@pytest.mark.asyncio
async def test_explicit_none_wait_overrides_event_default() -> None:
    scope = EventScope('unlimited')
    await scope.start()

    async def handler(event: ContractEvent) -> str:
        await asyncio.sleep(0.02)
        return 'done'

    scope.connect(ContractEvent, handler, mode=ConnectionType.QUEUED)
    event = scope.emit(ContractEvent(event_timeout=0.001))
    try:
        assert await event_results_list(event, timeout=None) == ['done']
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_required_wait_ignores_pending_observer() -> None:
    scope = EventScope('required')
    await scope.start()
    gate = asyncio.Event()
    scope.connect(ContractEvent, lambda event: 'handled', receiver_key='executor')

    async def observe(event: ContractEvent) -> None:
        await gate.wait()

    observer = scope.connect(ContractEvent, observe, mode=ConnectionType.QUEUED, participation=Participation.OBSERVER)
    event = scope.emit(
        ContractEvent(request_contract=RequestContract(required_receivers=('executor',), wait_for='required'))
    )
    try:
        await event.wait(timeout=0.005)
        event.check_success()
        assert not event.results[observer.id].is_terminal
        assert event.has_pending
    finally:
        gate.set()
        await scope.close()


@pytest.mark.asyncio
async def test_cancel_queued_and_running_facts_survive_and_next_delivery_runs() -> None:
    scope = EventScope('cancel')
    await scope.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handler(event: ContractEvent) -> str:
        calls.append(event.event_id)
        entered.set()
        await release.wait()
        return 'done'

    conn = scope.connect(ContractEvent, handler, mode=ConnectionType.QUEUED)
    first = scope.emit(ContractEvent())
    await entered.wait()
    second = scope.emit(ContractEvent())
    second.cancel_execution(include_running=False)
    await second
    cancelled = second.results[conn.id]
    assert cancelled.status is ResultStatus.CANCELLED
    assert cancelled.cancelled_before_start
    assert cancelled.scope_id == scope.scope_id
    first.cancel_execution()
    await first
    assert first.results[conn.id].status is ResultStatus.CANCELLED
    assert not first.results[conn.id].cancelled_before_start
    assert first.results[conn.id].cancel_requested
    release.set()
    third = scope.emit(ContractEvent())
    await third
    assert calls == [first.event_id, third.event_id]
    await scope.close()


@pytest.mark.asyncio
async def test_execution_timeout_uses_same_diagnostics_but_different_phase() -> None:
    scope = EventScope('execution-budget')
    await scope.start()

    async def slow(event: ContractEvent) -> None:
        await asyncio.Event().wait()

    conn = scope.connect(ContractEvent, slow, mode=ConnectionType.QUEUED)
    event = scope.emit(ContractEvent(execution_timeout=0.005, event_timeout=1))
    try:
        await event
        record = event.results[conn.id]
        assert record.status is ResultStatus.TIMEOUT
        assert isinstance(record.error, HandlerTimeoutError)
        assert record.error.phase == 'execution'
        assert record.error.pending[0].scope_id == scope.scope_id
        assert record.execution_duration >= 0.005
        assert record.completed_at is not None
    finally:
        await scope.close(graceful=False)


@pytest.mark.asyncio
async def test_self_wait_is_immediate_even_when_slow_warnings_disabled() -> None:
    scope = EventScope('self-wait', slow_handler_threshold=None)
    await scope.start()
    caught = []

    class ChildContractEvent(BaseEvent[None]):
        pass

    async def child(event: ChildContractEvent) -> None:
        pass

    async def parent(event: ContractEvent) -> None:
        child_event = scope.emit(ChildContractEvent())
        try:
            await child_event.wait(timeout=20)
        except SelfWaitError as error:
            caught.append(error)

    scope.connect(ChildContractEvent, child, mode=ConnectionType.QUEUED)
    scope.connect(ContractEvent, parent, mode=ConnectionType.QUEUED)
    try:
        await scope.emit_and_wait(ContractEvent(), timeout=0.1)
        await scope.flush(timeout=0.1)
        assert len(caught) == 1
        assert 'target_scope' in str(caught[0])
        assert caught[0].scope_id == scope.scope_id
    finally:
        await scope.close(graceful=False)


def test_obvious_mode_mismatches_fail_at_connection_and_dynamic_shapes_defer() -> None:
    scope = EventScope('shape')

    async def asynchronous(event: ContractEvent) -> None:
        pass

    def synchronous(event: ContractEvent) -> int:
        return 1

    with pytest.raises(AsyncHandlerError):
        scope.connect(ContractEvent, asynchronous, mode=ConnectionType.DIRECT)
    with pytest.raises(AsyncHandlerError):
        scope.connect_all(asynchronous, mode=ConnectionType.DIRECT)
    with pytest.raises(AsyncHandlerError):
        scope.connect(ContractEvent, synchronous, mode=ConnectionType.QUEUED)
    # Unknown synchronous return shape is checked when actually invoked.
    scope.connect(ContractEvent, lambda event: asynchronous(event), mode=ConnectionType.QUEUED)


@pytest.mark.asyncio
async def test_request_aggregates_required_execution_values_only() -> None:
    scope = EventScope('result-selection')
    scope.connect(ContractEvent, lambda event: 'observation', participation=Participation.OBSERVER, priority=30)
    scope.connect(ContractEvent, lambda event: 'permission', receiver_key='permission', priority=20)
    scope.connect(ContractEvent, lambda event: None, receiver_key='execute')
    event = scope.emit(ContractEvent(request_contract=RequestContract(required_receivers=('execute',))))
    assert await event_result(event, raise_if_none=False) is None
    assert await event_results_list(event) == []
    assert len(event.results) == 3


@pytest.mark.asyncio
async def test_free_connect_rejects_closed_target_without_source_leak() -> None:
    from agent_cdp import connect

    source = EventScope('source')
    target = EventScope('target')
    await target.close()

    async def handler(event: ContractEvent) -> None:
        pass

    with pytest.raises(RuntimeError, match='closed'):
        connect(source, ContractEvent, handler, target_scope=target)
    assert source.emit(ContractEvent()).results == {}
    await source.close()


@pytest.mark.asyncio
async def test_validator_self_cancellation_preserves_cause_and_consumer_continues() -> None:
    scope = EventScope('validator-cancel')
    await scope.start()
    cancellation = asyncio.CancelledError('validator cancelled its result')
    calls = []

    async def handler(event: ContractEvent) -> int:
        calls.append(event.event_id)
        return len(calls)

    def validate(value: object) -> bool:
        if value == 1:
            raise cancellation
        return True

    connection = scope.connect(
        ContractEvent, handler, mode=ConnectionType.QUEUED, result_contract=ResultContract(validate)
    )
    first = scope.emit(ContractEvent())
    second = scope.emit(ContractEvent())
    try:
        await first.wait(timeout=1)
        await second.wait(timeout=1)
        record = first.results[connection.id]
        assert record.status is ResultStatus.CANCELLED
        assert record.error is cancellation
        assert record.business_error is cancellation
        assert record.result == 1
        assert record.completed_at is not None
        assert second.results[connection.id].is_success
        assert calls == [first.event_id, second.event_id]
        with pytest.raises(EventExecutionError) as caught:
            first.check_success()
        assert caught.value.causes == (cancellation,)
    finally:
        await scope.close(graceful=False)


@pytest.mark.asyncio
async def test_repeated_pending_event_is_rejected_before_new_receiver_effects() -> None:
    scope = EventScope('duplicate-pending')
    await scope.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def queued(event: ContractEvent) -> str:
        entered.set()
        await release.wait()
        calls.append('original')
        return 'completed once'

    original = scope.connect(ContractEvent, queued, mode=ConnectionType.QUEUED)
    event = scope.emit(ContractEvent())
    try:
        await entered.wait()
        record = event.results[original.id]
        # A new earlier receiver must not run before the duplicate later receiver
        # is discovered. The entire applicable dispatch is checked first.
        added = scope.connect(ContractEvent, lambda event: calls.append('added'), priority=100)
        with pytest.raises(ValueError, match='fresh event instance') as caught:
            scope.emit(event)
        assert original.id in str(caught.value)
        assert event.results[original.id] is record
        assert added.id not in event.results
        assert scope.event_history == [event]
        assert scope.stats.accepted == 1
        release.set()
        await event.wait(timeout=1)
        assert calls == ['original']
        assert event.results[original.id].result == 'completed once'
        assert event.results[original.id].is_success
    finally:
        release.set()
        await scope.close(graceful=False)


def test_repeated_completed_event_preserves_result_and_history() -> None:
    scope = EventScope('duplicate-completed')
    calls = []
    connection = scope.connect(ContractEvent, lambda event: calls.append(event.event_id))
    event = scope.emit(ContractEvent())
    result = event.results[connection.id]
    with pytest.raises(ValueError, match='copy the payload'):
        scope.emit(event)
    assert calls == [event.event_id]
    assert event.results[connection.id] is result
    assert scope.event_history == [event]
    fresh = ContractEvent.model_validate(event.model_dump(exclude={'event_id', 'event_results'}))
    scope.emit(fresh)
    assert calls == [event.event_id, fresh.event_id]
    assert fresh.event_id != event.event_id


@pytest.mark.asyncio
async def test_same_event_can_reach_distinct_connections_from_distinct_scopes() -> None:
    first = EventScope('first-source')
    second = EventScope('second-source')
    target = EventScope('shared-target')
    await target.start()
    calls = []

    async def handler(event: ContractEvent) -> str:
        calls.append(event.event_id)
        return 'handled'

    first_connection = first.connect(ContractEvent, handler, target_scope=target)
    second_connection = second.connect(ContractEvent, handler, target_scope=target)
    event = ContractEvent()
    try:
        first.emit(event)
        second.emit(event)
        await event.wait(timeout=1)
        assert set(event.results) == {first_connection.id, second_connection.id}
        assert all(result.is_success for result in event.results.values())
        assert {result.source_scope_id for result in event.results.values()} == {'first-source', 'second-source'}
        assert {result.scope_id for result in event.results.values()} == {'shared-target'}
        assert calls == [event.event_id, event.event_id]
        assert target.stats.accepted == 2
    finally:
        await first.close(graceful=False)
        await second.close(graceful=False)
        await target.close(graceful=False)
