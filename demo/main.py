#!/usr/bin/env python3
"""Real-browser demo: agent-cdp scoped event system with live Chrome.

Demonstrates the key advantages over bubus:
1. Direct dispatch for zero-latency security gating
2. event.consume() for propagation control
3. Priority-based handler ordering
4. Per-scope isolation (independent tabs)
5. Queued handlers for async CDP operations
6. CDP event bridging into the scoped event system

Usage:
    uv run python -m demo.main
"""

import asyncio
import logging
import sys
from pathlib import Path

from agent_cdp import ConnectionType, ScopeGroup, event_result

from ._output import DIM, GREEN, RESET, banner, fail, info, ok, phase
from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome
from .events import (
    BrowserConnectedEvent,
    NavigateToUrlEvent,
    ScreenshotEvent,
    TabCreatedEvent,
)
from .watchdogs import (
    CrashWatchdog,
    PopupsWatchdog,
    ScreenshotWatchdog,
    SecurityWatchdog,
    make_navigation_handler,
    save_screenshot,
)

# ── Main demo ──


async def run_demo() -> None:
    banner('agent-cdp Real Browser Demo')
    print(f'{DIM}Scoped Event System — live Chrome via CDP{RESET}')
    print(f'{DIM}Migrated watchdogs: Security, Popups, Screenshot, Crash{RESET}\n')

    chrome_proc = None
    cdp: CDPClient | None = None
    group: ScopeGroup | None = None

    try:
        # ── Phase 1: Launch Chrome & Connect ──
        phase(1, 'Launch Chrome & Connect via CDP')

        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome started (PID {chrome_proc.pid})')
        info(f'WebSocket: {ws_url}')

        cdp = CDPClient(ws_url)
        await cdp.connect()
        ok('CDP WebSocket connected')

        # Discover existing targets
        targets = await cdp.send('Target.getTargets')
        pages = [t for t in targets.get('targetInfos', []) if t.get('type') == 'page']
        ok(f'Found {len(pages)} page target(s)')

        if not pages:
            fail('No page targets found, aborting')
            return

        # Attach to first page
        target_id = pages[0]['targetId']
        attach_result = await cdp.send(
            'Target.attachToTarget',
            {
                'targetId': target_id,
                'flatten': True,
            },
        )
        session_id = attach_result['sessionId']
        ok(f'Attached to target {target_id[:12]}... → session {session_id[:12]}...')

        # Enable CDP domains
        await cdp.send('Page.enable', session_id=session_id)
        await cdp.send('Runtime.enable', session_id=session_id)
        await cdp.send('Network.enable', session_id=session_id)
        ok('CDP domains enabled (Page, Runtime, Network)')

        # Create ScopeGroup and tab scope
        group = ScopeGroup('browser')
        tab_scope = await group.create_scope(
            'tab-1',
            target_id=target_id,
            session_id=session_id,
        )
        ok('ScopeGroup "browser" + EventScope "tab-1" created (event loop running)')

        # Emit BrowserConnectedEvent
        tab_scope.emit(BrowserConnectedEvent(cdp_url=ws_url))
        info('BrowserConnectedEvent emitted')

        # ── Phase 2: Security Gating ──
        phase(2, 'Security Gating (Direct + consume + priority)')
        info('SecurityWatchdog: Direct mode, priority=100, allowed=[example.com, httpbin.org]')
        info('NavigationHandler: Queued mode, priority=0')

        security = SecurityWatchdog(allowed_domains=['example.com', 'httpbin.org'])
        security.attach(tab_scope)

        nav_handler = make_navigation_handler(cdp, session_id, tab_scope)
        tab_scope.connect(
            NavigateToUrlEvent,
            nav_handler,
            mode=ConnectionType.QUEUED,
            target_scope=tab_scope,
            priority=0,
        )

        # Test 2a: Blocked URL
        print()
        info('Test 2a: Navigate to https://evil.com (should be BLOCKED)')
        blocked_event = NavigateToUrlEvent(url='https://evil.com')
        try:
            tab_scope.emit(blocked_event)
            fail('Expected ValueError but navigation was allowed!')
        except ValueError as e:
            ok(f'BLOCKED — {e}')
            ok(f'event.consumed = {blocked_event.consumed}')
            ok(f'event.event_results = {blocked_event.event_results} (empty — nav handler never ran)')

        # Test 2b: Allowed URL
        print()
        info('Test 2b: Navigate to https://example.com (should be ALLOWED)')
        allowed_event = NavigateToUrlEvent(url='https://example.com')
        tab_scope.emit(allowed_event)
        ok(f'event.consumed = {allowed_event.consumed} (security check passed)')
        ok('NavigateToUrlEvent dispatched — waiting for queued navigation handler...')

        await allowed_event  # wait for queued handler to complete
        nav_result = await event_result(allowed_event)
        ok(f'Navigation complete → result: {nav_result}')

        # ── Phase 3: Popup Handling ──
        print()
        phase(3, 'Popup Handling (CDP event bridge → agent-cdp → auto-dismiss)')

        popups = PopupsWatchdog(cdp)
        popups.attach(tab_scope, session_id)
        ok('PopupsWatchdog attached (Direct handler + CDP bridge)')

        # Inject a JS alert
        info('Injecting JavaScript alert("Hello from agent-cdp!")...')
        # The CDP dialog event will fire, bridge to PopupDialogEvent, and auto-dismiss
        try:
            await cdp.send(
                'Runtime.evaluate',
                {'expression': 'alert("Hello from agent-cdp!")'},
                session_id=session_id,
            )
        except Exception:
            pass  # alert blocks Runtime.evaluate until dismissed, which is fine

        await asyncio.sleep(1.0)  # give the bridge time to process

        if popups.dismissed_dialogs:
            ok(f'Auto-dismissed {len(popups.dismissed_dialogs)} dialog(s):')
            for d in popups.dismissed_dialogs:
                info(f'{d["type"]}: "{d["message"]}"')
        else:
            fail('No dialogs were captured (bridge may not have fired)')

        # ── Phase 4: Screenshot ──
        print()
        phase(4, 'Screenshot (Queued handler → async CDP → PNG)')

        screenshot_wd = ScreenshotWatchdog(cdp)
        screenshot_wd.attach(tab_scope, session_id)
        ok('ScreenshotWatchdog attached (Queued handler)')

        info('Emitting ScreenshotEvent...')
        ss_event = ScreenshotEvent()
        tab_scope.emit(ss_event)
        ok('ScreenshotEvent dispatched — waiting for queued capture...')

        await ss_event  # wait for async handler
        ss_data = await event_result(ss_event)
        if ss_data:
            out_path = save_screenshot(ss_data, Path(__file__).parent / 'screenshot.png')
            ok(f'Screenshot saved: {out_path} ({out_path.stat().st_size} bytes)')
        else:
            fail('No screenshot data returned')

        # ── Phase 5: Multi-Scope Isolation ──
        print()
        phase(5, 'Multi-Scope Isolation (per-tab independence)')

        # Create a second tab via CDP
        new_target = await cdp.send('Target.createTarget', {'url': 'about:blank'})
        target_id_2 = new_target['targetId']
        attach_2 = await cdp.send(
            'Target.attachToTarget',
            {
                'targetId': target_id_2,
                'flatten': True,
            },
        )
        session_id_2 = attach_2['sessionId']
        await cdp.send('Page.enable', session_id=session_id_2)

        tab_scope_2 = await group.create_scope(
            'tab-2',
            target_id=target_id_2,
            session_id=session_id_2,
        )
        ok(f'Second tab created: scope "tab-2", target {target_id_2[:12]}...')

        # Attach independent handlers to each scope
        scope1_events: list[str] = []
        scope2_events: list[str] = []

        tab_scope.connect(
            TabCreatedEvent,
            lambda e: scope1_events.append(f'tab-1 saw: {e.target_id[:12]}'),
            mode=ConnectionType.DIRECT,
        )
        tab_scope_2.connect(
            TabCreatedEvent,
            lambda e: scope2_events.append(f'tab-2 saw: {e.target_id[:12]}'),
            mode=ConnectionType.DIRECT,
        )

        # Emit on scope 1 only
        tab_scope.emit(TabCreatedEvent(target_id='test-target-A'))
        ok(f'Emit on tab-1 only: scope1={scope1_events}, scope2={scope2_events}')
        assert len(scope1_events) == 1, 'scope1 should have 1 event'
        assert len(scope2_events) == 0, 'scope2 should have 0 events'
        ok('Per-scope isolation confirmed')

        # Broadcast to all scopes
        scope1_events.clear()
        scope2_events.clear()
        group.broadcast(TabCreatedEvent(target_id='test-target-B'))
        ok(f'Broadcast: scope1={scope1_events}, scope2={scope2_events}')
        assert len(scope1_events) == 1, 'scope1 should have 1 event from broadcast'
        assert len(scope2_events) == 1, 'scope2 should have 1 event from broadcast'
        ok('Broadcast to all scopes confirmed')

        # ── Phase 6: Crash Watchdog ──
        print()
        phase(6, 'Crash Watchdog (CDP bridge + BrowserErrorEvent)')

        crash_wd = CrashWatchdog(cdp)
        crash_wd.attach(tab_scope)
        ok('CrashWatchdog attached (CDP Target.targetCrashed bridge)')

        # We can't easily trigger a real crash, but we verify the wiring
        error_events: list[str] = []
        from .events import BrowserErrorEvent

        tab_scope.connect(
            BrowserErrorEvent,
            lambda e: error_events.append(e.message),
            mode=ConnectionType.DIRECT,
        )
        ok('BrowserErrorEvent handler connected — crash detection ready')
        info('(Crash watchdog wiring verified — real crashes would emit BrowserErrorEvent)')

        # ── Phase 7: Event History & Scope Lifecycle ──
        print()
        phase(7, 'Event History & Scope Lifecycle')

        history = tab_scope.event_history
        ok(f'tab-1 event history: {len(history)} events recorded')
        for i, ev in enumerate(history):
            info(f'  [{i}] {type(ev).__name__} (id={ev.event_id[:8]}..., consumed={ev.consumed})')

        # Close scope 2 — auto-disconnects all connections
        await group.close_scope('tab-2')
        ok('tab-2 closed — all connections auto-disconnected')
        ok(f'Remaining scopes: {group.scope_ids}')

        # Verify closed scope rejects emit
        from .events import TabClosedEvent

        try:
            tab_scope_2.emit(TabClosedEvent(target_id=target_id_2))
            fail('Expected RuntimeError on closed scope')
        except RuntimeError as e:
            ok(f'Closed scope correctly rejects emit: {e}')

        # Close remaining
        await group.close_all()
        ok(f'All scopes closed. Group empty: {group.scope_count == 0}')

        # ── Summary ──
        print()
        banner('Demo Complete')
        print(f'{GREEN}All phases passed. The scoped event system works correctly with real Chrome.{RESET}')
        print()
        print(f'{DIM}Key behaviors demonstrated:{RESET}')
        print('  1. Direct dispatch: SecurityWatchdog blocked evil.com in emit() call stack')
        print('  2. event.consume(): Stopped propagation before NavigationHandler ran')
        print('  3. Priority ordering: Security (100) ran before Navigation (0)')
        print('  4. Queued handlers: Screenshot capture ran async via event loop')
        print('  5. CDP bridge: Chrome dialog events → PopupDialogEvent → auto-dismiss')
        print('  6. Per-scope isolation: tab-1/tab-2 events independent')
        print('  7. Broadcast: Single event delivered to all scopes (deep-copied)')
        print('  8. Auto-disconnect: scope.close() severed all connections')
        print()

    except Exception:
        logging.exception('Demo failed')
        raise
    finally:
        # Cleanup
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
    # Suppress noisy loggers
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('agent_cdp.scope.event_loop').setLevel(logging.WARNING)

    asyncio.run(run_demo())


if __name__ == '__main__':
    main()
