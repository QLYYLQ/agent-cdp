"""Tests for H5 — CDPEventBridge zero-hit report and hit_counts property."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from agent_cdp.bridge import CDPCommandProtocol, CDPEventBridge
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope

# ── Test event ──


class CDPTestEvent(BaseEvent[dict[str, Any]]):
    value: str = ''


# ── Fake CDP clients ──


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


class FakeCDPCommandClient(FakeCDPClient):
    """Fake CDP client satisfying CDPCommandProtocol."""

    def __init__(self) -> None:
        super().__init__()
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> Any:
        return {}


# ── Tests: zero-hit report ──


class TestZeroHitReport:
    """close() should warn about bridged methods that never received events."""

    def test_zero_hit_warning_on_close(self, caplog: pytest.LogCaptureFixture) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        bridge.bridge('Browser.downloadWillBegin', lambda p: CDPTestEvent())
        bridge.bridge('Page.loadEventFired', lambda p: CDPTestEvent())

        # Only fire Page event — Browser event never arrives
        cdp.fire('Page.loadEventFired', {})

        with caplog.at_level(logging.WARNING):
            bridge.close()

        assert 'zero-hit' in caplog.text
        assert 'Browser.downloadWillBegin' in caplog.text
        assert 'Page.loadEventFired' not in caplog.text  # had a hit

    def test_no_warning_when_all_methods_hit(self, caplog: pytest.LogCaptureFixture) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        bridge.bridge('Page.loadEventFired', lambda p: CDPTestEvent())
        cdp.fire('Page.loadEventFired', {})

        with caplog.at_level(logging.WARNING):
            bridge.close()

        assert 'zero-hit' not in caplog.text

    def test_zero_hit_all_methods(self, caplog: pytest.LogCaptureFixture) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        bridge.bridge('Inspector.targetCrashed', lambda p: CDPTestEvent())
        bridge.bridge('Browser.downloadWillBegin', lambda p: CDPTestEvent())

        with caplog.at_level(logging.WARNING):
            bridge.close()

        assert 'Inspector.targetCrashed' in caplog.text
        assert 'Browser.downloadWillBegin' in caplog.text

    def test_close_idempotent_no_double_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        bridge.bridge('Page.loadEventFired', lambda p: CDPTestEvent())

        with caplog.at_level(logging.WARNING):
            bridge.close()
            caplog.clear()
            bridge.close()

        # Second close should not produce warnings
        assert 'zero-hit' not in caplog.text


class TestHitCountsProperty:
    """hit_counts property exposes per-method counts."""

    def test_hit_counts_tracks_events(self) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)

        bridge.bridge('Page.loadEventFired', lambda p: CDPTestEvent())
        bridge.bridge('Network.requestWillBeSent', lambda p: CDPTestEvent())

        assert bridge.hit_counts == {'Page.loadEventFired': 0, 'Network.requestWillBeSent': 0}

        cdp.fire('Page.loadEventFired', {})
        cdp.fire('Page.loadEventFired', {})
        cdp.fire('Network.requestWillBeSent', {})

        assert bridge.hit_counts == {'Page.loadEventFired': 2, 'Network.requestWillBeSent': 1}

    def test_hit_counts_returns_copy(self) -> None:
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope)
        bridge.bridge('Page.loadEventFired', lambda p: CDPTestEvent())

        counts = bridge.hit_counts
        counts['Page.loadEventFired'] = 999
        assert bridge.hit_counts['Page.loadEventFired'] == 0  # original unmodified

    def test_session_filtered_events_dont_count(self) -> None:
        """Events filtered out by session_id should NOT increment hit count."""
        cdp = FakeCDPClient()
        scope = EventScope('tab-1')
        bridge = CDPEventBridge(cdp, scope, session_id='sess-A')

        bridge.bridge('Page.loadEventFired', lambda p: CDPTestEvent())

        # Wrong session → filtered, should not count
        cdp.fire('Page.loadEventFired', {'sessionId': 'sess-B'})
        assert bridge.hit_counts['Page.loadEventFired'] == 0

        # Correct session → should count
        cdp.fire('Page.loadEventFired', {'sessionId': 'sess-A'})
        assert bridge.hit_counts['Page.loadEventFired'] == 1


# ── Tests: CDPCommandProtocol.is_connected ──


class TestCDPCommandProtocolIsConnected:
    """CDPCommandProtocol now requires is_connected property."""

    def test_fake_command_client_satisfies_protocol(self) -> None:
        client = FakeCDPCommandClient()
        assert isinstance(client, CDPCommandProtocol)

    def test_is_connected_returns_bool(self) -> None:
        client = FakeCDPCommandClient()
        assert client.is_connected is True
        client._connected = False
        assert client.is_connected is False

    def test_client_without_is_connected_rejects_protocol(self) -> None:
        """A client that has send/on_event/off_event but no is_connected should not satisfy CDPCommandProtocol."""
        assert not isinstance(FakeCDPClient(), CDPCommandProtocol)
