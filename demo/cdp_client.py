"""Minimal Chrome DevTools Protocol client over WebSocket.

Just enough CDP to drive the demo — not a production client.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import orjson
import websockets

logger = logging.getLogger(__name__)


class CDPClient:
    """Thin CDP client: send commands, receive events."""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._ws: Any = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: dict[str, list[Callable[..., Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket connection is open and the receive loop is running."""
        return self._connected and self._ws is not None

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.ws_url, max_size=50 * 1024 * 1024)
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._connected = True

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a CDP command and wait for the response."""
        self._msg_id += 1
        msg: dict[str, Any] = {'id': self._msg_id, 'method': method}
        if params:
            msg['params'] = params
        if session_id:
            msg['sessionId'] = session_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[self._msg_id] = future

        # CDP requires text frames (not binary) — decode orjson bytes to str
        await self._ws.send(orjson.dumps(msg).decode())
        return await asyncio.wait_for(future, timeout=30.0)

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        """Register a handler for a CDP event (e.g. 'Page.javascriptDialogOpening')."""
        self._event_handlers.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        """Remove a previously registered CDP event handler."""
        handlers = self._event_handlers.get(method, [])
        if callback in handlers:
            handlers.remove(callback)

    # ── Browser-level session management ──

    async def init_browser_session(self) -> tuple[str, str]:
        """Initialize a browser-level connection: discover targets, attach to default tab.

        Call after connect() when connected to a browser-level WebSocket
        (ws://host/devtools/browser/xxx — the URL cloud providers give you).

        Returns (target_id, session_id) of the first available page target.
        """
        # Enable target discovery (required for Target.getTargets on browser WS)
        await self.send('Target.setDiscoverTargets', {'discover': True})

        # Find existing page targets
        targets = await self.send('Target.getTargets')
        pages = [t for t in targets.get('targetInfos', []) if t.get('type') == 'page']

        if not pages:
            result = await self.send('Target.createTarget', {'url': 'about:blank'})
            target_id: str = result['targetId']
        else:
            target_id = pages[0]['targetId']

        # Attach with flatten=true — all messages multiplexed on this single WS
        attach = await self.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
        session_id: str = attach['sessionId']

        await self.send('Page.enable', session_id=session_id)
        await self.send('Runtime.enable', session_id=session_id)
        await self.send('Network.enable', session_id=session_id)

        return target_id, session_id

    async def create_tab(self, url: str = 'about:blank') -> tuple[str, str]:
        """Create a new tab and attach to it. Returns (target_id, session_id).

        Requires a browser-level connection (init_browser_session called first).
        """
        result = await self.send('Target.createTarget', {'url': url})
        target_id: str = result['targetId']

        attach = await self.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
        session_id: str = attach['sessionId']

        await self.send('Page.enable', session_id=session_id)
        await self.send('Runtime.enable', session_id=session_id)
        await self.send('Network.enable', session_id=session_id)

        return target_id, session_id

    async def evaluate(
        self,
        expression: str,
        *,
        session_id: str | None = None,
    ) -> Any:
        """Convenience: Runtime.evaluate with returnByValue + awaitPromise."""
        result = await self.send(
            'Runtime.evaluate',
            {
                'expression': expression,
                'returnByValue': True,
                'awaitPromise': True,
            },
            session_id=session_id,
        )
        return result.get('result', {}).get('value')

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = orjson.loads(raw)
                if 'id' in msg:
                    future = self._pending.pop(msg['id'], None)
                    if future and not future.done():
                        if 'error' in msg:
                            future.set_exception(RuntimeError(msg['error'].get('message', str(msg['error']))))
                        else:
                            future.set_result(msg.get('result', {}))
                elif 'method' in msg:
                    handlers = self._event_handlers.get(msg['method'], [])
                    for handler in handlers:
                        try:
                            result = handler(msg.get('params', {}), msg.get('sessionId'))
                            if asyncio.iscoroutine(result):
                                asyncio.create_task(result)
                        except Exception:
                            logger.exception('CDP event handler error for %s', msg['method'])
        except websockets.ConnectionClosed:
            self._connected = False
            logger.info('CDP WebSocket closed')
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._connected = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
