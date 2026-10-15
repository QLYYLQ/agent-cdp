"""conscribe-based watchdog registry.

Provides auto-registration for watchdog classes with a normalized
``attach(scope, session_id=None)`` signature.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from conscribe import create_registrar

if TYPE_CHECKING:
    from agent_cdp.connection.connection import Connection
    from agent_cdp.scope.scope import EventScope


@runtime_checkable
class WatchdogProtocol(Protocol):
    """Protocol that all registered watchdogs must satisfy."""

    def attach(self, scope: EventScope, session_id: str | None = None) -> None: ...

    def detach(self, scope: EventScope, session_id: str | None = None) -> None:
        """Release exactly this scope/session binding, idempotently."""
        ...


WatchdogRegistrar = create_registrar(
    name='watchdog',
    protocol=WatchdogProtocol,
    discriminator_field='watchdog_type',
    strip_suffixes=['Watchdog'],
)

_WatchdogBridge = WatchdogRegistrar.bridge(object)


class WatchdogBinding:
    """Connections and synchronous cleanup owned by one watchdog attachment.

    Closing a scope closes this binding without touching the watchdog's other
    attachments. Failed cleanup remains available for an explicit retry.
    """

    def __init__(
        self,
        scope: EventScope,
        session_id: str | None = None,
        *,
        connections: Iterable[Connection] = (),
        close_callbacks: Iterable[Callable[[], None]] = (),
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.scope = scope
        self.session_id = session_id
        self._connections = list(connections)
        self._close_callbacks = list(close_callbacks)
        self.closed = False
        self._on_closed = on_closed
        scope.add_close_callback(self.close)

    def close(self) -> None:
        """Detach only this binding; try every cleanup before raising errors."""
        if self.closed:
            return
        errors: list[Exception] = []
        for conn in self._connections[:]:
            try:
                conn.disconnect()
            except Exception as exc:
                errors.append(exc)
            else:
                self._connections.remove(conn)
        for callback in self._close_callbacks[:]:
            try:
                callback()
            except Exception as exc:
                errors.append(exc)
            else:
                self._close_callbacks.remove(callback)
        if errors:
            raise ExceptionGroup(f'Watchdog binding cleanup failed for {self.scope.scope_id}', errors)
        self.closed = True
        self.scope.remove_close_callback(self.close)
        on_closed, self._on_closed = self._on_closed, None
        if on_closed is not None:
            on_closed()


class WatchdogBase(_WatchdogBridge):  # type: ignore[reportUntypedBaseClass]
    """Registered watchdog base with opt-in ownership of per-scope bindings.

    Implement ``attach`` and pass its connections/bridge close methods to
    ``track_binding``. Keep session-specific handler data in closures or binding
    objects, rather than overwriting one shared ``self.session_id``.
    """

    __abstract__ = True

    def track_binding(
        self,
        scope: EventScope,
        session_id: str | None = None,
        *,
        connections: Iterable[Connection] = (),
        close_callbacks: Iterable[Callable[[], None]] = (),
    ) -> WatchdogBinding:
        """Record resources from an attachment and bind their cleanup to scope."""
        bindings: list[WatchdogBinding] = self.__dict__.setdefault('_watchdog_bindings', [])
        bindings[:] = [binding for binding in bindings if not binding.closed]
        binding = WatchdogBinding(
            scope,
            session_id,
            connections=connections,
            close_callbacks=close_callbacks,
            on_closed=lambda: bindings.remove(binding),
        )
        bindings.append(binding)
        return binding

    @property
    def bindings(self) -> tuple[WatchdogBinding, ...]:
        """Snapshot of active attachments."""
        bindings: list[WatchdogBinding] = self.__dict__.get('_watchdog_bindings', [])
        return tuple(binding for binding in bindings if not binding.closed)

    def detach(self, scope: EventScope, session_id: str | None = None) -> None:
        """Detach exactly the matching scope/session attachments; safe to repeat."""
        errors: list[Exception] = []
        for binding in self.bindings:
            if binding.scope is scope and binding.session_id == session_id:
                try:
                    binding.close()
                except Exception as exc:
                    errors.append(exc)
        bindings: list[WatchdogBinding] = self.__dict__.get('_watchdog_bindings', [])
        bindings[:] = [binding for binding in bindings if not binding.closed]
        if errors:
            raise ExceptionGroup(f'Watchdog detach failed for {scope.scope_id}', errors)
