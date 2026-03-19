#!/usr/bin/env python3
"""Advanced demo: Multi-scope agent with real Chrome + global monitoring.

Demonstrates 8 architectural advantages of agent-cdp over bubus,
using real Chrome tabs and CDP commands:

  Phase 1 — Setup: Launch Chrome, create 5 real tabs + 1 monitor scope
  Phase 2 — Global Watchdog: connect_all_scopes fan-in (1 call → 5 connections)
  Phase 3 — Fan-out + Cross-scope: 1 event → 4 handlers across 2 scopes
  Phase 4 — Connection Filters: handler only runs for matching events
  Phase 5 — expect(): declarative future event waiting
  Phase 6 — Concurrent Dispatch: 5 scopes process events in parallel
  Phase 7 — EventLog: JSONL persistence + replay
  Phase 8 — Auto-disconnect: scope.close() severs cross-scope connections

Usage:
    uv run python -m demo.advanced
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agent_cdp import (
    BaseEvent,
    ConnectionType,
    EventLogWriter,
    EventScope,
    ScopeGroup,
    expect,
)

from ._output import BOLD, DIM, RESET, banner, fail, info, ok, phase, pr, trace
from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome
from .events import (
    BrowserErrorEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    ScreenshotEvent,
)

# ── Real CDP handler factories ───────────────────────────────

ALLOWED_DOMAINS = [
    'xiaohongshu.com',
    'bilibili.com',
    'google.com',
    'example.com',
]


def make_security_check(log: list[str]) -> Any:
    """Direct handler (p=100): blocks disallowed URLs."""

    def handler(event: NavigateToUrlEvent) -> None:
        if not any(d in event.url for d in ALLOWED_DOMAINS):
            log.append(f'[security] BLOCKED {event.url}')
            event.consume()
            raise ValueError(f'Blocked: {event.url}')
        log.append(f'[security] allowed {event.url}')

    handler.__name__ = 'security_check'
    return handler


def make_cdp_nav_handler(cdp: CDPClient, session_id: str, scope: EventScope, log: list[str]) -> Any:
    """Queued handler (p=50): real CDP Page.navigate + emit NavigationCompleteEvent."""

    async def handler(event: NavigateToUrlEvent) -> str:
        log.append(f'[navigate] {event.url} on {scope.scope_id}')
        # Register load listener BEFORE navigating
        load_done = asyncio.Event()

        def _on_load(params: dict[str, Any], sid: str | None) -> None:
            if sid == session_id:
                load_done.set()

        cdp.on_event('Page.loadEventFired', _on_load)
        try:
            await cdp.send('Page.navigate', {'url': event.url}, session_id=session_id)
            await asyncio.wait_for(load_done.wait(), timeout=10.0)
        except TimeoutError:
            pass
        finally:
            cdp.off_event('Page.loadEventFired', _on_load)
        scope.emit(NavigationCompleteEvent(target_id=scope.scope_id, url=event.url))
        return event.url

    handler.__name__ = f'cdp_navigate_{scope.scope_id}'
    return handler


def make_cdp_screenshot_handler(cdp: CDPClient, session_id: str, log: list[str]) -> Any:
    """Queued handler (p=0): real CDP screenshot capture."""

    async def handler(event: ScreenshotEvent) -> str:
        log.append('[screenshot] capturing via CDP')
        result = await cdp.send('Page.captureScreenshot', {'format': 'png'}, session_id=session_id)
        data = result.get('data', '')
        log.append(f'[screenshot] captured {len(data)} bytes base64')
        return data

    handler.__name__ = 'cdp_screenshot'
    return handler


def make_cross_scope_monitor(received: list[str], log: list[str]) -> Any:
    """Queued handler (p=-10, cross-scope → monitor): records events from any tab."""

    async def handler(event: NavigateToUrlEvent) -> None:
        received.append(event.url)
        log.append(f'[monitor] received {event.url}')

    handler.__name__ = 'cross_scope_monitor'
    return handler


def make_crash_handler(log: list[str]) -> Any:
    """Direct handler (p=100): global crash response."""

    def handler(event: BrowserErrorEvent) -> None:
        log.append(f'[crash] {event.error_type}: {event.message}')

    handler.__name__ = 'crash_handler'
    return handler


def make_filtered_security(log: list[str]) -> Any:
    """Direct handler with connection filter — only runs for dangerous URLs."""

    def handler(event: NavigateToUrlEvent) -> None:
        log.append(f'[strict_filter] intercepted {event.url}')
        event.consume()
        raise ValueError(f'Filtered and blocked: {event.url}')

    handler.__name__ = 'filtered_security'
    return handler


# ── Tab setup helper ─────────────────────────────────────────


async def create_real_tab(cdp: CDPClient, group: ScopeGroup, tab_name: str) -> tuple[EventScope, str, str]:
    """Create a real Chrome tab and a matching EventScope.

    Returns (scope, target_id, session_id).
    """
    target = await cdp.send('Target.createTarget', {'url': 'about:blank'})
    target_id = target['targetId']
    attach = await cdp.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
    session_id = attach['sessionId']
    await cdp.send('Page.enable', session_id=session_id)
    await cdp.send('Runtime.enable', session_id=session_id)

    scope = await group.create_scope(tab_name, target_id=target_id, session_id=session_id)
    return scope, target_id, session_id


# ── Main demo ────────────────────────────────────────────────


async def run_demo() -> None:
    banner('agent-cdp Advanced Architecture Demo')
    pr(f'{DIM}Real Chrome + CDP — 5 tabs, global monitoring, cross-scope routing.{RESET}')

    chrome_proc = None
    cdp: CDPClient | None = None
    group: ScopeGroup | None = None

    try:
        # ── Phase 1: Launch Chrome + Create Scopes ──

        phase(1, 'Launch Chrome — 5 real tabs + 1 monitor scope')

        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome started (PID {chrome_proc.pid})')

        cdp = CDPClient(ws_url)
        await cdp.connect()
        ok('CDP WebSocket connected')

        group = ScopeGroup('browser-agent')

        # Create 5 real Chrome tabs → 5 EventScopes
        tabs: list[EventScope] = []
        sessions: dict[str, str] = {}  # scope_id → session_id

        for i in range(1, 6):
            scope, _tid, sid = await create_real_tab(cdp, group, f'tab-{i}')
            tabs.append(scope)
            sessions[scope.scope_id] = sid

        # Create monitor scope (no Chrome tab — pure event routing)
        monitor = await group.create_scope('monitor')

        ok(f'ScopeGroup "{group.group_id}" — {group.scope_count} scopes: {group.scope_ids}')
        info('5 real Chrome tabs + 1 monitor scope (event-only)')

        # ── Phase 2: Global Watchdog — connect_all_scopes (Fan-in) ──

        phase(2, 'Global Watchdog — connect_all_scopes (Fan-in: N→1)')
        info('1 call → security handler connected to ALL scopes at once')

        handler_log: list[str] = []

        security_check = make_security_check(handler_log)
        security_conns = group.connect_all_scopes(
            NavigateToUrlEvent,
            security_check,
            mode=ConnectionType.DIRECT,
            priority=100,
        )
        ok(f'SecurityCheck: 1 call → {len(security_conns)} connections')

        crash_handler = make_crash_handler(handler_log)
        crash_conns = group.connect_all_scopes(
            BrowserErrorEvent,
            crash_handler,
            mode=ConnectionType.DIRECT,
            priority=100,
        )
        ok(f'CrashHandler: 1 call → {len(crash_conns)} connections')

        # Verify fan-in: emit on tab-1, tab-3, tab-5 — same handler catches all
        handler_log.clear()
        for tab in [tabs[0], tabs[2], tabs[4]]:
            tab.emit(NavigateToUrlEvent(url=f'https://www.bilibili.com/{tab.scope_id}'))

        ok(f'Emitted on 3 different tabs — same handler caught all {len(handler_log)}:')
        trace(handler_log)

        # ── Phase 3: Fan-out (1→4) + Cross-scope Routing ──

        phase(3, 'Fan-out (1→4) + Cross-scope Routing')
        info('1 NavigateToUrlEvent on tab-1 triggers 4 handlers:')
        info('  p=100  security_check     (Direct, same scope)')
        info('  p=50   cdp_navigate       (Queued, same scope — real Page.navigate)')
        info('  p=0    cdp_screenshot     (Queued, same scope — real screenshot)')
        info('  p=-10  cross_scope_mon    (Queued → monitor scope event loop)')

        monitor_received: list[str] = []
        handler_log.clear()

        tabs[0].connect(
            NavigateToUrlEvent,
            make_cdp_nav_handler(cdp, sessions['tab-1'], tabs[0], handler_log),
            mode=ConnectionType.QUEUED,
            target_scope=tabs[0],
            priority=50,
        )
        tabs[0].connect(
            ScreenshotEvent,
            make_cdp_screenshot_handler(cdp, sessions['tab-1'], handler_log),
            mode=ConnectionType.QUEUED,
            target_scope=tabs[0],
            priority=0,
        )
        tabs[0].connect(
            NavigateToUrlEvent,
            make_cross_scope_monitor(monitor_received, handler_log),
            mode=ConnectionType.QUEUED,
            target_scope=monitor,  # ← runs in monitor's event loop
            priority=-10,
        )

        nav_event = NavigateToUrlEvent(url='https://www.xiaohongshu.com')
        tabs[0].emit(nav_event)
        ok(f'Direct handler ran inline: {handler_log[0]}')

        await nav_event
        ok(f'All Queued handlers completed ({len(handler_log)} entries):')
        trace(handler_log)
        ok(f'Cross-scope: monitor received {monitor_received}')

        # Also fire a screenshot through the event system
        handler_log.clear()
        ss_event = ScreenshotEvent()
        tabs[0].emit(ss_event)
        await ss_event
        ok(f'Screenshot via Queued handler: {handler_log}')

        # ── Phase 4: Connection Filters ──

        phase(4, 'Connection Filters — selective handler execution')
        info('Filter: only intercept URLs containing "danger"')

        handler_log.clear()
        filtered_handler = make_filtered_security(handler_log)

        filter_conn = tabs[1].connect(
            NavigateToUrlEvent,
            filtered_handler,
            mode=ConnectionType.DIRECT,
            priority=200,
            filter=lambda e: 'danger' in e.url,
        )

        # 4a: Safe URL — filter=False → handler NEVER runs
        tabs[1].emit(NavigateToUrlEvent(url='https://www.bilibili.com/safe'))
        ran = any('strict_filter' in x for x in handler_log)
        ok(f'Safe URL → filtered handler ran = {ran} (expected False)')

        # 4b: Dangerous URL — filter=True → handler blocks
        handler_log.clear()
        try:
            tabs[1].emit(NavigateToUrlEvent(url='https://www.bilibili.com/danger-zone'))
            fail('Expected block')
        except ValueError:
            ok('Dangerous URL → filter matched, handler blocked + consumed')
        trace(handler_log)

        filter_conn.disconnect()

        # ── Phase 5: expect() — Declarative Waiting ──

        phase(5, 'expect() — Declarative Future Event Waiting')
        info('emit NavigateToUrlEvent, then expect(NavigationCompleteEvent)')

        handler_log.clear()

        # Nav handler on tab-2 (emits NavigationCompleteEvent after real CDP navigation)
        tabs[1].connect(
            NavigateToUrlEvent,
            make_cdp_nav_handler(cdp, sessions['tab-2'], tabs[1], handler_log),
            mode=ConnectionType.QUEUED,
            target_scope=tabs[1],
            priority=50,
        )

        expect_task = asyncio.create_task(
            expect(
                tabs[1],
                NavigationCompleteEvent,
                include=lambda e: 'xiaohongshu' in e.url,
                timeout=30.0,
            )
        )
        await asyncio.sleep(0)

        tabs[1].emit(NavigateToUrlEvent(url='https://www.xiaohongshu.com/explore'))

        complete = await expect_task
        ok(f'expect() resolved: NavigationCompleteEvent(url={complete.url})')
        ok('Clean declarative async — no polling, no manual callbacks')

        # ── Phase 6: Concurrent Dispatch ──

        phase(6, 'Concurrent Dispatch — 5 scopes in parallel')
        info('Navigate all 5 tabs to different URLs simultaneously')

        handler_log.clear()
        urls = [
            'https://www.xiaohongshu.com',
            'https://www.bilibili.com',
            'https://www.google.com',
            'https://www.google.com/search?q=agent-cdp',
            'https://www.xiaohongshu.com/explore',
        ]

        # Add nav handlers to remaining tabs
        for tab in tabs[2:]:
            tab.connect(
                NavigateToUrlEvent,
                make_cdp_nav_handler(cdp, sessions[tab.scope_id], tab, handler_log),
                mode=ConnectionType.QUEUED,
                target_scope=tab,
                priority=50,
            )

        # Sequential
        t0 = time.perf_counter_ns()
        for tab, url in zip(tabs, urls):
            e = NavigateToUrlEvent(url=url)
            tab.emit(e)
            await e
        seq_ms = (time.perf_counter_ns() - t0) / 1_000_000
        ok(f'Sequential: {seq_ms:.0f}ms')

        # Concurrent — emit all 5 at once, then wait for all
        handler_log.clear()
        events: list[BaseEvent[Any]] = []
        t0 = time.perf_counter_ns()
        for tab, url in zip(tabs, urls):
            e = NavigateToUrlEvent(url=url)
            tab.emit(e)
            events.append(e)

        async def _wait_event(event: BaseEvent[Any]) -> None:
            await event

        await asyncio.gather(*[_wait_event(e) for e in events])
        par_ms = (time.perf_counter_ns() - t0) / 1_000_000
        speedup = seq_ms / par_ms if par_ms > 0 else 0

        ok(f'Concurrent: {par_ms:.0f}ms')
        ok(f'Speedup: {speedup:.1f}x (bubus global queue → always sequential)')

        # ── Phase 7: EventLog ──

        phase(7, 'EventLog — JSONL persistence + replay')

        log_dir = Path(tempfile.mkdtemp(prefix='agent-cdp-log-'))
        log_path = log_dir / 'events.jsonl'
        log_writer = EventLogWriter(log_path)

        async def event_log_writer(event: BaseEvent[Any]) -> None:
            await log_writer.write(event)

        for tab in tabs:
            tab.connect_all(
                event_log_writer,
                mode=ConnectionType.QUEUED,
                target_scope=monitor,
                priority=-100,
            )

        # Navigate 3 tabs to generate logged events
        for i, tab in enumerate(tabs[:3]):
            e = NavigateToUrlEvent(url=f'https://www.bilibili.com/log-{i}')
            tab.emit(e)
            await e

        await asyncio.sleep(0.5)  # let cross-scope log handlers flush

        logged = await log_writer.read_all()
        ok(f'EventLog: {len(logged)} events written to {log_path}')
        for ev in logged[:5]:
            info(f'{type(ev).__name__}(id={ev.event_id[:8]}...)')
        if len(logged) > 5:
            info(f'... and {len(logged) - 5} more')

        # ── Phase 8: Auto-disconnect (cross-scope) ──

        phase(8, 'Auto-disconnect — scope.close() severs cross-scope connections')

        monitor_received.clear()

        # Connect cross-scope monitor to all tabs
        for tab in tabs[1:]:
            tab.connect(
                NavigateToUrlEvent,
                make_cross_scope_monitor(monitor_received, handler_log),
                mode=ConnectionType.QUEUED,
                target_scope=monitor,
                priority=-10,
            )

        # Emit on all 5 → monitor receives from all
        monitor_received.clear()
        for tab in tabs:
            e = NavigateToUrlEvent(url=f'https://www.bilibili.com/pre/{tab.scope_id}')
            tab.emit(e)
            await e
        await asyncio.sleep(0.2)
        ok(f'Before close: monitor received {len(monitor_received)} events')

        # Close tab-3
        closed = tabs[2]
        await group.close_scope(closed.scope_id)
        ok(f'Closed "{closed.scope_id}" — connections auto-severed')
        ok(f'Remaining: {group.scope_ids}')

        # Emit on remaining 4 → monitor receives 4
        monitor_received.clear()
        for tab in tabs:
            if tab.scope_id == closed.scope_id:
                try:
                    tab.emit(NavigateToUrlEvent(url='https://should-fail.com'))
                    fail('Expected RuntimeError')
                except RuntimeError as exc:
                    ok(f'Closed scope rejects emit: {exc}')
                continue
            e = NavigateToUrlEvent(url=f'https://www.bilibili.com/post/{tab.scope_id}')
            tab.emit(e)
            await e
        await asyncio.sleep(0.2)
        ok(f'After close: monitor received {len(monitor_received)} events (not 5)')

        # ── Cleanup ──
        await group.close_all()

        # ── Summary ──
        banner('Demo Complete — 8 Architectural Advantages')
        pr(f'  {BOLD}1. Fan-in (N:1){RESET}    connect_all_scopes: 1 call → {len(security_conns)} connections')
        pr(f'  {BOLD}2. Fan-out (1:N){RESET}   1 event → 4 handlers (Direct + Queued + cross-scope)')
        pr(f"  {BOLD}3. Cross-scope{RESET}     Handler runs in monitor's event loop, not emitter's")
        pr(f'  {BOLD}4. Filters{RESET}         Handler skipped entirely when filter=False')
        pr(f'  {BOLD}5. expect(){RESET}        Declarative waiting — no polling')
        pr(f'  {BOLD}6. Concurrency{RESET}     {speedup:.1f}x speedup — per-scope event loops')
        pr(f'  {BOLD}7. EventLog{RESET}        {len(logged)} events persisted + deserialized')
        pr(f'  {BOLD}8. Auto-disconnect{RESET}  scope.close() severed cross-scope connections')
        pr()

    except Exception:
        logging.exception('Demo failed')
        raise
    finally:
        if group:
            await group.close_all()
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

    asyncio.run(run_demo())


if __name__ == '__main__':
    main()
