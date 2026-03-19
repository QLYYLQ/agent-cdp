"""Tests for H1 — ScopeProtocol type safety and aggregation TypeVar propagation."""

from __future__ import annotations

import pytest

from agent_cdp._protocols import ScopeProtocol
from agent_cdp.connection.connection import Connection, connect
from agent_cdp.events import BaseEvent
from agent_cdp.events.aggregation import event_result, event_results_list
from agent_cdp.events.result import EventResult, ResultStatus
from agent_cdp.scope import EventScope


class StringEvent(BaseEvent[str]):
    pass


class IntEvent(BaseEvent[int]):
    pass


# ── ScopeStub (same as test_step_2_1 but verified against Protocol) ──


class ScopeStub:
    """Minimal scope satisfying ScopeProtocol."""

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


class TestScopeProtocol:
    """ScopeProtocol structural checks."""

    def test_event_scope_satisfies_protocol(self) -> None:
        scope = EventScope('test')
        assert isinstance(scope, ScopeProtocol)

    def test_scope_stub_satisfies_protocol(self) -> None:
        stub = ScopeStub('stub')
        assert isinstance(stub, ScopeProtocol)

    def test_arbitrary_object_rejects(self) -> None:
        assert not isinstance(object(), ScopeProtocol)

    def test_missing_method_rejects(self) -> None:
        class Incomplete:
            scope_id: str = 'x'
            def _add_connection(self, conn: Connection) -> None: ...
            # missing _remove_connection, _add_incoming, _remove_incoming

        assert not isinstance(Incomplete(), ScopeProtocol)


class TestConnectWithProtocol:
    """connect() works with any ScopeProtocol-satisfying object."""

    def test_connect_with_scope_stub(self) -> None:
        stub = ScopeStub()
        conn = connect(stub, StringEvent, lambda e: 'ok')
        assert conn.active
        assert conn in stub.connections

    def test_connect_with_event_scope(self) -> None:
        scope = EventScope('real')
        conn = connect(scope, StringEvent, lambda e: 'ok')
        assert conn.active

    def test_disconnect_removes_from_stub(self) -> None:
        stub = ScopeStub()
        conn = connect(stub, StringEvent, lambda e: 'ok')
        conn.disconnect()
        assert conn not in stub.connections


class TestAggregationTypedResult:
    """Aggregation functions preserve type info at runtime."""

    @pytest.mark.asyncio
    async def test_event_result_returns_correct_type(self) -> None:
        e = StringEvent()
        e.record_result(connection_id='c1', handler_name='h', result='hello')
        result = await event_result(e)
        assert result == 'hello'
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_event_results_list_returns_typed_list(self) -> None:
        e = IntEvent()
        e.record_result(connection_id='c1', handler_name='h1', result=42)
        e.record_result(connection_id='c2', handler_name='h2', result=99)
        results = await event_results_list(e)
        assert results == [42, 99]
        assert all(isinstance(r, int) for r in results)

    @pytest.mark.asyncio
    async def test_event_results_dict_type(self) -> None:
        """event_results stores EventResult objects, not raw values."""
        e = StringEvent()
        e.record_result(connection_id='c1', handler_name='h', result='val')
        er = e.event_results['c1']
        assert isinstance(er, EventResult)
        assert er.status is ResultStatus.COMPLETED
