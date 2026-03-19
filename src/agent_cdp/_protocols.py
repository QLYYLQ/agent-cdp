"""Structural protocols for cross-module type safety.

Leaf module — no runtime imports from agent_cdp subpackages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent_cdp.connection.connection import Connection


@runtime_checkable
class ScopeProtocol(Protocol):
    """Structural type for objects that can act as an event scope in connections.

    Satisfied by :class:`EventScope` and any test stub implementing the four
    connection-management methods plus *scope_id*.
    """

    scope_id: str

    def _add_connection(self, conn: Connection) -> None: ...
    def _remove_connection(self, conn: Connection) -> None: ...
    def _add_incoming(self, conn: Connection) -> None: ...
    def _remove_incoming(self, conn: Connection) -> None: ...
