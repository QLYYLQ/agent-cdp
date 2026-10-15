"""Conscribe 2 registration and per-binding watchdog resource ownership."""

from __future__ import annotations

import gc
import weakref

import pytest
from conscribe import validate_wiring
from conscribe.testing import isolated_registries

from agent_cdp import BaseEvent, ConnectionType, EventScope, WatchdogBase, WatchdogProtocol, WatchdogRegistrar
from agent_cdp._registry import EventRegistrar


class BindingEvent(BaseEvent[str]):
    __registry_key__ = 'v05.binding'


class SharedBindingWatchdog(WatchdogBase):
    __registry_key__ = 'v05_shared_binding'

    def __init__(self) -> None:
        self.seen: list[str | None] = []
        self.cleaned: list[str | None] = []

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self.detach(scope, session_id)

        def handle(event: BindingEvent) -> str:
            self.seen.append(session_id)
            return str(session_id)

        conn = scope.connect(BindingEvent, handle, mode=ConnectionType.DIRECT)
        self.track_binding(
            scope, session_id, connections=[conn], close_callbacks=[lambda: self.cleaned.append(session_id)]
        )


async def test_shared_watchdog_scope_close_only_detaches_owned_binding() -> None:
    first, second = EventScope('binding-first'), EventScope('binding-second')
    watchdog = SharedBindingWatchdog()
    assert isinstance(watchdog, WatchdogProtocol)
    watchdog.attach(first, 'session-A')
    watchdog.attach(second, 'session-B')
    first.emit(BindingEvent())
    second.emit(BindingEvent())
    await first.close()
    assert watchdog.cleaned == ['session-A']
    assert len(watchdog.bindings) == 1
    second.emit(BindingEvent())
    assert watchdog.seen == ['session-A', 'session-B', 'session-B']
    watchdog.detach(second, 'session-B')
    watchdog.detach(second, 'session-B')
    second.emit(BindingEvent())
    assert watchdog.seen == ['session-A', 'session-B', 'session-B']
    await second.close()
    assert watchdog.cleaned == ['session-A', 'session-B']
    assert not watchdog.bindings


async def test_closed_binding_does_not_retain_scope_on_live_watchdog() -> None:
    watchdog = SharedBindingWatchdog()
    scope = EventScope('binding-gc', max_history_size=0)
    watchdog.attach(scope, 'session')
    ref = weakref.ref(scope)
    await scope.close()
    del scope
    gc.collect()
    assert ref() is None
    assert not watchdog.bindings


async def test_detach_attempts_every_cleanup_and_allows_retry() -> None:
    scope = EventScope('binding-retry')
    watchdog = SharedBindingWatchdog()
    attempts: list[str] = []

    def fails_once() -> None:
        attempts.append('retry')
        if attempts.count('retry') == 1:
            raise RuntimeError('cleanup failure')

    watchdog.track_binding(scope, close_callbacks=[fails_once, lambda: attempts.append('other')])
    with pytest.raises(ExceptionGroup):
        watchdog.detach(scope)
    assert attempts == ['retry', 'other']
    assert len(watchdog.bindings) == 1
    watchdog.detach(scope)
    assert attempts == ['retry', 'other', 'retry']
    assert not watchdog.bindings
    await scope.close()


def test_conscribe2_registry_isolation_and_wiring_validation() -> None:
    with isolated_registries():

        class IsolatedEvent(BaseEvent[str]):
            __registry_key__ = 'v05.isolated'

        assert EventRegistrar.get('v05.isolated') is IsolatedEvent
        validate_wiring({'event': EventRegistrar, 'watchdog': WatchdogRegistrar})
    with pytest.raises(KeyError):
        EventRegistrar.get('v05.isolated')
