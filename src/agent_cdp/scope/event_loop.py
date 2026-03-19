"""ScopeEventLoop — full asyncio Task implementation for queued handler execution.

Includes backpressure support: bounded queue (default maxsize=1024) with drop-newest
policy when full. Safe by default — callers must explicitly opt into unbounded queues.

Per-handler timeout: wraps async handler execution with asyncio.wait_for using
event.event_timeout. Deadlock monitor task warns after 15s of continuous execution.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from agent_cdp._context import _current_connection, _current_event
from agent_cdp.advanced.timeout import _deadlock_warning
from agent_cdp.scope._helpers import _record, get_handler_name

if TYPE_CHECKING:
    from agent_cdp.connection.connection import Connection
    from agent_cdp.events.base import BaseEvent

logger = logging.getLogger(__name__)

# Default queue capacity. Safe by default — prevents OOM from high-frequency events
# (e.g., DOM mutations from 5 tabs flooding simultaneously). Set maxsize=0 to opt
# into unbounded queues when the caller is confident about event flow control.
DEFAULT_QUEUE_MAXSIZE = 1024


class ScopeEventLoop:
    """Asyncio Task-based event loop that drains a queue of (event, connection) pairs.

    Processes queued handlers in FIFO order with full ContextVar tracking,
    result/error recording, and graceful stop semantics.

    Backpressure: When the queue is full (maxsize reached), new events are dropped
    with a warning log including queue size and event type for diagnostics.
    """

    _queue: asyncio.Queue[tuple[BaseEvent[object], Connection]]

    def __init__(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        self._queue: asyncio.Queue[tuple[BaseEvent[object], Connection]] = asyncio.Queue(
            maxsize=maxsize,
        )
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False

    async def start(self) -> None:
        """Start the event loop task. Idempotent — safe to call multiple times."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, drain: bool = True) -> None:
        """Stop the event loop.

        drain=True: let _run() process remaining items then exit.
        drain=False: discard remaining items and cancel task.
        """
        if not self._running:
            return
        self._running = False

        if not drain:
            self._discard_remaining()

        if self._task is not None:
            if drain:
                # Wait for the task to finish naturally (it will exit the while loop
                # since _running is False, then drain remaining items)
                try:
                    await asyncio.wait_for(self._task, timeout=10.0)
                except TimeoutError:
                    self._task.cancel()
                    try:
                        await self._task
                    except asyncio.CancelledError:
                        pass
            else:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None

    def enqueue(self, event: BaseEvent[object], connection: Connection) -> None:
        """Enqueue an event+connection pair for later processing.

        If the queue is full (backpressure), the event is dropped and the pending
        count is decremented to avoid deadlocking await on the event. A warning is
        logged with queue size and event type for diagnostics.
        """
        try:
            self._queue.put_nowait((event, connection))
        except asyncio.QueueFull:
            event_type_name = type(event).__name__
            logger.warning(
                'Backpressure: dropping %s event (queue full, size=%d, maxsize=%d) — '
                'consider increasing maxsize or reducing event frequency',
                event_type_name,
                self._queue.qsize(),
                self._queue.maxsize,
            )
            # Decrement pending so `await event` doesn't hang forever
            event._decrement_pending()

    async def _run(self) -> None:
        """Main event loop — process queued (event, connection) pairs."""
        while self._running:
            try:
                event, conn = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._execute_handler(event, conn)
            finally:
                self._queue.task_done()

        # After _running=False, drain remaining items
        while not self._queue.empty():
            try:
                event, conn = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await self._execute_handler(event, conn)
            finally:
                self._queue.task_done()

    async def _execute_handler(self, event: BaseEvent[object], conn: Connection) -> None:
        """Execute a single queued handler with ContextVar tracking, timeout, and deadlock detection.

        Timeout: uses event.event_timeout (None = no timeout). On timeout, records a
        TimeoutError in the event results without crashing the loop.

        Deadlock detection: a background task logs a warning if the handler runs for
        longer than 15 seconds.
        """
        token = _current_event.set(event)
        conn_token = _current_connection.set(conn)
        handler_name = get_handler_name(conn.handler)
        timeout = event.event_timeout

        # Deadlock monitor — warns if handler runs too long
        deadlock_task = asyncio.create_task(_deadlock_warning(handler_name))

        try:
            coro = conn.handler(event)
            if timeout is not None:
                result = await asyncio.wait_for(coro, timeout=timeout)
            else:
                result = await coro
            _record(event, conn, result=result)
        except TimeoutError:
            _record(
                event,
                conn,
                error=TimeoutError(f'Handler {handler_name} timed out after {timeout}s'),
            )
        except Exception as exc:
            _record(event, conn, error=exc)
            logger.warning(
                'Queued handler %s raised %s: %s',
                handler_name,
                type(exc).__name__,
                exc,
            )
        finally:
            deadlock_task.cancel()
            try:
                await deadlock_task
            except asyncio.CancelledError:
                pass
            _current_connection.reset(conn_token)
            _current_event.reset(token)
            event._decrement_pending()

    def _discard_remaining(self) -> None:
        """Drain the queue without executing handlers. Decrement pending for each."""
        while not self._queue.empty():
            try:
                event, _conn = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            event._decrement_pending()
            self._queue.task_done()
