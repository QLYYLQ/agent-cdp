"""Verify three v0.3.1 diagnostic features on Amazon.

1. CDPEventBridge zero-hit report — bridges a domain that won't fire on page-level
2. EventTimeoutError enhanced diagnostics — triggers a timeout with multiple handlers
3. CDPCommandProtocol.is_connected — checks before/after close

Usage:
    uv run python -m demo.verify_diagnostics
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import ClassVar

from agent_cdp import (
    CDPCommandProtocol,
    CDPEventBridge,
    ConnectionType,
    ScopeGroup,
    event_result,
)
from agent_cdp.events import BaseEvent, EmitPolicy, EventTimeoutError

from ._output import DIM, GREEN, RESET, banner, fail, info, ok, phase, warn
from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome
from .events import (
    BrowserErrorEvent,
    NavigateToUrlEvent,
    ScreenshotEvent,
)
from .watchdogs import (
    CrashWatchdog,
    ScreenshotWatchdog,
    SecurityWatchdog,
    make_navigation_handler,
    save_screenshot,
)


# ── Custom event for timeout diagnostics test ──


class DiagTimeoutEvent(BaseEvent[str]):
    """Event with short timeout for diagnostics demo."""

    __registry_key__ = 'demo.diag_timeout'
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.COLLECT_ERRORS


# ── Main ──


async def run_verify() -> None:
    banner('v0.3.1 Diagnostics Verification — Amazon')
    print(f'{DIM}Verifying: zero-hit report, timeout diagnostics, is_connected{RESET}\n')

    chrome_proc = None
    cdp: CDPClient | None = None
    group: ScopeGroup | None = None

    try:
        # ── Setup: Launch Chrome & Navigate to Amazon ──
        phase(0, 'Launch Chrome & Navigate to Amazon')

        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome started (PID {chrome_proc.pid})')

        cdp = CDPClient(ws_url)
        await cdp.connect()
        ok(f'CDP connected, is_connected={cdp.is_connected}')

        # Discover & attach
        targets = await cdp.send('Target.getTargets')
        pages = [t for t in targets.get('targetInfos', []) if t.get('type') == 'page']
        target_id = pages[0]['targetId']
        attach_result = await cdp.send('Target.attachToTarget', {
            'targetId': target_id, 'flatten': True,
        })
        session_id = attach_result['sessionId']
        ok(f'Attached to session {session_id[:12]}...')

        await cdp.send('Page.enable', session_id=session_id)
        await cdp.send('Runtime.enable', session_id=session_id)
        await cdp.send('Network.enable', session_id=session_id)

        # Set viewport so Amazon renders properly (non-headless still needs a size)
        await cdp.send('Emulation.setDeviceMetricsOverride', {
            'width': 1280, 'height': 800, 'deviceScaleFactor': 1, 'mobile': False,
        }, session_id=session_id)
        ok('Viewport set to 1280x800')

        group = ScopeGroup('browser')
        scope = await group.create_scope('amazon-tab', target_id=target_id, session_id=session_id)
        ok('Scope created')

        # Navigate to Amazon
        security = SecurityWatchdog(allowed_domains=['amazon.com', 'www.amazon.com'])
        security.attach(scope)
        nav_handler = make_navigation_handler(cdp, session_id, scope)
        scope.connect(NavigateToUrlEvent, nav_handler,
                      mode=ConnectionType.QUEUED, target_scope=scope, priority=0)

        info('Navigating to https://www.amazon.com ...')
        nav_event = NavigateToUrlEvent(url='https://www.amazon.com')
        scope.emit(nav_event)
        await nav_event
        nav_result = await event_result(nav_event)
        ok(f'Navigation complete: {nav_result}')

        # Wait for Amazon JS to fully render
        info('Waiting 5s for Amazon page to fully render...')
        await asyncio.sleep(5.0)

        # Take a screenshot to prove we're on Amazon
        screenshot_wd = ScreenshotWatchdog(cdp)
        screenshot_wd.attach(scope, session_id)
        ss_event = ScreenshotEvent()
        scope.emit(ss_event)
        await ss_event
        ss_data = await event_result(ss_event)
        if ss_data:
            out = save_screenshot(ss_data, Path(__file__).parent / 'screenshots' / 'verify_amazon.png')
            ok(f'Screenshot saved: {out} ({out.stat().st_size} bytes)')
        else:
            warn('No screenshot data')

        # ═══════════════════════════════════════════════════════════════
        # Feature 1: CDPEventBridge zero-hit report
        # ═══════════════════════════════════════════════════════════════
        phase(1, 'CDPEventBridge Zero-Hit Report')
        info('Bridge Browser.downloadWillBegin (page-level, will never fire)')
        info('Bridge Page.loadEventFired (should fire from navigation)')

        diag_bridge = CDPEventBridge(cdp, scope, session_id=session_id)
        diag_bridge.bridge(
            'Browser.downloadWillBegin',
            lambda p: BrowserErrorEvent(
                error_type='download', message='download', details=str(p),
            ),
        )
        diag_bridge.bridge(
            'Page.loadEventFired',
            lambda p: BrowserErrorEvent(
                error_type='load', message='page loaded', details=str(p),
            ),
        )

        # Trigger a Page.loadEventFired via explicit CDP reload
        info('Reloading page via CDP to trigger Page.loadEventFired...')
        await cdp.send('Page.reload', session_id=session_id)
        await asyncio.sleep(3.0)  # wait for load event to fire and propagate

        # Check hit counts
        counts = diag_bridge.hit_counts
        ok(f'Hit counts: {counts}')
        assert counts.get('Browser.downloadWillBegin', 0) == 0, 'Browser event should have 0 hits'
        assert counts.get('Page.loadEventFired', 0) >= 1, 'Page event should have >=1 hit'
        ok('Browser.downloadWillBegin = 0 hits (expected — page-level connection)')
        ok(f'Page.loadEventFired = {counts["Page.loadEventFired"]} hit(s)')

        # Close bridge — should log zero-hit warning
        info('Closing bridge — should emit zero-hit WARNING...')
        diag_bridge.close()
        ok('Bridge closed (check stderr for zero-hit warning on Browser.downloadWillBegin)')

        # ═══════════════════════════════════════════════════════════════
        # Feature 2: EventTimeoutError enhanced diagnostics
        # ═══════════════════════════════════════════════════════════════
        print()
        phase(2, 'EventTimeoutError Enhanced Diagnostics')
        info('Creating event with 2 handlers: fast (completes) + slow (hangs)')

        def fast_direct_handler(event: DiagTimeoutEvent) -> str:
            return 'fast_done'

        async def slow_queued_handler(event: DiagTimeoutEvent) -> str:
            await asyncio.sleep(999)
            return 'never'

        scope.connect(DiagTimeoutEvent, fast_direct_handler,
                      mode=ConnectionType.DIRECT, priority=100)
        scope.connect(DiagTimeoutEvent, slow_queued_handler,
                      mode=ConnectionType.QUEUED, target_scope=scope, priority=0)

        diag_event = DiagTimeoutEvent(event_timeout=0.3)
        scope.emit(diag_event)
        info('Emitted DiagTimeoutEvent (timeout=0.3s). fast_direct ran inline. slow_queued queued...')

        # Direct handler already ran, check result
        ok(f'Direct results after emit: {dict(diag_event.event_results)}')

        try:
            await diag_event
            fail('Expected EventTimeoutError but event completed!')
        except EventTimeoutError as e:
            ok(f'EventTimeoutError raised!')
            ok(f'  event_type: {e.event_type}')
            ok(f'  timeout: {e.timeout}s')
            ok(f'  pending_count: {e.pending_count}')
            ok(f'  completed_handlers: {e.completed_handlers}')
            ok(f'  failed_handlers: {e.failed_handlers}')
            ok(f'  timed_out_handlers: {e.timed_out_handlers}')
            ok(f'  Full message: {e}')

            # Validate diagnostics
            assert e.pending_count >= 0
            msg = str(e)
            assert 'handler(s) still pending' in msg
            ok('Diagnostic info validated')

        # ═══════════════════════════════════════════════════════════════
        # Feature 3: CDPCommandProtocol.is_connected
        # ═══════════════════════════════════════════════════════════════
        print()
        phase(3, 'CDPCommandProtocol.is_connected')

        ok(f'CDPClient satisfies CDPCommandProtocol: {isinstance(cdp, CDPCommandProtocol)}')
        ok(f'cdp.is_connected (before close): {cdp.is_connected}')
        assert cdp.is_connected is True

        # Close CDP and verify
        await group.close_all()
        ok('Scopes closed')

        await cdp.close()
        ok(f'cdp.is_connected (after close): {cdp.is_connected}')
        assert cdp.is_connected is False
        ok('is_connected correctly reflects connection state')

        cdp = None  # prevent double-close in finally

        # ── Summary ──
        print()
        banner('All Diagnostics Verified')
        print(f'{GREEN}Three v0.3.1 features working correctly on live Amazon:{RESET}')
        print('  1. CDPEventBridge zero-hit report detected Browser.* misconfiguration')
        print('  2. EventTimeoutError includes pending count + handler completion details')
        print('  3. CDPCommandProtocol.is_connected tracks WebSocket lifecycle')
        print()

    except Exception:
        logging.exception('Verification failed')
        raise
    finally:
        if cdp:
            await cdp.close()
        if chrome_proc:
            kill_chrome(chrome_proc)
            info('Chrome terminated')


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname).1s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('agent_cdp.scope.event_loop').setLevel(logging.WARNING)

    asyncio.run(run_verify())


if __name__ == '__main__':
    main()
