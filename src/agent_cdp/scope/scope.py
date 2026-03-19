"""EventScope — core event dispatch engine with Direct/Queued/Auto modes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import weakref
from collections.abc import Callable
from typing import Any

from agent_cdp._context import _current_connection, _current_event, _emit_depth
from agent_cdp._registry import EventRegistrar
from agent_cdp.advanced.cycle_detect import _MAX_DIRECT_DEPTH
from agent_cdp.connection.connection import Connection, connect
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import AsyncHandlerError, BaseEvent, EmitPolicy
from agent_cdp.scope._helpers import _record, get_handler_name
from agent_cdp.scope.event_loop import ScopeEventLoop

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

    def __init__(self, scope_id: str, *, max_history_size: int = 1000, **metadata: Any) -> None:
        self.scope_id = scope_id
        self.metadata: dict[str, Any] = metadata
        self._connections_by_type: dict[type[BaseEvent[Any]], list[Connection]] = {}
        self._catch_all_connections: list[Connection] = []
        self._incoming: list[Connection] = []
        self._event_loop = ScopeEventLoop()
        self._closed = False
        self._event_history: list[BaseEvent[Any]] = []
        self._max_history_size = max_history_size

    @property
    def event_history(self) -> list[BaseEvent[Any]]:
        """Return a copy of the event history."""
        return list(self._event_history)

    def _record_history(self, event: BaseEvent[Any]) -> None:
        """Record event in history, respecting max_history_size."""
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
        )

    def connect_all(
        self,
        handler: Callable[..., Any],
        *,
        mode: ConnectionType = ConnectionType.AUTO,
        target_scope: EventScope | None = None,
        priority: int = 0,
        filter: Callable[[BaseEvent[Any]], bool] | None = None,
    ) -> Connection:
        """Create a catch-all connection that matches all event types.

        Independent implementation — does not go through connect() free function.
        Uses event_type=BaseEvent as a sentinel value in the Connection.
        """
        from uuid_utils import uuid7

        conn = Connection(
            id=str(uuid7()),
            source_scope=weakref.ref(self),
            event_type=BaseEvent,
            handler=handler,
            target_scope=weakref.ref(target_scope) if target_scope is not None else None,
            mode=mode,
            filter=filter,
            priority=priority,
        )
        self._catch_all_connections.append(conn)

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
        if parent is not None:
            event.event_parent_id = parent.event_id
        token = _current_event.set(event)

        # Record in event history
        self._record_history(event)

        try:
            connections = self._get_matching_connections(event)

            for conn in connections:
                if not conn.active:
                    continue

                # Apply connection-level filter
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

                effective_mode = self._resolve_mode(conn)

                if effective_mode == ConnectionType.DIRECT:
                    self._dispatch_direct(event, conn, policy)
                else:
                    # QUEUED path
                    if conn.target_scope is not None:
                        target = conn.target_scope()
                        if target is None:
                            # target GC'd, skip this connection
                            continue
                        target_loop = target._event_loop
                    else:
                        target_loop = self._event_loop

                    event._increment_pending()
                    target_loop.enqueue(event, conn)

                if event.consumed:
                    break
        finally:
            _current_event.reset(token)
            _emit_depth.reset(depth_token)

        return event

    async def emit_and_wait(
        self,
        event: BaseEvent[Any],
        *,
        timeout: float | None = None,
    ) -> BaseEvent[Any]:
        """Emit an event and ``await`` it in one call.

        Convenience wrapper that eliminates the common ``emit(); await event``
        two-step pattern seen in demo code. Optionally overrides *event_timeout*.
        """
        if timeout is not None:
            event.event_timeout = timeout
        self.emit(event)
        await event
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
        t0 = time.monotonic()
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

        except AsyncHandlerError:
            # Re-raise AsyncHandlerErrors from async detection (already recorded above)
            raise
        except Exception as exc:
            _record(event, conn, error=exc)
            if policy == EmitPolicy.FAIL_FAST:
                raise
        finally:
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
        """
        matching: list[Connection] = []
        for cls in type(event).__mro__:
            if cls in self._connections_by_type:
                matching.extend(c for c in self._connections_by_type[cls] if c.active)
        matching.extend(c for c in self._catch_all_connections if c.active)
        matching.sort(key=lambda c: c.priority, reverse=True)
        return matching

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

    def _remove_connection(self, conn: Connection) -> None:
        """Remove a connection from storage. Called by Connection.disconnect()."""
        conns = self._connections_by_type.get(conn.event_type)
        if conns is not None and conn in conns:
            conns.remove(conn)
            return
        if conn in self._catch_all_connections:
            self._catch_all_connections.remove(conn)

    def _add_incoming(self, conn: Connection) -> None:
        """Track a connection targeting this scope."""
        self._incoming.append(conn)

    def _remove_incoming(self, conn: Connection) -> None:
        """Remove an incoming connection reference."""
        if conn in self._incoming:
            self._incoming.remove(conn)

    # ── Lifecycle ──

    async def close(self) -> None:
        """Close this scope: stop event loop and disconnect all connections.

        After closing, emit() raises RuntimeError.
        """
        self._closed = True
        await self._event_loop.stop()

        # Disconnect outgoing typed connections
        for conns in self._connections_by_type.values():
            for conn in list(conns):
                conn.disconnect()

        # Disconnect outgoing catch-all connections
        for conn in list(self._catch_all_connections):
            conn.disconnect()

        # Disconnect incoming connections from other scopes
        for conn in list(self._incoming):
            conn.disconnect()

    # ── Validation ──

    @staticmethod
    def _validate_event_type(event_type: type[BaseEvent[Any]]) -> None:
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
        has_abstract = getattr(event_type, '__abstract__', False)
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
