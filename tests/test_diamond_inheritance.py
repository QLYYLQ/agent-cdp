"""Tests for Phase 8b — Diamond inheritance in event MRO matching.

Verifies that:
1. MRO matching: handler on SecurityEvent fires for SecureNavigationEvent
2. MRO matching: handler on NavigationEvent also fires
3. Handler on BrowserEvent fires (base of diamond)
4. No duplicate handler execution (each connection fires once)
5. Priority ordering preserved across diamond MRO chain
6. conscribe registration works for diamond events
"""

from __future__ import annotations

from agent_cdp._registry import EventRegistrar
from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope

# ── Diamond event hierarchy ──


class BrowserEvent(BaseEvent[str]):
    __abstract__ = True


class SecurityDiamondEvent(BrowserEvent):
    __abstract__ = True


class NavigationDiamondEvent(BrowserEvent):
    __abstract__ = True


class SecureNavigationEvent(SecurityDiamondEvent, NavigationDiamondEvent):
    """Diamond: SecurityDiamondEvent + NavigationDiamondEvent both → BrowserEvent → BaseEvent"""

    __registry_key__ = 'diamond.secure_navigation'


# ── Tests ──


class TestDiamondMROMatching:
    def test_security_handler_fires_for_diamond_event(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: SecurityDiamondEvent) -> None:
            calls.append('security')

        scope.connect(SecurityDiamondEvent, handler, mode=ConnectionType.DIRECT)
        scope.emit(SecureNavigationEvent())
        assert 'security' in calls

    def test_navigation_handler_fires_for_diamond_event(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: NavigationDiamondEvent) -> None:
            calls.append('navigation')

        scope.connect(NavigationDiamondEvent, handler, mode=ConnectionType.DIRECT)
        scope.emit(SecureNavigationEvent())
        assert 'navigation' in calls

    def test_base_handler_fires_for_diamond_event(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: BrowserEvent) -> None:
            calls.append('browser')

        scope.connect(BrowserEvent, handler, mode=ConnectionType.DIRECT)
        scope.emit(SecureNavigationEvent())
        assert 'browser' in calls

    def test_no_duplicate_handler_execution(self) -> None:
        """Each connection fires once, even though diamond MRO has shared ancestors."""
        scope = EventScope('s1')
        calls: list[str] = []

        def exact_handler(event: SecureNavigationEvent) -> None:
            calls.append('exact')

        def security_handler(event: SecurityDiamondEvent) -> None:
            calls.append('security')

        def nav_handler(event: NavigationDiamondEvent) -> None:
            calls.append('navigation')

        def base_handler(event: BrowserEvent) -> None:
            calls.append('browser')

        scope.connect(SecureNavigationEvent, exact_handler, mode=ConnectionType.DIRECT, priority=30)
        scope.connect(SecurityDiamondEvent, security_handler, mode=ConnectionType.DIRECT, priority=20)
        scope.connect(NavigationDiamondEvent, nav_handler, mode=ConnectionType.DIRECT, priority=10)
        scope.connect(BrowserEvent, base_handler, mode=ConnectionType.DIRECT, priority=0)

        scope.emit(SecureNavigationEvent())

        # Each connection should fire exactly once
        assert calls.count('exact') == 1
        assert calls.count('security') == 1
        assert calls.count('navigation') == 1
        assert calls.count('browser') == 1
        assert len(calls) == 4

    def test_priority_ordering_across_diamond_mro(self) -> None:
        """Priority ordering is preserved across diamond MRO chain."""
        scope = EventScope('s1')
        order: list[str] = []

        def high_handler(event: BrowserEvent) -> None:
            order.append('high')

        def medium_handler(event: SecurityDiamondEvent) -> None:
            order.append('medium')

        def low_handler(event: SecureNavigationEvent) -> None:
            order.append('low')

        scope.connect(BrowserEvent, high_handler, mode=ConnectionType.DIRECT, priority=100)
        scope.connect(SecurityDiamondEvent, medium_handler, mode=ConnectionType.DIRECT, priority=50)
        scope.connect(SecureNavigationEvent, low_handler, mode=ConnectionType.DIRECT, priority=0)

        scope.emit(SecureNavigationEvent())
        assert order == ['high', 'medium', 'low']


class TestDiamondConscribeRegistration:
    def test_diamond_event_registered(self) -> None:
        cls = EventRegistrar.get('diamond.secure_navigation')  # type: ignore[reportUnknownMemberType]
        assert cls is SecureNavigationEvent

    def test_diamond_event_in_get_all(self) -> None:
        all_events = EventRegistrar.get_all()  # type: ignore[reportUnknownMemberType]
        assert 'diamond.secure_navigation' in all_events
