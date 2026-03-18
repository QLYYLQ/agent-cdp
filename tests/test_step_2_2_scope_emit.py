"""Tests for EventScope core: emit + connection management (C6).

Covers Direct dispatch, EmitPolicy, priority, consume, filter,
MRO matching, connect_all, Auto mode, ContextVar parent tracking,
and has_pending for Queued stubs.
"""

from __future__ import annotations

import functools
from typing import Any, ClassVar

import pytest

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent, EmitPolicy
from agent_cdp.scope import EventScope

# ── Test event subclasses ──


class NavEvent(BaseEvent[str]):
    __registry_key__ = 'scope_test_nav'
    url: str = 'https://example.com'


class LifecycleEvent(BaseEvent[None]):
    __abstract__ = True


class SessionStartEvent(LifecycleEvent):
    __registry_key__ = 'scope_test_session_start'
    session_id: str = 'test-session'


class CollectErrorsEvent(BaseEvent[str]):
    __registry_key__ = 'scope_test_collect_errors'
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.COLLECT_ERRORS


# ── Direct dispatch ──


class TestDirectDispatch:
    def test_direct_handler_executes_synchronously(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: NavEvent) -> str:
            calls.append('called')
            return 'ok'

        scope.connect(NavEvent, handler, mode=ConnectionType.DIRECT)
        scope.emit(NavEvent())
        assert calls == ['called']

    def test_direct_handler_must_be_sync_coroutine(self) -> None:
        """iscoroutine → close + TypeError."""
        scope = EventScope('s1')

        async def async_handler(event: NavEvent) -> str:
            return 'oops'

        scope.connect(NavEvent, async_handler, mode=ConnectionType.DIRECT)
        with pytest.raises(TypeError, match='coroutine'):
            scope.emit(NavEvent())

    def test_direct_handler_must_be_sync_awaitable(self) -> None:
        """isawaitable → TypeError."""
        scope = EventScope('s1')

        class MyAwaitable:
            def __await__(self):  # type: ignore[reportReturnType]
                yield

        def handler(event: NavEvent) -> Any:
            return MyAwaitable()

        scope.connect(NavEvent, handler, mode=ConnectionType.DIRECT)
        with pytest.raises(TypeError, match='awaitable'):
            scope.emit(NavEvent())

    def test_direct_handler_result_recorded(self) -> None:
        scope = EventScope('s1')

        def handler(event: NavEvent) -> str:
            return 'result-value'

        conn = scope.connect(NavEvent, handler, mode=ConnectionType.DIRECT)
        event = NavEvent()
        scope.emit(event)
        assert conn.id in event.event_results
        er = event.event_results[conn.id]
        assert er.result == 'result-value'
        assert er.error is None

    def test_direct_handler_exception_recorded_and_propagated(self) -> None:
        scope = EventScope('s1')

        def handler(event: NavEvent) -> str:
            msg = 'boom'
            raise ValueError(msg)

        conn = scope.connect(NavEvent, handler, mode=ConnectionType.DIRECT)
        event = NavEvent()
        with pytest.raises(ValueError, match='boom'):
            scope.emit(event)
        assert conn.id in event.event_results
        er = event.event_results[conn.id]
        assert er.error is not None


# ── EmitPolicy ──


class TestEmitPolicy:
    def test_fail_fast_stops_on_exception(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_a(event: NavEvent) -> str:
            calls.append('a')
            msg = 'fail'
            raise ValueError(msg)

        def handler_b(event: NavEvent) -> str:
            calls.append('b')
            return 'b'

        scope.connect(NavEvent, handler_a, mode=ConnectionType.DIRECT, priority=10)
        scope.connect(NavEvent, handler_b, mode=ConnectionType.DIRECT, priority=5)
        with pytest.raises(ValueError, match='fail'):
            scope.emit(NavEvent())
        assert calls == ['a']  # b never called

    def test_collect_errors_continues_on_exception(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_a(event: CollectErrorsEvent) -> str:
            calls.append('a')
            msg = 'fail-a'
            raise ValueError(msg)

        def handler_b(event: CollectErrorsEvent) -> str:
            calls.append('b')
            return 'b-result'

        scope.connect(CollectErrorsEvent, handler_a, mode=ConnectionType.DIRECT, priority=10)
        scope.connect(CollectErrorsEvent, handler_b, mode=ConnectionType.DIRECT, priority=5)
        event = CollectErrorsEvent()
        scope.emit(event)  # should NOT raise
        assert calls == ['a', 'b']

    def test_collect_errors_all_errors_in_results(self) -> None:
        scope = EventScope('s1')

        def handler_a(event: CollectErrorsEvent) -> str:
            msg = 'err-a'
            raise ValueError(msg)

        def handler_b(event: CollectErrorsEvent) -> str:
            msg = 'err-b'
            raise RuntimeError(msg)

        conn_a = scope.connect(CollectErrorsEvent, handler_a, mode=ConnectionType.DIRECT, priority=10)
        conn_b = scope.connect(CollectErrorsEvent, handler_b, mode=ConnectionType.DIRECT, priority=5)
        event = CollectErrorsEvent()
        scope.emit(event)
        assert event.event_results[conn_a.id].error is not None
        assert event.event_results[conn_b.id].error is not None

    def test_emit_policy_inherited_from_event_class(self) -> None:
        """NavEvent inherits FAIL_FAST; CollectErrorsEvent overrides to COLLECT_ERRORS."""
        assert NavEvent.emit_policy == EmitPolicy.FAIL_FAST
        assert CollectErrorsEvent.emit_policy == EmitPolicy.COLLECT_ERRORS


# ── has_pending ──


class TestHasPending:
    def test_has_pending_false_when_no_queued(self) -> None:
        scope = EventScope('s1')

        def handler(event: NavEvent) -> str:
            return 'ok'

        scope.connect(NavEvent, handler, mode=ConnectionType.DIRECT)
        event = NavEvent()
        scope.emit(event)
        assert event.has_pending is False

    def test_has_pending_true_when_queued_enqueued(self) -> None:
        scope = EventScope('s1')

        def handler(event: NavEvent) -> str:
            return 'ok'

        scope.connect(NavEvent, handler, mode=ConnectionType.QUEUED)
        event = NavEvent()
        scope.emit(event)
        assert event.has_pending is True
        assert event._pending_count == 1


# ── Priority ──


class TestPriority:
    def test_handlers_execute_by_priority_descending(self) -> None:
        scope = EventScope('s1')
        order: list[str] = []

        def handler_low(event: NavEvent) -> str:
            order.append('low')
            return 'low'

        def handler_high(event: NavEvent) -> str:
            order.append('high')
            return 'high'

        scope.connect(NavEvent, handler_low, mode=ConnectionType.DIRECT, priority=1)
        scope.connect(NavEvent, handler_high, mode=ConnectionType.DIRECT, priority=10)
        scope.emit(NavEvent())
        assert order == ['high', 'low']

    def test_same_priority_preserves_registration_order(self) -> None:
        scope = EventScope('s1')
        order: list[str] = []

        def handler_a(event: NavEvent) -> str:
            order.append('a')
            return 'a'

        def handler_b(event: NavEvent) -> str:
            order.append('b')
            return 'b'

        scope.connect(NavEvent, handler_a, mode=ConnectionType.DIRECT, priority=0)
        scope.connect(NavEvent, handler_b, mode=ConnectionType.DIRECT, priority=0)
        scope.emit(NavEvent())
        assert order == ['a', 'b']


# ── Consume ──


class TestConsume:
    def test_consume_stops_subsequent_handlers(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_a(event: NavEvent) -> str:
            calls.append('a')
            event.consume()
            return 'a'

        def handler_b(event: NavEvent) -> str:
            calls.append('b')
            return 'b'

        scope.connect(NavEvent, handler_a, mode=ConnectionType.DIRECT, priority=10)
        scope.connect(NavEvent, handler_b, mode=ConnectionType.DIRECT, priority=5)
        scope.emit(NavEvent())
        assert calls == ['a']

    def test_consume_only_affects_current_emit(self) -> None:
        scope = EventScope('s1')

        def consuming_handler(event: NavEvent) -> str:
            event.consume()
            return 'consumed'

        scope.connect(NavEvent, consuming_handler, mode=ConnectionType.DIRECT)

        e1 = NavEvent()
        scope.emit(e1)
        assert e1.consumed is True

        e2 = NavEvent()
        scope.emit(e2)
        assert e2.consumed is True  # each event independently consumed

    def test_consume_orthogonal_to_emit_policy(self) -> None:
        """Consume works the same under COLLECT_ERRORS."""
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_a(event: CollectErrorsEvent) -> str:
            calls.append('a')
            event.consume()
            return 'a'

        def handler_b(event: CollectErrorsEvent) -> str:
            calls.append('b')
            return 'b'

        scope.connect(CollectErrorsEvent, handler_a, mode=ConnectionType.DIRECT, priority=10)
        scope.connect(CollectErrorsEvent, handler_b, mode=ConnectionType.DIRECT, priority=5)
        scope.emit(CollectErrorsEvent())
        assert calls == ['a']


# ── Filter ──


class TestFilter:
    def test_filter_skips_non_matching_events(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: NavEvent) -> str:
            calls.append(event.url)
            return event.url

        scope.connect(
            NavEvent,
            handler,
            mode=ConnectionType.DIRECT,
            filter=lambda e: e.url.startswith('https://secure'),
        )
        scope.emit(NavEvent(url='https://example.com'))
        assert calls == []

    def test_filter_passes_matching_events(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: NavEvent) -> str:
            calls.append(event.url)
            return event.url

        scope.connect(
            NavEvent,
            handler,
            mode=ConnectionType.DIRECT,
            filter=lambda e: e.url.startswith('https://secure'),
        )
        scope.emit(NavEvent(url='https://secure.example.com'))
        assert calls == ['https://secure.example.com']


# ── MRO matching ──


class TestMROMatching:
    def test_subclass_event_matches_parent_connection(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: LifecycleEvent) -> None:
            calls.append('lifecycle')

        scope.connect(LifecycleEvent, handler, mode=ConnectionType.DIRECT)
        scope.emit(SessionStartEvent())
        assert calls == ['lifecycle']

    def test_exact_type_also_matches(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: SessionStartEvent) -> None:
            calls.append('session')

        scope.connect(SessionStartEvent, handler, mode=ConnectionType.DIRECT)
        scope.emit(SessionStartEvent())
        assert calls == ['session']

    def test_mro_collects_connections_at_each_level(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_lifecycle(event: LifecycleEvent) -> None:
            calls.append('lifecycle')

        def handler_session(event: SessionStartEvent) -> None:
            calls.append('session')

        scope.connect(LifecycleEvent, handler_lifecycle, mode=ConnectionType.DIRECT, priority=0)
        scope.connect(SessionStartEvent, handler_session, mode=ConnectionType.DIRECT, priority=0)
        scope.emit(SessionStartEvent())
        # Both should fire — session first (exact match comes first in MRO)
        assert 'session' in calls
        assert 'lifecycle' in calls
        assert len(calls) == 2

    def test_connect_base_event_raises_type_error(self) -> None:
        scope = EventScope('s1')
        with pytest.raises(TypeError, match='BaseEvent'):
            scope.connect(BaseEvent, lambda e: None, mode=ConnectionType.DIRECT)

    def test_connect_all_matches_all_events(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def catch_all(event: BaseEvent[Any]) -> None:
            calls.append(type(event).__name__)

        scope.connect_all(catch_all, mode=ConnectionType.DIRECT)
        scope.emit(NavEvent())
        scope.emit(SessionStartEvent())
        assert calls == ['NavEvent', 'SessionStartEvent']

    def test_catch_all_not_in_connections_by_type(self) -> None:
        scope = EventScope('s1')
        scope.connect_all(lambda e: None, mode=ConnectionType.DIRECT)
        assert len(scope._catch_all_connections) == 1
        # BaseEvent should NOT appear in _connections_by_type
        assert BaseEvent not in scope._connections_by_type

    def test_catch_all_respects_priority(self) -> None:
        scope = EventScope('s1')
        order: list[str] = []

        def typed_handler(event: NavEvent) -> str:
            order.append('typed')
            return 'typed'

        def catch_all(event: BaseEvent[Any]) -> None:
            order.append('catch_all')

        scope.connect(NavEvent, typed_handler, mode=ConnectionType.DIRECT, priority=0)
        scope.connect_all(catch_all, mode=ConnectionType.DIRECT, priority=10)
        scope.emit(NavEvent())
        assert order == ['catch_all', 'typed']  # catch_all has higher priority

    def test_connect_validates_event_type(self) -> None:
        """Non-BaseEvent subclass raises TypeError."""
        scope = EventScope('s1')
        with pytest.raises(TypeError):
            scope.connect(str, lambda e: None, mode=ConnectionType.DIRECT)  # type: ignore[arg-type]


# ── Auto mode ──


class TestAutoMode:
    def test_auto_same_scope_uses_direct(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: NavEvent) -> str:
            calls.append('direct')
            return 'ok'

        # Auto + target_scope=self → Direct
        scope.connect(NavEvent, handler, mode=ConnectionType.AUTO, target_scope=scope)
        scope.emit(NavEvent())
        assert calls == ['direct']

    def test_auto_cross_scope_uses_queued(self) -> None:
        source = EventScope('source')
        target = EventScope('target')

        def handler(event: NavEvent) -> str:
            return 'ok'

        source.connect(NavEvent, handler, mode=ConnectionType.AUTO, target_scope=target)
        event = NavEvent()
        source.emit(event)
        # Cross-scope AUTO → QUEUED, so handler is enqueued, not executed
        assert event.has_pending is True

    def test_auto_no_target_scope_uses_direct(self) -> None:
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: NavEvent) -> str:
            calls.append('direct')
            return 'ok'

        # Auto + no target_scope → Direct
        scope.connect(NavEvent, handler, mode=ConnectionType.AUTO)
        scope.emit(NavEvent())
        assert calls == ['direct']


# ── ContextVar parent tracking ──


class TestContextVarParentTracking:
    def test_parent_id_set_when_emit_inside_handler(self) -> None:
        scope = EventScope('s1')
        child_parent_ids: list[str | None] = []

        def outer_handler(event: NavEvent) -> str:
            child = SessionStartEvent()
            scope.emit(child)
            child_parent_ids.append(child.event_parent_id)
            return 'outer'

        def inner_handler(event: SessionStartEvent) -> None:
            pass

        scope.connect(NavEvent, outer_handler, mode=ConnectionType.DIRECT)
        scope.connect(SessionStartEvent, inner_handler, mode=ConnectionType.DIRECT)

        parent_event = NavEvent()
        scope.emit(parent_event)

        assert len(child_parent_ids) == 1
        assert child_parent_ids[0] == parent_event.event_id

    def test_no_parent_id_when_top_level_emit(self) -> None:
        scope = EventScope('s1')
        scope.connect(NavEvent, lambda e: 'ok', mode=ConnectionType.DIRECT)

        event = NavEvent()
        scope.emit(event)
        assert event.event_parent_id is None


# ── Closed scope ──


class TestClosedScope:
    @pytest.mark.asyncio
    async def test_emit_on_closed_scope_raises(self) -> None:
        scope = EventScope('s1')
        await scope.close()
        with pytest.raises(RuntimeError, match='closed'):
            scope.emit(NavEvent())

    @pytest.mark.asyncio
    async def test_close_disconnects_all_connections(self) -> None:
        scope = EventScope('s1')
        conn = scope.connect(NavEvent, lambda e: 'ok', mode=ConnectionType.DIRECT)
        catch_all = scope.connect_all(lambda e: None, mode=ConnectionType.DIRECT)
        assert conn.active is True
        assert catch_all.active is True
        await scope.close()
        assert conn.active is False
        assert catch_all.active is False


# ── Handler naming ──


class TestGetHandlerName:
    def test_qualname_preferred(self) -> None:
        from agent_cdp.scope._helpers import get_handler_name

        def my_func(e: NavEvent) -> str:
            return 'ok'

        name = get_handler_name(my_func)
        assert 'my_func' in name

    def test_partial_unwrap(self) -> None:
        from agent_cdp.scope._helpers import get_handler_name

        def my_func(x: int, event: NavEvent) -> str:
            return 'ok'

        p = functools.partial(my_func, 42)
        name = get_handler_name(p)
        assert 'my_func' in name

    def test_callable_object_fallback(self) -> None:
        from agent_cdp.scope._helpers import get_handler_name

        class Handler:
            def __call__(self, event: NavEvent) -> str:
                return 'ok'

        h = Handler()
        name = get_handler_name(h)
        # Should get __qualname__ of the __call__ or repr
        assert name  # non-empty string
