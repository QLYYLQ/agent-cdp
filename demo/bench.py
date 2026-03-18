#!/usr/bin/env python3
"""Performance benchmark: agent-cdp watchdogs on real websites.

Measures framework overhead, Direct/Queued handler latency, CDP round-trips,
and end-to-end operations across real-world sites.

Usage:
    uv run python -m demo.bench
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.aggregation import event_result
from agent_cdp.scope.group import ScopeGroup
from agent_cdp.scope.scope import EventScope

from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome
from .events import (
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    ScreenshotEvent,
)
from .timing import TimingCollector
from .watchdogs import (
    PopupsWatchdog,
    ScreenshotWatchdog,
    SecurityWatchdog,
    save_screenshot,
)

# ── Test sites ──

SITES = [
    {
        'name': 'Google',
        'url': 'https://www.google.com',
        'domain': 'google.com',
    },
    {
        'name': 'Xiaohongshu',
        'url': 'https://www.xiaohongshu.com',
        'domain': 'xiaohongshu.com',
    },
    {
        'name': 'Bilibili',
        'url': 'https://www.bilibili.com',
        'domain': 'bilibili.com',
    },
    {
        'name': 'reCAPTCHA Demo',
        'url': 'https://www.google.com/recaptcha/api2/demo',
        'domain': 'google.com',
    },
]

# ── Output helpers ──

BOLD = '\033[1m'
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
DIM = '\033[2m'
RESET = '\033[0m'


def banner(text: str) -> None:
    print(f'\n{BOLD}{CYAN}{"═" * 65}')
    print(f'  {text}')
    print(f'{"═" * 65}{RESET}\n')


def phase(text: str) -> None:
    print(f'\n{BOLD}{YELLOW}── {text} {"─" * max(1, 55 - len(text))}{RESET}')


def ok(text: str) -> None:
    print(f'  {GREEN}✓{RESET} {text}')


def info(text: str) -> None:
    print(f'  {DIM}→ {text}{RESET}')


def warn(text: str) -> None:
    print(f'  {YELLOW}!{RESET} {text}')


def fmt_us(us: float) -> str:
    """Format microseconds to human-readable."""
    if us < 1000:
        return f'{us:.1f}us'
    elif us < 1_000_000:
        return f'{us / 1000:.2f}ms'
    else:
        return f'{us / 1_000_000:.2f}s'


# ── Framework overhead measurements ──


async def measure_framework_overhead(tc: TimingCollector) -> None:
    """Measure pure agent-cdp framework overhead without any real I/O."""
    phase('Framework Overhead (no I/O, pure event system)')

    group = ScopeGroup('bench-framework')
    scope = await group.create_scope('overhead-test')

    # 1. BaseEvent construction
    for _ in range(100):
        with tc.measure('BaseEvent construction', 'framework', '__framework__'):
            _ = NavigateToUrlEvent(url='https://test.com')
    # keep only last 100
    construction_records = [r for r in tc.records if r.label == 'BaseEvent construction']
    avg_construct = sum(r.duration_us for r in construction_records) / len(construction_records)
    ok(f'BaseEvent construction: avg {fmt_us(avg_construct)} (100 iterations)')

    # 2. emit() with zero handlers
    evt = NavigateToUrlEvent(url='https://test.com')
    # need a handler connected for event type validation... actually emit doesn't validate event type.
    # But there are no connections so it just loops over empty list.
    for _ in range(100):
        e = NavigateToUrlEvent(url='https://test.com')
        with tc.measure('emit() zero handlers', 'framework', '__framework__'):
            scope.emit(e)
    zero_records = [r for r in tc.records if r.label == 'emit() zero handlers']
    avg_zero = sum(r.duration_us for r in zero_records) / len(zero_records)
    ok(f'emit() zero handlers: avg {fmt_us(avg_zero)} (100 iterations)')

    # 3. emit() with 1 Direct no-op handler
    def noop(event: NavigateToUrlEvent) -> None:
        pass

    scope.connect(NavigateToUrlEvent, noop, mode=ConnectionType.DIRECT)
    for _ in range(100):
        e = NavigateToUrlEvent(url='https://test.com')
        with tc.measure('emit() 1 Direct noop', 'framework', '__framework__'):
            scope.emit(e)
    one_records = [r for r in tc.records if r.label == 'emit() 1 Direct noop']
    avg_one = sum(r.duration_us for r in one_records) / len(one_records)
    ok(f'emit() 1 Direct no-op: avg {fmt_us(avg_one)} (100 iterations)')

    # 4. emit() with 5 Direct handlers (priority ordering)
    await scope.close()
    scope = await group.create_scope('overhead-test-5')
    for i in range(5):
        scope.connect(NavigateToUrlEvent, noop, mode=ConnectionType.DIRECT, priority=i * 10)
    for _ in range(100):
        e = NavigateToUrlEvent(url='https://test.com')
        with tc.measure('emit() 5 Direct noops', 'framework', '__framework__'):
            scope.emit(e)
    five_records = [r for r in tc.records if r.label == 'emit() 5 Direct noops']
    avg_five = sum(r.duration_us for r in five_records) / len(five_records)
    ok(f'emit() 5 Direct no-ops: avg {fmt_us(avg_five)} (100 iterations)')

    # 5. emit() with Direct handler that does consume()
    await scope.close()
    scope = await group.create_scope('overhead-test-consume')

    def consume_handler(event: NavigateToUrlEvent) -> None:
        event.consume()

    scope.connect(NavigateToUrlEvent, consume_handler, mode=ConnectionType.DIRECT, priority=100)
    scope.connect(NavigateToUrlEvent, noop, mode=ConnectionType.DIRECT, priority=0)
    for _ in range(100):
        e = NavigateToUrlEvent(url='https://test.com')
        with tc.measure('emit() Direct+consume', 'framework', '__framework__'):
            scope.emit(e)
    consume_records = [r for r in tc.records if r.label == 'emit() Direct+consume']
    avg_consume = sum(r.duration_us for r in consume_records) / len(consume_records)
    ok(f'emit() Direct+consume (skip 2nd handler): avg {fmt_us(avg_consume)} (100 iterations)')

    # 6. emit() with Queued handler (enqueue only, not execution)
    await scope.close()
    scope = await group.create_scope('overhead-test-queued')

    async def async_noop(event: NavigateToUrlEvent) -> None:
        pass

    scope.connect(NavigateToUrlEvent, async_noop, mode=ConnectionType.QUEUED, target_scope=scope)
    for _ in range(100):
        e = NavigateToUrlEvent(url='https://test.com')
        with tc.measure('emit() 1 Queued enqueue', 'framework', '__framework__'):
            scope.emit(e)
        await e  # drain
    queued_records = [r for r in tc.records if r.label == 'emit() 1 Queued enqueue']
    avg_queued = sum(r.duration_us for r in queued_records) / len(queued_records)
    ok(f'emit() 1 Queued (enqueue only): avg {fmt_us(avg_queued)} (100 iterations)')

    # 7. SecurityWatchdog.check_navigation (real handler, not no-op)
    await scope.close()
    scope = await group.create_scope('overhead-test-security')
    security = SecurityWatchdog(allowed_domains=['example.com', 'google.com', 'bilibili.com'])
    security.attach(scope)

    # Allowed URL
    for _ in range(100):
        e = NavigateToUrlEvent(url='https://www.google.com/search?q=test')
        with tc.measure('SecurityWatchdog (allowed)', 'direct', '__framework__'):
            scope.emit(e)
    sec_allowed = [r for r in tc.records if r.label == 'SecurityWatchdog (allowed)']
    avg_sec_allowed = sum(r.duration_us for r in sec_allowed) / len(sec_allowed)
    ok(f'SecurityWatchdog check (allowed URL): avg {fmt_us(avg_sec_allowed)} (100 iterations)')

    # Blocked URL
    for _ in range(100):
        e = NavigateToUrlEvent(url='https://evil.example.org/malware')
        with tc.measure('SecurityWatchdog (blocked)', 'direct', '__framework__'):
            try:
                scope.emit(e)
            except ValueError:
                pass
    sec_blocked = [r for r in tc.records if r.label == 'SecurityWatchdog (blocked)']
    avg_sec_blocked = sum(r.duration_us for r in sec_blocked) / len(sec_blocked)
    ok(f'SecurityWatchdog check (blocked+consume+raise): avg {fmt_us(avg_sec_blocked)} (100 iterations)')

    await group.close_all()

    # Print framework report
    print()
    info('Framework overhead summary:')
    print(tc.framework_report())


# ── Per-site navigation + watchdog benchmark ──


async def wait_for_load(cdp: CDPClient, session_id: str, timeout: float = 30.0) -> float:
    """Wait for Page.loadEventFired and return load time in microseconds."""
    load_event = asyncio.Event()
    t_start = time.perf_counter_ns()

    def on_load(params: dict, sid: str | None) -> None:
        load_event.set()

    cdp.on_event('Page.loadEventFired', on_load)
    try:
        await asyncio.wait_for(load_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        # Remove our handler
        handlers = cdp._event_handlers.get('Page.loadEventFired', [])
        if on_load in handlers:
            handlers.remove(on_load)

    return (time.perf_counter_ns() - t_start) / 1000.0


async def benchmark_site(
    site: dict,
    cdp: CDPClient,
    session_id: str,
    scope: EventScope,
    security: SecurityWatchdog,
    popups: PopupsWatchdog,
    screenshot_wd: ScreenshotWatchdog,
    tc: TimingCollector,
    output_dir: Path,
) -> None:
    """Run full benchmark for a single site."""
    name = site['name']
    url = site['url']

    phase(f'{name} ({url})')

    t_total_start = time.perf_counter_ns()

    # 1. Security check (Direct handler in emit)
    nav_event = NavigateToUrlEvent(url=url)
    with tc.measure('security check (Direct)', 'direct', name):
        try:
            scope.emit(nav_event)
        except ValueError as e:
            warn(f'Blocked by security: {e}')
            return
    sec_time = tc.records[-1].duration_us
    ok(f'Security check passed: {fmt_us(sec_time)}')

    # We emitted the event above which also enqueued the Queued navigation handler.
    # But we want to measure CDP navigate separately. Let's not use the queued handler
    # for navigation — instead drive CDP directly with fine-grained timing.
    # The emit above was to measure security check. Discard it and navigate manually.
    await nav_event  # drain any queued handlers from the security-check emit

    # 2. CDP Page.navigate command round-trip
    # Set up load waiter BEFORE navigate
    load_future = asyncio.Event()

    def on_load(params: dict, sid: str | None) -> None:
        load_future.set()

    cdp.on_event('Page.loadEventFired', on_load)

    with tc.measure('CDP Page.navigate', 'cdp', name):
        await cdp.send('Page.navigate', {'url': url}, session_id=session_id)
    cdp_nav_time = tc.records[-1].duration_us
    ok(f'CDP Page.navigate: {fmt_us(cdp_nav_time)}')

    # 3. Wait for page load
    t_load_start = time.perf_counter_ns()
    try:
        await asyncio.wait_for(load_future.wait(), timeout=30.0)
        load_us = (time.perf_counter_ns() - t_load_start) / 1000.0
        tc.add('page load wait', 'e2e', load_us, name)
        ok(f'Page load: {fmt_us(load_us)}')
    except asyncio.TimeoutError:
        load_us = 30_000_000.0
        tc.add('page load wait (timeout)', 'e2e', load_us, name)
        warn(f'Page load timed out (30s)')
    finally:
        handlers = cdp._event_handlers.get('Page.loadEventFired', [])
        if on_load in handlers:
            handlers.remove(on_load)

    # 4. Small settle delay for rendering
    await asyncio.sleep(0.5)

    # 5. Popup check: inject alert and measure handling
    popups.dismissed_dialogs.clear()

    popup_load = asyncio.Event()

    def on_dialog(params: dict, sid: str | None) -> None:
        popup_load.set()

    cdp.on_event('Page.javascriptDialogOpening', on_dialog)

    t_popup_start = time.perf_counter_ns()
    try:
        # Inject alert — this blocks until dialog is dismissed
        await asyncio.wait_for(
            cdp.send(
                'Runtime.evaluate',
                {'expression': 'alert("bench-popup-test")'},
                session_id=session_id,
            ),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, RuntimeError):
        pass

    popup_us = (time.perf_counter_ns() - t_popup_start) / 1000.0
    tc.add('popup inject+dismiss', 'e2e', popup_us, name)

    handlers = cdp._event_handlers.get('Page.javascriptDialogOpening', [])
    if on_dialog in handlers:
        handlers.remove(on_dialog)

    if popups.dismissed_dialogs:
        ok(f'Popup auto-dismissed: {fmt_us(popup_us)}')
    else:
        warn(f'Popup handling: {fmt_us(popup_us)} (no dialog captured)')

    # 6. Screenshot via agent-cdp (Queued handler)
    ss_event = ScreenshotEvent()
    with tc.measure('screenshot emit+enqueue', 'framework', name):
        scope.emit(ss_event)
    enqueue_time = tc.records[-1].duration_us

    t_ss_start = time.perf_counter_ns()
    await ss_event
    ss_total_us = (time.perf_counter_ns() - t_ss_start) / 1000.0
    tc.add('screenshot queued handler', 'queued', ss_total_us, name)

    ss_data = await event_result(ss_event)
    if ss_data:
        out_path = save_screenshot(ss_data, output_dir / f'bench_{name.lower().replace(" ", "_")}.png')
        file_size = out_path.stat().st_size
        ok(f'Screenshot: enqueue={fmt_us(enqueue_time)}, handler={fmt_us(ss_total_us)}, file={file_size}B')
    else:
        warn('Screenshot: no data returned')

    # 7. NavigationComplete emit overhead (Direct, no I/O)
    nc_event = NavigationCompleteEvent(target_id=session_id, url=url)
    with tc.measure('NavigationComplete emit', 'framework', name):
        scope.emit(nc_event)
    nc_time = tc.records[-1].duration_us
    ok(f'NavigationComplete emit (0 handlers): {fmt_us(nc_time)}')

    # Total time for this site
    total_us = (time.perf_counter_ns() - t_total_start) / 1000.0
    tc.add('total (navigate+screenshot)', 'e2e', total_us, name)
    ok(f'Total: {fmt_us(total_us)}')


# ── Main ──


async def run_bench() -> None:
    banner('agent-cdp Performance Benchmark')
    print(f'{DIM}Measuring framework overhead + real-world watchdog latency{RESET}')
    print(f'{DIM}Sites: {", ".join(s["name"] for s in SITES)}{RESET}')

    tc = TimingCollector()
    chrome_proc = None
    cdp: CDPClient | None = None

    try:
        # ── Phase 0: Pure framework overhead ──
        await measure_framework_overhead(tc)

        # ── Setup: Launch Chrome ──
        phase('Chrome Setup')
        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome started (PID {chrome_proc.pid})')

        cdp = CDPClient(ws_url)
        await cdp.connect()
        ok('CDP connected')

        # Get initial target
        targets = await cdp.send('Target.getTargets')
        pages = [t for t in targets.get('targetInfos', []) if t.get('type') == 'page']
        target_id = pages[0]['targetId']

        attach = await cdp.send('Target.attachToTarget', {
            'targetId': target_id,
            'flatten': True,
        })
        session_id = attach['sessionId']

        await cdp.send('Page.enable', session_id=session_id)
        await cdp.send('Runtime.enable', session_id=session_id)
        await cdp.send('Network.enable', session_id=session_id)
        ok(f'Attached to target, CDP domains enabled')

        # Create scope and attach watchdogs
        group = ScopeGroup('bench')
        scope = await group.create_scope('bench-tab')

        all_domains = [s['domain'] for s in SITES]
        security = SecurityWatchdog(allowed_domains=all_domains)
        security.attach(scope)

        popups = PopupsWatchdog(cdp)
        popups.attach(scope, session_id)

        screenshot_wd = ScreenshotWatchdog(cdp)
        screenshot_wd.attach(scope, session_id)

        ok('Watchdogs attached: Security(Direct,p=100), Popups(Direct,p=50), Screenshot(Queued)')

        output_dir = Path(__file__).parent / 'screenshots'
        output_dir.mkdir(exist_ok=True)

        # ── Per-site benchmarks ──
        for site in SITES:
            await benchmark_site(
                site, cdp, session_id, scope,
                security, popups, screenshot_wd, tc, output_dir,
            )

        # ── Cleanup ──
        await group.close_all()

        # ── Final report ──
        banner('Timing Report')

        for site in SITES:
            print(f'{BOLD}  {site["name"]}{RESET}')
            print(tc.site_report(site['name']))
            print()

        banner('Summary')
        print(tc.summary_report())

        # ── Key insight ──
        framework_records = [r for r in tc.records if r.category == 'framework' and r.site != '__framework__']
        cdp_records = [r for r in tc.records if r.category == 'cdp']
        e2e_records = [r for r in tc.records if r.category == 'e2e' and 'total' in r.label]

        if framework_records and e2e_records:
            avg_fw = sum(r.duration_us for r in framework_records) / len(framework_records)
            avg_e2e = sum(r.duration_us for r in e2e_records) / len(e2e_records)
            pct = (avg_fw / avg_e2e) * 100 if avg_e2e > 0 else 0

            print()
            print(f'{BOLD}  Key Insight:{RESET}')
            print(f'  Framework overhead (emit+dispatch) averages {fmt_us(avg_fw)}')
            print(f'  vs end-to-end total averaging {fmt_us(avg_e2e)}')
            print(f'  → agent-cdp overhead = {BOLD}{GREEN}{pct:.4f}%{RESET} of total time')

            if cdp_records:
                avg_cdp = sum(r.duration_us for r in cdp_records) / len(cdp_records)
                print(f'  → CDP I/O averages {fmt_us(avg_cdp)} ({(avg_cdp / avg_e2e * 100):.2f}% of total)')
            print()

    except Exception:
        logging.exception('Benchmark failed')
        raise
    finally:
        if cdp:
            await cdp.close()
        if chrome_proc:
            kill_chrome(chrome_proc)
            info('Chrome terminated')


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format='%(levelname).1s %(name)s: %(message)s',
        stream=sys.stderr,
    )

    asyncio.run(run_bench())


if __name__ == '__main__':
    main()
