"""Tests for ScopeGroup — multi-scope lifecycle, broadcast, and batch connection (C8).

Covers create/retrieve, close, broadcast with deep copy and exclude,
connect_all_scopes, close_all, and property accessors.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_cdp.connection import Connection, ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope, ScopeGroup

# ── Test event subclasses ──


class GroupNavEvent(BaseEvent[str]):
    __registry_key__ = 'group_test_nav'
    url: str = 'https://example.com'


class GroupPingEvent(BaseEvent[None]):
    __registry_key__ = 'group_test_ping'
    seq: int = 0


# ── Create / retrieve ──


class TestCreateScopeAndRetrieve:
    @pytest.mark.asyncio
    async def test_create_scope_and_retrieve(self) -> None:
        """create_scope returns EventScope, get_scope retrieves it, scope_ids lists it."""
        group = ScopeGroup('g1')
        try:
            scope = await group.create_scope('tab-1', title='Example')
            assert isinstance(scope, EventScope)
            assert scope.scope_id == 'tab-1'
            assert scope.metadata.get('title') == 'Example'

            # get_scope retrieves the same instance
            assert group.get_scope('tab-1') is scope

            # scope_ids includes it
            assert 'tab-1' in group.scope_ids
        finally:
            await group.close_all()

    @pytest.mark.asyncio
    async def test_create_scope_duplicate_raises(self) -> None:
        """Creating a scope with an existing ID raises KeyError."""
        group = ScopeGroup('g1')
        try:
            await group.create_scope('tab-1')
            with pytest.raises(KeyError, match='tab-1'):
                await group.create_scope('tab-1')
        finally:
            await group.close_all()

    @pytest.mark.asyncio
    async def test_get_scope_missing_raises(self) -> None:
        """get_scope for nonexistent ID raises KeyError."""
        group = ScopeGroup('g1')
        with pytest.raises(KeyError, match='no-such'):
            group.get_scope('no-such')


# ── Close scope ──


class TestCloseScopeRemovesFromGroup:
    @pytest.mark.asyncio
    async def test_close_scope_removes_from_group(self) -> None:
        """close_scope removes the scope; get_scope then raises KeyError."""
        group = ScopeGroup('g1')
        try:
            await group.create_scope('tab-1')
            await group.close_scope('tab-1')

            with pytest.raises(KeyError, match='tab-1'):
                group.get_scope('tab-1')

            assert 'tab-1' not in group.scope_ids
            assert group.scope_count == 0
        finally:
            await group.close_all()

    @pytest.mark.asyncio
    async def test_close_scope_missing_raises(self) -> None:
        """close_scope for nonexistent ID raises KeyError."""
        group = ScopeGroup('g1')
        with pytest.raises(KeyError, match='no-such'):
            await group.close_scope('no-such')


# ── Broadcast ──


class TestBroadcastReachesAllScopes:
    @pytest.mark.asyncio
    async def test_broadcast_reaches_all_scopes(self) -> None:
        """Broadcast sends event to all 3 scopes; every handler fires."""
        group = ScopeGroup('g1')
        calls: list[str] = []

        try:
            for name in ('s1', 's2', 's3'):
                scope = await group.create_scope(name)
                scope.connect(
                    GroupPingEvent,
                    lambda _, n=name: calls.append(n),
                    mode=ConnectionType.DIRECT,
                )

            group.broadcast(GroupPingEvent(seq=1))
            assert sorted(calls) == ['s1', 's2', 's3']
        finally:
            await group.close_all()


class TestBroadcastDeepCopiesEvent:
    @pytest.mark.asyncio
    async def test_broadcast_deep_copies_event(self) -> None:
        """Each scope receives an independent deep copy of the event."""
        group = ScopeGroup('g1')
        received: list[BaseEvent[Any]] = []

        try:
            for name in ('s1', 's2'):
                scope = await group.create_scope(name)
                scope.connect(
                    GroupPingEvent,
                    lambda e: received.append(e),
                    mode=ConnectionType.DIRECT,
                )

            original = GroupPingEvent(seq=42)
            copies = group.broadcast(original)

            # Two copies returned, each independent
            assert len(copies) == 2
            assert copies[0] is not copies[1]
            assert copies[0] is not original
            assert copies[1] is not original

            # Received events are the copies (same objects emitted to scopes)
            assert len(received) == 2
            assert received[0] is not received[1]

            # Mutating one copy doesn't affect the other
            copies[0].consumed = True
            assert copies[1].consumed is False
        finally:
            await group.close_all()


class TestBroadcastExcludeSkipsScope:
    @pytest.mark.asyncio
    async def test_broadcast_exclude_skips_scope(self) -> None:
        """exclude={'s2'} prevents s2 handler from being called."""
        group = ScopeGroup('g1')
        calls: list[str] = []

        try:
            for name in ('s1', 's2', 's3'):
                scope = await group.create_scope(name)
                scope.connect(
                    GroupPingEvent,
                    lambda _, n=name: calls.append(n),
                    mode=ConnectionType.DIRECT,
                )

            group.broadcast(GroupPingEvent(seq=1), exclude={'s2'})
            assert sorted(calls) == ['s1', 's3']
        finally:
            await group.close_all()


# ── connect_all_scopes ──


class TestConnectAllScopesConnectsToEveryScope:
    @pytest.mark.asyncio
    async def test_connect_all_scopes_connects_to_every_scope(self) -> None:
        """connect_all_scopes attaches handler to every current scope; all fire on emit."""
        group = ScopeGroup('g1')
        calls: list[str] = []

        try:
            for name in ('s1', 's2', 's3'):
                await group.create_scope(name)

            connections = group.connect_all_scopes(
                GroupNavEvent,
                lambda _: calls.append('fired'),
                mode=ConnectionType.DIRECT,
            )

            assert len(connections) == 3
            assert all(isinstance(c, Connection) for c in connections)

            # Each scope should fire independently
            for name in ('s1', 's2', 's3'):
                group.get_scope(name).emit(GroupNavEvent())

            assert len(calls) == 3
        finally:
            await group.close_all()


# ── close_all ──


class TestCloseAllClosesEveryScope:
    @pytest.mark.asyncio
    async def test_close_all_closes_every_scope(self) -> None:
        """close_all closes all scopes; emit raises RuntimeError on each."""
        group = ScopeGroup('g1')

        scopes: list[EventScope] = []
        for name in ('s1', 's2', 's3'):
            scope = await group.create_scope(name)
            scopes.append(scope)

        await group.close_all()

        assert group.scope_count == 0
        assert group.scope_ids == []

        # Emit on any closed scope raises RuntimeError
        for scope in scopes:
            with pytest.raises(RuntimeError, match='closed'):
                scope.emit(GroupPingEvent())


# ── Properties ──


class TestScopeCountAndIds:
    @pytest.mark.asyncio
    async def test_scope_count_and_ids(self) -> None:
        """scope_count and scope_ids reflect current state accurately."""
        group = ScopeGroup('g1')

        try:
            assert group.scope_count == 0
            assert group.scope_ids == []

            await group.create_scope('a')
            await group.create_scope('b')
            await group.create_scope('c')

            assert group.scope_count == 3
            assert sorted(group.scope_ids) == ['a', 'b', 'c']

            await group.close_scope('b')
            assert group.scope_count == 2
            assert sorted(group.scope_ids) == ['a', 'c']
        finally:
            await group.close_all()
