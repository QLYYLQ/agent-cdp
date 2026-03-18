"""ContextVar definitions for parent-child event tracking.

Two ContextVars (vs bubus's 3): _current_event.get() is not None
replaces bubus's inside_handler_context.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_cdp.connection.connection import Connection
    from agent_cdp.events.base import BaseEvent

_current_event: ContextVar[BaseEvent[object] | None] = ContextVar('_current_event', default=None)
_current_connection: ContextVar[Connection | None] = ContextVar('_current_connection', default=None)
_emit_depth: ContextVar[int] = ContextVar('_emit_depth', default=0)
