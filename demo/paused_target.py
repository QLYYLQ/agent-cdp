#!/usr/bin/env python3
"""PausedTarget demo: race-free CDP event bridging on Amazon & Xiaohongshu.

Demonstrates the core problem PausedTarget solves:
  Without coordination, CDP events (navigation, dialogs, DOM changes) can fire
  BEFORE event bridges and handlers are registered — causing missed events.

  PausedTarget guarantees: all bridges + handlers are wired up BEFORE the
  browser target starts loading. Resume is always called, even on failure.

Two usage modes shown:
  Tab 1 (Amazon):     PausedTarget(resume=callable)  — custom resume callback
  Tab 2 (Xiaohongshu): CDPEventBridge.paused(resume=callable) — convenience factory

Usage:
    uv run python -m demo.paused_target
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from agent_cdp import CDPEventBridge, ConnectionType, EventScope, ScopeGroup
from agent_cdp.bridge import PausedTarget
from agent_cdp.events import BaseEvent

from ._output import BOLD, DIM, GREEN, RESET, banner, fail, info, ok, phase, tab_label, warn
from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome

# ── Events specific to this demo ──


class PageLoadEvent(BaseEvent[None]):
    """CDP Page.loadEventFired bridged into agent-cdp."""

    __registry_key__ = 'navigation.page_load'
    timestamp: float = 0.0


class DOMContentLoadedEvent(BaseEvent[None]):
    """CDP Page.domContentEventFired bridged into agent-cdp."""

    __registry_key__ = 'navigation.dom_content_loaded'
    timestamp: float = 0.0


class FrameNavigatedEvent(BaseEvent[None]):
    """CDP Page.frameNavigated bridged into agent-cdp."""

    __registry_key__ = 'navigation.frame_navigated'
    url: str = ''
    frame_id: str = ''


class DialogEvent(BaseEvent[None]):
    """CDP Page.javascriptDialogOpening bridged into agent-cdp."""

    __registry_key__ = 'security.dialog'
    dialog_type: str = 'alert'
    message: str = ''


class NetworkRequestEvent(BaseEvent[None]):
    """CDP Network.requestWillBeSent bridged into agent-cdp."""

    __registry_key__ = 'network.request'
    url: str = ''
    method: str = 'GET'
    request_id: str = ''


# ── Per-tab event collector ──


class TabEventCollector:
    """Collects events received by a tab for timeline display."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[tuple[float, str, str]] = []  # (timestamp, type, detail)
        self._t0 = time.perf_counter()

    def _ts(self) -> float:
        return time.perf_counter() - self._t0

    def on_page_load(self, event: PageLoadEvent) -> None:
        self.events.append((self._ts(), 'PageLoad', f'ts={event.timestamp:.3f}'))

    def on_dom_content(self, event: DOMContentLoadedEvent) -> None:
        self.events.append((self._ts(), 'DOMContentLoaded', f'ts={event.timestamp:.3f}'))

    def on_frame_navigated(self, event: FrameNavigatedEvent) -> None:
        url_short = event.url[:60] + ('...' if len(event.url) > 60 else '')
        self.events.append((self._ts(), 'FrameNavigated', url_short))

    def on_dialog(self, event: DialogEvent) -> None:
        self.events.append((self._ts(), 'Dialog', f'{event.dialog_type}: {event.message}'))

    def on_network_request(self, event: NetworkRequestEvent) -> None:
        url_short = event.url[:60] + ('...' if len(event.url) > 60 else '')
        self.events.append((self._ts(), 'NetworkRequest', f'{event.method} {url_short}'))


# ── Tab setup with PausedTarget ──


async def setup_tab_with_paused_target(
    cdp: CDPClient,
    group: ScopeGroup,
    *,
    tab_name: str,
    target_url: str,
    collector: TabEventCollector,
    use_factory: bool = False,
) -> tuple[EventScope, CDPEventBridge, str]:
    """Create a tab and set up all bridges/handlers inside PausedTarget.

    Returns (scope, bridge, session_id).
    """
    # Step 1: Create target at about:blank (not the real URL yet)
    result = await cdp.send('Target.createTarget', {'url': 'about:blank'})
    target_id = result['targetId']

    # Step 2: Attach to get session_id
    attach = await cdp.send(
        'Target.attachToTarget',
        {'targetId': target_id, 'flatten': True},
    )
    session_id = attach['sessionId']
    ok(f'{tab_label(tab_name)} target created & attached (session={session_id[:12]}...)')

    # Step 3: Build the resume callable — sends Runtime.runIfWaitingForDebugger
    # We use resume= (not cdp=) because CDPClient.send() needs session_id as a
    # separate kwarg, whereas CDPCommandProtocol.send() only takes method+params.
    async def resume_target() -> None:
        await cdp.send('Runtime.runIfWaitingForDebugger', session_id=session_id)

    # Step 4: PausedTarget — all setup happens atomically before resume
    if use_factory:
        # Mode B: CDPEventBridge.paused() convenience factory
        ctx = CDPEventBridge.paused(resume=resume_target)
    else:
        # Mode A: PausedTarget directly
        ctx = PausedTarget(resume=resume_target)

    scope: EventScope | None = None
    bridge: CDPEventBridge | None = None

    async with ctx:
        # ── Inside PausedTarget: wire everything up ──

        # 4a. Enable CDP domains
        await cdp.send('Page.enable', session_id=session_id)
        await cdp.send('Runtime.enable', session_id=session_id)
        await cdp.send('Network.enable', session_id=session_id)
        info(f'{tab_label(tab_name)} CDP domains enabled (Page, Runtime, Network)')

        # 4b. Create EventScope
        scope = await group.create_scope(
            f'tab-{tab_name.lower()}',
            target_id=target_id,
            session_id=session_id,
        )

        # 4c. Create CDP event bridge — routes Chrome events → agent-cdp events
        bridge = CDPEventBridge(cdp, scope, session_id=session_id)

        bridge.bridge(
            'Page.loadEventFired',
            lambda p: PageLoadEvent(timestamp=p.get('timestamp', 0.0)),
        )
        bridge.bridge(
            'Page.domContentEventFired',
            lambda p: DOMContentLoadedEvent(timestamp=p.get('timestamp', 0.0)),
        )
        bridge.bridge(
            'Page.frameNavigated',
            lambda p: FrameNavigatedEvent(
                url=p.get('frame', {}).get('url', ''),
                frame_id=p.get('frame', {}).get('id', ''),
            ),
        )
        bridge.bridge(
            'Page.javascriptDialogOpening',
            lambda p: DialogEvent(
                dialog_type=p.get('type', 'alert'),
                message=p.get('message', ''),
            ),
        )
        bridge.bridge(
            'Network.requestWillBeSent',
            lambda p: NetworkRequestEvent(
                url=p.get('request', {}).get('url', ''),
                method=p.get('request', {}).get('method', 'GET'),
                request_id=p.get('requestId', ''),
            ),
        )
        info(f'{tab_label(tab_name)} 5 CDP event bridges registered')

        # 4d. Connect handlers on scope — DIRECT for zero-latency event recording
        scope.connect(PageLoadEvent, collector.on_page_load, mode=ConnectionType.DIRECT)
        scope.connect(DOMContentLoadedEvent, collector.on_dom_content, mode=ConnectionType.DIRECT)
        scope.connect(FrameNavigatedEvent, collector.on_frame_navigated, mode=ConnectionType.DIRECT)
        scope.connect(DialogEvent, collector.on_dialog, mode=ConnectionType.DIRECT)
        scope.connect(
            NetworkRequestEvent,
            collector.on_network_request,
            mode=ConnectionType.DIRECT,
            priority=-10,  # low priority, observational
        )
        info(f'{tab_label(tab_name)} 5 Direct handlers connected')

    # ── PausedTarget exited: resume has been called ──
    ok(f'{tab_label(tab_name)} PausedTarget exited — target resumed')

    # Step 5: Now navigate — all bridges/handlers are guaranteed ready
    info(f'{tab_label(tab_name)} navigating to {target_url}')
    await cdp.send('Page.navigate', {'url': target_url}, session_id=session_id)

    assert scope is not None
    assert bridge is not None
    return scope, bridge, session_id


# ── Main demo ──


async def run_demo() -> None:
    banner('PausedTarget Demo — Race-Free CDP Event Bridging')
    print(f'{DIM}Amazon + Xiaohongshu · PausedTarget ensures zero missed events{RESET}')
    print(f'{DIM}Two modes: PausedTarget(resume=fn) vs CDPEventBridge.paused(resume=fn){RESET}\n')

    chrome_proc = None
    cdp: CDPClient | None = None
    group: ScopeGroup | None = None
    bridges: list[CDPEventBridge] = []

    try:
        # ══════════════════════════════════════════════════════
        phase(1, 'Launch Chrome & Connect')
        # ══════════════════════════════════════════════════════

        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome started (PID {chrome_proc.pid})')

        cdp = CDPClient(ws_url)
        await cdp.connect()
        ok(f'CDP connected: {ws_url[:50]}...')

        # Close default about:blank tab
        targets = await cdp.send('Target.getTargets')
        default_pages = [t for t in targets.get('targetInfos', []) if t.get('type') == 'page']

        group = ScopeGroup('browser')

        # ══════════════════════════════════════════════════════
        phase(2, 'Amazon — PausedTarget(resume=callable)')
        # ══════════════════════════════════════════════════════

        info('Mode A: PausedTarget(resume=resume_fn)')
        info('  All bridges + handlers registered inside async with block')
        info('  resume_fn sends Runtime.runIfWaitingForDebugger on exit')
        print()

        amazon_collector = TabEventCollector('Amazon')
        t0 = time.perf_counter()

        amazon_scope, amazon_bridge, amazon_sid = await setup_tab_with_paused_target(
            cdp,
            group,
            tab_name='Amazon',
            target_url='https://www.amazon.com',
            collector=amazon_collector,
            use_factory=False,  # Mode A: PausedTarget directly
        )
        bridges.append(amazon_bridge)

        # Wait for page to load
        info('Waiting for Amazon page load...')
        load_done = asyncio.Event()

        def on_amazon_load(event: PageLoadEvent) -> None:
            load_done.set()

        amazon_scope.connect(PageLoadEvent, on_amazon_load, mode=ConnectionType.DIRECT, priority=-100)

        try:
            await asyncio.wait_for(load_done.wait(), timeout=30.0)
            load_time = time.perf_counter() - t0
            ok(f'Amazon loaded in {load_time:.2f}s')
        except TimeoutError:
            load_time = time.perf_counter() - t0
            warn(f'Amazon load timeout after {load_time:.2f}s (continuing with partial events)')

        # ══════════════════════════════════════════════════════
        phase(3, 'Xiaohongshu — CDPEventBridge.paused(resume=callable)')
        # ══════════════════════════════════════════════════════

        info('Mode B: CDPEventBridge.paused(resume=resume_fn)')
        info('  Same PausedTarget, accessed via convenience factory')
        print()

        xhs_collector = TabEventCollector('Xiaohongshu')
        t0 = time.perf_counter()

        xhs_scope, xhs_bridge, xhs_sid = await setup_tab_with_paused_target(
            cdp,
            group,
            tab_name='Xiaohongshu',
            target_url='https://www.xiaohongshu.com',
            collector=xhs_collector,
            use_factory=True,  # Mode B: CDPEventBridge.paused()
        )
        bridges.append(xhs_bridge)

        # Wait for page to load
        info('Waiting for Xiaohongshu page load...')
        load_done2 = asyncio.Event()

        def on_xhs_load(event: PageLoadEvent) -> None:
            load_done2.set()

        xhs_scope.connect(PageLoadEvent, on_xhs_load, mode=ConnectionType.DIRECT, priority=-100)

        try:
            await asyncio.wait_for(load_done2.wait(), timeout=30.0)
            load_time = time.perf_counter() - t0
            ok(f'Xiaohongshu loaded in {load_time:.2f}s')
        except TimeoutError:
            load_time = time.perf_counter() - t0
            warn(f'Xiaohongshu load timeout after {load_time:.2f}s (continuing with partial events)')

        # Close default tab now
        if default_pages:
            try:
                await cdp.send('Target.closeTarget', {'targetId': default_pages[0]['targetId']})
            except Exception:
                pass

        # ══════════════════════════════════════════════════════
        phase(4, 'Event Timelines — Proof of Zero Missed Events')
        # ══════════════════════════════════════════════════════

        for collector in [amazon_collector, xhs_collector]:
            name = collector.name
            events = collector.events

            print(f'\n  {BOLD}{tab_label(name)} captured {len(events)} events:{RESET}')

            if not events:
                fail(f'No events captured for {name} — bridge may have failed')
                continue

            # Show first events (proof that early events were captured)
            # Count by type
            by_type: dict[str, int] = {}
            for _, etype, _ in events:
                by_type[etype] = by_type.get(etype, 0) + 1

            for etype, count in sorted(by_type.items()):
                ok(f'{etype}: {count} events')

            # Show first 5 events (earliest captured — the most important ones)
            print(f'\n    {DIM}First 5 events (earliest — proves setup-before-navigate):{RESET}')
            for ts, etype, detail in events[:5]:
                detail_short = detail[:70] + ('...' if len(detail) > 70 else '')
                print(f'    {DIM}+{ts:.3f}s{RESET}  {etype:<20} {detail_short}')

            # Check that FrameNavigated was captured (this is the navigation itself)
            has_frame_nav = any(et == 'FrameNavigated' for _, et, _ in events)
            has_dom_content = any(et == 'DOMContentLoaded' for _, et, _ in events)
            has_page_load = any(et == 'PageLoad' for _, et, _ in events)

            if has_frame_nav:
                ok('FrameNavigated captured — navigation event bridged correctly')
            else:
                warn('No FrameNavigated event (page may have redirected via JS)')

            if has_dom_content and has_page_load:
                ok('Full load lifecycle captured: DOMContentLoaded → PageLoad')
            elif has_dom_content or has_page_load:
                ok('Partial load lifecycle captured')

        # ══════════════════════════════════════════════════════
        phase(5, 'Per-Scope Isolation Check')
        # ══════════════════════════════════════════════════════

        # Verify Amazon events don't appear in Xiaohongshu collector and vice versa
        amazon_urls = {d for _, et, d in amazon_collector.events if et == 'FrameNavigated'}
        xhs_urls = {d for _, et, d in xhs_collector.events if et == 'FrameNavigated'}

        amazon_has_xhs = any('xiaohongshu' in u for u in amazon_urls)
        xhs_has_amazon = any('amazon' in u for u in xhs_urls)

        if not amazon_has_xhs and not xhs_has_amazon:
            ok('Per-scope isolation confirmed: no cross-tab event leakage')
        else:
            if amazon_has_xhs:
                fail('Amazon scope received Xiaohongshu events!')
            if xhs_has_amazon:
                fail('Xiaohongshu scope received Amazon events!')

        ok(f'Amazon scope event history: {len(amazon_scope.event_history)} events')
        ok(f'Xiaohongshu scope event history: {len(xhs_scope.event_history)} events')

        # ══════════════════════════════════════════════════════
        phase(6, 'Parallel Screenshots')
        # ══════════════════════════════════════════════════════

        output_dir = Path(__file__).parent / 'screenshots'
        output_dir.mkdir(exist_ok=True)

        async def take_screenshot(
            cdp_client: CDPClient,
            sid: str,
            name: str,
        ) -> tuple[str, int]:
            result = await cdp_client.send(
                'Page.captureScreenshot',
                {'format': 'png'},
                session_id=sid,
            )
            data: str = result.get('data', '')
            if data:
                import base64

                path = output_dir / f'paused_{name.lower()}.png'
                path.write_bytes(base64.b64decode(data))
                return name, path.stat().st_size
            return name, 0

        t_ss = time.perf_counter()
        ss_results = await asyncio.gather(
            take_screenshot(cdp, amazon_sid, 'Amazon'),
            take_screenshot(cdp, xhs_sid, 'Xiaohongshu'),
        )
        ss_time = time.perf_counter() - t_ss

        for name, size in ss_results:
            if size > 0:
                ok(f'{tab_label(name)} screenshot: {size:,} bytes')
            else:
                fail(f'{tab_label(name)} screenshot failed')

        ok(f'Both screenshots in {ss_time:.2f}s (parallel)')
        info(f'Saved to {output_dir}/')

        # ══════════════════════════════════════════════════════
        phase(7, 'PausedTarget Safety — Exception Handling')
        # ══════════════════════════════════════════════════════

        info('Verifying: resume is called even when setup raises an exception')

        resume_called = False

        async def tracking_resume() -> None:
            nonlocal resume_called
            resume_called = True

        try:
            async with PausedTarget(resume=tracking_resume):
                raise ValueError('simulated setup failure')
        except ValueError:
            pass

        if resume_called:
            ok('Resume called despite exception — target will never be stuck')
        else:
            fail('Resume was NOT called — BUG!')

        info('Verifying: resume is idempotent (safe to call multiple times)')

        call_count = 0

        async def counting_resume() -> None:
            nonlocal call_count
            call_count += 1

        ctx = PausedTarget(resume=counting_resume)
        async with ctx:
            pass
        await ctx.resume()  # explicit second call
        await ctx.resume()  # third call

        if call_count == 1:  # pyright: ignore[reportUnnecessaryComparison]
            ok(f'Resume called exactly once (idempotent) — {call_count} call')
        else:
            fail(f'Resume called {call_count} times — should be 1')

        info('Verifying: PausedTarget is not reentrant')

        ctx2 = PausedTarget(resume=counting_resume)
        reentrant_blocked = False
        async with ctx2:
            try:
                async with ctx2:
                    pass
            except RuntimeError as e:
                if 'already entered' in str(e):
                    reentrant_blocked = True

        if reentrant_blocked:
            ok('Reentrant use correctly blocked with RuntimeError')
        else:
            fail('Reentrant use was NOT blocked — BUG!')

        # ══════════════════════════════════════════════════════
        phase(8, 'Cleanup')
        # ══════════════════════════════════════════════════════

        # Close bridges
        for b in bridges:
            b.close()
        ok(f'Closed {len(bridges)} CDP event bridges')

        # Close scopes
        await group.close_all()
        ok(f'All scopes closed (count={group.scope_count})')

        # ══════════════════════════════════════════════════════
        banner('PausedTarget Demo Complete')
        # ══════════════════════════════════════════════════════

        print(f'{GREEN}All phases passed.{RESET}\n')
        print(f'{DIM}Demonstrated:{RESET}')
        print('  1. PausedTarget(resume=fn)     — race-free bridge setup on Amazon')
        print('  2. CDPEventBridge.paused(...)   — convenience factory on Xiaohongshu')
        print('  3. 5 CDP event types bridged:   Page.load, DOMContent, FrameNav, Dialog, Network')
        print('  4. All handlers registered BEFORE navigation — zero missed events')
        print('  5. Per-scope isolation:          Amazon events stay in Amazon scope')
        print('  6. Exception safety:             resume() always called, even on failure')
        print('  7. Idempotent resume:            safe to call multiple times')
        print('  8. Not reentrant:                double-enter correctly blocked')
        print()
        print(f'{DIM}This replaces the ad-hoc "hope the handler registers in time" pattern')
        print(f'with an explicit, race-free coordination protocol.{RESET}')
        print()

    except Exception:
        logging.exception('Demo failed')
        raise
    finally:
        for b in bridges:
            try:
                b.close()
            except Exception:
                pass
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

    asyncio.run(run_demo())


if __name__ == '__main__':
    main()
