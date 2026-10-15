"""Regression acceptance for admission, serial cancellation and owned lifecycle."""

from __future__ import annotations

import asyncio
import gc
import time
import weakref
from dataclasses import FrozenInstanceError

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.events.base import EventTimeoutError, HandlerTimeoutError, QueueFullError, SelfWaitError
from agent_cdp.events.result import ResultStatus
from agent_cdp.scope import EventScope, ScopeGroup


class RuntimeEvent(BaseEvent[object]):
    __registry_key__ = 'scope_runtime_contract'
    value: int = 0


async def test_explicit_capacity_metadata_and_zero_history() -> None:
    group = ScopeGroup('public-config')
    scope = await group.create_scope('large', queue_maxsize=65536, max_history_size=0, metadata={'tab': 'a'})
    assert scope.queue_maxsize == 65536
    assert scope.metadata == {'tab': 'a'}
    scope.emit(RuntimeEvent())
    assert scope.event_history == []
    with pytest.raises(TypeError):
        EventScope('bad', queue_max_size=3)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await group.create_scope('bad', queue_max_size=3)  # type: ignore[call-arg]
    await group.close_all()


async def test_capacity_rejection_and_no_drop_partial_facts() -> None:
    source = EventScope('source')
    first = EventScope('first', queue_maxsize=1)
    second = EventScope('second', queue_maxsize=0)

    async def handler(event: RuntimeEvent) -> int:
        return event.value

    c1 = source.connect(RuntimeEvent, handler, target_scope=first)
    c2 = source.connect(RuntimeEvent, handler, target_scope=second)
    source.emit(RuntimeEvent())
    event = RuntimeEvent(non_droppable=True)
    with pytest.raises(QueueFullError) as caught:
        source.emit(event)
    assert caught.value.connection_id == c1.id
    assert event.results[c1.id].status is ResultStatus.REJECTED
    assert event.results[c2.id].status is ResultStatus.QUEUED
    assert first.stats.rejected_by_connection[c1.id] == 1
    assert first.stats.rejected_by_event_type['RuntimeEvent'] == 1
    assert second.stats.accepted == 2
    with pytest.raises(FrozenInstanceError):
        first.stats.accepted = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.stats.rejected_by_connection[c1.id] = 0  # type: ignore[index]
    await asyncio.gather(source.close(), first.close(), second.close())


async def test_cancel_queued_settles_and_frees_capacity() -> None:
    scope = EventScope('cancel-before-start', queue_maxsize=1)
    calls: list[int] = []

    async def handler(event: RuntimeEvent) -> None:
        calls.append(event.value)

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    cancelled = scope.emit(RuntimeEvent(value=1))
    cancelled.cancel_execution(include_running=False)
    assert cancelled.results[conn.id].status is ResultStatus.CANCELLED
    assert scope.stats.queued == 0
    later = scope.emit(RuntimeEvent(value=2))
    await scope.start()
    await later
    assert calls == [2]
    assert scope.stats.cancelled == 1
    await scope.close()


async def test_ignoring_cancellation_retains_serial_execution_and_close_refs() -> None:
    scope = EventScope('stubborn', slow_handler_threshold=None)
    started = asyncio.Event()
    interrupted = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def handler(event: RuntimeEvent) -> int:
        calls.append(event.value)
        if event.value == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                await release.wait()
        return event.value

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    first = scope.emit(RuntimeEvent(value=1))
    second = scope.emit(RuntimeEvent(value=2))
    await started.wait()
    first.cancel_execution()
    await interrupted.wait()
    await asyncio.sleep(0)
    assert calls == [1]
    assert first.results[conn.id].status is ResultStatus.RUNNING
    # A repeat explicit cancel is idempotent. The child stays supervised.
    first.cancel_execution()
    release.set()
    await first
    await second
    assert first.results[conn.id].status is ResultStatus.COMPLETED
    assert first.results[conn.id].cancel_requested
    assert calls == [1, 2]
    await scope.close()


async def test_immediate_close_returns_incomplete_without_orphaning_handler() -> None:
    group = ScopeGroup('close-stubborn')
    scope = await group.create_scope('s')
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(event: RuntimeEvent) -> None:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    event = scope.emit(RuntimeEvent())
    await started.wait()
    began = time.monotonic()
    outcomes = await group.close_all(graceful=False, timeout=0.02)
    assert time.monotonic() - began < 0.2
    assert not outcomes['s'].complete
    assert group.get_scope('s') is scope
    assert not scope.is_closed and scope.closed
    assert conn.active
    assert event.results[conn.id].status is ResultStatus.RUNNING
    release.set()
    await event
    assert (await group.close_all())['s'].complete
    assert group.scope_count == 0
    assert not conn.active


async def test_handler_self_cancellation_does_not_kill_consumer() -> None:
    scope = EventScope('self-cancel')

    async def handler(event: RuntimeEvent) -> int:
        if event.value == 1:
            raise asyncio.CancelledError('own decision')
        return event.value

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    first, second = scope.emit(RuntimeEvent(value=1)), scope.emit(RuntimeEvent(value=2))
    await first
    await second
    assert first.results[conn.id].status is ResultStatus.CANCELLED
    assert second.results[conn.id].result == 2
    await scope.close()


async def test_execution_timeout_waits_for_real_exit_and_reports_diagnostics() -> None:
    scope = EventScope('execution-budget')
    interrupted, release = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    async def handler(event: RuntimeEvent) -> int:
        calls.append(event.value)
        if event.value == 1:
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                await release.wait()
        return event.value

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    first = scope.emit(RuntimeEvent(value=1, execution_timeout=0.01))
    second = scope.emit(RuntimeEvent(value=2))
    await interrupted.wait()
    assert calls == [1]
    assert first.results[conn.id].status is ResultStatus.RUNNING
    release.set()
    await first
    await second
    result = first.results[conn.id]
    assert result.status is ResultStatus.TIMEOUT
    assert isinstance(result.error, HandlerTimeoutError)
    assert result.error.pending[0].scope_id == 'execution-budget'
    assert calls == [1, 2]
    await scope.close()


async def test_flush_frozen_prefix_and_no_queue_capacity() -> None:
    scope = EventScope('prefix', queue_maxsize=1)
    release, entered, later_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(event: RuntimeEvent) -> int:
        if event.value == 1:
            entered.set()
            await release.wait()
            raise ValueError('prefix failure')
        await later_release.wait()
        return event.value

    scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    scope.emit(RuntimeEvent(value=1))
    rejected = scope.emit(RuntimeEvent(value=99))
    assert next(iter(rejected.results.values())).status is ResultStatus.REJECTED
    flushing = asyncio.create_task(scope.flush(timeout=1))
    await asyncio.sleep(0)
    await scope.start()
    await entered.wait()
    scope.emit(RuntimeEvent(value=2))
    release.set()
    result = await flushing
    assert len(result.results) == 1
    assert {failure.error_type for failure in result.failures} == {'QueueFullError', 'ValueError'}
    assert result.stats.rejected == 1
    assert scope.stats.accepted == 2
    later_release.set()
    await scope.close()


async def test_flush_timeout_and_self_wait_fail_immediately_with_monitor_disabled() -> None:
    scope = EventScope('self-flush', slow_handler_threshold=None)
    errors: list[BaseException] = []

    async def handler(event: RuntimeEvent) -> None:
        try:
            await scope.flush(timeout=1)
        except SelfWaitError as error:
            errors.append(error)

    scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    scope.emit(RuntimeEvent())
    with pytest.raises(EventTimeoutError) as caught:
        await scope.flush(timeout=0.001)
    assert caught.value.phase == 'flush'
    assert caught.value.pending[0].status is ResultStatus.QUEUED
    await scope.start()
    await scope.flush()
    assert len(errors) == 1
    assert 'target_scope' in str(errors[0])
    await scope.close()


async def test_close_all_attempts_all_resources_and_does_not_repeat_handler_errors() -> None:
    group = ScopeGroup('cleanup-errors')
    first, second = await group.create_scope('a'), await group.create_scope('b')
    cleaned: list[str] = []

    def broken_cleanup() -> None:
        cleaned.append('a')
        raise ValueError('cleanup failure')

    def okay_cleanup() -> None:
        cleaned.append('b')

    async def broken_handler(event: RuntimeEvent) -> None:
        raise RuntimeError('business failure')

    first.add_close_callback(broken_cleanup)
    second.add_close_callback(okay_cleanup)
    second.connect(RuntimeEvent, broken_handler, mode=ConnectionType.QUEUED)
    event = second.emit(RuntimeEvent())
    await event
    with pytest.raises(ExceptionGroup) as caught:
        await group.close_all()
    assert sorted(cleaned) == ['a', 'b']
    assert 'business failure' not in str(caught.value)
    assert group.scope_ids == ['a']
    first.remove_close_callback(broken_cleanup)
    await group.close_all()
    assert group.scope_count == 0


async def test_idle_close_fast_and_completed_payload_not_retained_by_timer() -> None:
    scope = EventScope('memory', max_history_size=0)

    class Payload:
        pass

    async def handler(event: RuntimeEvent) -> object:
        return Payload()

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    event = scope.emit(RuntimeEvent(execution_timeout=3600))
    await event
    result_ref = weakref.ref(event.results[conn.id].result)
    event_ref = weakref.ref(event)
    del event
    for _ in range(3):
        await asyncio.sleep(0)
    gc.collect()
    assert event_ref() is None
    assert result_ref() is None
    began = time.monotonic()
    assert (await scope.close()).complete
    assert time.monotonic() - began < 0.1


async def test_flush_reports_previously_settled_failure_and_advances_checkpoint() -> None:
    scope = EventScope('prior-failure', max_history_size=0)

    async def handler(event: RuntimeEvent) -> None:
        raise ValueError('previously settled')

    scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    event = scope.emit(RuntimeEvent())
    await event
    del event
    flushed = await scope.flush()
    assert flushed.results == ()
    assert len(flushed.failures) == 1
    assert flushed.failures[0].error_message == 'previously settled'
    assert flushed.failures[0].event_type == 'RuntimeEvent'
    assert (await scope.flush()).deliveries == ()
    assert scope.stats.failed == 1
    await scope.close()


async def test_timeout_preserves_post_cancel_exception_as_cause() -> None:
    scope = EventScope('timeout-cause')
    failure = ValueError('failed during cancellation')

    async def handler(event: RuntimeEvent) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise failure

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    event = scope.emit(RuntimeEvent(execution_timeout=0.001))
    await event
    assert event.results[conn.id].status is ResultStatus.TIMEOUT
    error = event.results[conn.id].error
    assert isinstance(error, HandlerTimeoutError)
    assert error.__cause__ is failure
    await scope.close()


async def test_close_waiting_for_another_close_observes_own_budget() -> None:
    scope = EventScope('concurrent-close')
    entered, release = asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        entered.set()
        await release.wait()

    scope.add_close_callback(cleanup)
    first = asyncio.create_task(scope.close(timeout=1))
    await entered.wait()
    started = time.monotonic()
    second = await scope.close(timeout=0.01)
    assert time.monotonic() - started < 0.1
    assert not second.complete
    release.set()
    assert (await first).complete


async def test_failed_cleanup_stays_owned_until_retry_succeeds() -> None:
    scope = EventScope('retry-resource')
    attempts = 0

    async def cleanup() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError('retry me')

    scope.add_close_callback(cleanup)
    with pytest.raises(ExceptionGroup):
        await scope.close()
    assert not scope.is_closed
    assert (await scope.close()).complete
    assert attempts == 2


async def test_nested_scope_workers_do_not_retain_parent_event_context() -> None:
    parent = EventScope('parent', max_history_size=0)
    group = ScopeGroup('nested')

    async def handler(event: RuntimeEvent) -> None:
        await group.create_scope('child', max_history_size=0)

    parent.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await parent.start()
    event = parent.emit(RuntimeEvent())
    reference = weakref.ref(event)
    await event
    del event
    for _ in range(3):
        await asyncio.sleep(0)
    gc.collect()
    assert reference() is None
    await group.close_all()
    await parent.close()


async def test_same_named_independent_scopes_do_not_trigger_false_self_wait() -> None:
    source, target = EventScope('same'), EventScope('same')

    async def target_handler(event: RuntimeEvent) -> int:
        return 42

    async def source_handler(event: RuntimeEvent) -> object:
        target_event = target.emit(RuntimeEvent())
        await target.flush(timeout=0.2)
        await target_event
        return 'done'

    source.connect(RuntimeEvent, source_handler, mode=ConnectionType.QUEUED)
    target.connect(RuntimeEvent, target_handler, mode=ConnectionType.QUEUED)
    await asyncio.gather(source.start(), target.start())
    event = source.emit(RuntimeEvent())
    await event
    assert next(iter(event.results.values())).is_success
    await asyncio.gather(source.close(), target.close())


async def test_inherited_context_after_handler_exit_does_not_trigger_self_wait() -> None:
    scope = EventScope('inherited-context')
    release = asyncio.Event()
    children: list[asyncio.Task[None]] = []

    async def later() -> None:
        await release.wait()
        child = scope.emit(RuntimeEvent(value=2))
        await scope.flush(timeout=0.2)
        await child

    async def handler(event: RuntimeEvent) -> int:
        if event.value == 1:
            children.append(asyncio.create_task(later()))
        return event.value

    scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    event = scope.emit(RuntimeEvent(value=1))
    await event
    release.set()
    await children[0]
    await scope.close()


async def test_external_consumer_cancellation_remains_lifecycle_event() -> None:
    scope = EventScope('external-consumer')
    started, interrupted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(event: RuntimeEvent) -> int:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            interrupted.set()
            await release.wait()
        return event.value

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    first = scope.emit(RuntimeEvent(value=1))
    second = scope.emit(RuntimeEvent(value=2))
    await started.wait()
    consumer = scope._event_loop._task
    assert consumer is not None
    consumer.cancel()
    await interrupted.wait()
    assert not consumer.done()
    assert first.results[conn.id].status is ResultStatus.RUNNING
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert first.results[conn.id].status is ResultStatus.COMPLETED
    assert first.results[conn.id].cancel_requested
    assert second.results[conn.id].cancelled_before_start
    assert (await scope.close()).complete


async def test_original_handler_cancellation_reason_is_preserved() -> None:
    scope = EventScope('cancel-original')
    original = asyncio.CancelledError('handler decision')

    async def handler(event: RuntimeEvent) -> None:
        raise original

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    event = scope.emit(RuntimeEvent())
    await event
    assert event.results[conn.id].error is original
    await scope.close()


async def test_repeated_close_does_not_interrupt_in_progress_cancellation_cleanup() -> None:
    scope = EventScope('repeat-cancel')
    started, interrupted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(event: RuntimeEvent) -> int:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            await release.wait()
        return 1

    conn = scope.connect(RuntimeEvent, handler, mode=ConnectionType.QUEUED)
    await scope.start()
    event = scope.emit(RuntimeEvent())
    await started.wait()
    assert not (await scope.close(graceful=False, timeout=0.001)).complete
    await interrupted.wait()
    assert not (await scope.close(graceful=False, timeout=0.001)).complete
    assert event.results[conn.id].status is ResultStatus.RUNNING
    release.set()
    await event
    assert event.results[conn.id].result == 1
    assert (await scope.close()).complete
