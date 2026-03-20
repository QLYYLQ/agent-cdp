"""Tests for PausedTarget, CDPCommandProtocol, and CDPEventBridge.paused()."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from agent_cdp.bridge import CDPClientProtocol, CDPCommandProtocol, CDPEventBridge, PausedTarget
from agent_cdp.connection import ConnectionType
from agent_cdp.events import BaseEvent
from agent_cdp.scope import EventScope

# ── Test events ──


class DialogEvent(BaseEvent[dict[str, Any]]):
    url: str = ''
    message: str = ''


# ── Fake CDP clients ──


class FakeCDPClient:
    """Minimal CDP client satisfying CDPClientProtocol."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[..., Any]]] = {}

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        self._listeners.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        cbs = self._listeners.get(method, [])
        if callback in cbs:
            cbs.remove(callback)

    def fire(self, method: str, params: dict[str, Any]) -> None:
        for cb in list(self._listeners.get(method, [])):
            cb(params)


class FakeCDPCommandClient(FakeCDPClient):
    """CDP client satisfying CDPCommandProtocol (adds send() + is_connected)."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_commands: list[tuple[str, dict[str, Any] | None]] = []

    @property
    def is_connected(self) -> bool:
        return True

    async def send(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> Any:
        self.sent_commands.append((method, params))
        return {}


# ── CDPCommandProtocol structural checks ──


class TestCDPCommandProtocol:
    def test_command_client_satisfies_protocol(self) -> None:
        assert isinstance(FakeCDPCommandClient(), CDPCommandProtocol)

    def test_command_client_also_satisfies_base_protocol(self) -> None:
        assert isinstance(FakeCDPCommandClient(), CDPClientProtocol)

    def test_plain_client_does_not_satisfy_command_protocol(self) -> None:
        assert not isinstance(FakeCDPClient(), CDPCommandProtocol)

    def test_arbitrary_object_rejects(self) -> None:
        assert not isinstance(object(), CDPCommandProtocol)


# ── PausedTarget resume behavior ──


class TestPausedTargetResume:
    @pytest.mark.asyncio
    async def test_resume_called_on_exit(self) -> None:
        cdp = FakeCDPCommandClient()
        async with PausedTarget(cdp=cdp):
            pass
        assert ('Runtime.runIfWaitingForDebugger', None) in cdp.sent_commands

    @pytest.mark.asyncio
    async def test_resume_called_on_exception(self) -> None:
        cdp = FakeCDPCommandClient()
        with pytest.raises(ValueError, match='boom'):
            async with PausedTarget(cdp=cdp):
                raise ValueError('boom')
        # Resume must still have been sent
        assert ('Runtime.runIfWaitingForDebugger', None) in cdp.sent_commands

    @pytest.mark.asyncio
    async def test_resume_is_idempotent(self) -> None:
        cdp = FakeCDPCommandClient()
        ctx = PausedTarget(cdp=cdp)
        async with ctx:
            pass
        # Manually call resume again — should be no-op
        await ctx.resume()
        resume_count = sum(
            1 for m, _ in cdp.sent_commands if m == 'Runtime.runIfWaitingForDebugger'
        )
        assert resume_count == 1

    @pytest.mark.asyncio
    async def test_custom_resume_callable(self) -> None:
        calls: list[str] = []

        async def my_resume() -> None:
            calls.append('resumed')

        async with PausedTarget(resume=my_resume):
            pass
        assert calls == ['resumed']

    @pytest.mark.asyncio
    async def test_custom_resume_idempotent(self) -> None:
        calls: list[str] = []

        async def my_resume() -> None:
            calls.append('resumed')

        ctx = PausedTarget(resume=my_resume)
        async with ctx:
            pass
        await ctx.resume()
        assert calls == ['resumed']  # only once


# ── PausedTarget ordering ──


class TestPausedTargetOrdering:
    @pytest.mark.asyncio
    async def test_setup_completes_before_resume(self) -> None:
        """All bridge+connect work inside the `async with` block runs before resume."""
        cdp = FakeCDPCommandClient()
        order: list[str] = []

        async with PausedTarget(cdp=cdp):
            order.append('setup')
            # Simulate bridge registration work
            await asyncio.sleep(0)
            order.append('bridge_done')

        order.append('after_exit')
        assert order == ['setup', 'bridge_done', 'after_exit']
        # Resume was the last command sent
        assert cdp.sent_commands[-1] == ('Runtime.runIfWaitingForDebugger', None)


# ── PausedTarget integration (end-to-end) ──


class TestPausedTargetIntegration:
    @pytest.mark.asyncio
    async def test_pause_bridge_resume_fire(self) -> None:
        """End-to-end: pause → register bridge + handler → resume → fire CDP → handler called."""
        cdp = FakeCDPCommandClient()
        scope = EventScope('tab-1')
        received: list[DialogEvent] = []

        def handler(event: DialogEvent) -> None:
            received.append(event)

        async with PausedTarget(cdp=cdp, session_id='sess-A'):
            bridge = CDPEventBridge(cdp, scope, session_id='sess-A')
            bridge.bridge(
                'Page.javascriptDialogOpening',
                lambda p: DialogEvent(url=p.get('url', ''), message=p.get('message', '')),
            )
            scope.connect(DialogEvent, handler, mode=ConnectionType.DIRECT, priority=100)

        # After resume, simulate CDP event
        cdp.fire('Page.javascriptDialogOpening', {
            'url': 'https://example.com',
            'message': 'Are you sure?',
            'sessionId': 'sess-A',
        })

        assert len(received) == 1
        assert received[0].url == 'https://example.com'
        assert received[0].message == 'Are you sure?'

        bridge.close()


# ── PausedTarget lifecycle ──


class TestPausedTargetLifecycle:
    @pytest.mark.asyncio
    async def test_not_reentrant(self) -> None:
        cdp = FakeCDPCommandClient()
        ctx = PausedTarget(cdp=cdp)
        async with ctx:
            with pytest.raises(RuntimeError, match='already entered'):
                async with ctx:
                    pass

    @pytest.mark.asyncio
    async def test_convenience_factory(self) -> None:
        cdp = FakeCDPCommandClient()
        ctx = CDPEventBridge.paused(cdp=cdp)
        assert isinstance(ctx, PausedTarget)
        async with ctx:
            pass
        assert ('Runtime.runIfWaitingForDebugger', None) in cdp.sent_commands

    @pytest.mark.asyncio
    async def test_neither_cdp_nor_resume_raises(self) -> None:
        with pytest.raises(ValueError, match='cdp.*resume'):
            PausedTarget()

    @pytest.mark.asyncio
    async def test_both_cdp_and_resume_raises(self) -> None:
        cdp = FakeCDPCommandClient()

        async def noop() -> None:
            pass

        with pytest.raises(ValueError, match='cdp.*resume'):
            PausedTarget(cdp=cdp, resume=noop)

    @pytest.mark.asyncio
    async def test_session_id_forwarded_to_cdp(self) -> None:
        """When session_id is given, resume command includes it in params."""
        cdp = FakeCDPCommandClient()
        async with PausedTarget(cdp=cdp, session_id='sess-X'):
            pass
        # The resume command should include sessionId
        method, params = cdp.sent_commands[-1]
        assert method == 'Runtime.runIfWaitingForDebugger'
        assert params == {'sessionId': 'sess-X'}
