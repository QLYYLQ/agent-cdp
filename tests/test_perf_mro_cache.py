"""Tests for MRO match cache (ABC-inspired positive + negative + WeakRef).

Covers:
- Positive cache hit/miss/invalidation
- Negative cache hit/invalidation/transition
- WeakRef GC behavior for dynamic event classes
- MRO cache independence per event type
"""

from __future__ import annotations

import gc
import weakref
from typing import Any

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope

# ── Test event subclasses ──


class CacheNavEvent(BaseEvent[str]):
    __registry_key__ = 'cache_test_nav'
    url: str = 'https://example.com'


class CacheLifecycleEvent(BaseEvent[None]):
    __abstract__ = True


class CacheSessionEvent(CacheLifecycleEvent):
    __registry_key__ = 'cache_test_session'
    session_id: str = 's1'


class UnhandledEvent(BaseEvent[None]):
    __registry_key__ = 'cache_test_unhandled'


# ── Positive cache tests ──


class TestPositiveCacheHit:
    def test_positive_cache_hit(self) -> None:
        """Second emit of same event type hits the positive cache."""
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: CacheNavEvent) -> str:
            calls.append('called')
            return 'ok'

        scope.connect(CacheNavEvent, handler, mode=ConnectionType.DIRECT)

        scope.emit(CacheNavEvent())
        assert len(calls) == 1

        # Verify cache is populated
        assert CacheNavEvent in scope._match_cache

        scope.emit(CacheNavEvent())
        assert len(calls) == 2


class TestPositiveCacheInvalidatedOnConnect:
    def test_positive_cache_invalidated_on_connect(self) -> None:
        """Connecting a new handler increments version, staling old cache."""
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_a(event: CacheNavEvent) -> str:
            calls.append('a')
            return 'a'

        scope.connect(CacheNavEvent, handler_a, mode=ConnectionType.DIRECT)
        scope.emit(CacheNavEvent())
        assert calls == ['a']

        # Cache is populated with version V
        cached = scope._match_cache.get(CacheNavEvent)
        assert cached is not None
        old_version = cached[0]

        # Connect new handler → version increments
        def handler_b(event: CacheNavEvent) -> str:
            calls.append('b')
            return 'b'

        scope.connect(CacheNavEvent, handler_b, mode=ConnectionType.DIRECT)
        assert scope._connection_version > old_version

        # Next emit should see both handlers (cache rebuilt)
        calls.clear()
        scope.emit(CacheNavEvent())
        assert 'a' in calls
        assert 'b' in calls


class TestPositiveCacheInvalidatedOnDisconnect:
    def test_positive_cache_invalidated_on_disconnect(self) -> None:
        """Disconnecting a handler increments version."""
        scope = EventScope('s1')
        calls: list[str] = []

        def handler(event: CacheNavEvent) -> str:
            calls.append('called')
            return 'ok'

        conn = scope.connect(CacheNavEvent, handler, mode=ConnectionType.DIRECT)
        scope.emit(CacheNavEvent())
        assert calls == ['called']

        version_before = scope._connection_version
        conn.disconnect()
        assert scope._connection_version > version_before

        calls.clear()
        scope.emit(CacheNavEvent())
        assert calls == []  # handler disconnected


class TestPositiveCacheInvalidatedOnConnectAll:
    def test_positive_cache_invalidated_on_connect_all(self) -> None:
        """Adding a catch-all handler increments version."""
        scope = EventScope('s1')
        calls: list[str] = []

        def typed_handler(event: CacheNavEvent) -> str:
            calls.append('typed')
            return 'typed'

        scope.connect(CacheNavEvent, typed_handler, mode=ConnectionType.DIRECT)
        scope.emit(CacheNavEvent())
        assert calls == ['typed']

        version_before = scope._connection_version

        def catch_all(event: BaseEvent[Any]) -> None:
            calls.append('catch_all')

        scope.connect_all(catch_all, mode=ConnectionType.DIRECT, priority=10)
        assert scope._connection_version > version_before

        calls.clear()
        scope.emit(CacheNavEvent())
        assert 'catch_all' in calls
        assert 'typed' in calls


class TestInactiveConnFilteredFromCachedList:
    def test_inactive_conn_filtered(self) -> None:
        """Cached list contains connection, but active filter excludes it."""
        scope = EventScope('s1')
        calls: list[str] = []

        def handler_a(event: CacheNavEvent) -> str:
            calls.append('a')
            return 'a'

        def handler_b(event: CacheNavEvent) -> str:
            calls.append('b')
            return 'b'

        conn_a = scope.connect(CacheNavEvent, handler_a, mode=ConnectionType.DIRECT)
        scope.connect(CacheNavEvent, handler_b, mode=ConnectionType.DIRECT)

        # Emit to populate cache
        scope.emit(CacheNavEvent())
        assert 'a' in calls
        assert 'b' in calls

        # Disconnect conn_a — version increments, cache stale
        calls.clear()
        conn_a.disconnect()
        scope.emit(CacheNavEvent())
        assert calls == ['b']


# ── Negative cache tests ──


class TestNegativeCacheHit:
    def test_negative_cache_hit(self) -> None:
        """Emitting an event with no handlers populates negative cache."""
        scope = EventScope('s1')

        scope.emit(UnhandledEvent())
        assert UnhandledEvent in scope._negative_cache

        # Second emit hits the negative cache (no MRO traversal)
        scope.emit(UnhandledEvent())
        assert UnhandledEvent in scope._negative_cache


class TestNegativeCacheClearedOnVersionChange:
    def test_negative_cache_cleared_on_version_change(self) -> None:
        """Connecting a handler increments version, clearing negative cache."""
        scope = EventScope('s1')

        scope.emit(UnhandledEvent())
        assert UnhandledEvent in scope._negative_cache
        assert scope._negative_cache_version == scope._connection_version

        # Connect a handler for a DIFFERENT event type
        scope.connect(CacheNavEvent, lambda e: None, mode=ConnectionType.DIRECT)

        # Version incremented → negative cache version is now stale
        assert scope._negative_cache_version < scope._connection_version

        # Next emit for UnhandledEvent will clear negative cache and re-check
        scope.emit(UnhandledEvent())
        # Should still end up in negative cache (no handlers) but with new version
        assert scope._negative_cache_version == scope._connection_version
        assert UnhandledEvent in scope._negative_cache


class TestNegativeToPositiveTransition:
    def test_negative_to_positive_transition(self) -> None:
        """Event type transitions from negative to positive cache when handler added."""
        scope = EventScope('s1')
        calls: list[str] = []

        # Emit with no handler → negative cache
        scope.emit(CacheNavEvent())
        assert CacheNavEvent in scope._negative_cache

        # Connect handler → version change
        def handler(event: CacheNavEvent) -> str:
            calls.append('called')
            return 'ok'

        scope.connect(CacheNavEvent, handler, mode=ConnectionType.DIRECT)

        # Emit again → negative cache stale, rebuilds, finds handler
        scope.emit(CacheNavEvent())
        assert calls == ['called']

        # Now in positive cache
        assert CacheNavEvent in scope._match_cache


# ── WeakRef tests ──


class TestWeakRefCacheContainers:
    def test_match_cache_is_weak_key_dict(self) -> None:
        """Positive cache uses WeakKeyDictionary — keys are weakly held."""
        scope = EventScope('s1')
        assert isinstance(scope._match_cache, weakref.WeakKeyDictionary)

    def test_negative_cache_is_weak_set(self) -> None:
        """Negative cache uses WeakSet — entries are weakly held."""
        scope = EventScope('s1')
        assert isinstance(scope._negative_cache, weakref.WeakSet)

    def test_weakref_positive_cache_auto_cleanup(self) -> None:
        """WeakKeyDictionary auto-removes entry when key class is GC'd.

        Pydantic's SchemaValidator holds strong internal refs to model classes,
        preventing GC of BaseEvent subclasses. We test the WeakRef property
        by directly inserting into the cache with a plain type key.
        """
        scope = EventScope('s1')

        # Create a plain class (not Pydantic) to test WeakRef behavior
        DummyClass = type('DummyClass', (), {})
        scope._match_cache[DummyClass] = (0, [])  # type: ignore[reportArgumentType]
        assert DummyClass in scope._match_cache

        ref = weakref.ref(DummyClass)
        del DummyClass
        gc.collect()

        assert ref() is None
        assert len(scope._match_cache) == 0

    def test_weakref_negative_cache_auto_cleanup(self) -> None:
        """WeakSet auto-removes entry when class is GC'd."""
        scope = EventScope('s1')

        DummyClass = type('DummyClass', (), {})
        scope._negative_cache.add(DummyClass)  # type: ignore[reportArgumentType]
        assert DummyClass in scope._negative_cache

        ref = weakref.ref(DummyClass)
        del DummyClass
        gc.collect()

        assert ref() is None
        assert len(scope._negative_cache) == 0


# ── MRO independence test ──


class TestMROCacheIndependentPerType:
    def test_mro_cache_independent_per_type(self) -> None:
        """Subclass and parent class have independent cache entries."""
        scope = EventScope('s1')
        calls: list[str] = []

        def lifecycle_handler(event: CacheLifecycleEvent) -> None:
            calls.append('lifecycle')

        def session_handler(event: CacheSessionEvent) -> None:
            calls.append('session')

        scope.connect(CacheLifecycleEvent, lifecycle_handler, mode=ConnectionType.DIRECT)
        scope.connect(CacheSessionEvent, session_handler, mode=ConnectionType.DIRECT)

        # Emit SessionEvent → matches both via MRO
        scope.emit(CacheSessionEvent())
        assert 'lifecycle' in calls
        assert 'session' in calls

        # Both types should NOT share cache entries
        # SessionEvent has its own entry (with both connections via MRO)
        assert CacheSessionEvent in scope._match_cache
