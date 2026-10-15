"""Serial queued dispatch with bounded admission and cooperative cancellation."""

from __future__ import annotations

# Internal integration points intentionally share event lifecycle methods.
# pyright: reportPrivateUsage=false
import asyncio
import inspect
import logging
import time
from collections import Counter
from collections.abc import Mapping
from contextvars import Context
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from agent_cdp._context import _current_connection, _current_event, _current_scope_id, _current_scope_token
from agent_cdp.events.base import EventTimeoutError, HandlerTimeoutError, QueueFullError, SelfWaitError
from agent_cdp.events.result import DeliveryDiagnostic, EventResult, ResultStatus
from agent_cdp.scope._helpers import get_handler_name

if TYPE_CHECKING:
    from agent_cdp.connection.connection import Connection
    from agent_cdp.events.base import BaseEvent

logger = logging.getLogger(__name__)
DEFAULT_QUEUE_MAXSIZE = 1024
DEFAULT_DEADLOCK_SCAN_INTERVAL = 5.0
DEFAULT_DEADLOCK_THRESHOLD = 15.0


@dataclass(frozen=True)
class ScopeStats:
    """Delivery counters and timing totals; contains no event or result objects."""

    queued: int
    running: int
    queue_peak: int
    accepted: int
    completed: int
    failed: int
    cancelled: int
    rejected: int
    queue_wait_seconds: float
    execution_seconds: float
    queue_wait_max_seconds: float
    execution_max_seconds: float
    rejected_by_event_type: Mapping[str, int]
    rejected_by_connection: Mapping[str, int]
    rejected_by_reason: Mapping[str, int]


@dataclass(frozen=True)
class DeliverySummary:
    """A payload-free settled delivery fact retained until a successful flush.

    Error descriptions are capped at 512 characters. The caller-owned event keeps
    the original exception and complete business result.
    """

    event_id: str
    event_type: str
    connection_id: str
    handler_name: str
    source_scope_id: str
    scope_id: str
    status: ResultStatus
    cancel_requested: bool
    is_success: bool
    queue_duration: float
    execution_duration: float
    error_type: str | None
    error_message: str | None

    @classmethod
    def from_result(cls, result: EventResult[Any]) -> DeliverySummary:
        error = result.business_error or result.error
        return cls(
            result.event_id,
            result.event_type,
            result.connection_id,
            result.handler_name,
            result.source_scope_id,
            result.scope_id,
            result.status,
            result.cancel_requested,
            result.is_success,
            result.queue_duration,
            result.execution_duration,
            type(error).__name__ if error is not None else None,
            str(error)[:512] if error is not None else None,
        )


@dataclass(frozen=True)
class FlushResult:
    """The active prefix captured by flush, with cumulative delivery statistics.

    ``deliveries`` includes all settled facts since the preceding successful
    flush checkpoint plus this call's outstanding prefix. ``results`` contains
    payloads only for that outstanding prefix. Cumulative ``stats`` never reset.
    Concurrent barriers each capture their own checkpoint; later input is excluded.
    """

    results: tuple[EventResult[Any], ...]
    stats: ScopeStats
    deliveries: tuple[DeliverySummary, ...] = ()

    @property
    def failures(self) -> tuple[DeliverySummary, ...]:
        return tuple(result for result in self.deliveries if not result.is_success)


@dataclass(frozen=True)
class CloseResult:
    complete: bool
    pending: tuple[DeliveryDiagnostic, ...] = ()
    pending_resources: int = 0


@dataclass
class _Delivery:
    sequence: int
    event: BaseEvent[Any]
    connection: Connection
    admitted_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: EventResult[Any] | None = None
    task: asyncio.Task[Any] | None = None
    timeout_error: HandlerTimeoutError | None = None
    cancel_sent: bool = False


class ScopeEventLoop:
    """One consumer; an interrupted handler must actually exit before the next runs."""

    def __init__(
        self,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        *,
        scope_id: str = '',
        deadlock_scan_interval: float = DEFAULT_DEADLOCK_SCAN_INTERVAL,
        deadlock_threshold: float | None = DEFAULT_DEADLOCK_THRESHOLD,
    ) -> None:
        if maxsize < 0:
            raise ValueError('queue_maxsize must be >= 0')
        if deadlock_scan_interval <= 0:
            raise ValueError('slow_handler_check_interval must be > 0')
        if deadlock_threshold is not None and deadlock_threshold < 0:
            raise ValueError('slow_handler_threshold must be >= 0 or None')
        self.scope_id = scope_id
        self._queue: asyncio.Queue[_Delivery] = asyncio.Queue(maxsize=maxsize)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._stopping = False
        self._pending: dict[int, _Delivery] = {}
        self._sequence = 0
        self._settled_since_flush: dict[int, DeliverySummary] = {}
        self._active: _Delivery | None = None
        self._accepted = self._completed = self._failed = self._cancelled = self._rejected = 0
        self._queue_peak = 0
        self._queue_wait = self._execution_time = self._queue_wait_max = self._execution_max = 0.0
        self._rejected_types: Counter[str] = Counter()
        self._rejected_connections: Counter[str] = Counter()
        self._rejected_reasons: Counter[str] = Counter()
        self._active_handlers: dict[int, tuple[str, float]] = {}
        self._handler_id_counter = 0
        self._warned_handlers: set[int] = set()
        self._monitor_task: asyncio.Task[None] | None = None
        self._deadlock_scan_interval = deadlock_scan_interval
        self._deadlock_threshold = deadlock_threshold

    @property
    def stats(self) -> ScopeStats:
        return ScopeStats(
            queued=self._queue.qsize(),
            running=int(self._active is not None),
            queue_peak=self._queue_peak,
            accepted=self._accepted,
            completed=self._completed,
            failed=self._failed,
            cancelled=self._cancelled,
            rejected=self._rejected,
            queue_wait_seconds=self._queue_wait,
            execution_seconds=self._execution_time,
            queue_wait_max_seconds=self._queue_wait_max,
            execution_max_seconds=self._execution_max,
            rejected_by_event_type=MappingProxyType(dict(self._rejected_types)),
            rejected_by_connection=MappingProxyType(dict(self._rejected_connections)),
            rejected_by_reason=MappingProxyType(dict(self._rejected_reasons)),
        )

    @property
    def stopped(self) -> bool:
        return not self._pending and (self._task is None or self._task.done())

    @property
    def pending_diagnostics(self) -> tuple[DeliveryDiagnostic, ...]:
        return tuple(
            DeliveryDiagnostic.from_result(d.event.event_results[d.connection.id]) for d in self._pending.values()
        )

    async def start(self) -> None:
        if self._running:
            return
        if self._task is not None and not self._task.done():
            raise RuntimeError('Cannot restart a scope while its previous handler is still running')
        self._stopping = False
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f'agent-cdp:{self.scope_id}', context=Context())
        if self._deadlock_threshold is not None:
            self._monitor_task = asyncio.create_task(self._deadlock_monitor(), context=Context())

    async def stop(self, *, drain: bool = True, timeout: float = 10.0) -> bool:
        if timeout < 0:
            raise ValueError('close timeout must be >= 0')
        if self._pending and self._task is None and drain:
            await self.start()
        self._stopping = True
        self._running = False
        self._wake.set()
        if not drain:
            self._cancel_remaining()
        task = self._task
        if task is not None and not task.done() and timeout > 0:
            await asyncio.wait({task}, timeout=timeout)
        if task is not None and not task.done():
            self._cancel_remaining()
            return False
        if self._pending:
            self._cancel_remaining()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        if task is not None:
            # A cancelled consumer is a lifecycle event, not a handler business error.
            if not task.cancelled():
                task.result()
            self._task = None
        return not self._pending

    def enqueue(self, event: BaseEvent[Any], connection: Connection) -> Exception | None:
        """Admit without blocking; record every rejection as a delivery fact."""
        if connection.id not in event.event_results:
            event._prepare_delivery(connection, self.scope_id, queued=True)
        event._set_delivery_scope(connection.id, self)
        self._sequence += 1
        if self._queue.full() or self._stopping:
            error = (
                RuntimeError(f'Scope {self.scope_id!r} no longer accepts queued deliveries')
                if self._stopping
                else QueueFullError(event.event_id, connection.id, self.scope_id)
            )
            self._rejected_reasons['scope_stopped' if self._stopping else 'queue_full'] += 1
            event.record_result(
                connection_id=connection.id,
                handler_name=get_handler_name(connection.handler),
                error=error,
                status=ResultStatus.REJECTED,
            )
            event._decrement_pending()
            self._rejected += 1
            self._rejected_types[type(event).__name__] += 1
            self._rejected_connections[connection.id] += 1
            self._settled_since_flush[self._sequence] = DeliverySummary.from_result(event.event_results[connection.id])
            return error
        delivery = _Delivery(self._sequence, event, connection)
        self._pending[delivery.sequence] = delivery
        self._queue.put_nowait(delivery)
        self._accepted += 1
        self._queue_peak = max(self._queue_peak, self._queue.qsize())
        event._set_cancel_callback(connection.id, lambda running: self._cancel_delivery(delivery, running))
        self._wake.set()
        return None

    def direct_started(self) -> tuple[int, float]:
        self._accepted += 1
        self._sequence += 1
        return self._sequence, time.monotonic()

    def direct_finished(self, result: EventResult[Any], started: tuple[int, float]) -> None:
        sequence, began = started
        elapsed = time.monotonic() - began
        self._settled_since_flush[sequence] = DeliverySummary.from_result(result)
        self._execution_time += elapsed
        self._execution_max = max(self._execution_max, elapsed)
        self._count_result(result)

    async def flush(self, *, timeout: float | None = None) -> FlushResult:
        if timeout is not None and timeout < 0:
            raise ValueError('flush timeout must be >= 0 or None')
        prefix = tuple(self._pending.values())
        sequence = self._sequence
        settled = dict(self._settled_since_flush)
        current_event, current_connection = _current_event.get(), _current_connection.get()
        current_result = (
            current_event.event_results.get(current_connection.id)
            if current_event is not None and current_connection is not None
            else None
        )
        if (
            prefix
            and _current_scope_token.get() is self
            and current_result is not None
            and current_result.status is ResultStatus.RUNNING
        ):
            raise SelfWaitError(self.scope_id, self.pending_diagnostics)
        if not prefix:
            self._settled_since_flush.clear()
            return FlushResult((), self.stats, tuple(settled.values()))
        waits = [asyncio.create_task(delivery.done.wait()) for delivery in prefix]
        try:
            _, pending = await asyncio.wait(waits, timeout=timeout)
            if pending:
                diagnostics = tuple(
                    DeliveryDiagnostic.from_result(d.event.event_results[d.connection.id])
                    for d in prefix
                    if not d.done.is_set()
                )
                raise EventTimeoutError(
                    'scope.flush',
                    self.scope_id,
                    timeout or 0.0,
                    pending=diagnostics,
                    pending_count=len(diagnostics),
                    phase='flush',
                )
            for delivery in prefix:
                if delivery.result is not None:
                    settled[delivery.sequence] = DeliverySummary.from_result(delivery.result)
            for number in tuple(self._settled_since_flush):
                if number <= sequence:
                    self._settled_since_flush.pop(number, None)
            return FlushResult(
                tuple(d.result for d in prefix if d.result is not None),
                self.stats,
                tuple(settled[number] for number in sorted(settled)),
            )
        finally:
            for wait in waits:
                if not wait.done():
                    wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    def _cancel_delivery(self, delivery: _Delivery, include_running: bool) -> None:
        if delivery.done.is_set():
            return
        if delivery.task is not None:
            if include_running and not delivery.cancel_sent:
                delivery.cancel_sent = True
                delivery.event._mark_cancel_requested(delivery.connection.id)
                delivery.task.cancel()
            return
        delivery.event._mark_cancel_requested(delivery.connection.id)
        delivery.event.record_result(
            connection_id=delivery.connection.id,
            handler_name=get_handler_name(delivery.connection.handler),
            error=asyncio.CancelledError('Delivery cancelled before execution'),
            status=ResultStatus.CANCELLED,
        )
        self._finish(delivery)
        # Remove cancelled admissions immediately so they do not consume capacity.
        retained: list[_Delivery] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            self._queue.task_done()
            if not item.done.is_set():
                retained.append(item)
        for item in retained:
            self._queue.put_nowait(item)
        self._wake.set()

    def _cancel_remaining(self) -> None:
        for delivery in tuple(self._pending.values()):
            self._cancel_delivery(delivery, True)

    async def _run(self) -> None:
        try:
            while True:
                if self._queue.empty():
                    if self._stopping:
                        return
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                delivery = self._queue.get_nowait()
                try:
                    if not delivery.done.is_set():
                        await self._execute_handler(delivery)
                finally:
                    self._queue.task_done()
                    del delivery
        except asyncio.CancelledError:
            self._running = False
            self._stopping = True
            self._cancel_remaining()
            # Preserve the child and serial supervisor if the child ignores cancellation.
            active = self._active
            if active is not None and active.task is not None:
                await asyncio.shield(active.task)
            raise
        finally:
            self._running = False
            if self._monitor_task is not None:
                self._monitor_task.cancel()

    async def _execute_handler(self, delivery: _Delivery) -> None:
        event, conn = delivery.event, delivery.connection
        handler_name = get_handler_name(conn.handler)
        delivery.started_at = time.monotonic()
        queued_for = delivery.started_at - delivery.admitted_at
        self._queue_wait += queued_for
        self._queue_wait_max = max(self._queue_wait_max, queued_for)
        event._mark_running(conn.id)
        self._active = delivery
        handler_id = self._register_handler(handler_name)

        async def invoke() -> Any:
            event_token = _current_event.set(event)
            connection_token = _current_connection.set(conn)
            scope_id_token = _current_scope_id.set(self.scope_id)
            scope_token = _current_scope_token.set(self)
            try:
                value = conn.handler(event)
                if not inspect.isawaitable(value):
                    raise TypeError(f'Queued handler {handler_name} returned a non-awaitable; use DIRECT')
                return await value
            finally:
                _current_scope_token.reset(scope_token)
                _current_scope_id.reset(scope_id_token)
                _current_connection.reset(connection_token)
                _current_event.reset(event_token)

        task = asyncio.create_task(invoke())
        delivery.task = task
        timer: asyncio.TimerHandle | None = None
        timeout = event.execution_timeout
        if timeout is not None:

            def expire() -> None:
                if not task.done():
                    diagnostic = DeliveryDiagnostic.from_result(event.event_results[conn.id])
                    delivery.timeout_error = HandlerTimeoutError(
                        type(event).__name__,
                        event.event_id,
                        timeout,
                        pending=(diagnostic,),
                    )
                    event._mark_cancel_requested(conn.id)
                    if not delivery.cancel_sent:
                        delivery.cancel_sent = True
                        task.cancel()

            timer = asyncio.get_running_loop().call_later(timeout, expire)
        consumer_cancel: asyncio.CancelledError | None = None
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    consumer_cancel = exc
                    if not delivery.cancel_sent:
                        delivery.cancel_sent = True
                        event._mark_cancel_requested(conn.id)
                        task.cancel()
                    # External consumer cancellation must never orphan a child that
                    # suppresses cancellation or permit another consumer to start.
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
            except Exception:
                # Read the actual task outcome below, preserving its original cause.
                pass
            try:
                result = task.result()
            except asyncio.CancelledError as exc:
                timeout_error = delivery.timeout_error
                if timeout_error is not None:
                    timeout_error.__cause__ = exc
                event.record_result(
                    connection_id=conn.id,
                    handler_name=handler_name,
                    error=timeout_error or exc,
                    status=ResultStatus.TIMEOUT if timeout_error else ResultStatus.CANCELLED,
                )
            except Exception as exc:
                timeout_error = delivery.timeout_error
                if timeout_error is not None:
                    timeout_error.__cause__ = exc
                event.record_result(
                    connection_id=conn.id,
                    handler_name=handler_name,
                    error=timeout_error or exc,
                    status=ResultStatus.TIMEOUT if timeout_error else ResultStatus.FAILED,
                )
                logger.warning(
                    'Queued handler %s raised %s: %s on scope %r', handler_name, type(exc).__name__, exc, self.scope_id
                )
            else:
                event.record_result(
                    connection_id=conn.id,
                    handler_name=handler_name,
                    result=result,
                    error=delivery.timeout_error,
                    status=ResultStatus.TIMEOUT if delivery.timeout_error else None,
                )
        finally:
            if timer is not None:
                timer.cancel()
            delivery.task = None
            self._active = None
            self._unregister_handler(handler_id)
            self._finish(delivery)
        if consumer_cancel is not None:
            raise consumer_cancel

    def _finish(self, delivery: _Delivery) -> None:
        if delivery.done.is_set():
            return
        result: EventResult[Any] = delivery.event.event_results[delivery.connection.id]
        delivery.result = result
        self._settled_since_flush[delivery.sequence] = DeliverySummary.from_result(result)
        self._count_result(result)
        if delivery.started_at is not None:
            elapsed = time.monotonic() - delivery.started_at
            self._execution_time += elapsed
            self._execution_max = max(self._execution_max, elapsed)
        self._pending.pop(delivery.sequence, None)
        delivery.event._decrement_pending()
        delivery.done.set()

    def _count_result(self, result: EventResult[Any]) -> None:
        if result.status == ResultStatus.COMPLETED and result.is_success:
            self._completed += 1
        elif result.status == ResultStatus.CANCELLED:
            self._cancelled += 1
        else:
            self._failed += 1

    def _register_handler(self, handler_name: str) -> int:
        self._handler_id_counter += 1
        hid = self._handler_id_counter
        self._active_handlers[hid] = (handler_name, time.monotonic())
        return hid

    def _unregister_handler(self, handler_id: int) -> None:
        self._active_handlers.pop(handler_id, None)
        self._warned_handlers.discard(handler_id)

    async def _deadlock_monitor(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._deadlock_scan_interval)
            now = time.monotonic()
            for hid, (name, start) in tuple(self._active_handlers.items()):
                elapsed = now - start
                if (
                    self._deadlock_threshold is not None
                    and elapsed >= self._deadlock_threshold
                    and hid not in self._warned_handlers
                ):
                    self._warned_handlers.add(hid)
                    logger.warning(
                        'Handler %s has been running for %.1fs on scope %r — possible deadlock or slow operation',
                        name,
                        elapsed,
                        self.scope_id,
                    )
