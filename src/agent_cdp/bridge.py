"""CDPEventBridge — standardized CDP → EventScope event bridging.

Replaces ad-hoc CDP callback wiring patterns with a single utility class.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from agent_cdp.events.base import BaseEvent
from agent_cdp.scope.scope import EventScope

logger = logging.getLogger(__name__)


@runtime_checkable
class CDPClientProtocol(Protocol):
    """Structural type for CDP clients that support event subscription."""

    def on_event(self, method: str, callback: Callable[..., Any]) -> None: ...
    def off_event(self, method: str, callback: Callable[..., Any]) -> None: ...


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

        def _callback(params: dict[str, Any], session_id: str | None = None) -> None:
            # Session-ID filtering — check callback arg first, then params dict
            if self._session_id is not None:
                incoming_sid = session_id or params.get('sessionId')
                if incoming_sid is not None and incoming_sid != self._session_id:
                    return

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

    def close(self) -> None:
        """Remove all CDP callbacks registered by this bridge. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for method, callback in self._registrations:
            self._cdp.off_event(method, callback)
        self._registrations.clear()
