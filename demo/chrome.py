"""Chrome process launcher for WSL2 (--disable-gpu --no-sandbox)."""

import asyncio
import json
import logging
import subprocess
import tempfile
import urllib.request

logger = logging.getLogger(__name__)

CHROME_BIN = 'google-chrome'


async def launch_chrome(port: int = 9222) -> tuple[subprocess.Popen[bytes], str]:
    """Launch Chrome with remote debugging enabled.

    Returns (process, browser_ws_url).
    Raises TimeoutError if Chrome doesn't start within 15 seconds.
    """
    user_data_dir = tempfile.mkdtemp(prefix='agent-cdp-chrome-')
    args = [
        CHROME_BIN,
        f'--remote-debugging-port={port}',
        f'--user-data-dir={user_data_dir}',
        '--disable-gpu',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows',
        'about:blank',
    ]
    logger.info('Launching Chrome: %s', ' '.join(args))
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Poll until CDP is ready
    for attempt in range(30):
        try:
            resp = urllib.request.urlopen(f'http://localhost:{port}/json/version', timeout=2)
            data = json.loads(resp.read())
            ws_url: str = data['webSocketDebuggerUrl']
            logger.info('Chrome ready on attempt %d — ws: %s', attempt + 1, ws_url)
            return proc, ws_url
        except Exception:
            await asyncio.sleep(0.5)

    proc.kill()
    msg = f'Chrome did not start within 15s on port {port}'
    raise TimeoutError(msg)


def kill_chrome(proc: subprocess.Popen[bytes]) -> None:
    """Terminate Chrome gracefully, then force-kill after 3s."""
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
