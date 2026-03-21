#!/usr/bin/env python3
"""Tier 1 optimization benchmark: real-browser multi-tab with full watchdog stack.

Launches Chrome via browser-level CDP (the same URL cloud providers give you),
opens multiple tabs to heavy real-world sites, attaches 5 watchdogs per tab,
and measures framework overhead vs I/O.

Usage:
    uv run python -m demo.bench_optimizations
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import orjson

from agent_cdp import ScopeGroup, event_result
from agent_cdp.events import BaseEvent
from agent_cdp.scope.event_loop import ScopeEventLoop

from ._output import BOLD, DIM, GREEN, RED, RESET, banner, fmt_us, info, ok, phase, warn
from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome
from .events import NavigateToUrlEvent, ScreenshotEvent
from .timing import TimingCollector
from .watchdogs import (
    CaptchaWatchdog,
    CrashWatchdog,
    PopupsWatchdog,
    ScreenshotWatchdog,
    SecurityWatchdog,
    save_screenshot,
)

SITES = [
    {'name': 'Xiaohongshu', 'url': 'https://www.xiaohongshu.com', 'domain': 'xiaohongshu.com'},
    {'name': 'Amazon', 'url': 'https://www.amazon.com', 'domain': 'amazon.com'},
    {'name': 'Bilibili', 'url': 'https://www.bilibili.com', 'domain': 'bilibili.com'},
    {'name': 'reCAPTCHA', 'url': 'https://www.google.com/recaptcha/api2/demo', 'domain': 'google.com'},
]


class StormUnhandled(BaseEvent[None]):
    __registry_key__ = 'bench_opt_storm.unhandled'


# ── Helpers ──


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _report_ab(
    label: str,
    a_times: list[float],
    b_times: list[float],
    a_label: str = 'before',
    b_label: str = 'after',
) -> None:
    a_avg, b_avg = _avg(a_times), _avg(b_times)
    speedup = a_avg / b_avg if b_avg > 0 else float('inf')
    color = GREEN if speedup > 1.05 else (RED if speedup < 0.95 else DIM)
    print(
        f'    {label:<45}  {a_label}={fmt_us(a_avg):>10}  {b_label}={fmt_us(b_avg):>10}  {color}{speedup:.2f}x{RESET}'
    )


# ══════════════════════════════════════════════════════════════════════
#  Benchmark 1: Multi-tab real navigation (5 watchdogs per tab)
# ══════════════════════════════════════════════════════════════════════


async def bench_multi_tab(cdp: CDPClient, tc: TimingCollector, output_dir: Path) -> dict[str, str]:
    """Open each site in its own tab with full watchdog stack.

    Returns {site_name: session_id} for subsequent benchmarks.
    """
    phase('', 'Multi-Tab Real Navigation (browser-level CDP, 5 watchdogs)')
    all_domains = [s['domain'] for s in SITES]
    group = ScopeGroup('bench-multitab')
    sessions: dict[str, str] = {}

    for site in SITES:
        name = site['name']
        url = site['url']

        print(f'\n  {BOLD}{name}{RESET} ({url})')
        t_site = time.perf_counter_ns()

        # Create tab via browser-level CDP
        target_id, session_id = await cdp.create_tab('about:blank')
        sessions[name] = session_id
        scope = await group.create_scope(f'tab-{name}')

        # Attach 5 watchdogs
        SecurityWatchdog(allowed_domains=all_domains).attach(scope)
        popups = PopupsWatchdog(cdp)
        popups.attach(scope, session_id)
        screenshot_wd = ScreenshotWatchdog(cdp)
        screenshot_wd.attach(scope, session_id)
        captcha_wd = CaptchaWatchdog(cdp)
        captcha_wd.attach(scope, session_id)
        CrashWatchdog(cdp).attach(scope)

        # 1. Security check (Direct, p=100) — framework overhead
        nav_event = NavigateToUrlEvent(url=url)
        with tc.measure('security check', 'direct', name):
            try:
                scope.emit(nav_event)
            except ValueError as e:
                warn(f'Blocked: {e}')
                continue
        await nav_event
        ok(f'Security check: {fmt_us(tc.records[-1].duration_us)}')

        # 2. CDP navigate — network I/O
        load_done = asyncio.Event()

        def on_load(_p: dict[str, Any], _s: str | None) -> None:
            load_done.set()

        cdp.on_event('Page.loadEventFired', on_load)
        with tc.measure('CDP navigate', 'cdp', name):
            await cdp.send('Page.navigate', {'url': url}, session_id=session_id)
        ok(f'CDP navigate: {fmt_us(tc.records[-1].duration_us)}')

        # 3. Page load
        t_load = time.perf_counter_ns()
        try:
            await asyncio.wait_for(load_done.wait(), timeout=30.0)
            load_us = (time.perf_counter_ns() - t_load) / 1000.0
            tc.add('page load', 'e2e', load_us, name)
            ok(f'Page load: {fmt_us(load_us)}')
        except TimeoutError:
            tc.add('page load (timeout)', 'e2e', 30_000_000.0, name)
            warn('Page load timed out')
        finally:
            cdp.off_event('Page.loadEventFired', on_load)

        await asyncio.sleep(1.5)  # settle for heavy pages

        # 4. Popup inject + auto-dismiss
        popups.dismissed_dialogs.clear()
        t_popup = time.perf_counter_ns()
        try:
            await asyncio.wait_for(
                cdp.send('Runtime.evaluate', {'expression': 'alert("bench")'}, session_id=session_id),
                timeout=5.0,
            )
        except (TimeoutError, RuntimeError):
            pass
        popup_us = (time.perf_counter_ns() - t_popup) / 1000.0
        tc.add('popup dismiss', 'e2e', popup_us, name)
        if popups.dismissed_dialogs:
            ok(f'Popup auto-dismissed: {fmt_us(popup_us)}')
        else:
            warn(f'Popup: {fmt_us(popup_us)} (no dialog captured)')

        # 5. Screenshot (Queued → CDP capture on heavy DOM)
        ss_event = ScreenshotEvent()
        with tc.measure('screenshot emit', 'framework', name):
            scope.emit(ss_event)
        emit_us = tc.records[-1].duration_us
        t_ss = time.perf_counter_ns()
        await ss_event
        handler_us = (time.perf_counter_ns() - t_ss) / 1000.0
        tc.add('screenshot handler', 'queued', handler_us, name)
        ss_data = await event_result(ss_event)
        if ss_data:
            fpath = save_screenshot(ss_data, output_dir / f'opt_{name.lower()}.png')
            ok(f'Screenshot: emit={fmt_us(emit_us)}, handler={fmt_us(handler_us)}, file={fpath.stat().st_size:,}B')
        else:
            warn('Screenshot: no data')

        # 6. Captcha scan (DOM detection)
        t_cap = time.perf_counter_ns()
        cap_result = await captcha_wd.scan(url)
        cap_us = (time.perf_counter_ns() - t_cap) / 1000.0
        tc.add('captcha scan', 'queued', cap_us, name)
        detected = cap_result.get('detected', False) if cap_result else False
        vendor = cap_result.get('vendor', 'none') if cap_result else 'none'
        ok(f'Captcha scan: {fmt_us(cap_us)} (detected={detected}, vendor={vendor})')

        # 7. Emit storm (100x, MRO cache hit test)
        emit_times: list[float] = []
        for _ in range(100):
            e = NavigateToUrlEvent(url=url)
            t0 = time.perf_counter_ns()
            try:
                scope.emit(e)
            except ValueError:
                pass
            emit_times.append((time.perf_counter_ns() - t0) / 1000.0)
        avg_emit = _avg(emit_times)
        tc.add('emit storm avg', 'framework', avg_emit, name)
        ok(f'Emit storm (100x NavEvent, cached): avg={fmt_us(avg_emit)}/event')

        # 8. Negative cache (100x unhandled events)
        neg_times: list[float] = []
        for _ in range(100):
            t0 = time.perf_counter_ns()
            scope.emit(StormUnhandled())
            neg_times.append((time.perf_counter_ns() - t0) / 1000.0)
        avg_neg = _avg(neg_times)
        tc.add('neg-cache avg', 'framework', avg_neg, name)
        ok(f'Unhandled (100x, neg-cache): avg={fmt_us(avg_neg)}/event')

        total_us = (time.perf_counter_ns() - t_site) / 1000.0
        tc.add('total', 'e2e', total_us, name)
        info(f'Total: {fmt_us(total_us)}')

    await group.close_all()
    return sessions


# ══════════════════════════════════════════════════════════════════════
#  Benchmark 1b: Parallel multi-tab (all tabs concurrently)
# ══════════════════════════════════════════════════════════════════════


async def _run_tab(
    cdp: CDPClient,
    site: dict[str, str],
    all_domains: list[str],
    group: ScopeGroup,
    tc: TimingCollector,
    output_dir: Path,
) -> tuple[str, str]:
    """Run a single tab's full lifecycle. Designed to run concurrently via gather()."""
    name = site['name']
    url = site['url']
    t_site = time.perf_counter_ns()

    # Create tab + attach watchdogs
    _target_id, session_id = await cdp.create_tab('about:blank')
    scope = await group.create_scope(f'par-{name}')

    SecurityWatchdog(allowed_domains=all_domains).attach(scope)
    popups = PopupsWatchdog(cdp)
    popups.attach(scope, session_id)
    screenshot_wd = ScreenshotWatchdog(cdp)
    screenshot_wd.attach(scope, session_id)
    captcha_wd = CaptchaWatchdog(cdp)
    captcha_wd.attach(scope, session_id)
    CrashWatchdog(cdp).attach(scope)

    # Security check
    nav_event = NavigateToUrlEvent(url=url)
    with tc.measure('security check', 'direct', f'P:{name}'):
        try:
            scope.emit(nav_event)
        except ValueError:
            return name, session_id
    await nav_event

    # Navigate + wait for load
    load_done = asyncio.Event()
    bound_sid = session_id  # capture for closure

    def on_load(_p: dict[str, Any], sid: str | None) -> None:
        if sid == bound_sid:
            load_done.set()

    cdp.on_event('Page.loadEventFired', on_load)
    with tc.measure('CDP navigate', 'cdp', f'P:{name}'):
        await cdp.send('Page.navigate', {'url': url}, session_id=session_id)

    t_load = time.perf_counter_ns()
    try:
        await asyncio.wait_for(load_done.wait(), timeout=30.0)
        tc.add('page load', 'e2e', (time.perf_counter_ns() - t_load) / 1000.0, f'P:{name}')
    except TimeoutError:
        tc.add('page load (timeout)', 'e2e', 30_000_000.0, f'P:{name}')
    finally:
        cdp.off_event('Page.loadEventFired', on_load)

    await asyncio.sleep(1.0)

    # Popup
    popups.dismissed_dialogs.clear()
    t_popup = time.perf_counter_ns()
    try:
        await asyncio.wait_for(
            cdp.send('Runtime.evaluate', {'expression': 'alert("bench")'}, session_id=session_id),
            timeout=5.0,
        )
    except (TimeoutError, RuntimeError):
        pass
    tc.add('popup dismiss', 'e2e', (time.perf_counter_ns() - t_popup) / 1000.0, f'P:{name}')

    # Screenshot
    ss_event = ScreenshotEvent()
    with tc.measure('screenshot emit', 'framework', f'P:{name}'):
        scope.emit(ss_event)
    t_ss = time.perf_counter_ns()
    await ss_event
    tc.add('screenshot handler', 'queued', (time.perf_counter_ns() - t_ss) / 1000.0, f'P:{name}')
    ss_data = await event_result(ss_event)
    if ss_data:
        save_screenshot(ss_data, output_dir / f'par_{name.lower()}.png')

    # Captcha scan
    t_cap = time.perf_counter_ns()
    await captcha_wd.scan(url)
    tc.add('captcha scan', 'queued', (time.perf_counter_ns() - t_cap) / 1000.0, f'P:{name}')

    # Total
    tc.add('total', 'e2e', (time.perf_counter_ns() - t_site) / 1000.0, f'P:{name}')

    return name, session_id


async def bench_multi_tab_parallel(cdp: CDPClient, tc: TimingCollector, output_dir: Path) -> None:
    """All tabs concurrently via asyncio.gather — measures parallelism benefit."""
    phase('', 'Parallel Multi-Tab (all 4 tabs concurrently, 5 watchdogs each)')
    all_domains = [s['domain'] for s in SITES]
    group = ScopeGroup('bench-parallel')

    t_total = time.perf_counter_ns()

    results = await asyncio.gather(
        *[_run_tab(cdp, site, all_domains, group, tc, output_dir) for site in SITES],
        return_exceptions=True,
    )

    wall_us = (time.perf_counter_ns() - t_total) / 1000.0

    succeeded = [r for r in results if isinstance(r, tuple)]
    failed = [r for r in results if isinstance(r, BaseException)]

    for name, _ in succeeded:
        per_tab = [r for r in tc.records if r.site == f'P:{name}' and r.label == 'total']
        tab_us = per_tab[-1].duration_us if per_tab else 0
        ok(f'{name}: {fmt_us(tab_us)}')

    for exc in failed:
        warn(f'Tab failed: {exc}')

    # Compare wall-clock with sum of individual tab times
    tab_totals = [r.duration_us for r in tc.records if r.site.startswith('P:') and r.label == 'total']
    sum_sequential = sum(tab_totals)

    print()
    ok(f'Wall-clock (parallel): {fmt_us(wall_us)}')
    ok(f'Sum of tab times (if sequential): {fmt_us(sum_sequential)}')
    speedup = sum_sequential / wall_us if wall_us > 0 else 0
    color = GREEN if speedup > 1.2 else DIM
    ok(f'Parallelism speedup: {color}{speedup:.2f}x{RESET}')

    await group.close_all()


# ══════════════════════════════════════════════════════════════════════
#  Benchmark 2: Deadlock monitor overhead
# ══════════════════════════════════════════════════════════════════════


async def bench_deadlock(n: int = 500) -> None:
    phase('', 'Deadlock Monitor: create_task vs dict_ops')

    async def _stub(_: str) -> None:
        await asyncio.sleep(15.0)

    old_times: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter_ns()
        task = asyncio.create_task(_stub('handler'))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        old_times.append((time.perf_counter_ns() - t0) / 1000.0)

    loop = ScopeEventLoop()
    new_times: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter_ns()
        hid = loop._register_handler('handler')
        loop._unregister_handler(hid)
        new_times.append((time.perf_counter_ns() - t0) / 1000.0)

    _report_ab('per-handler overhead', old_times, new_times, a_label='create_task', b_label='dict_ops')


# ══════════════════════════════════════════════════════════════════════
#  Benchmark 3: orjson on real CDP payloads from loaded pages
# ══════════════════════════════════════════════════════════════════════


async def bench_orjson(cdp: CDPClient, session_id: str) -> None:
    phase('', 'orjson vs json (real CDP payloads from loaded page)')

    payloads: dict[str, str] = {}

    try:
        r = await cdp.send('Page.getLayoutMetrics', session_id=session_id)
        payloads['getLayoutMetrics'] = json.dumps(r)
    except Exception:
        pass

    try:
        r = await cdp.send('DOM.getDocument', {'depth': 3}, session_id=session_id)
        payloads['DOM.getDocument(d=3)'] = json.dumps(r)
    except Exception:
        pass

    try:
        r = await cdp.send('Page.captureScreenshot', {'format': 'png'}, session_id=session_id)
        payloads['captureScreenshot'] = json.dumps(r)
    except Exception:
        pass

    n = 500
    for label, raw_json in payloads.items():
        size_kb = len(raw_json) / 1024

        json_loads: list[float] = []
        json_dumps: list[float] = []
        for _ in range(n):
            t0 = time.perf_counter_ns()
            obj = json.loads(raw_json)
            json_loads.append((time.perf_counter_ns() - t0) / 1000.0)
            t0 = time.perf_counter_ns()
            json.dumps(obj)
            json_dumps.append((time.perf_counter_ns() - t0) / 1000.0)

        raw_bytes = raw_json.encode()
        orjson_loads: list[float] = []
        orjson_dumps: list[float] = []
        for _ in range(n):
            t0 = time.perf_counter_ns()
            obj = orjson.loads(raw_bytes)
            orjson_loads.append((time.perf_counter_ns() - t0) / 1000.0)
            t0 = time.perf_counter_ns()
            orjson.dumps(obj)
            orjson_dumps.append((time.perf_counter_ns() - t0) / 1000.0)

        _report_ab(f'loads {label} ({size_kb:.0f}KB)', json_loads, orjson_loads, a_label='json', b_label='orjson')
        _report_ab(f'dumps {label} ({size_kb:.0f}KB)', json_dumps, orjson_dumps, a_label='json', b_label='orjson')


# ══════════════════════════════════════════════════════════════════════
#  Report
# ══════════════════════════════════════════════════════════════════════


def print_report(tc: TimingCollector) -> None:
    banner('Timing Report')
    for site in SITES:
        name = site['name']
        print(f'  {BOLD}{name}{RESET}')
        print(tc.site_report(name))
        print()

    banner('Summary')
    print(tc.summary_report())

    fw = [r for r in tc.records if r.category == 'framework' and r.site != '__framework__']
    e2e = [r for r in tc.records if r.category == 'e2e' and r.label == 'total']
    if fw and e2e:
        avg_fw = sum(r.duration_us for r in fw) / len(fw)
        avg_e2e = sum(r.duration_us for r in e2e) / len(e2e)
        pct = (avg_fw / avg_e2e) * 100 if avg_e2e > 0 else 0
        print(f'\n  {BOLD}Key Insight:{RESET}')
        print(f'    Framework overhead avg: {fmt_us(avg_fw)}')
        print(f'    End-to-end total avg:   {fmt_us(avg_e2e)}')
        print(f'    → Framework = {BOLD}{GREEN}{pct:.4f}%{RESET} of total time\n')


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════


async def run_bench() -> None:
    logging.basicConfig(level=logging.WARNING, format='%(levelname).1s %(name)s: %(message)s')
    logging.getLogger('agent_cdp.scope.event_loop').setLevel(logging.ERROR)

    banner('Tier 1 Optimization Benchmark (Real Browser, Multi-Tab)')
    print(f'{DIM}Browser-level CDP → {len(SITES)} tabs × 5 watchdogs{RESET}')
    print(f'{DIM}Sites: {", ".join(s["name"] for s in SITES)}{RESET}')

    tc = TimingCollector()
    chrome_proc = None
    cdp: CDPClient | None = None

    try:
        phase('', 'Chrome Setup (browser-level CDP)')
        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome PID={chrome_proc.pid}')
        ok(f'Browser WS: {ws_url[:60]}...')

        cdp = CDPClient(ws_url)
        await cdp.connect()
        _default_tid, default_sid = await cdp.init_browser_session()
        ok('Browser session initialized (setDiscoverTargets + attach)')

        output_dir = Path(__file__).parent / 'screenshots'
        output_dir.mkdir(exist_ok=True)

        # Benchmark 1a: Sequential multi-tab
        sessions = await bench_multi_tab(cdp, tc, output_dir)

        # Benchmark 1b: Parallel multi-tab
        await bench_multi_tab_parallel(cdp, tc, output_dir)

        # Benchmark 2: Deadlock monitor
        await bench_deadlock()

        # Benchmark 3: orjson on real CDP payloads
        # Use first available session (should be on a loaded page)
        real_sid = next(iter(sessions.values())) if sessions else default_sid
        await bench_orjson(cdp, real_sid)

        # Report
        print_report(tc)

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
    asyncio.run(run_bench())


if __name__ == '__main__':
    main()
