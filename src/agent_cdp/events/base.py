"""Events, independent wait budgets, execution cancellation and result checking."""

from __future__ import annotations

# Internal event/queue coordination deliberately crosses module boundaries.
# pyright: reportPrivateUsage=false
import asyncio
import copy
import functools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, Self, TypeVar

from pydantic import ConfigDict, Field, PrivateAttr

from agent_cdp._registry import EventBridge
from agent_cdp.events.contracts import Participation, ReceiverContractError, RequestContract, ResultContract
from agent_cdp.events.result import DeliveryDiagnostic, EventResult, ResultStatus

if TYPE_CHECKING:
    from agent_cdp.connection.connection import Connection

T_Result = TypeVar('T_Result')


class EmitPolicy(StrEnum):
    FAIL_FAST = 'fail_fast'
    COLLECT_ERRORS = 'collect_errors'


class _DefaultTimeout(Enum):
    DEFAULT = 'default'


DEFAULT_TIMEOUT = _DefaultTimeout.DEFAULT
WaitTimeout = float | None | _DefaultTimeout


class EventTimeoutError(TimeoutError):
    """A wait or execution budget expired, with per-delivery diagnostics.

    ``pending`` is a snapshot; the actual handler may still be running. Read
    ``event.results`` for current facts. Waiting never cancels execution.
    """

    def __init__(
        self,
        event_type: str,
        event_id: str,
        timeout: float,
        *,
        pending_count: int = 0,
        completed_handlers: list[str] | None = None,
        failed_handlers: list[str] | None = None,
        timed_out_handlers: list[str] | None = None,
        pending: tuple[DeliveryDiagnostic, ...] = (),
        phase: Literal['wait', 'execution', 'flush'] = 'wait',
    ) -> None:
        self.event_type = event_type
        self.event_id = event_id
        self.timeout = timeout
        self.phase = phase
        self.pending = pending
        self.pending_count = pending_count or len(pending)
        self.completed_handlers = completed_handlers or []
        self.failed_handlers = failed_handlers or []
        self.timed_out_handlers = timed_out_handlers or []
        parts = [f'{event_type}(id={event_id[:12]}...) {phase} timed out after {timeout:.1f}s']
        parts.append(f'{self.pending_count} handler(s) still pending')
        for label, names in (
            ('completed', self.completed_handlers),
            ('failed', self.failed_handlers),
            ('timed_out', self.timed_out_handlers),
        ):
            if names:
                parts.append(f'{label}: {names}')
        if not self.completed_handlers and not self.failed_handlers and not self.timed_out_handlers:
            parts.append('no handlers responded')
        parts.extend(
            f'{d.handler_name} connection={d.connection_id} scope={d.scope_id!r} '
            f'state={d.status} queued={d.queue_duration:.3f}s running={d.execution_duration:.3f}s'
            for d in pending
        )
        super().__init__('; '.join(parts))


class HandlerTimeoutError(EventTimeoutError):
    """Execution budget expired; distinct from caller waiting timeout."""

    def __init__(
        self, event_type: str, event_id: str, timeout: float, *, pending: tuple[DeliveryDiagnostic, ...] = ()
    ) -> None:
        super().__init__(event_type, event_id, timeout, pending=pending, phase='execution')


class SelfWaitError(RuntimeError):
    """A serial scope handler attempted to wait for its own queue."""

    def __init__(self, scope_id: str, pending: tuple[DeliveryDiagnostic, ...]) -> None:
        self.scope_id = scope_id
        self.pending = pending
        super().__init__(
            f'Scope {scope_id!r} cannot wait for its own queued/running deliveries: '
            f'{[(d.event_id, d.connection_id, d.handler_name) for d in pending]}. '
            'Use target_scope for independent execution or adjust the calling structure.'
        )


class QueueFullError(RuntimeError):
    """A specific event-to-connection delivery was rejected by a full queue."""

    def __init__(self, event_id: str, connection_id: str, scope_id: str) -> None:
        self.event_id = event_id
        self.connection_id = connection_id
        self.scope_id = scope_id
        super().__init__(f'Queue full on scope {scope_id!r}: event {event_id}, connection {connection_id} rejected')


class EventExecutionError(Exception):
    """Selected receivers did not all satisfy their execution/result contracts."""

    def __init__(self, event_id: str, failures: tuple[EventResult[Any], ...], missing: tuple[str, ...] = ()) -> None:
        self.event_id = event_id
        self.failures = failures
        self.missing = missing
        self.causes = tuple(r.business_error or r.error for r in failures if r.business_error or r.error)
        super().__init__(
            f'Event {event_id} failed result checks: '
            f'{[(r.handler_name, r.connection_id, r.scope_id, r.status) for r in failures]}; '
            f'unsettled or missing receivers: {missing}'
        )


class AsyncHandlerError(TypeError):
    """The handler shape does not match its configured dispatch mode."""


def _make_set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


class BaseEvent(EventBridge, Generic[T_Result]):  # type: ignore[reportUntypedBaseClass]
    """An event and its immutable per-connection delivery snapshots.

    ``event_timeout`` is the default waiting budget only; ``execution_timeout``
    limits each actual queued handler from its start. A waiter's explicit timeout
    never mutates either field. ``None`` disables the respective budget.
    """

    __abstract__ = True
    model_config = ConfigDict(arbitrary_types_allowed=True)

    event_id: str = Field(default_factory=lambda: _uuid7_str())
    event_timeout: float | None = Field(default=300.0, ge=0)
    execution_timeout: float | None = Field(default=300.0, ge=0)
    non_droppable: bool = False
    request_contract: RequestContract | None = Field(default=None, exclude=True)
    consumed: bool = False
    event_parent_id: str | None = None
    event_results: dict[str, Any] = Field(default_factory=dict)
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST

    _completion: asyncio.Event = PrivateAttr(default_factory=_make_set_event)
    _changed: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _pending_count: int = PrivateAttr(default=0)
    _cancel_callbacks: dict[str, Callable[[bool], None]] = PrivateAttr(
        default_factory=dict[str, Callable[[bool], None]]
    )
    _result_contracts: dict[str, tuple[ResultContract, ...]] = PrivateAttr(
        default_factory=dict[str, tuple[ResultContract, ...]]
    )
    _required_ids: set[str] = PrivateAttr(default_factory=set[str])
    _preflight_error: ReceiverContractError | None = PrivateAttr(default=None)
    _delivery_scope_tokens: dict[str, object] = PrivateAttr(default_factory=dict[str, object])

    def consume(self) -> None:
        """Stop propagation through the current scope's remaining receivers."""
        self.consumed = True

    @property
    def has_pending(self) -> bool:
        return self._pending_count > 0

    @property
    def results(self) -> Mapping[str, EventResult[T_Result]]:
        """Read-only snapshot, including partial, rejected and cancelled results."""
        return MappingProxyType(dict(self.event_results))

    def _notify_changed(self) -> None:
        changed = self._changed
        self._changed = asyncio.Event()
        changed.set()

    def _preflight(self, connections: Iterable[Connection]) -> None:
        """Validate already-filtered applicable connections before side effects."""
        handlers = [c for c in connections if c.participation is Participation.HANDLER]
        contract = self.request_contract
        self._preflight_error = None
        if contract is None:
            self._required_ids.update(c.id for c in handlers)
            return
        names = contract.required_receivers
        missing = tuple(name for name in names if not any(name in (c.id, c.receiver_key) for c in handlers))
        if missing or len(handlers) < contract.min_receivers:
            error = ReceiverContractError(
                self.event_id,
                type(self).__name__,
                missing_receivers=missing,
                required_count=contract.min_receivers,
                actual_count=len(handlers),
            )
            self._preflight_error = error
            raise error
        self._required_ids.update(
            c.id for c in handlers if not names or any(name in (c.id, c.receiver_key) for name in names)
        )

    def _prepare_delivery(self, conn: Connection, scope_id: str, *, queued: bool) -> EventResult[T_Result]:
        source = conn.source_scope()
        handler = conn.handler
        while isinstance(handler, functools.partial):
            handler = handler.func
        name = getattr(handler, '__qualname__', getattr(handler, '__name__', repr(handler)))
        now = datetime.now(UTC)
        record: EventResult[T_Result] = EventResult(
            handler_name=name,
            connection_id=conn.id,
            event_id=self.event_id,
            event_type=type(self).__name__,
            source_scope_id=source.scope_id if source is not None else '',
            scope_id=scope_id,
            participation=conn.participation,
            receiver_key=conn.receiver_key,
            status=ResultStatus.QUEUED if queued else ResultStatus.PENDING,
            queued_at=now if queued else None,
            started_at=None,
        )
        self.event_results[conn.id] = record
        rules: list[ResultContract] = []
        if conn.result_contract is not None:
            rules.append(conn.result_contract)
        request = self.request_contract
        if request is not None and request.result_contract is not None and conn.participation is Participation.HANDLER:
            rules.append(request.result_contract)
        self._result_contracts[conn.id] = tuple(rules)
        self._notify_changed()
        return record

    def _mark_running(self, connection_id: str) -> EventResult[T_Result]:
        current: EventResult[T_Result] = self.event_results[connection_id]
        if current.is_terminal:
            return current
        record = replace(current, status=ResultStatus.RUNNING, started_at=datetime.now(UTC))
        self.event_results[connection_id] = record
        self._notify_changed()
        return record

    def _mark_cancel_requested(self, connection_id: str) -> None:
        current: EventResult[T_Result] = self.event_results[connection_id]
        if not current.is_terminal:
            self.event_results[connection_id] = replace(current, cancel_requested=True)
            self._notify_changed()

    def _set_delivery_scope(self, connection_id: str, scope_token: object) -> None:
        self._delivery_scope_tokens[connection_id] = scope_token

    def _set_cancel_callback(self, connection_id: str, callback: Callable[[bool], None]) -> None:
        self._cancel_callbacks[connection_id] = callback

    def _finish(self, connection_id: str) -> None:
        self._delivery_scope_tokens.pop(connection_id, None)
        self._cancel_callbacks.pop(connection_id, None)
        self._result_contracts.pop(connection_id, None)
        self._notify_changed()

    def cancel_execution(
        self,
        *,
        include_running: bool = True,
        scope_id: str | None = None,
        connection_ids: Iterable[str] | None = None,
    ) -> Mapping[str, EventResult[T_Result]]:
        """Request cancellation of selected deliveries and return current facts.

        Queued deliveries settle immediately and will never run. Running handlers
        receive cooperative cancellation; they stay RUNNING until actual exit.
        Await ``wait()`` afterwards to observe settlement. Ordinary waits and their
        cancellation never call this method implicitly.
        """
        selected = set(connection_ids) if connection_ids is not None else None
        for cid, callback in list(self._cancel_callbacks.items()):
            record: EventResult[T_Result] = self.event_results[cid]
            if record.is_terminal or (scope_id is not None and record.scope_id != scope_id):
                continue
            if selected is not None and cid not in selected:
                continue
            running = record.status is ResultStatus.RUNNING
            if running and not include_running:
                continue
            if record.cancel_requested:
                continue
            self._mark_cancel_requested(cid)
            callback(running)
        return self.results

    def record_result(
        self,
        *,
        connection_id: str,
        handler_name: str,
        result: T_Result | None = None,
        error: BaseException | None = None,
        status: ResultStatus | None = None,
    ) -> None:
        """Settle an actual delivery, preserving identity and original causes."""
        existing: EventResult[T_Result] | None = self.event_results.get(connection_id)
        if existing is not None and existing.is_terminal:
            return
        if status is None:
            if isinstance(error, asyncio.CancelledError):
                status = ResultStatus.CANCELLED
            elif isinstance(error, TimeoutError):
                status = ResultStatus.TIMEOUT
            elif error is not None:
                status = ResultStatus.FAILED
            else:
                status = ResultStatus.COMPLETED
        business_error: BaseException | None = None
        if status is ResultStatus.COMPLETED:
            try:
                for rule in self._result_contracts.get(connection_id, ()):
                    rule.validate(result)
            except asyncio.CancelledError as exc:
                # A synchronous business validator may cancel itself. Settle this
                # delivery without cancelling the scope's serial consumer.
                business_error = exc
                error = exc
                status = ResultStatus.CANCELLED
            except Exception as exc:
                business_error = exc
        if existing is None:
            existing = EventResult(
                handler_name=handler_name,
                connection_id=connection_id,
                event_id=self.event_id,
                event_type=type(self).__name__,
            )
        self.event_results[connection_id] = replace(
            existing,
            status=status,
            result=result,
            error=error,
            business_error=business_error,
            completed_at=datetime.now(UTC),
        )
        self._finish(connection_id)

    def check_success(self) -> Mapping[str, EventResult[T_Result]]:
        """Check explicit requirements and all selected HANDLER business results.

        Waiting means settlement, not success. This synchronous method also works
        on partial results and retains individual errors in EventExecutionError.
        Observers never stand in for required execution.
        """
        if self._preflight_error is not None:
            raise self._preflight_error
        contract = self.request_contract
        records = [r for r in self.results.values() if r.participation is Participation.HANDLER]
        if contract is not None:
            missing = tuple(
                name
                for name in contract.required_receivers
                if not any(name in (r.connection_id, r.receiver_key) for r in records)
            )
            if missing or len(records) < contract.min_receivers:
                raise ReceiverContractError(
                    self.event_id,
                    type(self).__name__,
                    missing_receivers=missing,
                    required_count=contract.min_receivers,
                    actual_count=len(records),
                )
        selected = self._required_ids or {r.connection_id for r in records}
        failures = tuple(r for r in records if r.connection_id in selected and not r.is_success)
        missing_ids = tuple(sorted(selected - {r.connection_id for r in records}))
        if failures or missing_ids:
            raise EventExecutionError(self.event_id, failures, missing_ids)
        return self.results

    def raise_for_status(self) -> None:
        """Raise if ``check_success()`` finds a required failure or incomplete result."""
        self.check_success()

    def _increment_pending(self) -> None:
        if self._pending_count == 0:
            self._completion.clear()
        self._pending_count += 1
        self._notify_changed()

    def _decrement_pending(self) -> None:
        if self._pending_count > 0:
            self._pending_count -= 1
        if self._pending_count == 0:
            self._completion.set()
        self._notify_changed()

    def __await__(self) -> Any:
        return self.wait().__await__()

    async def _await_impl(self) -> None:
        await self.wait()

    async def wait(self, *, timeout: WaitTimeout = DEFAULT_TIMEOUT) -> None:
        """Wait using an independent budget; explicit ``None`` means unlimited."""
        await wait_for_results(self, timeout=timeout)

    def _timeout_diagnostics(self, connection_ids: set[str] | None = None) -> dict[str, Any]:
        records = list(self.results.values())
        pending = tuple(
            DeliveryDiagnostic.from_result(r)
            for r in records
            if not r.is_terminal and (connection_ids is None or r.connection_id in connection_ids)
        )
        return {
            'pending_count': len(pending) or self._pending_count,
            'pending': pending,
            'completed_handlers': [r.handler_name for r in records if r.status is ResultStatus.COMPLETED],
            'failed_handlers': [r.handler_name for r in records if r.status is ResultStatus.FAILED],
            'timed_out_handlers': [r.handler_name for r in records if r.status is ResultStatus.TIMEOUT],
        }

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        if memo is None:
            memo = {}
        cls = type(self)
        new = cls.model_construct(**{name: copy.deepcopy(getattr(self, name), memo) for name in cls.model_fields})
        # model_construct initializes fresh PrivateAttr state, including callbacks.
        memo[id(self)] = new
        return new


async def wait_for_results(event: BaseEvent[Any], *, timeout: WaitTimeout = DEFAULT_TIMEOUT) -> None:
    """The common waiting implementation for events and all aggregations."""
    from agent_cdp._context import _current_connection, _current_event, _current_scope_id, _current_scope_token

    effective = event.event_timeout if isinstance(timeout, _DefaultTimeout) else timeout
    if effective is not None and effective < 0:
        raise ValueError('timeout must be nonnegative or None')
    request = event.request_contract
    selected = event._required_ids.copy() if request is not None and request.wait_for == 'required' else None

    def outstanding() -> tuple[EventResult[Any], ...]:
        return tuple(
            r for r in event.results.values() if not r.is_terminal and (selected is None or r.connection_id in selected)
        )

    async def wait_until_settled() -> None:
        while True:
            changed = event._changed
            pending = outstanding()
            if not pending and (selected is not None or event._pending_count == 0):
                return
            scope_id = _current_scope_id.get()
            scope_token = _current_scope_token.get()
            current_event, current_connection = _current_event.get(), _current_connection.get()
            current_result = (
                current_event.results.get(current_connection.id)
                if current_event is not None and current_connection is not None
                else None
            )
            blocking = tuple(
                DeliveryDiagnostic.from_result(record)
                for record in pending
                if scope_token is not None and event._delivery_scope_tokens.get(record.connection_id) is scope_token
            )
            if (
                scope_id is not None
                and blocking
                and current_result is not None
                and current_result.status is ResultStatus.RUNNING
            ):
                raise SelfWaitError(scope_id, blocking)
            await changed.wait()

    if not outstanding() and (selected is not None or event._pending_count == 0):
        return
    try:
        async with asyncio.timeout(effective):
            await wait_until_settled()
    except TimeoutError:
        assert effective is not None
        raise EventTimeoutError(
            type(event).__name__,
            event.event_id,
            effective,
            **event._timeout_diagnostics(selected),
        ) from None


def _uuid7_str() -> str:
    from uuid_utils import uuid7

    return str(uuid7())
