"""Isolated Chrome fixture for opt-in browser acceptance tests."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from anyio import Path

from demo.cdp_client import CDPClient


@asynccontextmanager
async def isolated_chrome() -> AsyncIterator[CDPClient]:
    chrome = os.environ.get('CHROME_BIN') or shutil.which('google-chrome') or shutil.which('chromium')
    mac_chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if chrome is None and await mac_chrome.exists():
        chrome = str(mac_chrome)
    if chrome is None:
        pytest.skip('Chrome binary unavailable; set CHROME_BIN')
    with tempfile.TemporaryDirectory(prefix='agent-cdp-acceptance-') as profile:
        process = await asyncio.create_subprocess_exec(
            chrome,
            '--headless=new',
            '--no-sandbox',
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--no-first-run',
            '--no-default-browser-check',
            '--remote-debugging-port=0',
            '--window-size=1280,900',
            f'--user-data-dir={profile}',
            'about:blank',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        client: CDPClient | None = None
        try:
            port_file = Path(profile) / 'DevToolsActivePort'
            async with asyncio.timeout(15):
                while not await port_file.exists():
                    if process.returncode is not None:
                        pytest.fail(f'Chrome exited with {process.returncode}')
                    await asyncio.sleep(0.05)
                port, browser_path = (await port_file.read_text()).splitlines()[:2]
            client = CDPClient(f'ws://127.0.0.1:{port}{browser_path}')
            await client.connect()
            yield client
        finally:
            if client is not None:
                await client.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
