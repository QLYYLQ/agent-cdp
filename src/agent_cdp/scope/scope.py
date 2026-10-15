"""EventScope — core event dispatch engine with Direct/Queued/Auto modes."""

from __future__ import annotations

# Internal integration points intentionally share event lifecycle methods.
# pyright: reportPrivateUsage=false
import asyncio
import inspect
import logging
import time
import weakref
from collections.abc import Callable, Mapping
from typing import Any, cast

from agent_cdp._context import _current_connection, _current_event, _emit_depth
from agent_cdp._registry import EventRegistrar
from agent_cdp.advanced.cycle_detect import _MAX_DIRECT_DEPTH
from agent_cdp.connection.connection import Connection, connect
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import DEFAULT_TIMEOUT, AsyncHandlerError, BaseEvent, EmitPolicy, WaitTimeout
from agent_cdp.events.contracts import Participation, ResultContract
from agent_cdp.events.result import ResultStatus
from agent_cdp.scope._helpers import _record, get_handler_name
from agent_cdp.scope.event_loop import CloseResult, FlushResult, ScopeEventLoop, ScopeStats

logger = logging.getLogger(__name__)

# Direct handler execution time threshold (seconds).
# Handlers exceeding this emit a warning — they should use QUEUED mode instead.
_DIRECT_HANDLER_WARN_THRESHOLD = 0.1  # 100ms


class EventScope:
    """Isolated event processing domain with its own event loop.

    Maps to a browser tab, monitoring channel, or any isolated dispatch domain.
    Supports Direct (synchronous) and Queued (async) handler dispatch,
    with Auto mode resolving based on same-scope vs cross-scope.
    """

    def __init__(
        self,
        scope_id: str,
        *,
        queue_maxsize: int = 1024,
        max_history_size: int = 1000,
        slow_handler_threshold: float | None = 15.0,
        slow_handler_check_interval: float = 5.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if max_history_size < 0:
            raise ValueError('max_history_size must be >= 0')
        self.scope_id = scope_id
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.queue_maxsize = queue_maxsize
        self.max_history_size = max_history_size
        self.slow_handler_threshold = slow_handler_threshold
        self.slow_handler_check_interval = slow_handler_check_interval
        self._connections_by_type: dict[type[BaseEvent[Any]], list[Connection]] = {}
        self._catch_all_connections: list[Connection] = []
        self._incoming: list[Connection] = []
        self._event_loop = ScopeEventLoop(
            queue_maxsize,
            scope_id=scope_id,
            deadlock_scan_interval=slow_handler_check_interval,
            deadlock_threshold=slow_handler_threshold,
        )
        self._closed = False
        self._is_closed = False
        self._close_callbacks: list[Callable[[], Any]] = []
        self._cleanup_tasks: dict[asyncio.Task[Any], Callable[[], Any]] = {}
        self._close_lock = asyncio.Lock()
        self._event_history: list[BaseEvent[Any]] = []
        self._max_history_size = max_history_size

        # MRO match cache (ABC-inspired: positive + negative + version counter)
        self._match_cache: weakref.WeakKeyDictionary[type[BaseEvent[Any]], tuple[int, list[Connection]]] = (
            weakref.WeakKeyDictionary()
        )
        self._negative_cache: weakref.WeakSet[type[BaseEvent[Any]]] = weakref.WeakSet()
        self._connection_version: int = 0
        self._negative_cache_version: int = 0

    @property
    def event_history(self) -> list[BaseEvent[Any]]:
        """Return a copy of the event history."""
        return list(self._event_history)

    def _record_history(self, event: BaseEvent[Any]) -> None:
        """Record event in history, respecting max_history_size."""
        if self._max_history_size == 0:
            return
        self._event_history.append(event)
        if len(self._event_history) > self._max_history_size:
            self._event_history = self._event_history[-self._max_history_size :]

    # ── Connection management (called by connect() free function and connect_all) ──

    def connect(
        self,
        event_type: type[BaseEvent[Any]],
        handler: Callable[..., Any],
        *,
        mode: ConnectionType = ConnectionType.AUTO,
        target_scope: EventScope | None = None,
        priority: int = 0,
        filter: Callable[[BaseEvent[Any]], bool] | None = None,
        participation: Participation = Participation.HANDLER,
        receiver_key: str | None = None,
        result_contract: ResultContract | None = None,
    ) -> Connection:
        """Create a connection from this scope for a specific event type.

        Validates that event_type is a concrete registered event subclass,
        then delegates to the connect() free function.

        Raises:
            TypeError: If event_type is BaseEvent itself (use connect_all).
            TypeError: If event_type is not a valid event subclass.
        """
        self._validate_event_type(event_type)
        return connect(
            self,
            event_type,
            handler,
            mode=mode,
            target_scope=target_scope,
            priority=priority,
            filter=filter,
            participation=participation,
            receiver_key=receiver_key,
            result_contract=result_contract,
        )

    def connect_all(
        self,
        handler: Callable[..., Any],
        *,
        mode: ConnectionType = ConnectionType.AUTO,
        target_scope: EventScope | None = None,
        priority: int = 0,
        filter: Callable[[BaseEvent[Any]], bool] | None = None,
        participation: Participation = Participation.HANDLER,
        receiver_key: str | None = None,
        result_contract: ResultContract | None = None,
    ) -> Connection:
        """Create a catch-all connection that matches all event types.

        Independent implementation — does not go through connect() free function.
        Uses event_type=BaseEvent as a sentinel value in the Connection.
        """
        from uuid_utils import uuid7

        if self._closed:
            raise RuntimeError(f'Cannot connect to closed scope {self.scope_id!r}')
        if target_scope is not None and target_scope.closed:
            raise RuntimeError(f'Cannot target closed scope {target_scope.scope_id!r}')
        conn = Connection(
            id=str(uuid7()),
            source_scope=weakref.ref(self),
            event_type=BaseEvent,
            handler=handler,
            target_scope=weakref.ref(target_scope) if target_scope is not None else None,
            mode=mode,
            filter=filter,
            priority=priority,
            participation=participation,
            receiver_key=receiver_key,
            result_contract=result_contract,
        )
        self._catch_all_connections.append(conn)
        self._connection_version += 1

        if target_scope is not None:
            target_scope._add_incoming(conn)

        return conn

    # ── Emit ──

    def emit(self, event: BaseEvent[Any]) -> BaseEvent[Any]:
        """Dispatch an event to all matching connections.

        Synchronous function. Direct handlers execute inline; Queued handlers
        are enqueued to the target scope's event loop for later processing.

        Returns the event (with results recorded on it).

        Raises:
            RuntimeError: If this scope is closed.
            ValueError: If this event already has a delivery for an applicable connection.
            TypeError: If a Direct handler returns a coroutine or awaitable (FAIL_FAST).
            Exception: Re-raised from Direct handlers under FAIL_FAST policy.
        """
        if self._closed:
            msg = f'Cannot emit on closed scope {self.scope_id!r}'
            raise RuntimeError(msg)

        # ── Cycle detection: Direct emit chain depth limit ──
        depth = _emit_depth.get()
        if depth >= _MAX_DIRECT_DEPTH:
            raise RecursionError(
                f'Direct emit depth {depth} exceeds limit {_MAX_DIRECT_DEPTH}. Possible cycle in Direct connections.'
            )
        depth_token = _emit_depth.set(depth + 1)

        policy = type(event).emit_policy

        # Parent-child tracking via ContextVar
        parent = _current_event.get()
        token = _current_event.set(event)

        try:
            connections: list[Connection] = []
            for conn in self._get_matching_connections(event):
                if not conn.active:
                    continue
                if conn.target_scope is not None:
                    target_scope = cast(EventScope | None, conn.target_scope())
                    if target_scope is None or target_scope.closed:
                        continue
                if conn.filter is not None:
                    try:
                        if not conn.filter(event):
                            continue
                    except Exception:
                        logger.warning(
                            'Connection filter for %s raised on scope %r — skipping',
                            get_handler_name(conn.handler),
                            self.scope_id,
                            exc_info=True,
                        )
                        continue
                connections.append(conn)
            repeated = [conn.id for conn in connections if conn.id in event.event_results]
            if repeated:
                raise ValueError(
                    f'Event {event.event_id} already has deliveries for connections {repeated} '
                    f'on scope {self.scope_id!r}; use a fresh event instance '
                    '(copy the payload without previous delivery results).'
                )
            event._preflight(connections)
            if parent is not None:
                event.event_parent_id = parent.event_id
            self._record_history(event)
            for conn in connections:
                target = cast(EventScope | None, conn.target_scope()) if conn.target_scope is not None else self
                assert target is not None
                event._prepare_delivery(conn, target.scope_id, queued=self._resolve_mode(conn) != ConnectionType.DIRECT)
                event._set_delivery_scope(conn.id, target._event_loop)

            rejection: Exception | None = None
            dispatched: set[str] = set()
            try:
                for conn in connections:
                    dispatched.add(conn.id)
                    effective_mode = self._resolve_mode(conn)
                    if effective_mode == ConnectionType.DIRECT:
                        self._dispatch_direct(event, conn, policy)
                    else:
                        target = cast(EventScope | None, conn.target_scope()) if conn.target_scope is not None else self
                        assert target is not None
                        event._increment_pending()
                        error = target._event_loop.enqueue(event, conn)
                        if error is not None:
                            rejection = error
                    if event.consumed:
                        break
            finally:
                for conn in connections:
                    if conn.id not in dispatched:
                        event.record_result(
                            connection_id=conn.id,
                            handler_name=get_handler_name(conn.handler),
                            error=asyncio.CancelledError('Dispatch stopped before this receiver'),
                            status=ResultStatus.CANCELLED,
                        )
            if rejection is not None and event.non_droppable:
                raise rejection
        finally:
            _current_event.reset(token)
            _emit_depth.reset(depth_token)

        return event

    async def emit_and_wait(
        self,
        event: BaseEvent[Any],
        *,
        timeout: WaitTimeout = DEFAULT_TIMEOUT,
    ) -> BaseEvent[Any]:
        """Emit an event and ``await`` it in one call.

        The wait budget belongs to this caller and never changes execution_timeout.
        """
        self.emit(event)
        await event.wait(timeout=timeout)
        return event

    # ── Internal dispatch ──

    def _dispatch_direct(
        self,
        event: BaseEvent[Any],
        conn: Connection,
        policy: EmitPolicy,
    ) -> None:
        """Execute a Direct handler synchronously with full error/async detection."""
        conn_token = _current_connection.set(conn)
        handler_name = get_handler_name(conn.handler)
        target = cast(EventScope | None, conn.target_scope()) if conn.target_scope is not None else self
        target_loop = target._event_loop if target is not None else self._event_loop
        dispatch_started = target_loop.direct_started()
        t0 = dispatch_started[1]
        event._mark_running(conn.id)
        try:
            result = conn.handler(event)

            # Detect async handler returning coroutine
            if asyncio.iscoroutine(result):
                result.close()  # avoid RuntimeWarning
                err = AsyncHandlerError(
                    f'Direct handler {handler_name} returned a coroutine; use ConnectionType.QUEUED for async handlers'
                )
                _record(event, conn, error=err)
                if policy == EmitPolicy.FAIL_FAST:
                    raise err
                return

            # Detect other awaitables
            if inspect.isawaitable(result):
                err = AsyncHandlerError(
                    f'Direct handler {handler_name} returned an awaitable; use ConnectionType.QUEUED for async handlers'
                )
                _record(event, conn, error=err)
                if policy == EmitPolicy.FAIL_FAST:
                    raise err
                return

            _record(event, conn, result=result)

        except AsyncHandlerError as exc:
            # Shape errors above are recorded; a handler may itself raise this type.
            if not event.event_results[conn.id].is_terminal:
                _record(event, conn, error=exc)
            if policy == EmitPolicy.FAIL_FAST:
                raise
        except asyncio.CancelledError as exc:
            event.record_result(
                connection_id=conn.id, handler_name=handler_name, error=exc, status=ResultStatus.CANCELLED
            )
            if policy == EmitPolicy.FAIL_FAST:
                raise
        except Exception as exc:
            _record(event, conn, error=exc)
            if policy == EmitPolicy.FAIL_FAST:
                raise
        finally:
            recorded = event.event_results.get(conn.id)
            if recorded is not None:
                target_loop.direct_finished(recorded, dispatch_started)
            elapsed = time.monotonic() - t0
            if elapsed > _DIRECT_HANDLER_WARN_THRESHOLD:
                logger.warning(
                    'Direct handler %s took %.3fs (>%.0fms) on scope %r for %s — '
                    'consider using ConnectionType.QUEUED for slow operations',
                    handler_name,
                    elapsed,
                    _DIRECT_HANDLER_WARN_THRESHOLD * 1000,
                    self.scope_id,
                    type(event).__name__,
                )
            _current_connection.reset(conn_token)

    # ── Connection resolution ──

    def _get_matching_connections(self, event: BaseEvent[Any]) -> list[Connection]:
        """Collect connections matching the event type via MRO, plus catch-all.

        Returns connections sorted by priority (highest first).

        Uses an ABC-inspired two-level cache (positive + negative) with a
        per-scope version counter for invalidation.  See cpython/Lib/_py_abc.py
        for the pattern origin.
        """
        event_type = type(event)

        # ── Negative cache check (cheapest path) ──
        if self._negative_cache_version == self._connection_version:
            if event_type in self._negative_cache:
                return []
        else:
            # Version mismatch → negative cache stale, clear it (ABC pattern)
            self._negative_cache = weakref.WeakSet()
            self._negative_cache_version = self._connection_version

        # ── Positive cache check ──
        cached = self._match_cache.get(event_type)
        if cached is not None:
            version, connections = cached
            if version == self._connection_version:
                return [c for c in connections if c.active]
            # Stale entry — fall through to rebuild

        # ── Cache miss: full MRO traversal + sort ──
        matching: list[Connection] = []
        for cls in event_type.__mro__:
            if cls in self._connections_by_type:
                matching.extend(self._connections_by_type[cls])
        matching.extend(self._catch_all_connections)

        if not matching:
            # Negative result → cache in negative cache
            self._negative_cache.add(event_type)
            return []

        matching.sort(key=lambda c: c.priority, reverse=True)
        self._match_cache[event_type] = (self._connection_version, matching)
        return [c for c in matching if c.active]

    def _resolve_mode(self, conn: Connection) -> ConnectionType:
        """Resolve AUTO mode to DIRECT or QUEUED based on scope relationship."""
        if conn.mode != ConnectionType.AUTO:
            return conn.mode

        if conn.target_scope is None:
            return ConnectionType.DIRECT  # no target → Direct

        target = conn.target_scope()
        if target is None:
            return ConnectionType.DIRECT  # GC'd target → Direct
        if target is self:
            return ConnectionType.DIRECT  # same scope → Direct

        return ConnectionType.QUEUED  # cross-scope → Queued

    # ── Scope-internal connection storage ──

    def _add_connection(self, conn: Connection) -> None:
        """Register a typed connection. Called by connect() free function."""
        self._connections_by_type.setdefault(conn.event_type, []).append(conn)
        self._connection_version += 1

    def _remove_connection(self, conn: Connection) -> None:
        """Remove a connection from storage. Called by Connection.disconnect()."""
        conns = self._connections_by_type.get(conn.event_type)
        if conns is not None and conn in conns:
            conns.remove(conn)
            self._match_cache.clear()
            self._connection_version += 1
            return
        if conn in self._catch_all_connections:
            self._catch_all_connections.remove(conn)
            self._match_cache.clear()
            self._connection_version += 1

    def _add_incoming(self, conn: Connection) -> None:
        """Track a connection targeting this scope."""
        self._incoming.append(conn)

    def _remove_incoming(self, conn: Connection) -> None:
        """Remove an incoming connection reference."""
        if conn in self._incoming:
            self._incoming.remove(conn)

    # ── Lifecycle ──

    @property
    def closed(self) -> bool:
        """Whether new input has been stopped (cleanup may still be in progress)."""
        return self._closed

    @property
    def is_closed(self) -> bool:
        """Whether all supervised handlers and resource cleanup have finished."""
        return self._is_closed

    @property
    def stats(self) -> ScopeStats:
        return self._event_loop.stats

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError(f'Cannot start closed scope {self.scope_id!r}')
        await self._event_loop.start()

    async def flush(self, *, timeout: float | None = None) -> FlushResult:
        """Wait for admissions outstanding now; later input never extends the barrier."""
        return await self._event_loop.flush(timeout=timeout)

    def add_close_callback(self, callback: Callable[[], Any]) -> None:
        """Register owned resource cleanup. A callback may return an awaitable."""
        if self._closed:
            raise RuntimeError(f'Cannot bind a resource to closed scope {self.scope_id!r}')
        if callback not in self._close_callbacks:
            self._close_callbacks.append(callback)

    def remove_close_callback(self, callback: Callable[[], Any]) -> None:
        if callback in self._close_callbacks:
            self._close_callbacks.remove(callback)

    async def close(self, *, graceful: bool = True, timeout: float = 10.0) -> CloseResult:
        """Stop input and resources, then drain or request cancellation within a budget.

        A false ``complete`` result preserves unfinished tasks for later close calls.
        Python cancellation is cooperative and cannot undo external side effects.
        """
        if timeout < 0:
            raise ValueError('close timeout must be >= 0')
        self._closed = True
        deadline = time.monotonic() + timeout
        if self._close_lock.locked():
            try:
                await asyncio.wait_for(self._close_lock.acquire(), timeout=timeout)
            except TimeoutError:
                return CloseResult(False, self._event_loop.pending_diagnostics, len(self._close_callbacks))
        else:
            await self._close_lock.acquire()
        try:
            if self._is_closed:
                return CloseResult(True)
            errors: list[Exception] = []
            for callback in tuple(self._close_callbacks):
                if callback in self._cleanup_tasks.values():
                    continue
                try:
                    value = callback()
                    if inspect.isawaitable(value):

                        async def finish_resource(awaitable: Any = value) -> None:
                            await awaitable

                        self._cleanup_tasks[asyncio.create_task(finish_resource())] = callback
                    else:
                        self.remove_close_callback(callback)
                except Exception as exc:
                    if callback not in self._close_callbacks:
                        self._close_callbacks.append(callback)
                    errors.append(exc)
            if self._cleanup_tasks:
                await asyncio.wait(set(self._cleanup_tasks), timeout=max(0.0, deadline - time.monotonic()))
                for task in tuple(self._cleanup_tasks):
                    if task.done():
                        callback = self._cleanup_tasks.pop(task)
                        if task.cancelled():
                            if callback not in self._close_callbacks:
                                self._close_callbacks.append(callback)
                        else:
                            error = task.exception()
                            if isinstance(error, Exception):
                                if callback not in self._close_callbacks:
                                    self._close_callbacks.append(callback)
                                errors.append(error)
                            else:
                                self.remove_close_callback(callback)
                    elif not task.cancelling():
                        task.cancel()
            stopped = await self._event_loop.stop(drain=graceful, timeout=max(0.0, deadline - time.monotonic()))
            complete = stopped and not self._cleanup_tasks and not self._close_callbacks
            if complete:
                for conns in list(self._connections_by_type.values()):
                    for conn in list(conns):
                        conn.disconnect()
                for conn in list(self._catch_all_connections):
                    conn.disconnect()
                for conn in list(self._incoming):
                    conn.disconnect()
                self._match_cache.clear()
                self._negative_cache.clear()
                self._is_closed = True
            if errors:
                raise ExceptionGroup(f'Resource cleanup failed for scope {self.scope_id!r}', errors)
            return CloseResult(complete, self._event_loop.pending_diagnostics, len(self._close_callbacks))
        finally:
            self._close_lock.release()

    # ── Validation ──

    @staticmethod
    def _validate_event_type(event_type: object) -> None:
        """Validate that event_type is a concrete, registered event subclass.

        Raises TypeError if event_type is BaseEvent itself (handled by connect() free function)
        or is not a proper subclass of BaseEvent with conscribe registration or __abstract__.
        """
        if event_type is BaseEvent:
            msg = 'Cannot connect to BaseEvent directly — use connect_all() for catch-all connections.'
            raise TypeError(msg)

        if not (isinstance(event_type, type) and issubclass(event_type, BaseEvent)):
            msg = f'{event_type!r} is not a subclass of BaseEvent'
            raise TypeError(msg)

        # Must be either abstract or registered in conscribe
        event_class = cast(type[BaseEvent[Any]], event_type)
        has_abstract = getattr(event_class, '__abstract__', False)
        is_registered = event_type in EventRegistrar.get_all().values()

        if not has_abstract and not is_registered:
            msg = (
                f'{event_type.__name__} is not registered in EventRegistrar and does not '
                f'have __abstract__ = True. Ensure it inherits from BaseEvent properly.'
            )
            raise TypeError(msg)

    def __repr__(self) -> str:
        n_typed = sum(len(v) for v in self._connections_by_type.values())
        n_all = len(self._catch_all_connections)
        return f'EventScope({self.scope_id!r}, connections={n_typed}, catch_all={n_all}, closed={self._closed})'
