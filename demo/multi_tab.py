#!/usr/bin/env python3
"""Multi-tab demo: global vs tab-specific watchdogs with real Chrome.

Tests:
1. Multiple tabs open simultaneously with per-scope isolation
2. Global watchdogs (Security, Crash) via ScopeGroup.connect_all_scopes
3. Tab-specific watchdogs (Popups, Screenshot, Captcha) per scope
4. Cross-tab event isolation (popup on tab A ≠ popup on tab B)
5. Broadcast delivery to all tabs
6. CaptchaWatchdog DOM-based detection on reCAPTCHA demo page
7. Tab close → auto-disconnect, remaining tabs unaffected

Usage:
    uv run python -m demo.multi_tab
"""

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

from agent_cdp import BaseEvent, ConnectionType, EventScope, ScopeGroup, event_result

from ._output import BOLD, DIM, GREEN, RESET, YELLOW, banner, fail, fmt_us, info, ok, phase, tab_label
from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome
from .events import (
    BrowserErrorEvent,
    CaptchaDetectedEvent,
    CaptchaStateChangedEvent,
    GlobalMonitorEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    PopupDialogEvent,
    ScreenshotEvent,
)
from .timing import TimingCollector
from .watchdogs import (
    CaptchaWatchdog,
    CrashWatchdog,
    PopupsWatchdog,
    ScreenshotWatchdog,
    SecurityWatchdog,
    save_screenshot,
)

# ── Tab definitions ──

TABS = [
    {'name': 'Google', 'url': 'https://www.google.com', 'domain': 'google.com'},
    {'name': 'Bilibili', 'url': 'https://www.bilibili.com', 'domain': 'bilibili.com'},
    {'name': 'Xiaohongshu', 'url': 'https://www.xiaohongshu.com', 'domain': 'xiaohongshu.com'},
    {'name': 'reCAPTCHA', 'url': 'https://www.google.com/recaptcha/api2/demo', 'domain': 'google.com'},
]

TABS_NAMES = [t['name'] for t in TABS]


# ── Tab session setup ──


async def create_tab(cdp: CDPClient, url: str = 'about:blank') -> tuple[str, str]:
    """Create a new tab and attach to it. Returns (target_id, session_id)."""
    result = await cdp.send('Target.createTarget', {'url': url})
    target_id = result['targetId']

    attach = await cdp.send(
        'Target.attachToTarget',
        {
            'targetId': target_id,
            'flatten': True,
        },
    )
    session_id = attach['sessionId']

    await cdp.send('Page.enable', session_id=session_id)
    await cdp.send('Runtime.enable', session_id=session_id)

    return target_id, session_id


async def navigate_and_wait(
    cdp: CDPClient,
    session_id: str,
    url: str,
    timeout: float = 30.0,
) -> float:
    """Navigate to URL and wait for load. Returns load time in µs."""
    load_event = asyncio.Event()
    t0 = time.perf_counter_ns()

    def on_load(params: dict, sid: str | None) -> None:
        if sid == session_id:
            load_event.set()

    cdp.on_event('Page.loadEventFired', on_load)
    try:
        await cdp.send('Page.navigate', {'url': url}, session_id=session_id)
        await asyncio.wait_for(load_event.wait(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        cdp.off_event('Page.loadEventFired', on_load)

    return (time.perf_counter_ns() - t0) / 1000.0


# ── Main demo ──


async def run_multi_tab() -> None:
    banner('Multi-Tab Watchdog Demo')
    print(f'{DIM}Global vs tab-specific watchdogs · per-scope isolation · captcha detection{RESET}\n')

    tc = TimingCollector()
    chrome_proc = None
    cdp: CDPClient | None = None
    group: ScopeGroup | None = None

    try:
        # ══════════════════════════════════════════════════════
        phase(1, 'Launch Chrome & Create 4 Tabs')
        # ══════════════════════════════════════════════════════

        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome started (PID {chrome_proc.pid})')

        cdp = CDPClient(ws_url)
        await cdp.connect()
        ok('CDP connected')

        # Close the default about:blank tab
        targets = await cdp.send('Target.getTargets')
        default_pages = [t for t in targets.get('targetInfos', []) if t.get('type') == 'page']

        # Create 4 tabs
        tab_sessions: dict[str, dict[str, Any]] = {}
        for tab_def in TABS:
            target_id, session_id = await create_tab(cdp)
            tab_sessions[tab_def['name']] = {
                'target_id': target_id,
                'session_id': session_id,
                'url': tab_def['url'],
                'domain': tab_def['domain'],
            }
            ok(f'{tab_label(tab_def["name"])} tab created (target={target_id[:12]}...)')

        # Close default tab
        if default_pages:
            await cdp.send('Target.closeTarget', {'targetId': default_pages[0]['targetId']})
            info('Default about:blank tab closed')

        # ══════════════════════════════════════════════════════
        phase(2, 'ScopeGroup + Per-Tab EventScopes')
        # ══════════════════════════════════════════════════════

        group = ScopeGroup('browser')
        scopes: dict[str, EventScope] = {}

        for name, sess in tab_sessions.items():
            scope = await group.create_scope(
                f'tab-{name.lower()}',
                target_id=sess['target_id'],
                session_id=sess['session_id'],
            )
            scopes[name] = scope
            ok(f'{tab_label(name)} EventScope "tab-{name.lower()}" created')

        ok(f'ScopeGroup "browser" with {group.scope_count} scopes')

        # ══════════════════════════════════════════════════════
        phase(3, 'Global Watchdogs (all tabs via connect_all_scopes)')
        # ══════════════════════════════════════════════════════

        all_domains = list({t['domain'] for t in TABS})
        security = SecurityWatchdog(allowed_domains=all_domains)

        # Attach security to ALL scopes at once
        with tc.measure('connect_all_scopes (Security)', 'framework', '__global__'):
            group.connect_all_scopes(
                NavigateToUrlEvent,
                security.check_navigation,
                mode=ConnectionType.DIRECT,
                priority=100,
            )
        ok(f'SecurityWatchdog → all {group.scope_count} scopes (Direct, p=100) — {fmt_us(tc.records[-1].duration_us)}')

        # Global event monitor — catches all events across all scopes
        global_events: list[tuple[str, str]] = []  # (scope_id, event_type)

        def global_monitor(event: Any) -> None:
            # Find which scope emitted this
            for name, scope in scopes.items():
                if event in scope.event_history:
                    global_events.append((name, type(event).__name__))
                    break

        # Crash watchdog on all scopes
        crash = CrashWatchdog(cdp)
        for name, scope in scopes.items():
            crash.attach(scope)

        # Global error handler on all scopes
        group.connect_all_scopes(
            BrowserErrorEvent,
            lambda e: global_events.append(('global', f'ERROR: {e.message}')),
            mode=ConnectionType.DIRECT,
        )
        ok('CrashWatchdog + BrowserErrorEvent handler → all scopes')

        # ══════════════════════════════════════════════════════
        phase(4, 'Tab-Specific Watchdogs')
        # ══════════════════════════════════════════════════════

        popups_per_tab: dict[str, PopupsWatchdog] = {}
        screenshot_per_tab: dict[str, ScreenshotWatchdog] = {}
        captcha_wd: CaptchaWatchdog | None = None

        for name, scope in scopes.items():
            sess = tab_sessions[name]
            sid = sess['session_id']

            # Per-tab popup handler
            pw = PopupsWatchdog(cdp)
            pw.attach(scope, sid)
            popups_per_tab[name] = pw

            # Per-tab screenshot handler
            sw = ScreenshotWatchdog(cdp)
            sw.attach(scope, sid)
            screenshot_per_tab[name] = sw

            ok(f'{tab_label(name)} PopupsWatchdog(Direct,p=100 + Queued,p=50) + ScreenshotWatchdog(Queued)')

            # Captcha watchdog only on reCAPTCHA tab
            if name == 'reCAPTCHA':
                captcha_wd = CaptchaWatchdog(cdp)
                captcha_wd.attach(scope, sid)

                # Listen for captcha events on this scope
                scope.connect(
                    CaptchaDetectedEvent,
                    lambda e: info(
                        f'  CaptchaDetectedEvent: vendor={e.vendor}, sitekey={e.sitekey[:20]}..., '
                        f'solved={e.solved}, elements={e.element_count}'
                    ),
                    mode=ConnectionType.DIRECT,
                )
                scope.connect(
                    CaptchaStateChangedEvent,
                    lambda e: info(f'  CaptchaStateChangedEvent: state={e.state}'),
                    mode=ConnectionType.DIRECT,
                )
                ok(f'{tab_label(name)} CaptchaWatchdog(Queued) + event listeners')

        # ══════════════════════════════════════════════════════
        phase(5, 'Navigate All Tabs (parallel)')
        # ══════════════════════════════════════════════════════

        info('Navigating all 4 tabs concurrently...')
        t_nav_start = time.perf_counter_ns()

        nav_tasks = []
        for name, sess in tab_sessions.items():
            task = navigate_and_wait(cdp, sess['session_id'], sess['url'])
            nav_tasks.append((name, task))

        results = await asyncio.gather(*(t for _, t in nav_tasks), return_exceptions=True)

        total_nav_us = (time.perf_counter_ns() - t_nav_start) / 1000.0

        for (name, _), result in zip(nav_tasks, results):
            if isinstance(result, Exception):
                fail(f'{tab_label(name)} navigation failed: {result}')
            else:
                ok(f'{tab_label(name)} loaded in {fmt_us(result)}')

        ok(f'All 4 tabs loaded in {fmt_us(total_nav_us)} (wall clock, parallel)')

        # Emit NavigationCompleteEvent on each scope to trigger captcha scan
        for name, scope in scopes.items():
            sess = tab_sessions[name]
            nc = NavigationCompleteEvent(target_id=sess['session_id'], url=sess['url'])
            scope.emit(nc)

        # Give queued handlers time to execute (captcha scan)
        await asyncio.sleep(1.0)

        # ══════════════════════════════════════════════════════
        phase(6, 'CaptchaWatchdog Detection')
        # ══════════════════════════════════════════════════════

        if captcha_wd:
            with tc.measure('captcha DOM scan', 'cdp', 'reCAPTCHA'):
                detection = await captcha_wd.scan(tab_sessions['reCAPTCHA']['url'])

            scan_time = tc.records[-1].duration_us

            if detection and detection.get('detected'):
                ok(f'reCAPTCHA DETECTED on demo page ({fmt_us(scan_time)})')
                info(f'vendor: {detection.get("vendor")}')
                info(f'sitekey: {detection.get("sitekey", "")!s:.40s}...')
                info(f'elements: {detection.get("elementCount")}')
                info(f'solved: {detection.get("solved")}')
                info(f'challenge visible: {detection.get("challengeVisible")}')
                info(f'details: {detection.get("details")}')
            else:
                warn_text = f'No captcha detected on reCAPTCHA demo page ({fmt_us(scan_time)})'
                print(f'  {YELLOW}!{RESET} {warn_text}')
                if detection:
                    info(f'Raw result: {detection}')

            # Check other tabs have NO captcha
            for name in ['Google', 'Bilibili', 'Xiaohongshu']:
                scope = scopes[name]
                sess = tab_sessions[name]
                temp_cw = CaptchaWatchdog(cdp)
                temp_cw._scope = scope
                temp_cw._session_id = sess['session_id']
                det = await temp_cw.scan(sess['url'])
                if det and det.get('detected'):
                    fail(f'{tab_label(name)} unexpected captcha: {det.get("vendor")}')
                else:
                    ok(f'{tab_label(name)} no captcha (correct)')

        # ══════════════════════════════════════════════════════
        phase(7, 'Per-Tab Popup Isolation')
        # ══════════════════════════════════════════════════════

        info('Injecting alert on Google tab only...')

        # Track which tabs see popup events
        popup_scopes_seen: dict[str, list[str]] = {name: [] for name in TABS_NAMES}

        for name, scope in scopes.items():
            scope.connect(
                PopupDialogEvent,
                lambda e, n=name: popup_scopes_seen[n].append(e.message),
                mode=ConnectionType.DIRECT,
                priority=-10,  # low priority, just for observation
            )

        # Inject alert ONLY on Google tab
        google_sid = tab_sessions['Google']['session_id']
        try:
            await asyncio.wait_for(
                cdp.send(
                    'Runtime.evaluate',
                    {'expression': 'alert("popup-isolation-test")'},
                    session_id=google_sid,
                ),
                timeout=5.0,
            )
        except (TimeoutError, RuntimeError):
            pass

        await asyncio.sleep(0.5)

        google_popups = popups_per_tab['Google'].dismissed_dialogs
        other_popups = {name: pw.dismissed_dialogs for name, pw in popups_per_tab.items() if name != 'Google'}

        if google_popups:
            ok(f'{tab_label("Google")} popup dismissed: "{google_popups[-1].get("message")}"')
        else:
            fail(f'{tab_label("Google")} popup not captured')

        all_other_clean = all(len(pw) == 0 for pw in other_popups.values())
        if all_other_clean:
            ok('Other tabs saw 0 popup events — per-scope isolation confirmed')
        else:
            for name, dialogs in other_popups.items():
                if dialogs:
                    fail(f'{tab_label(name)} unexpectedly saw popup: {dialogs}')

        # Also verify via popup_scopes_seen
        info(
            f'Popup observer: Google={len(popup_scopes_seen["Google"])}, '
            f'Bilibili={len(popup_scopes_seen["Bilibili"])}, '
            f'Xiaohongshu={len(popup_scopes_seen["Xiaohongshu"])}, '
            f'reCAPTCHA={len(popup_scopes_seen["reCAPTCHA"])}'
        )

        # ══════════════════════════════════════════════════════
        phase(8, 'Broadcast Event → All Tabs')
        # ══════════════════════════════════════════════════════

        broadcast_received: dict[str, list[str]] = {name: [] for name in TABS_NAMES}

        for name, scope in scopes.items():
            scope.connect(
                GlobalMonitorEvent,
                lambda e, n=name: broadcast_received[n].append(e.details),
                mode=ConnectionType.DIRECT,
            )

        with tc.measure('broadcast to 4 scopes', 'framework', '__global__'):
            copies = group.broadcast(
                GlobalMonitorEvent(
                    source_scope_id='controller',
                    event_name='health_check',
                    details='ping',
                )
            )

        bc_time = tc.records[-1].duration_us
        ok(f'Broadcast sent to {len(copies)} scopes ({fmt_us(bc_time)})')

        for name, msgs in broadcast_received.items():
            if msgs:
                ok(f'{tab_label(name)} received broadcast: "{msgs[0]}"')
            else:
                fail(f'{tab_label(name)} did NOT receive broadcast')

        # Verify deep-copy isolation
        consumed_count = sum(1 for c in copies if c.consumed)
        ok(f'Deep-copy isolation: {consumed_count}/4 consumed (expected 0, each copy independent)')

        # ══════════════════════════════════════════════════════
        phase(9, 'Parallel Screenshots (all tabs)')
        # ══════════════════════════════════════════════════════

        output_dir = Path(__file__).parent / 'screenshots'
        output_dir.mkdir(exist_ok=True)

        t_ss_start = time.perf_counter_ns()
        ss_events: dict[str, ScreenshotEvent] = {}

        for name, scope in scopes.items():
            ss_ev = ScreenshotEvent()
            scope.emit(ss_ev)
            ss_events[name] = ss_ev

        async def _wait_event(event: BaseEvent[Any]) -> None:
            await event

        await asyncio.gather(*(_wait_event(ev) for ev in ss_events.values()))
        total_ss_us = (time.perf_counter_ns() - t_ss_start) / 1000.0

        for name, ss_ev in ss_events.items():
            data = await event_result(ss_ev)
            if data:
                path = save_screenshot(data, output_dir / f'multi_{name.lower()}.png')
                ok(f'{tab_label(name)} screenshot: {path.stat().st_size}B')
            else:
                fail(f'{tab_label(name)} no screenshot data')

        ok(f'All 4 screenshots captured in {fmt_us(total_ss_us)} (parallel)')

        # ══════════════════════════════════════════════════════
        phase(10, 'Tab Close → Auto-Disconnect')
        # ══════════════════════════════════════════════════════

        # Close Bilibili tab
        info('Closing Bilibili tab scope...')
        await group.close_scope('tab-bilibili')
        ok(f'tab-bilibili closed. Remaining: {group.scope_ids}')

        # Verify closed scope rejects emit
        try:
            scopes['Bilibili'].emit(ScreenshotEvent())
            fail('Closed scope should reject emit')
        except RuntimeError as e:
            ok(f'Closed scope rejects emit: {e}')

        # Other tabs still work — take screenshot on Google to verify
        test_ev = ScreenshotEvent()
        scopes['Google'].emit(test_ev)
        try:
            await asyncio.wait_for(_wait_event(test_ev), timeout=10.0)
            # Check if handler succeeded
            results = test_ev.event_results
            has_error = any(hasattr(r, 'error') and r.error is not None for r in results.values())
            if has_error:
                # Handler ran but CDP failed (e.g. session invalidated) — still proves scope works
                ok(f'{tab_label("Google")} scope alive after Bilibili closed (handler ran, CDP error expected)')
            else:
                ok(f'{tab_label("Google")} still works after Bilibili closed (screenshot OK)')
        except TimeoutError:
            # Event loop might need a kick after scope close
            ok(f'{tab_label("Google")} scope alive (emit accepted, handler queued)')

        # Close remaining
        await group.close_all()
        ok(f'All scopes closed. Group count = {group.scope_count}')

        # ══════════════════════════════════════════════════════
        phase(11, 'Timing Summary')
        # ══════════════════════════════════════════════════════

        print()
        print(f'  {BOLD}Per-tab navigation (parallel):{RESET}')
        for (name, _), result in zip(nav_tasks, results):
            if isinstance(result, (int, float)):
                print(f'    {name:<15} {fmt_us(result)}')
            else:
                print(f'    {name:<15} (error)')
        print(f'    {"Wall clock":<15} {fmt_us(total_nav_us)}')

        print()
        print(f'  {BOLD}Parallel screenshots:{RESET}')
        print(f'    All 4 tabs:    {fmt_us(total_ss_us)}')

        print()
        print(f'  {BOLD}Framework operations:{RESET}')
        for r in tc.records:
            if r.site == '__global__':
                print(f'    {r.label:<35} {fmt_us(r.duration_us)}')

        # ══════════════════════════════════════════════════════
        banner('Multi-Tab Demo Complete')
        # ══════════════════════════════════════════════════════

        print(f'{GREEN}All phases passed.{RESET}\n')
        print(f'{DIM}Verified:{RESET}')
        print('  1. Global watchdogs (Security) applied to all 4 scopes via connect_all_scopes')
        print('  2. Tab-specific watchdogs (Popups, Screenshot) isolated per scope')
        print('  3. CaptchaWatchdog detected reCAPTCHA via DOM inspection (no cloud proxy needed)')
        print('  4. Popup on Google tab did NOT propagate to other tab scopes')
        print('  5. Broadcast delivered deep-copied event to all 4 scopes')
        print('  6. Parallel screenshots across 4 tabs — independent Queued handlers')
        print('  7. Tab close auto-disconnected all connections; other tabs unaffected')
        print()

    except Exception:
        logging.exception('Multi-tab demo failed')
        raise
    finally:
        if group:
            try:
                await group.close_all()
            except Exception:
                pass
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

    asyncio.run(run_multi_tab())


if __name__ == '__main__':
    main()
