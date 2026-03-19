"""Connection — frozen dataclass linking source scope + event type → handler.

connect() is the free function that creates connections.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_cdp._protocols import ScopeProtocol
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import BaseEvent


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
    )

    # Register on source scope
    source._add_connection(conn)

    # Register on target scope
    if target_scope is not None:
        target_scope._add_incoming(conn)

    return conn
