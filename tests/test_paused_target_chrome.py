"""Opt-in real Chrome regression: AGENT_CDP_CHROME_TEST=1 pytest -k chrome.

Uses a temporary, isolated headless profile. CHROME_BIN may override discovery.
A worker starts paused by auto-attach, then the built-in PausedTarget resume must
actually permit its JavaScript to execute through the session envelope.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Any

import pytest
from anyio import Path

from agent_cdp.bridge import PausedTarget
from demo.cdp_client import CDPClient

pytestmark = pytest.mark.skipif(
    os.environ.get('AGENT_CDP_CHROME_TEST') != '1', reason='set AGENT_CDP_CHROME_TEST=1 to launch isolated Chrome'
)


async def test_builtin_resume_unblocks_real_chrome_target() -> None:
    chrome = os.environ.get('CHROME_BIN') or shutil.which('google-chrome') or shutil.which('chromium')
    mac_chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if chrome is None and await mac_chrome.exists():
        chrome = str(mac_chrome)
    if chrome is None:
        pytest.skip('Chrome binary unavailable; set CHROME_BIN')

    with tempfile.TemporaryDirectory(prefix='agent-cdp-resume-test-') as profile:
        process = await asyncio.create_subprocess_exec(
            chrome,
            '--headless=new',
            '--no-sandbox',
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--no-first-run',
            '--no-default-browser-check',
            '--remote-debugging-port=0',
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
            attached: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            def on_attached(params: dict[str, Any], route: str | None) -> None:
                attached.put_nowait(params)

            client.on_event('Target.attachedToTarget', on_attached)
            await client.send(
                'Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True}
            )
            created = await client.send('Target.createTarget', {'url': 'about:blank'})
            async with asyncio.timeout(5):
                while True:
                    attachment = await attached.get()
                    if attachment['targetInfo']['targetId'] == created['targetId']:
                        break
            page_session = attachment['sessionId']
            await client.send('Runtime.runIfWaitingForDebugger', session_id=page_session)
            # Workers are paused before their first script instruction. A page
            # may emit an initial about:blank load even while waiting to start.
            await client.send(
                'Target.setAutoAttach',
                {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True},
                session_id=page_session,
            )
            await client.evaluate(
                'new Worker(URL.createObjectURL(new Blob(['
                '\'self.agentCdpResumeVerified=12345;console.log("agent-cdp-resumed")\''
                "], {type: 'text/javascript'})))",
                session_id=page_session,
            )
            async with asyncio.timeout(5):
                while True:
                    attachment = await attached.get()
                    if attachment['targetInfo']['type'] == 'worker':
                        break
            assert attachment['waitingForDebugger'] is True
            session = attachment['sessionId']
            script_ran = asyncio.Event()

            def on_console(params: dict[str, Any], route: str | None) -> None:
                if route == session and any(arg.get('value') == 'agent-cdp-resumed' for arg in params.get('args', [])):
                    script_ran.set()

            client.on_event('Runtime.consoleAPICalled', on_console)
            await client.send('Runtime.enable', session_id=session)
            # The old params-only command goes to the browser, not this worker.
            try:
                await client.send('Runtime.runIfWaitingForDebugger', {'sessionId': session})
            except RuntimeError:
                pass
            await asyncio.sleep(0.1)
            assert not script_ran.is_set()
            async with PausedTarget(cdp=client, session_id=session) as paused:
                assert not script_ran.is_set()
            assert paused.resumed
            await asyncio.wait_for(script_ran.wait(), timeout=5)
            assert await client.evaluate('self.agentCdpResumeVerified', session_id=session) == 12345
            await client.send('Target.closeTarget', {'targetId': created['targetId']})
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
