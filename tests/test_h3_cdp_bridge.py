"""Tests for H3 — CDPEventBridge and CDPClientProtocol."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_cdp.bridge import CDPClientProtocol, CDPEventBridge
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope

# ── Test event ──


class CDPPageLoad(BaseEvent[dict[str, Any]]):
    timestamp: float = 0.0


# ── Fake CDP client ──


class FakeCDPClient:
    """Minimal CDP client satisfying CDPClientProtocol, with a fire() helper."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[..., Any]]] = {}

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        self._listeners.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        cbs = self._listeners.get(method, [])
        if callback in cbs:
            cbs.remove(callback)

    def fire(self, method: str, params: dict[str, Any]) -> None:
        """Simulate a CDP event arriving."""
        for cb in list(self._listeners.get(method, [])):
            cb(params)


# ── Tests ──


class TestCDPClientProtocol:
    """CDPClientProtocol structural checks."""

    def test_fake_client_satisfies_protocol(self) -> None:
        assert isinstance(FakeCDPClient(), CDPClientProtocol)

    def test_arbitrary_object_rejects(self) -> None:
        assert not isinstance(object(), CDPClientProtocol)


class TestBridgeEmitsEvent:
    """bridge() should emit events on the scope when CDP fires."""

    def test_bridge_emits_event(self) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        handler = MagicMock()
        scope.connect(CDPPageLoad, handler, mode='direct')

        bridge.bridge('Page.loadEventFired', lambda p: CDPPageLoad(timestamp=p.get('timestamp', 0.0)))

        cdp.fire('Page.loadEventFired', {'timestamp': 1.5})

        handler.assert_called_once()
        event = handler.call_args[0][0]
        assert isinstance(event, CDPPageLoad)
        assert event.timestamp == 1.5


class TestSessionIdFiltering:
    """Session-ID filtering skips events from wrong sessions."""

    def test_session_id_filtering(self) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope, session_id='sess-A')

        handler = MagicMock()
        scope.connect(CDPPageLoad, handler, mode='direct')

        bridge.bridge('Page.loadEventFired', lambda p: CDPPageLoad())

        # Wrong session → should be ignored
        cdp.fire('Page.loadEventFired', {'sessionId': 'sess-B'})
        handler.assert_not_called()

        # Correct session → should fire
        cdp.fire('Page.loadEventFired', {'sessionId': 'sess-A'})
        handler.assert_called_once()

    def test_no_session_filter_when_none(self) -> None:
        """session_id=None → bridge all events regardless of sessionId."""
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)  # no session_id

        handler = MagicMock()
        scope.connect(CDPPageLoad, handler, mode='direct')

        bridge.bridge('Page.loadEventFired', lambda p: CDPPageLoad())

        cdp.fire('Page.loadEventFired', {'sessionId': 'any-session'})
        handler.assert_called_once()

    def test_no_session_id_in_params_passes_through(self) -> None:
        """When session_id is set but params lack sessionId, event should still fire."""
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope, session_id='sess-A')

        handler = MagicMock()
        scope.connect(CDPPageLoad, handler, mode='direct')

        bridge.bridge('Page.loadEventFired', lambda p: CDPPageLoad())

        cdp.fire('Page.loadEventFired', {})  # no sessionId key
        handler.assert_called_once()


class TestCloseRemovesCallbacks:
    """close() removes all CDP callbacks."""

    def test_close_removes_callbacks(self) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        handler = MagicMock()
        scope.connect(CDPPageLoad, handler, mode='direct')

        bridge.bridge('Page.loadEventFired', lambda p: CDPPageLoad())
        bridge.close()

        cdp.fire('Page.loadEventFired', {})
        handler.assert_not_called()

    def test_close_idempotent(self) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)
        bridge.bridge('Page.loadEventFired', lambda p: CDPPageLoad())
        bridge.close()
        bridge.close()  # should not raise

    def test_bridge_after_close_raises(self) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)
        bridge.close()

        with pytest.raises(RuntimeError, match='closed'):
            bridge.bridge('Page.loadEventFired', lambda p: CDPPageLoad())


class TestFactoryErrorLogged:
    """Factory errors are logged, not propagated."""

    def test_factory_error_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        def bad_factory(params: dict[str, Any]) -> CDPPageLoad:
            raise ValueError('bad params')

        bridge.bridge('Page.loadEventFired', bad_factory)

        with caplog.at_level(logging.ERROR):
            cdp.fire('Page.loadEventFired', {})

        assert 'event_factory' in caplog.text
        assert 'bad params' in caplog.text
