"""Minimal Chrome DevTools Protocol client over WebSocket.

Just enough CDP to drive the demo — not a production client.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

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

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.ws_url, max_size=50 * 1024 * 1024)
        self._recv_task = asyncio.create_task(self._recv_loop())

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

        await self._ws.send(json.dumps(msg))
        return await asyncio.wait_for(future, timeout=30.0)

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        """Register a handler for a CDP event (e.g. 'Page.javascriptDialogOpening')."""
        self._event_handlers.setdefault(method, []).append(callback)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if 'id' in msg:
                    future = self._pending.pop(msg['id'], None)
                    if future and not future.done():
                        if 'error' in msg:
                            future.set_exception(
                                RuntimeError(msg['error'].get('message', str(msg['error'])))
                            )
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
            logger.info('CDP WebSocket closed')
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
