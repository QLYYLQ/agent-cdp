"""ScopeGroup — manage a collection of EventScopes with lifecycle, broadcast, and batch connection."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from agent_cdp.connection.connection import Connection
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import BaseEvent
from agent_cdp.scope.scope import EventScope


class ScopeGroup:
    """Manage a collection of EventScopes with lifecycle, broadcast, and batch connection.

    Maps to a browser window (group of tabs), a monitoring domain, etc.
    Provides create/close lifecycle, broadcast to all scopes (with deep copy isolation),
    and connect_all_scopes for batch handler registration.
    """

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        self._scopes: dict[str, EventScope] = {}

    async def create_scope(self, scope_id: str, **metadata: Any) -> EventScope:
        """Create a new scope and auto-start its event loop.

        Args:
            scope_id: Unique identifier for the scope within this group.
            **metadata: Arbitrary metadata passed to EventScope constructor.

        Returns:
            The newly created EventScope with its event loop running.

        Raises:
            KeyError: If scope_id already exists in this group.
        """
        if scope_id in self._scopes:
            msg = f'Scope {scope_id!r} already exists in group {self.group_id!r}'
            raise KeyError(msg)

        scope = EventScope(scope_id, **metadata)
        await scope._event_loop.start()
        self._scopes[scope_id] = scope
        return scope

    async def close_scope(self, scope_id: str) -> None:
        """Close and remove a scope from the group.

        Args:
            scope_id: The scope to close.

        Raises:
            KeyError: If scope_id is not found in this group.
        """
        if scope_id not in self._scopes:
            msg = f'Scope {scope_id!r} not found in group {self.group_id!r}'
            raise KeyError(msg)

        scope = self._scopes.pop(scope_id)
        await scope.close()

    def get_scope(self, scope_id: str) -> EventScope:
        """Get scope by ID.

        Args:
            scope_id: The scope to retrieve.

        Returns:
            The EventScope instance.

        Raises:
            KeyError: If scope_id is not found in this group.
        """
        if scope_id not in self._scopes:
            msg = f'Scope {scope_id!r} not found in group {self.group_id!r}'
            raise KeyError(msg)
        return self._scopes[scope_id]

    def broadcast(
        self,
        event: BaseEvent[Any],
        *,
        exclude: set[str] | None = None,
    ) -> list[BaseEvent[Any]]:
        """Emit a deep-copied event to each scope in the group.

        Each scope receives an independent copy so that consume(), result recording,
        and any handler mutations are fully isolated across scopes.

        Args:
            event: The event to broadcast.
            exclude: Optional set of scope_ids to skip.

        Returns:
            List of deep-copied event instances (one per scope that received it).
        """
        copies: list[BaseEvent[Any]] = []
        for scope_id, scope in self._scopes.items():
            if exclude and scope_id in exclude:
                continue
            event_copy = copy.deepcopy(event)
            scope.emit(event_copy)
            copies.append(event_copy)
        return copies

    def connect_all_scopes(
        self,
        event_type: type[BaseEvent[Any]],
        handler: Callable[..., Any],
        *,
        mode: ConnectionType = ConnectionType.AUTO,
        target_scope: EventScope | None = None,
        priority: int = 0,
        filter: Callable[[BaseEvent[Any]], bool] | None = None,
    ) -> list[Connection]:
        """Connect handler to event_type on ALL current scopes.

        This is a ScopeGroup-level "all scopes" operation, distinct from
        EventScope.connect_all() which connects to "all event types" on a single scope.

        Args:
            event_type: The event class to listen for on each scope.
            handler: Callable to invoke when the event is emitted.
            mode: ConnectionType — DIRECT, QUEUED, or AUTO (default).
            target_scope: The scope the handler belongs to (for Auto mode and lifecycle).
            priority: Execution priority (higher = earlier). Default 0.
            filter: Optional predicate — connection only fires if filter returns True.

        Returns:
            List of Connection objects (one per scope).
        """
        connections: list[Connection] = []
        for scope in self._scopes.values():
            conn = scope.connect(
                event_type,
                handler,
                mode=mode,
                target_scope=target_scope,
                priority=priority,
                filter=filter,
            )
            connections.append(conn)
        return connections

    async def close_all(self) -> None:
        """Close all scopes in the group.

        Iterates over a copy of the scopes dict to avoid mutation during iteration.
        After closing, the group is empty.
        """
        for scope in list(self._scopes.values()):
            await scope.close()
        self._scopes.clear()

    @property
    def scope_ids(self) -> list[str]:
        """List of all scope IDs in the group."""
        return list(self._scopes.keys())

    @property
    def scope_count(self) -> int:
        """Number of scopes in the group."""
        return len(self._scopes)

    def __repr__(self) -> str:
        return f'ScopeGroup({self.group_id!r}, scopes={self.scope_count})'
