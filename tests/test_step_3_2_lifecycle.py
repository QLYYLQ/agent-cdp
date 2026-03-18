"""Tests for Step 3.2 — auto-disconnect lifecycle, concurrent multi-scope dispatch,
and cross-scope ContextVar parent tracking (C9).

Integration tests verifying that scope closure properly severs connections,
multiple scopes dispatch concurrently without global locks, and ContextVar
parent tracking works across Direct→Queued→Direct chains.
"""

from __future__ import annotations

import asyncio
import gc
import time
import weakref

import pytest

from typing import Any

from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import ScopeGroup


async def _wait_event(event: BaseEvent[Any]) -> None:
    """Await an event's completion — avoids unhashable-type issue with asyncio.gather."""
    await event

# ── Test event subclasses ──


class LifecycleNavEvent(BaseEvent[str]):
    __registry_key__ = 'lifecycle_test_nav'
    url: str = 'https://example.com'


class LifecyclePingEvent(BaseEvent[None]):
    __registry_key__ = 'lifecycle_test_ping'


class LifecycleChainEventX(BaseEvent[str]):
    __registry_key__ = 'lifecycle_test_chain_x'


class LifecycleChainEventY(BaseEvent[str]):
    __registry_key__ = 'lifecycle_test_chain_y'


class LifecycleChainEventZ(BaseEvent[str]):
    __registry_key__ = 'lifecycle_test_chain_z'


# ── Auto-disconnect tests ──


class TestCloseScopeDisconnectsOutgoing:
    @pytest.mark.asyncio
    async def test_close_scope_disconnects_outgoing(self) -> None:
        """Closing scope_a severs all outgoing connections (typed + catch-all);
        scope_b remains functional."""
        group = ScopeGroup('g1')
        try:
            scope_a = await group.create_scope('scope_a')
            scope_b = await group.create_scope('scope_b')

            # Typed outgoing connection on scope_a
            conn_typed = scope_a.connect(
                LifecycleNavEvent,
                lambda e: 'typed',
                mode=ConnectionType.DIRECT,
            )
            # Catch-all outgoing connection on scope_a
            conn_all = scope_a.connect_all(
                lambda e: 'catch-all',
                mode=ConnectionType.DIRECT,
            )

            assert conn_typed.active is True
            assert conn_all.active is True

            await group.close_scope('scope_a')

            assert conn_typed.active is False
            assert conn_all.active is False

            # scope_b still works
            calls: list[str] = []
            scope_b.connect(
                LifecyclePingEvent,
                lambda e: calls.append('ping'),
                mode=ConnectionType.DIRECT,
            )
            scope_b.emit(LifecyclePingEvent())
            assert calls == ['ping']
        finally:
            await group.close_all()


class TestCloseScopeDisconnectsIncoming:
    @pytest.mark.asyncio
    async def test_close_scope_disconnects_incoming(self) -> None:
        """Closing scope_b (target) severs incoming connection; scope_a emit
        no longer triggers the disconnected handler."""
        group = ScopeGroup('g1')
        try:
            scope_a = await group.create_scope('scope_a')
            scope_b = await group.create_scope('scope_b')

            calls: list[str] = []

            # scope_a → handler with target_scope=scope_b (tracked as incoming on scope_b)
            conn = scope_a.connect(
                LifecycleNavEvent,
                lambda e: calls.append('fired'),
                mode=ConnectionType.DIRECT,
                target_scope=scope_b,
            )

            assert conn.active is True

            # Close scope_b (the target) — incoming connection should be severed
            await group.close_scope('scope_b')

            assert conn.active is False

            # scope_a emit no longer triggers the disconnected handler
            scope_a.emit(LifecycleNavEvent())
            assert calls == []
        finally:
            await group.close_all()


class TestEmitAfterCloseRaises:
    @pytest.mark.asyncio
    async def test_emit_after_close_raises(self) -> None:
        """Emitting on a closed scope raises RuntimeError with 'closed' in message."""
        group = ScopeGroup('g1')
        try:
            scope = await group.create_scope('tab-1')
            await group.close_scope('tab-1')

            with pytest.raises(RuntimeError, match='closed'):
                scope.emit(LifecycleNavEvent())
        finally:
            await group.close_all()


class TestHandlerRefsReleasedAfterClose:
    @pytest.mark.asyncio
    async def test_handler_refs_released_after_close(self) -> None:
        """After scope close + deletion of external refs, handler is garbage-collected."""
        group = ScopeGroup('g1')
        try:
            scope = await group.create_scope('tab-1')

            def handler(event: LifecycleNavEvent) -> str:
                return 'ok'

            ref = weakref.ref(handler)
            conn = scope.connect(LifecycleNavEvent, handler, mode=ConnectionType.DIRECT)

            await group.close_scope('tab-1')

            # Remove all strong references to handler and connection
            del handler
            del conn
            gc.collect()

            assert ref() is None, 'Handler should be garbage-collected after scope close and ref cleanup'
        finally:
            await group.close_all()


# ── Concurrency tests ──


class TestParallelEmitAcrossScopes:
    @pytest.mark.asyncio
    async def test_parallel_emit_across_scopes(self) -> None:
        """Three scopes emit simultaneously via QUEUED handlers; results are isolated per scope."""
        group = ScopeGroup('g1')
        results_by_scope: dict[str, list[str]] = {'s0': [], 's1': [], 's2': []}

        try:
            events: list[LifecycleNavEvent] = []
            for name in ('s0', 's1', 's2'):
                scope = await group.create_scope(name)

                async def handler(event: LifecycleNavEvent, n: str = name) -> str:
                    results_by_scope[n].append(event.url)
                    return f'{n}-done'

                scope.connect(
                    LifecycleNavEvent,
                    handler,
                    mode=ConnectionType.QUEUED,
                )

                event = LifecycleNavEvent(url=f'https://{name}.example.com')
                scope.emit(event)
                events.append(event)

            await asyncio.gather(*(_wait_event(e) for e in events))

            # Each scope processed only its own event — no cross-contamination
            assert results_by_scope['s0'] == ['https://s0.example.com']
            assert results_by_scope['s1'] == ['https://s1.example.com']
            assert results_by_scope['s2'] == ['https://s2.example.com']
        finally:
            await group.close_all()


class TestNoGlobalLockBottleneck:
    @pytest.mark.asyncio
    async def test_no_global_lock_bottleneck(self) -> None:
        """Two scopes with 0.2s QUEUED handlers complete in ~0.2s (concurrent), not ~0.4s."""
        group = ScopeGroup('g1')
        try:
            events: list[LifecycleNavEvent] = []
            for name in ('s0', 's1'):
                scope = await group.create_scope(name)

                async def handler(event: LifecycleNavEvent) -> str:
                    await asyncio.sleep(0.2)
                    return 'done'

                scope.connect(
                    LifecycleNavEvent,
                    handler,
                    mode=ConnectionType.QUEUED,
                )

                event = LifecycleNavEvent(url=f'https://{name}.example.com')
                scope.emit(event)
                events.append(event)

            t0 = time.monotonic()
            await asyncio.gather(*(_wait_event(e) for e in events))
            elapsed = time.monotonic() - t0

            assert elapsed < 0.35, f'Expected concurrent ~0.2s, got {elapsed:.3f}s (sequential would be ~0.4s)'
        finally:
            await group.close_all()


class TestConcurrentScopeCreationAndClose:
    @pytest.mark.asyncio
    async def test_concurrent_scope_creation_and_close(self) -> None:
        """Concurrent close of s1/s3 + creation of s5/s6 leaves correct final state."""
        group = ScopeGroup('g1')
        try:
            for i in range(5):
                await group.create_scope(f's{i}')

            await asyncio.gather(
                group.close_scope('s1'),
                group.close_scope('s3'),
                group.create_scope('s5'),
                group.create_scope('s6'),
            )

            assert sorted(group.scope_ids) == ['s0', 's2', 's4', 's5', 's6']
            assert group.scope_count == 5

            # Closed scopes raise KeyError on lookup
            for closed_id in ('s1', 's3'):
                with pytest.raises(KeyError):
                    group.get_scope(closed_id)

            # Remaining scopes can still emit
            for sid in ('s0', 's2', 's4', 's5', 's6'):
                scope = group.get_scope(sid)
                scope.emit(LifecyclePingEvent())
        finally:
            await group.close_all()


# ── Cross-scope ContextVar chain test ──


class TestCrossScopeDirectQueuedAlternatingChain:
    @pytest.mark.asyncio
    async def test_cross_scope_direct_queued_alternating_chain(self) -> None:
        """Direct→Queued→Direct chain across scopes preserves parent_id chain.

        Chain: scope_a emits EventX (Direct handler) → handler emits EventY
        → QUEUED to scope_b → scope_b's async handler emits EventZ (Direct).
        Parent chain: X.parent=None, Y.parent=X.id, Z.parent=Y.id.
        """
        group = ScopeGroup('g1')
        try:
            scope_a = await group.create_scope('scope_a')
            scope_b = await group.create_scope('scope_b')

            event_y_holder: list[LifecycleChainEventY] = []
            event_z_holder: list[LifecycleChainEventZ] = []

            def handler_x(event: LifecycleChainEventX) -> str:
                """Direct handler on scope_a: emits EventY (queued to scope_b)."""
                event_y = LifecycleChainEventY()
                scope_a.emit(event_y)
                event_y_holder.append(event_y)
                return 'x-done'

            async def handler_y(event: LifecycleChainEventY) -> str:
                """Queued handler executing in scope_b's event loop: emits EventZ (Direct)."""
                event_z = LifecycleChainEventZ()
                scope_b.emit(event_z)
                event_z_holder.append(event_z)
                return 'y-done'

            def handler_z(event: LifecycleChainEventZ) -> str:
                """Direct handler on scope_b."""
                return 'z-done'

            # scope_a: EventX → Direct handler_x
            scope_a.connect(LifecycleChainEventX, handler_x, mode=ConnectionType.DIRECT)
            # scope_a: EventY → Auto (cross-scope → Queued) to scope_b
            scope_a.connect(
                LifecycleChainEventY,
                handler_y,
                mode=ConnectionType.AUTO,
                target_scope=scope_b,
            )
            # scope_b: EventZ → Direct handler_z
            scope_b.connect(LifecycleChainEventZ, handler_z, mode=ConnectionType.DIRECT)

            # Emit the chain trigger
            event_x = LifecycleChainEventX()
            scope_a.emit(event_x)

            # EventY was created inside handler_x and enqueued to scope_b
            assert len(event_y_holder) == 1
            event_y = event_y_holder[0]

            # Wait for the queued handler to process EventY in scope_b
            await asyncio.wait_for(event_y, timeout=5.0)

            # EventZ was created inside handler_y
            assert len(event_z_holder) == 1
            event_z = event_z_holder[0]

            # Verify parent chain: X → Y → Z
            assert event_x.event_parent_id is None
            assert event_y.event_parent_id == event_x.event_id
            assert event_z.event_parent_id == event_y.event_id
        finally:
            await group.close_all()
