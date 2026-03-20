"""CDPEventBridge — standardized CDP → EventScope event bridging.

Replaces ad-hoc CDP callback wiring patterns with a single utility class.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from agent_cdp.events.base import BaseEvent
from agent_cdp.scope.scope import EventScope

logger = logging.getLogger(__name__)


@runtime_checkable
class CDPClientProtocol(Protocol):
    """Structural type for CDP clients that support event subscription."""

    def on_event(self, method: str, callback: Callable[..., Any]) -> None: ...
    def off_event(self, method: str, callback: Callable[..., Any]) -> None: ...


@runtime_checkable
class CDPCommandProtocol(CDPClientProtocol, Protocol):
    """CDP client that supports both event subscription and command sending."""

    @property
    def is_connected(self) -> bool: ...

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


class PausedTarget:
    """Async context manager for pause-setup-resume coordination.

    Ensures all CDP event bridges and scope connections are registered before
    the browser target is resumed.  Two usage modes:

    Mode A — pass a ``CDPCommandProtocol`` instance::

        async with PausedTarget(cdp=client, session_id='s1') as ctx:
            bridge = CDPEventBridge(client, scope, session_id='s1')
            ...
        # Runtime.runIfWaitingForDebugger sent automatically on exit

    Mode B — pass a custom async *resume* callable::

        async with PausedTarget(resume=my_fn):
            ...
        # my_fn() called on exit

    Guarantees:
    - Resume is called even if the body raises an exception.
    - Resume is idempotent (called at most once).
    - Not reentrant (entering twice raises ``RuntimeError``).
    """

    def __init__(
        self,
        *,
        cdp: CDPCommandProtocol | None = None,
        resume: Callable[[], Coroutine[Any, Any, None]] | None = None,
        session_id: str | None = None,
    ) -> None:
        if (cdp is None) == (resume is None):
            msg = 'Provide exactly one of cdp or resume, not both/neither'
            raise ValueError(msg)

        self._cdp = cdp
        self._session_id = session_id
        self._custom_resume = resume
        self._resumed = False
        self._entered = False

    async def __aenter__(self) -> Self:
        if self._entered:
            msg = 'PausedTarget already entered; not reentrant'
            raise RuntimeError(msg)
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.resume()

    async def resume(self) -> None:
        """Resume the paused target. Idempotent — second call is a no-op."""
        if self._resumed:
            return
        self._resumed = True

        if self._custom_resume is not None:
            await self._custom_resume()
        elif self._cdp is not None:
            params: dict[str, Any] | None = None
            if self._session_id is not None:
                params = {'sessionId': self._session_id}
            await self._cdp.send('Runtime.runIfWaitingForDebugger', params)


class CDPEventBridge:
    """Bridge CDP events into an EventScope.

    Usage::

        bridge = CDPEventBridge(cdp_client, scope, session_id='ABC')
        bridge.bridge('Page.loadEventFired', lambda params: PageLoadEvent(**params))
        # ... later
        bridge.close()

    Key behaviors:
    - ``bridge()`` registers a CDP callback that constructs an event via *event_factory*
      and emits it on the bound scope.
    - Optional *session_id* filtering: when set, CDP messages whose ``sessionId``
      does not match are silently ignored.
    - Exceptions in *event_factory* are logged, not propagated — a broken factory
      must not kill the CDP message loop.
    - ``close()`` removes all registered CDP callbacks. Idempotent.
    """

    def __init__(
        self,
        cdp: CDPClientProtocol,
        scope: EventScope,
        *,
        session_id: str | None = None,
    ) -> None:
        self._cdp = cdp
        self._scope = scope
        self._session_id = session_id
        self._registrations: list[tuple[str, Callable[..., Any]]] = []
        self._hit_counts: dict[str, int] = {}
        self._closed = False

    def bridge(
        self,
        cdp_method: str,
        event_factory: Callable[[dict[str, Any]], BaseEvent[Any]],
    ) -> None:
        """Register a CDP method → event factory mapping.

        The factory receives the CDP ``params`` dict and must return a
        :class:`BaseEvent` subclass instance.
        """
        if self._closed:
            msg = 'Cannot bridge on a closed CDPEventBridge'
            raise RuntimeError(msg)

        self._hit_counts[cdp_method] = 0

        def _callback(params: dict[str, Any], session_id: str | None = None) -> None:
            # Session-ID filtering — check callback arg first, then params dict
            if self._session_id is not None:
                incoming_sid = session_id or params.get('sessionId')
                if incoming_sid is not None and incoming_sid != self._session_id:
                    return

            self._hit_counts[cdp_method] += 1

            try:
                event = event_factory(params)
            except Exception:
                logger.exception(
                    'CDPEventBridge: event_factory for %r raised',
                    cdp_method,
                )
                return

            self._scope.emit(event)

        self._cdp.on_event(cdp_method, _callback)
        self._registrations.append((cdp_method, _callback))

    @property
    def hit_counts(self) -> dict[str, int]:
        """Per-method hit counts. Useful for diagnostics and testing."""
        return dict(self._hit_counts)

    def close(self) -> None:
        """Remove all CDP callbacks registered by this bridge. Idempotent.

        Logs a warning for any bridged method that never received an event
        (zero-hit report) — helps detect silent misconfigurations like
        ``Browser.*`` on a page-level WebSocket.
        """
        if self._closed:
            return
        self._closed = True

        zero_hit = [m for m, c in self._hit_counts.items() if c == 0]
        if zero_hit:
            logger.warning(
                'CDPEventBridge(%s): zero-hit methods (never fired): %s — '
                'verify these CDP domains are enabled and reachable on this connection',
                self._scope.scope_id,
                zero_hit,
            )

        for method, callback in self._registrations:
            self._cdp.off_event(method, callback)
        self._registrations.clear()

    @staticmethod
    def paused(
        *,
        cdp: CDPCommandProtocol | None = None,
        resume: Callable[[], Coroutine[Any, Any, None]] | None = None,
        session_id: str | None = None,
    ) -> PausedTarget:
        """Convenience factory for :class:`PausedTarget`."""
        return PausedTarget(cdp=cdp, resume=resume, session_id=session_id)
