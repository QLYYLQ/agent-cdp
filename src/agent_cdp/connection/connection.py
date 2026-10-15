"""Connection — frozen dataclass linking source scope + event type → handler.

connect() is the free function that creates connections.
"""

from __future__ import annotations

# ScopeProtocol defines these internal connection coordination methods.
# pyright: reportPrivateUsage=false
import inspect
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, get_origin

from agent_cdp._protocols import ScopeProtocol
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import AsyncHandlerError, BaseEvent
from agent_cdp.events.contracts import Participation, ResultContract


@dataclass(frozen=True)
class Connection:
    """Explicit link between a source scope + event type and a handler.

    Frozen dataclass — the only mutation point is disconnect() via object.__setattr__.
    Uses weakref.ref for scope references to avoid circular references.
    """

    id: str
    source_scope: weakref.ref[ScopeProtocol]
    event_type: type[BaseEvent[Any]]
    handler: Callable[..., Any]
    target_scope: weakref.ref[ScopeProtocol] | None
    mode: ConnectionType
    filter: Callable[[BaseEvent[Any]], bool] | None
    priority: int
    _active: bool = field(default=True, repr=False)
    participation: Participation = Participation.HANDLER
    receiver_key: str | None = None
    result_contract: ResultContract | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'mode', ConnectionType(self.mode))
        object.__setattr__(self, 'participation', Participation(self.participation))
        source = self.source_scope()
        target = self.target_scope() if self.target_scope is not None else None
        effective = self.mode
        if effective is ConnectionType.AUTO:
            effective = ConnectionType.QUEUED if target is not None and target is not source else ConnectionType.DIRECT
        validate_handler_mode(self.handler, effective)

    @property
    def active(self) -> bool:
        """Whether this connection is still active."""
        return self._active

    def disconnect(self) -> None:
        """Disconnect this connection. Idempotent — safe to call multiple times.

        Notifies source and target scopes to remove references.
        Safe even if scopes have been garbage collected.
        """
        if not self._active:
            return

        object.__setattr__(self, '_active', False)

        # Notify source scope (may have been GC'd)
        source = self.source_scope()
        if source is not None:
            source._remove_connection(self)

        # Notify target scope (may have been GC'd or be None)
        if self.target_scope is not None:
            target = self.target_scope()
            if target is not None:
                target._remove_incoming(self)


def connect(
    source: ScopeProtocol,
    event_type: type[BaseEvent[Any]],
    handler: Callable[..., Any],
    *,
    mode: ConnectionType = ConnectionType.AUTO,
    target_scope: ScopeProtocol | None = None,
    priority: int = 0,
    filter: Callable[[BaseEvent[Any]], bool] | None = None,
    participation: Participation = Participation.HANDLER,
    receiver_key: str | None = None,
    result_contract: ResultContract | None = None,
) -> Connection:
    """Create a connection from source scope to handler for the given event type.

    Args:
        source: The EventScope (or ScopeStub) that emits events.
        event_type: The event class to listen for. Must not be BaseEvent itself.
        handler: Callable to invoke when the event is emitted.
        mode: ConnectionType — DIRECT, QUEUED, or AUTO (default).
        target_scope: The scope the handler belongs to (for Auto mode and lifecycle).
        priority: Execution priority (higher = earlier). Default 0.
        filter: Optional predicate — connection only fires if filter returns True.

    Returns:
        A frozen Connection object.

    Raises:
        TypeError: If event_type is BaseEvent (use connect_all instead).
    """
    if event_type is BaseEvent:
        msg = 'Cannot connect to BaseEvent directly — use connect_all() for catch-all connections.'
        raise TypeError(msg)

    if not isinstance(event_type, type) or not issubclass(event_type, BaseEvent):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f'{event_type!r} is not a subclass of BaseEvent')
    if getattr(source, 'closed', False) or (target_scope is not None and getattr(target_scope, 'closed', False)):
        raise RuntimeError('Cannot connect a closed source or target scope')

    from uuid_utils import uuid7

    conn = Connection(
        id=str(uuid7()),
        source_scope=weakref.ref(source),
        event_type=event_type,
        handler=handler,
        target_scope=weakref.ref(target_scope) if target_scope is not None else None,
        mode=mode,
        filter=filter,
        priority=priority,
        participation=participation,
        receiver_key=receiver_key,
        result_contract=result_contract,
    )

    # Register on source scope
    source._add_connection(conn)

    # Register on target scope
    if target_scope is not None:
        try:
            target_scope._add_incoming(conn)
        except BaseException:
            conn.disconnect()
            raise

    return conn


def validate_handler_mode(handler: Callable[..., Any], mode: ConnectionType) -> None:
    """Reject statically certain mismatches; dynamic values are checked at execution.

    A synchronous callable may legitimately return an awaitable. Without an
    informative return annotation we leave that check to queued execution.
    """
    if not callable(handler):
        raise TypeError('handler must be callable')
    candidate = handler if inspect.isroutine(handler) else getattr(handler, '__call__', handler)
    if inspect.isasyncgenfunction(handler) or inspect.isasyncgenfunction(candidate):
        raise AsyncHandlerError('Async generator handlers are not supported; return a value from an async function')
    is_async = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(candidate)
    if mode is ConnectionType.DIRECT and is_async:
        raise AsyncHandlerError('Async handler cannot use DIRECT dispatch; use ConnectionType.QUEUED')
    if mode is not ConnectionType.QUEUED or is_async:
        return
    try:
        annotation = inspect.signature(handler, eval_str=True).return_annotation
    except (ValueError, TypeError, NameError):
        return
    if annotation is inspect.Signature.empty or annotation is Any or isinstance(annotation, str):
        return
    origin = get_origin(annotation) or annotation
    if origin in (Awaitable, Coroutine):
        return
    # Reject only concrete ordinary value types. Unions, TypeVars and custom
    # awaitables are left for the runtime check, where the actual value is known.
    if origin in (None, type(None), bool, str, bytes, int, float, list, dict, tuple, set):
        raise AsyncHandlerError('Synchronous handler cannot use QUEUED dispatch unless it returns an awaitable')
