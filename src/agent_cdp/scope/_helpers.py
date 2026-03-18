"""Helper utilities for scope dispatch — handler naming and result recording.

This is the sole 'dual-import' point that bridges events and connection packages.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from agent_cdp.connection.connection import Connection
from agent_cdp.events.base import BaseEvent


def get_handler_name(handler: Callable[..., Any]) -> str:
    """Extract a human-readable name from a handler callable.

    Priority: unwrap functools.partial → __qualname__ → __name__ → repr().
    """
    # Unwrap functools.partial to get the underlying function
    while isinstance(handler, functools.partial):
        handler = handler.func

    if hasattr(handler, '__qualname__'):
        return handler.__qualname__
    if hasattr(handler, '__name__'):
        return handler.__name__
    return repr(handler)


def _record(
    event: BaseEvent[Any],
    conn: Connection,
    *,
    result: Any = None,
    error: Exception | None = None,
) -> None:
    """Record a handler result on an event via Connection → primitives decomposition."""
    event.record_result(
        connection_id=conn.id,
        handler_name=get_handler_name(conn.handler),
        result=result,
        error=error,
    )
