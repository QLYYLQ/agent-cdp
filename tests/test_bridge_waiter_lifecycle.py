"""Bridge session boundaries, owned observers, and bounded resume attempts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest

from agent_cdp.advanced.expect import expect
from agent_cdp.bridge import CDPEventBridge, PausedTarget, ResumeTimeoutError
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope


class BridgeLifecycleEvent(BaseEvent[None]):
    value: int = 0


class Client:
    def __init__(self) -> None:
        self.callbacks: dict[str, list[Callable[..., Any]]] = {}

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        self.callbacks.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        self.callbacks[method].remove(callback)

    def fire(self, params: dict[str, Any], route: str | None = None, method: str = 'Target.attachedToTarget') -> None:
        for callback in list(self.callbacks.get(method, [])):
            callback(params, route)


@pytest.mark.parametrize('route', [None, 'other'])
def test_payload_session_never_substitutes_for_route(route: str | None) -> None:
    scope = EventScope('parent')
    client = Client()
    bridge = CDPEventBridge(client, scope, session_id='child')
    calls: list[dict[str, Any]] = []

    def factory(params: dict[str, Any]) -> BridgeLifecycleEvent:
        calls.append(params)
        return BridgeLifecycleEvent()

    bridge.bridge('Target.attachedToTarget', factory)
    client.fire({'sessionId': 'child'}, route)
    assert not calls
    assert bridge.hit_counts == {'Target.attachedToTarget': 0}
    bridge.close()


def test_factory_receives_distinct_child_payload_and_parent_route() -> None:
    scope = EventScope('parent')
    client = Client()
    bridge = CDPEventBridge(client, scope, session_id='parent')
    calls: list[tuple[str, str | None]] = []

    def factory(params: dict[str, Any], route: str | None) -> BridgeLifecycleEvent:
        calls.append((params['sessionId'], route))
        return BridgeLifecycleEvent()

    bridge.bridge('Target.attachedToTarget', factory, with_session=True)
    client.fire({'sessionId': 'child'}, 'parent')
    assert calls == [('child', 'parent')]
    bridge.close()


def test_unbound_bridge_accepts_routed_and_browser_messages() -> None:
    scope = EventScope('browser')
    client = Client()
    bridge = CDPEventBridge(client, scope)
    routes: list[str | None] = []

    def factory(params: dict[str, Any], route: str | None) -> BridgeLifecycleEvent:
        routes.append(route)
        return BridgeLifecycleEvent()

    bridge.bridge('Target.attachedToTarget', factory, with_session=True)
    client.fire({'sessionId': 'child'})
    client.fire({'sessionId': 'child'}, 'parent')
    assert routes == [None, 'parent']
    bridge.close()


async def test_scope_closure_removes_bridge_and_late_callbacks_are_harmless() -> None:
    scope = EventScope('owned')
    client = Client()
    bridge = CDPEventBridge(client, scope)
    bridge.bridge('Target.attachedToTarget', lambda params: BridgeLifecycleEvent())
    stale_callback = client.callbacks['Target.attachedToTarget'][0]
    await scope.close()
    assert bridge.closed
    assert not client.callbacks['Target.attachedToTarget']
    stale_callback({})
    assert bridge.hit_counts['Target.attachedToTarget'] == 0
    bridge.close()


def test_delivery_failure_is_logged_without_escaping_receive_loop(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = EventScope('delivery')
    client = Client()
    bridge = CDPEventBridge(client, scope)
    bridge.bridge('Target.attachedToTarget', lambda params: BridgeLifecycleEvent())

    def fail(event: BridgeLifecycleEvent) -> None:
        raise RuntimeError('cannot deliver')

    monkeypatch.setattr(scope, 'emit', fail)
    with caplog.at_level(logging.ERROR):
        client.fire({})
    assert 'delivery' in caplog.text and 'cannot deliver' in caplog.text
    bridge.close()


def test_zero_hits_are_debug_unless_requested_per_subscription(caplog: pytest.LogCaptureFixture) -> None:
    scope = EventScope('diagnostics')
    client = Client()
    bridge = CDPEventBridge(client, scope)
    bridge.bridge('normal', lambda params: BridgeLifecycleEvent())
    # Two subscriptions to the same method have independent hit expectations.
    bridge.bridge('required', lambda params: BridgeLifecycleEvent(), expect_hits=True)
    with caplog.at_level(logging.DEBUG):
        bridge.close()
    assert [
        (record.levelno, 'normal' in record.message) for record in caplog.records if 'normal' in record.message
    ] == [(logging.DEBUG, True)]
    assert any(record.levelno == logging.WARNING and 'required' in record.message for record in caplog.records)


async def test_expect_is_armed_before_await_and_detaches_on_match() -> None:
    scope = EventScope('expect')
    waiter = expect(scope, BridgeLifecycleEvent)
    event = scope.emit(BridgeLifecycleEvent(value=42))
    assert waiter.closed
    assert await waiter is event
    assert (await expect_match(scope)).value == 7
    await scope.close()


async def expect_match(scope: EventScope) -> BridgeLifecycleEvent:
    async with expect(scope, BridgeLifecycleEvent) as waiter:
        scope.emit(BridgeLifecycleEvent(value=7))
        return await waiter


async def test_expect_timeout_starts_at_registration_without_await() -> None:
    scope = EventScope('expect-timeout')
    waiter = expect(scope, BridgeLifecycleEvent, timeout=0.01)
    await asyncio.sleep(0.03)
    assert waiter.closed
    with pytest.raises(TimeoutError, match='registration'):
        await waiter
    await scope.close()


async def test_expect_context_exception_scope_close_and_cancellation_release_listener() -> None:
    scope = EventScope('expect-cleanup')
    with pytest.raises(ValueError):
        with expect(scope, BridgeLifecycleEvent) as abandoned:
            raise ValueError('abandon operation')
    assert abandoned.closed
    cancelled = expect(scope, BridgeLifecycleEvent)
    task = asyncio.ensure_future(cancelled)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.closed
    pending = expect(scope, BridgeLifecycleEvent)
    await scope.close()
    assert pending.closed
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_resume_failure_retries_and_only_acknowledgement_sets_resumed() -> None:
    calls = 0

    async def resume() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError('send failed')

    paused = PausedTarget(resume=resume)
    with pytest.raises(ValueError, match='send failed'):
        await paused.resume()
    assert not paused.resumed
    await paused.resume()
    await paused.resume()
    assert paused.resumed and calls == 2


async def test_concurrent_resume_waits_for_one_acknowledgement() -> None:
    started, ack = asyncio.Event(), asyncio.Event()
    calls = 0

    async def resume() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await ack.wait()

    paused = PausedTarget(resume=resume)
    first = asyncio.create_task(paused.resume())
    await started.wait()
    second = asyncio.create_task(paused.resume())
    await asyncio.sleep(0)
    assert not paused.resumed and not second.done() and calls == 1
    ack.set()
    await asyncio.gather(first, second)
    assert paused.resumed and calls == 1


async def test_timeout_retains_cancellation_resistant_attempt_and_prevents_new_send() -> None:
    released = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def resume() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await released.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()

    paused = PausedTarget(resume=resume, resume_timeout=0.01)
    with pytest.raises(ResumeTimeoutError) as error:
        await paused.resume()
    await cancelled.wait()
    assert not paused.resumed
    assert paused.resume_task is error.value.task and not error.value.task.done()
    with pytest.raises(ResumeTimeoutError):
        await paused.resume()
    assert calls == 1
    released.set()
    await error.value.task
    assert not paused.resumed
    await paused.resume()
    assert paused.resumed and calls == 2


async def test_cancelled_resume_does_not_report_success_and_allows_retry_after_exit() -> None:
    started = asyncio.Event()
    calls = 0

    async def resume() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()

    paused = PausedTarget(resume=resume)
    task = asyncio.create_task(paused.resume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paused.resumed
    assert paused.resume_task is not None
    await asyncio.gather(paused.resume_task, return_exceptions=True)
    await paused.resume()
    assert calls == 2 and paused.resumed


async def test_body_failure_is_preserved_with_cleanup_cause() -> None:
    body_error = ValueError('setup failed')
    cleanup_error = RuntimeError('resume failed')

    async def resume() -> None:
        raise cleanup_error

    with pytest.raises(ValueError) as error:
        async with PausedTarget(resume=resume):
            raise body_error
    assert error.value is body_error
    assert error.value.__cause__ is cleanup_error


async def test_successful_body_reports_cleanup_failure() -> None:
    async def resume() -> None:
        raise RuntimeError('resume failed')

    with pytest.raises(RuntimeError, match='resume failed'):
        async with PausedTarget(resume=resume):
            pass


async def test_body_cancellation_survives_ordinary_cleanup_error() -> None:
    async def resume() -> None:
        raise RuntimeError('resume failed')

    with pytest.raises(asyncio.CancelledError) as error:
        async with PausedTarget(resume=resume):
            raise asyncio.CancelledError('body cancelled')
    assert isinstance(error.value.__cause__, RuntimeError)


async def test_new_cancellation_during_cleanup_supersedes_ordinary_body_error() -> None:
    started = asyncio.Event()

    async def resume() -> None:
        started.set()
        await asyncio.Event().wait()

    paused = PausedTarget(resume=resume)

    async def body() -> None:
        async with paused:
            raise ValueError('earlier failure')

    task = asyncio.create_task(body())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert paused.resume_task is not None
    await asyncio.gather(paused.resume_task, return_exceptions=True)


async def test_expect_observer_cannot_satisfy_required_receiver() -> None:
    from agent_cdp.events.contracts import ReceiverContractError, RequestContract

    scope = EventScope('observer-contract')
    with expect(scope, BridgeLifecycleEvent) as waiter:
        with pytest.raises(ReceiverContractError):
            scope.emit(BridgeLifecycleEvent(request_contract=RequestContract(min_receivers=1)))
        assert not waiter.done
        event = scope.emit(BridgeLifecycleEvent())
        assert await waiter is event
    await scope.close()


async def test_waiter_cancellation_leaves_handler_running() -> None:
    scope = EventScope('observer-cancel')
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event: BridgeLifecycleEvent) -> None:
        entered.set()
        await release.wait()

    scope.connect(BridgeLifecycleEvent, handler, mode='queued')
    await scope.start()
    waiter = expect(scope, BridgeLifecycleEvent, include=lambda event: False)
    waiting = asyncio.ensure_future(waiter)
    event = scope.emit(BridgeLifecycleEvent())
    await entered.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await event
    assert all(result.error is None for result in event.event_results.values())
    await scope.close()


async def test_builtin_resume_does_not_override_client_command_timeout() -> None:
    class CommandClient(Client):
        is_connected = True

        async def send(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            session_id: str | None = None,
            timeout: float | None = 42.0,
        ) -> None:
            assert method == 'Runtime.runIfWaitingForDebugger'
            assert params is None and session_id == 'target'
            assert timeout == 42.0

    paused = PausedTarget(cdp=CommandClient(), session_id='target', resume_timeout=0.1)
    await paused.resume()
    assert paused.resumed


async def test_failed_unregistration_can_be_retried_on_scope_close() -> None:
    class FlakyClient(Client):
        failed = False

        def off_event(self, method: str, callback: Callable[..., Any]) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError('transient cleanup failure')
            super().off_event(method, callback)

    scope = EventScope('bridge-retry')
    client = FlakyClient()
    bridge = CDPEventBridge(client, scope)
    bridge.bridge('first', lambda params: BridgeLifecycleEvent())
    bridge.bridge('second', lambda params: BridgeLifecycleEvent())
    with pytest.raises(ExceptionGroup, match='callback cleanup failed'):
        bridge.close()
    assert client.callbacks['first'] and not client.callbacks['second']
    await scope.close()
    assert not client.callbacks['first']


@pytest.mark.parametrize('failure_stage', ['factory', 'delivery'])
def test_callback_self_cancellation_does_not_escape_receive_loop(
    failure_stage: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = EventScope('callback-cancellation')
    client = Client()
    bridge = CDPEventBridge(client, scope)

    def factory(params: dict[str, Any]) -> BridgeLifecycleEvent:
        if failure_stage == 'factory':
            raise asyncio.CancelledError('factory cancelled itself')
        return BridgeLifecycleEvent()

    def delivery(event: BridgeLifecycleEvent) -> None:
        raise asyncio.CancelledError('delivery cancelled itself')

    bridge.bridge('Target.attachedToTarget', factory)
    if failure_stage == 'delivery':
        monkeypatch.setattr(scope, 'emit', delivery)
    with caplog.at_level(logging.ERROR):
        client.fire({})
    assert 'CancelledError' in caplog.text
    assert failure_stage in caplog.text
    bridge.close()


async def test_finished_waiter_does_not_retain_event_in_scope_match_cache() -> None:
    import gc
    import weakref

    scope = EventScope('waiter-references', max_history_size=0)
    waiter = expect(scope, BridgeLifecycleEvent)
    event = BridgeLifecycleEvent()
    reference = weakref.ref(event)
    scope.emit(event)
    assert await waiter is event
    del event, waiter
    gc.collect()
    assert reference() is None
    await scope.close()


def test_direct_handler_self_cancellation_is_recorded_without_escaping_cdp() -> None:
    from agent_cdp.events.result import ResultStatus

    scope = EventScope('direct-cancel')
    client = Client()
    bridge = CDPEventBridge(client, scope)
    event = BridgeLifecycleEvent()
    bridge.bridge('Target.attachedToTarget', lambda params: event)

    def cancel_self(event: BridgeLifecycleEvent) -> None:
        raise asyncio.CancelledError('handler stopped')

    connection = scope.connect(BridgeLifecycleEvent, cancel_self, mode='direct')
    client.fire({})
    assert event.event_results[connection.id].status is ResultStatus.CANCELLED
    bridge.close()
