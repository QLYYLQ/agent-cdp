"""Per-handler timeout utilities — deadlock warning monitor.

Provides the background deadlock detection task used by ScopeEventLoop._execute_handler.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Default delay before emitting a deadlock warning (seconds)
DEFAULT_DEADLOCK_DELAY = 15.0


async def _deadlock_warning(handler_name: str, delay: float = DEFAULT_DEADLOCK_DELAY) -> None:
    """Background task that logs a warning if a handler runs too long.

    Launched alongside each queued handler execution. Cancelled in the finally
    block when the handler completes (normally, via error, or via timeout).
    """
    await asyncio.sleep(delay)
    logger.warning(
        'Handler %s has been running for %.1fs — possible deadlock or slow operation',
        handler_name,
        delay,
    )
