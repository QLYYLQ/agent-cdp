"""conscribe-based watchdog registry.

Provides auto-registration for watchdog classes with a normalized
``attach(scope, session_id=None)`` signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from conscribe import create_registrar

if TYPE_CHECKING:
    from agent_cdp.scope.scope import EventScope


@runtime_checkable
class WatchdogProtocol(Protocol):
    """Protocol that all registered watchdogs must satisfy."""

    def attach(self, scope: EventScope, session_id: str | None = None) -> None: ...


WatchdogRegistrar = create_registrar(
    name='watchdog',
    protocol=WatchdogProtocol,
    discriminator_field='watchdog_type',
    strip_suffixes=['Watchdog'],
)

WatchdogBase = WatchdogRegistrar.bridge(object)  # type: ignore[reportUnknownVariableType,reportUnknownMemberType]
