"""Immediately armed, explicitly owned observation waiters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from types import TracebackType
from typing import TYPE_CHECKING, Any, Generic, Self, TypeVar, cast

from agent_cdp.connection.connection import Connection
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.base import BaseEvent
from agent_cdp.events.contracts import Participation

if TYPE_CHECKING:
    from agent_cdp.scope.scope import EventScope

T_Event = TypeVar('T_Event', bound=BaseEvent[Any])
_EXPECT_PRIORITY = -1000


class EventWaiter(Generic[T_Event]):
    """Awaitable observation installed at construction, with sync/async contexts.

    The timeout starts at registration, even if the object is never awaited.
    Use a context manager or ``close()`` when abandoning a waiter without a
    timeout. Closing or cancelling the waiter never cancels event execution.
    """

    def __init__(
        self,
        scope: EventScope,
        event_type: type[T_Event],
        *,
        include: Callable[[T_Event], bool],
        exclude: Callable[[T_Event], bool],
        timeout: float | None,
    ) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError('timeout must be non-negative or None')
        loop = asyncio.get_running_loop()
        registered_at = loop.time()
        self._future: asyncio.Future[T_Event] = loop.create_future()
        self._scope: EventScope | None = scope
        self._connection: Connection | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

        def receive(event: T_Event) -> None:
            if not self._future.done() and include(event) and not exclude(event):
                self._future.set_result(event)
                self._detach()

        self._connection = scope.connect(
            event_type,
            receive,
            mode=ConnectionType.DIRECT,
            priority=_EXPECT_PRIORITY,
            participation=Participation.OBSERVER,
        )
        try:
            scope.add_close_callback(self.close)
        except BaseException:
            self.close()
            raise
        if timeout is not None:
            scope_id = scope.scope_id
            event_name = cast(type[Any], event_type).__name__

            def expire() -> None:
                if not self._future.done():
                    self._future.set_exception(
                        TimeoutError(
                            f'No matching {event_name} in scope {scope_id!r} within {timeout}s of registration'
                        )
                    )
                    # A timed-out waiter may intentionally never be awaited.
                    # Retrieving here prevents unhandled-future warnings while
                    # preserving the exception for every later await.
                    self._future.exception()
                self._detach()

            self._timer = loop.call_at(registered_at + timeout, expire)

    @property
    def closed(self) -> bool:
        """Whether the observation connection has been released."""
        return self._closed

    @property
    def done(self) -> bool:
        return self._future.done()

    def _detach(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._connection is not None:
            self._connection.disconnect()
            self._connection = None
        if self._scope is not None:
            self._scope.remove_close_callback(self.close)
            self._scope = None

    def close(self) -> None:
        """Release the listener; unfinished awaits raise ``CancelledError``."""
        if not self._future.done():
            self._future.cancel()
        self._detach()

    async def _wait(self) -> T_Event:
        try:
            return await self._future
        finally:
            self.close()

    def __await__(self) -> Generator[Any, None, T_Event]:
        return self._wait().__await__()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


def expect(
    scope: EventScope,
    event_type: type[T_Event],
    *,
    include: Callable[[T_Event], bool] = lambda _: True,
    exclude: Callable[[T_Event], bool] = lambda _: False,
    timeout: float | None = None,
) -> EventWaiter[T_Event]:
    """Register now, then await the next matching event.

    Example::

        async with expect(scope, PageLoaded, timeout=5) as waiter:
            await navigate()
            loaded = await waiter

    This is an awaitable object, not a coroutine: ``asyncio.ensure_future`` can
    schedule it when needed, but registration requires no task or scheduling.
    """
    return EventWaiter(scope, event_type, include=include, exclude=exclude, timeout=timeout)
