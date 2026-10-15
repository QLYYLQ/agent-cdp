"""ScopeGroup — manage a collection of EventScopes with lifecycle, broadcast, and batch connection."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Mapping
from typing import Any

from agent_cdp.connection.connection import Connection
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import BaseEvent
from agent_cdp.events.contracts import Participation, ResultContract
from agent_cdp.scope.event_loop import CloseResult
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

    async def create_scope(
        self,
        scope_id: str,
        *,
        queue_maxsize: int = 1024,
        max_history_size: int = 1000,
        slow_handler_threshold: float | None = 15.0,
        slow_handler_check_interval: float = 5.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> EventScope:
        """Create and start a scope with the same explicit options as EventScope."""
        if scope_id in self._scopes:
            raise KeyError(f'Scope {scope_id!r} already exists in group {self.group_id!r}')
        scope = EventScope(
            scope_id,
            queue_maxsize=queue_maxsize,
            max_history_size=max_history_size,
            slow_handler_threshold=slow_handler_threshold,
            slow_handler_check_interval=slow_handler_check_interval,
            metadata=metadata,
        )
        await scope.start()
        self._scopes[scope_id] = scope
        return scope

    async def close_scope(self, scope_id: str, *, graceful: bool = True, timeout: float = 10.0) -> CloseResult:
        """Remove a scope only after all its supervised work has actually ended."""
        scope = self.get_scope(scope_id)
        try:
            return await scope.close(graceful=graceful, timeout=timeout)
        finally:
            if scope.is_closed:
                self._scopes.pop(scope_id, None)

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
        participation: Participation = Participation.HANDLER,
        receiver_key: str | None = None,
        result_contract: ResultContract | None = None,
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
                participation=participation,
                receiver_key=receiver_key,
                result_contract=result_contract,
            )
            connections.append(conn)
        return connections

    async def close_all(self, *, graceful: bool = True, timeout: float = 10.0) -> dict[str, CloseResult]:
        """Attempt all scopes concurrently; retain unfinished scopes and aggregate cleanup failures."""
        ids = tuple(self._scopes)
        outcomes = await asyncio.gather(
            *(self.close_scope(scope_id, graceful=graceful, timeout=timeout) for scope_id in ids),
            return_exceptions=True,
        )
        errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        if errors:
            raise ExceptionGroup(f'Resource cleanup failed in group {self.group_id!r}', errors)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        return {
            scope_id: outcome
            for scope_id, outcome in zip(ids, outcomes, strict=True)
            if isinstance(outcome, CloseResult)
        }

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
