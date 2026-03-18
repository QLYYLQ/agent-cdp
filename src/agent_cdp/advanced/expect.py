"""expect() — wait for the next matching event emitted on a scope.

Creates a temporary Direct connection with very low priority, backed by an
asyncio.Future. Automatically disconnects after the future resolves or times out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

# Use TYPE_CHECKING to avoid circular import for type hints
from typing import TYPE_CHECKING, TypeVar

from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import BaseEvent

if TYPE_CHECKING:
    from agent_cdp.scope.scope import EventScope

T_Event = TypeVar('T_Event', bound=BaseEvent[object])

# Very low priority so expect() doesn't interfere with normal handlers
_EXPECT_PRIORITY = -1000


async def expect(
    scope: EventScope,
    event_type: type[T_Event],
    *,
    include: Callable[[T_Event], bool] = lambda _: True,
    exclude: Callable[[T_Event], bool] = lambda _: False,
    timeout: float | None = None,
) -> T_Event:
    """Wait for the next matching event emitted on scope.

    Creates a temporary Direct connection that sets an asyncio.Future when a
    matching event is emitted. The connection is automatically disconnected
    after the future resolves or the timeout expires.

    Args:
        scope: The EventScope to listen on.
        event_type: The event class to wait for.
        include: Optional predicate — only events where include(event) is True match.
        exclude: Optional predicate — events where exclude(event) is True are skipped.
        timeout: Maximum seconds to wait. None means wait indefinitely.

    Returns:
        The first matching event.

    Raises:
        TimeoutError: If no matching event arrives within the timeout.
    """
    future: asyncio.Future[T_Event] = asyncio.get_running_loop().create_future()

    def _waiter(event: T_Event) -> None:
        if not future.done() and include(event) and not exclude(event):
            future.set_result(event)

    conn = scope.connect(
        event_type,
        _waiter,
        mode=ConnectionType.DIRECT,
        priority=_EXPECT_PRIORITY,
    )
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        conn.disconnect()
