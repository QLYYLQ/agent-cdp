"""ScopeEventLoop — full asyncio Task implementation for queued handler execution.

Includes backpressure support: bounded queue (default maxsize=1024) with drop-newest
policy when full. Safe by default — callers must explicitly opt into unbounded queues.

Per-handler timeout: wraps async handler execution with asyncio.wait_for using
event.event_timeout. Scope-level deadlock monitor scans active handlers periodically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from agent_cdp._context import _current_connection, _current_event
from agent_cdp.scope._helpers import _record, get_handler_name

if TYPE_CHECKING:
    from agent_cdp.connection.connection import Connection
    from agent_cdp.events.base import BaseEvent

logger = logging.getLogger(__name__)

# Default queue capacity. Safe by default — prevents OOM from high-frequency events
# (e.g., DOM mutations from 5 tabs flooding simultaneously). Set maxsize=0 to opt
# into unbounded queues when the caller is confident about event flow control.
DEFAULT_QUEUE_MAXSIZE = 1024

# Default deadlock monitor settings
DEFAULT_DEADLOCK_SCAN_INTERVAL = 5.0
DEFAULT_DEADLOCK_THRESHOLD = 15.0


class ScopeEventLoop:
    """Asyncio Task-based event loop that drains a queue of (event, connection) pairs.

    Processes queued handlers in FIFO order with full ContextVar tracking,
    result/error recording, and graceful stop semantics.

    Backpressure: When the queue is full (maxsize reached), new events are dropped
    with a warning log including queue size and event type for diagnostics.

    Deadlock detection: A single scope-level monitor task periodically scans active
    handlers, warning once per handler that exceeds the threshold. Replaces the
    previous per-handler task creation pattern for lower overhead.
    """

    _queue: asyncio.Queue[tuple[BaseEvent[object], Connection]]

    def __init__(
        self,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        *,
        deadlock_scan_interval: float = DEFAULT_DEADLOCK_SCAN_INTERVAL,
        deadlock_threshold: float = DEFAULT_DEADLOCK_THRESHOLD,
    ) -> None:
        self._queue: asyncio.Queue[tuple[BaseEvent[object], Connection]] = asyncio.Queue(
            maxsize=maxsize,
        )
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False

        # Deadlock monitor state
        self._active_handlers: dict[int, tuple[str, float]] = {}
        self._handler_id_counter: int = 0
        self._warned_handlers: set[int] = set()
        self._monitor_task: asyncio.Task[None] | None = None
        self._deadlock_scan_interval = deadlock_scan_interval
        self._deadlock_threshold = deadlock_threshold

    async def start(self) -> None:
        """Start the event loop task. Idempotent — safe to call multiple times."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        self._monitor_task = asyncio.create_task(self._deadlock_monitor())

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

        # Stop deadlock monitor
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

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
        """Execute a single queued handler with ContextVar tracking and timeout.

        Timeout: uses event.event_timeout (None = no timeout). On timeout, records a
        TimeoutError in the event results without crashing the loop.

        Deadlock detection is handled by the scope-level _deadlock_monitor task,
        which periodically scans _active_handlers for long-running entries.
        """
        token = _current_event.set(event)
        conn_token = _current_connection.set(conn)
        handler_name = get_handler_name(conn.handler)
        timeout = event.event_timeout

        handler_id = self._register_handler(handler_name)

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
            self._unregister_handler(handler_id)
            _current_connection.reset(conn_token)
            _current_event.reset(token)
            event._decrement_pending()

    # ── Deadlock monitor ──

    def _register_handler(self, handler_name: str) -> int:
        """Register a handler as active. Returns a unique handler ID."""
        self._handler_id_counter += 1
        hid = self._handler_id_counter
        self._active_handlers[hid] = (handler_name, time.monotonic())
        return hid

    def _unregister_handler(self, handler_id: int) -> None:
        """Unregister a handler when it completes."""
        self._active_handlers.pop(handler_id, None)
        self._warned_handlers.discard(handler_id)

    async def _deadlock_monitor(self) -> None:
        """Scope-level monitor that periodically scans active handlers.

        Logs a warning once per handler that exceeds the deadlock threshold.
        Replaces the previous per-handler asyncio.create_task pattern, reducing
        overhead from ~15µs/handler to ~0.1µs/handler (dict ops only).
        """
        while self._running:
            await asyncio.sleep(self._deadlock_scan_interval)
            now = time.monotonic()
            for hid, (name, start) in list(self._active_handlers.items()):
                elapsed = now - start
                if elapsed >= self._deadlock_threshold and hid not in self._warned_handlers:
                    self._warned_handlers.add(hid)
                    logger.warning(
                        'Handler %s has been running for %.1fs — possible deadlock or slow operation',
                        name,
                        elapsed,
                    )

    def _discard_remaining(self) -> None:
        """Drain the queue without executing handlers. Decrement pending for each."""
        while not self._queue.empty():
            try:
                event, _conn = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            event._decrement_pending()
            self._queue.task_done()
