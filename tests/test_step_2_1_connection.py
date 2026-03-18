"""Tests for ConnectionType + Connection + connect() (C5).

TDD: These tests are written BEFORE the implementation.
Uses ScopeStub to simulate EventScope (C6 not yet implemented).
"""

from __future__ import annotations

import weakref

import pytest

from agent_cdp.connection import Connection, ConnectionType, connect
from agent_cdp.events import BaseEvent

# ── Test event subclass ──


class NavEvent(BaseEvent[str]):
    url: str = 'https://example.com'


# ── ScopeStub — minimal EventScope stand-in ──


class ScopeStub:
    """Minimal EventScope stand-in implementing the 4 methods Connection needs."""

    def __init__(self, scope_id: str = 'stub') -> None:
        self.scope_id = scope_id
        self.connections: list[Connection] = []
        self.incoming: list[Connection] = []

    def _add_connection(self, conn: Connection) -> None:
        self.connections.append(conn)

    def _remove_connection(self, conn: Connection) -> None:
        if conn in self.connections:
            self.connections.remove(conn)

    def _add_incoming(self, conn: Connection) -> None:
        self.incoming.append(conn)

    def _remove_incoming(self, conn: Connection) -> None:
        if conn in self.incoming:
            self.incoming.remove(conn)


def sample_handler(event: NavEvent) -> str:
    return 'handled'


# ── Test groups ──


class TestCreateConnectionViaConnect:
    """Create via connect(); registered on source; unique id; BaseEvent raises TypeError; target incoming registered."""

    def test_connect_returns_connection(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert isinstance(conn, Connection)

    def test_connection_registered_on_source(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn in scope.connections

    def test_connection_has_unique_id(self) -> None:
        scope = ScopeStub()
        c1 = connect(scope, NavEvent, sample_handler)
        c2 = connect(scope, NavEvent, sample_handler)
        assert c1.id != c2.id

    def test_connect_base_event_raises_type_error(self) -> None:
        scope = ScopeStub()
        with pytest.raises(TypeError, match='BaseEvent'):
            connect(scope, BaseEvent, sample_handler)

    def test_target_incoming_registered(self) -> None:
        source = ScopeStub('source')
        target = ScopeStub('target')
        conn = connect(source, NavEvent, sample_handler, target_scope=target)
        assert conn in target.incoming

    def test_default_mode_is_auto(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn.mode is ConnectionType.AUTO

    def test_event_type_stored(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn.event_type is NavEvent

    def test_handler_stored(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn.handler is sample_handler


class TestConnectionIsFrozen:
    """Cannot set mode; cannot set priority."""

    def test_cannot_set_mode(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        with pytest.raises(AttributeError):
            conn.mode = ConnectionType.DIRECT  # type: ignore[misc]

    def test_cannot_set_priority(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        with pytest.raises(AttributeError):
            conn.priority = 99  # type: ignore[misc]


class TestDisconnectSetsInactive:
    """active=True → False; idempotent."""

    def test_initially_active(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn.active is True

    def test_disconnect_sets_inactive(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        conn.disconnect()
        assert conn.active is False

    def test_disconnect_idempotent(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        conn.disconnect()
        conn.disconnect()  # should not raise
        assert conn.active is False


class TestDisconnectRemovesFromScope:
    """Removes from source.connections and target.incoming; scope GC'd → disconnect doesn't error."""

    def test_removed_from_source(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn in scope.connections
        conn.disconnect()
        assert conn not in scope.connections

    def test_removed_from_target(self) -> None:
        source = ScopeStub('s')
        target = ScopeStub('t')
        conn = connect(source, NavEvent, sample_handler, target_scope=target)
        assert conn in target.incoming
        conn.disconnect()
        assert conn not in target.incoming

    def test_scope_gc_disconnect_safe(self) -> None:
        """After source is garbage collected, disconnect should not raise."""
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        del scope  # GC the scope
        conn.disconnect()  # should not raise
        assert conn.active is False


class TestWeakrefScopeReference:
    """source_scope is weakref.ref; target_scope is weakref.ref; no target → None."""

    def test_source_scope_is_weakref(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert isinstance(conn.source_scope, weakref.ref)
        assert conn.source_scope() is scope

    def test_target_scope_is_weakref(self) -> None:
        source = ScopeStub('s')
        target = ScopeStub('t')
        conn = connect(source, NavEvent, sample_handler, target_scope=target)
        assert isinstance(conn.target_scope, weakref.ref)
        assert conn.target_scope() is target

    def test_no_target_is_none(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn.target_scope is None


class TestConnectionWithFilter:
    """filter stored; no filter → None."""

    def test_filter_stored(self) -> None:
        scope = ScopeStub()
        f = lambda e: e.url.startswith('https')  # noqa: E731
        conn = connect(scope, NavEvent, sample_handler, filter=f)
        assert conn.filter is f

    def test_no_filter_is_none(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn.filter is None


class TestConnectionWithPriority:
    """Default 0; custom value; negative value."""

    def test_default_priority_zero(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler)
        assert conn.priority == 0

    def test_custom_priority(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler, priority=100)
        assert conn.priority == 100

    def test_negative_priority(self) -> None:
        scope = ScopeStub()
        conn = connect(scope, NavEvent, sample_handler, priority=-5)
        assert conn.priority == -5


class TestConnectionTypeEnumValues:
    """direct/queued/auto string values; full member set."""

    def test_direct_value(self) -> None:
        assert ConnectionType.DIRECT == 'direct'

    def test_queued_value(self) -> None:
        assert ConnectionType.QUEUED == 'queued'

    def test_auto_value(self) -> None:
        assert ConnectionType.AUTO == 'auto'

    def test_all_members(self) -> None:
        members = set(ConnectionType)
        assert members == {ConnectionType.DIRECT, ConnectionType.QUEUED, ConnectionType.AUTO}
