#!/usr/bin/env python3
"""CDP vs Playwright: browser automation performance benchmark.

Compares latency of equivalent operations through two channels:
  CDP  — direct WebSocket commands via our minimal CDPClient
  PW   — Playwright high-level API via connect_over_cdp()

Both channels drive the same Chrome instance, same page.
Each operation runs ITERATIONS times (after WARMUP) on an already-loaded
page, isolating automation overhead from network variance.

Usage:
    uv run python -m demo.bench_cdp_vs_pw
"""

from __future__ import annotations

import asyncio
import base64
import gc  # noqa: F401
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from playwright.async_api import async_playwright

from .cdp_client import CDPClient
from .chrome import kill_chrome, launch_chrome

# ── Configuration ────────────────────────────────────────────

ITERATIONS = 100
WARMUP = 5
TEST_PAGES = [
    ('example.com', 'https://example.com'),
    ('quotes.toscrape', 'https://quotes.toscrape.com'),
]

# ── Shared JavaScript snippets ───────────────────────────────
# Identical JS executed through both channels to isolate
# pure channel overhead from computation differences.

JS_TITLE = 'document.title'

JS_FULL_HTML = 'document.documentElement.outerHTML'

JS_LINKS = """(() => {
    const links = document.querySelectorAll('a[href]');
    return Array.from(links, a => ({
        href: a.href, text: (a.textContent || '').trim().slice(0, 80)
    }));
})()"""

JS_TEXT = 'document.body.innerText'

JS_DOM_STATS = """(() => {
    let count = 0, maxD = 0;
    function w(n, d) {
        count++;
        if (d > maxD) maxD = d;
        for (const c of n.childNodes) w(c, d + 1);
    }
    w(document.documentElement, 0);
    return {
        nodeCount: count,
        maxDepth: maxD,
        elementCount: document.querySelectorAll('*').length
    };
})()"""

JS_INTERACTIVE = """(() => {
    const s = 'a,button,input,select,textarea,[role="button"],[onclick]';
    const els = document.querySelectorAll(s);
    return Array.from(els, el => ({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        text: (el.textContent || '').trim().slice(0, 50),
        href: el.getAttribute('href') || ''
    }));
})()"""

JS_HEADINGS = """(() => {
    return Array.from(
        document.querySelectorAll('h1,h2,h3,h4,h5,h6'),
        h => ({ level: parseInt(h.tagName[1]), text: (h.textContent || '').trim() })
    );
})()"""


# ── Data types ───────────────────────────────────────────────


@dataclass
class OpTiming:
    """Per-iteration timings for one (operation, channel, site) combo."""

    name: str
    channel: str  # 'cdp' | 'pw'
    site: str
    times_us: list[float] = field(default_factory=list)
    error: str = ''

    @property
    def n(self) -> int:
        return len(self.times_us)

    @property
    def total_us(self) -> float:
        return sum(self.times_us)

    @property
    def mean_us(self) -> float:
        return statistics.mean(self.times_us) if self.times_us else 0.0

    @property
    def median_us(self) -> float:
        return statistics.median(self.times_us) if self.times_us else 0.0

    @property
    def stdev_us(self) -> float:
        return statistics.stdev(self.times_us) if len(self.times_us) > 1 else 0.0

    @property
    def min_us(self) -> float:
        return min(self.times_us) if self.times_us else 0.0

    @property
    def max_us(self) -> float:
        return max(self.times_us) if self.times_us else 0.0

    def percentile(self, p: float) -> float:
        if not self.times_us:
            return 0.0
        s = sorted(self.times_us)
        k = (len(s) - 1) * p / 100.0
        f = int(k)
        c = min(f + 1, len(s) - 1)
        return s[f] + (k - f) * (s[c] - s[f])


# ── Output formatting ────────────────────────────────────────

BOLD = '\033[1m'
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
DIM = '\033[2m'
RESET = '\033[0m'


def banner(t: str) -> None:
    print(f'\n{BOLD}{CYAN}{"═" * 70}')
    print(f'  {t}')
    print(f'{"═" * 70}{RESET}\n')


def phase(t: str) -> None:
    print(f'\n{BOLD}{YELLOW}── {t} {"─" * max(1, 60 - len(t))}{RESET}')


def ok(t: str) -> None:
    print(f'  {GREEN}✓{RESET} {t}')


def info(t: str) -> None:
    print(f'  {DIM}→ {t}{RESET}')


def warn(t: str) -> None:
    print(f'  {YELLOW}!{RESET} {t}')


def fmt(us: float) -> str:
    """Format microseconds to human-readable."""
    if us < 1000:
        return f'{us:.0f}us'
    if us < 1_000_000:
        return f'{us / 1000:.2f}ms'
    return f'{us / 1_000_000:.2f}s'


# ── Benchmark runner ─────────────────────────────────────────


async def bench_op(
    name: str,
    channel: str,
    site: str,
    op: Callable[[], Awaitable[Any]],
    warmup: int = WARMUP,
    iters: int = ITERATIONS,
) -> OpTiming:
    """Run op (warmup + iters) times, return timing data.

    - Warmup and measurement have separate error handling.
    - GC is disabled during measurement to reduce jitter.
    - Each iteration has a 10s timeout to prevent hangs.
    """
    timing = OpTiming(name=name, channel=channel, site=site)

    # Warmup phase (GC enabled, errors abort early)
    for i in range(warmup):
        try:
            await asyncio.wait_for(op(), timeout=10.0)
        except Exception as e:
            timing.error = f'warmup iter {i}: {str(e)[:100]}'
            return timing

    # Measurement phase (GC disabled)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(iters):
            try:
                t0 = time.perf_counter_ns()
                await asyncio.wait_for(op(), timeout=10.0)
                timing.times_us.append((time.perf_counter_ns() - t0) / 1000.0)
            except Exception as e:
                timing.error = f'after {len(timing.times_us)}/{iters}: {str(e)[:80]}'
                break
    finally:
        if gc_was_enabled:
            gc.enable()

    return timing


# ── CDP operations ───────────────────────────────────────────


class CDPBench:
    """Raw CDP operations via our minimal WebSocket client."""

    def __init__(self, cdp: CDPClient, sid: str) -> None:
        self.cdp = cdp
        self.sid = sid

    async def _eval(self, expr: str) -> Any:
        r = await self.cdp.send(
            'Runtime.evaluate',
            {'expression': expr, 'returnByValue': True},
            session_id=self.sid,
        )
        return r.get('result', {}).get('value')

    # ── JS-eval based operations ──

    async def get_html(self) -> Any:
        return await self._eval(JS_FULL_HTML)

    async def eval_title(self) -> Any:
        return await self._eval(JS_TITLE)

    async def eval_links(self) -> Any:
        return await self._eval(JS_LINKS)

    async def eval_text(self) -> Any:
        return await self._eval(JS_TEXT)

    async def eval_dom_stats(self) -> Any:
        return await self._eval(JS_DOM_STATS)

    async def eval_interactive(self) -> Any:
        return await self._eval(JS_INTERACTIVE)

    # ── DOM-domain operations ──

    async def query_h1(self) -> str:
        doc = await self.cdp.send('DOM.getDocument', {'depth': 0}, session_id=self.sid)
        root_id = doc['root']['nodeId']
        r = await self.cdp.send(
            'DOM.querySelector',
            {'nodeId': root_id, 'selector': 'h1'},
            session_id=self.sid,
        )
        nid = r.get('nodeId', 0)
        if not nid:
            return ''
        h = await self.cdp.send('DOM.getOuterHTML', {'nodeId': nid}, session_id=self.sid)
        return h.get('outerHTML', '')

    async def query_all_links(self) -> int:
        doc = await self.cdp.send('DOM.getDocument', {'depth': 0}, session_id=self.sid)
        root_id = doc['root']['nodeId']
        r = await self.cdp.send(
            'DOM.querySelectorAll',
            {'nodeId': root_id, 'selector': 'a'},
            session_id=self.sid,
        )
        return len(r.get('nodeIds', []))

    # ── Binary / specialized operations ──

    async def screenshot(self) -> int:
        r = await self.cdp.send(
            'Page.captureScreenshot',
            {'format': 'png'},
            session_id=self.sid,
        )
        return len(base64.b64decode(r.get('data', '')))

    async def dom_snapshot(self) -> int:
        r = await self.cdp.send(
            'DOMSnapshot.captureSnapshot',
            {'computedStyles': ['display', 'visibility', 'opacity']},
            session_id=self.sid,
        )
        return len(r.get('documents', []))

    async def accessibility_tree(self) -> int:
        r = await self.cdp.send('Accessibility.getFullAXTree', session_id=self.sid)
        return len(r.get('nodes', []))

    # ── Combined pipeline ──

    async def cleaning_pipeline(self) -> dict[str, int]:
        """Simulate agent DOM extraction: 5 sequential JS evals."""
        html = await self._eval(JS_FULL_HTML)
        text = await self._eval(JS_TEXT)
        links = await self._eval(JS_LINKS)
        headings = await self._eval(JS_HEADINGS)
        interactive = await self._eval(JS_INTERACTIVE)
        return {
            'html_len': len(html or ''),
            'text_len': len(text or ''),
            'links': len(links or []),
            'headings': len(headings or []),
            'interactive': len(interactive or []),
        }


# ── Playwright operations ────────────────────────────────────


class PWBench:
    """Playwright operations via high-level API."""

    def __init__(self, page: Any, cdp_session: Any = None) -> None:
        self.page = page
        self.cdp_session = cdp_session  # for operations without native PW API

    # ── JS-eval based operations ──

    async def get_html(self) -> str:
        return await self.page.content()

    async def eval_title(self) -> Any:
        return await self.page.evaluate(JS_TITLE)

    async def eval_links(self) -> Any:
        return await self.page.evaluate(JS_LINKS)

    async def eval_text(self) -> Any:
        return await self.page.evaluate(JS_TEXT)

    async def eval_dom_stats(self) -> Any:
        return await self.page.evaluate(JS_DOM_STATS)

    async def eval_interactive(self) -> Any:
        return await self.page.evaluate(JS_INTERACTIVE)

    # ── Element handle operations ──

    async def query_h1(self) -> str:
        el = await self.page.query_selector('h1')
        if not el:
            return ''
        return await el.inner_html()

    async def query_all_links(self) -> int:
        els = await self.page.query_selector_all('a')
        return len(els)

    # ── Binary / specialized operations ──

    async def screenshot(self) -> int:
        data = await self.page.screenshot(type='png')
        return len(data)

    async def dom_snapshot(self) -> int:
        """Use CDP DOMSnapshot through Playwright's CDP session (fair comparison)."""
        if not self.cdp_session:
            raise RuntimeError('No CDP session for dom_snapshot')
        r = await self.cdp_session.send(
            'DOMSnapshot.captureSnapshot',
            {'computedStyles': ['display', 'visibility', 'opacity']},
        )
        return len(r.get('documents', []))

    async def accessibility_tree(self) -> int:
        """Use CDP session through Playwright (page.accessibility removed in PW 1.58)."""
        if not self.cdp_session:
            raise RuntimeError('No CDP session for accessibility_tree')
        r = await self.cdp_session.send('Accessibility.getFullAXTree')
        return len(r.get('nodes', []))

    # ── Combined pipeline ──

    async def cleaning_pipeline(self) -> dict[str, int]:
        """Simulate agent DOM extraction: content() + 4 evaluates."""
        html = await self.page.content()
        text = await self.page.evaluate(JS_TEXT)
        links = await self.page.evaluate(JS_LINKS)
        headings = await self.page.evaluate(JS_HEADINGS)
        interactive = await self.page.evaluate(JS_INTERACTIVE)
        return {
            'html_len': len(html or ''),
            'text_len': len(text or ''),
            'links': len(links or []),
            'headings': len(headings or []),
            'interactive': len(interactive or []),
        }


# ── Operation registry ───────────────────────────────────────
# (method_name, human description, notes)

OPERATIONS: list[tuple[str, str, str]] = [
    ('get_html', 'Get full page HTML', 'CDP: Runtime.evaluate | PW: page.content()'),
    ('eval_title', 'Evaluate document.title', 'Same JS, different channel'),
    ('eval_links', 'Extract all links via JS', 'Same JS, different channel'),
    ('eval_text', 'Get body innerText', 'Same JS, different channel'),
    ('eval_dom_stats', 'DOM tree statistics', 'Same JS, different channel'),
    ('eval_interactive', 'Extract interactive elements', 'Same JS, different channel'),
    ('query_h1', 'querySelector h1 + HTML', 'CDP: DOM domain | PW: element handle'),
    ('query_all_links', 'querySelectorAll a (count)', 'CDP: DOM domain | PW: element handles'),
    ('screenshot', 'Capture PNG screenshot', 'CDP: Page.captureScreenshot | PW: page.screenshot'),
    ('dom_snapshot', 'DOM snapshot + styles', 'CDP: direct WS | PW: CDP session proxy (same API)'),
    ('accessibility_tree', 'Accessibility tree', 'CDP: direct WS | PW: CDP session proxy (no native PW API)'),
    ('cleaning_pipeline', 'Full cleaning pipeline', '5-step DOM extraction (agent workflow)'),
]


# ── Report printing ──────────────────────────────────────────


def print_comparison(results: list[OpTiming], site: str) -> None:
    """Print per-operation comparison table for one site."""
    hdr = (
        f'  {"Operation":<22} '
        f'{"CDP mean":>10} {"CDP p50":>10} {"CDP total":>11} '
        f'{"PW mean":>10} {"PW p50":>10} {"PW total":>11} '
        f'{"PW/CDP":>7}'
    )
    print(hdr)
    print(f'  {"─" * 22} {"─" * 10} {"─" * 10} {"─" * 11} {"─" * 10} {"─" * 10} {"─" * 11} {"─" * 7}')

    for op_name, _, _ in OPERATIONS:
        cdp_r = next((r for r in results if r.name == op_name and r.channel == 'cdp' and r.site == site), None)
        pw_r = next((r for r in results if r.name == op_name and r.channel == 'pw' and r.site == site), None)
        if not cdp_r or not pw_r:
            continue

        if cdp_r.error:
            print(f'  {op_name:<22} {"ERROR":>10} {"":>10} {"":>11} {"":>10} {"":>10} {"":>11} {"N/A":>7}')
            continue
        if pw_r.error:
            cm, cp, ct = fmt(cdp_r.mean_us), fmt(cdp_r.median_us), fmt(cdp_r.total_us)
            print(f'  {op_name:<22} {cm:>10} {cp:>10} {ct:>11} {"ERROR":>10} {"":>10} {"":>11} {"N/A":>7}')
            continue

        cm, cp, ct = fmt(cdp_r.mean_us), fmt(cdp_r.median_us), fmt(cdp_r.total_us)
        pm, pp, pt = fmt(pw_r.mean_us), fmt(pw_r.median_us), fmt(pw_r.total_us)
        ratio = pw_r.mean_us / cdp_r.mean_us if cdp_r.mean_us > 0 else 0.0
        ratio_s = f'{ratio:.2f}x'
        print(f'  {op_name:<22} {cm:>10} {cp:>10} {ct:>11} {pm:>10} {pp:>10} {pt:>11} {ratio_s:>7}')


def print_detailed_stats(results: list[OpTiming]) -> None:
    """Print detailed per-operation statistics."""
    hdr = (
        f'  {"ch":>3} {"site":<16} {"operation":<22} '
        f'{"mean":>9} {"p50":>9} {"p95":>9} {"min":>9} {"max":>9} {"stdev":>9}'
    )
    print(hdr)
    print(f'  {"─" * 3} {"─" * 16} {"─" * 22} {"─" * 9} {"─" * 9} {"─" * 9} {"─" * 9} {"─" * 9} {"─" * 9}')

    for r in results:
        if r.error:
            continue
        print(
            f'  {r.channel:>3} {r.site:<16} {r.name:<22} '
            f'{fmt(r.mean_us):>9} {fmt(r.median_us):>9} '
            f'{fmt(r.percentile(95)):>9} {fmt(r.min_us):>9} '
            f'{fmt(r.max_us):>9} {fmt(r.stdev_us):>9}'
        )


def print_grand_total(results: list[OpTiming]) -> None:
    """Print aggregated totals across all sites and operations."""
    cdp_total = sum(r.total_us for r in results if r.channel == 'cdp' and not r.error)
    pw_total = sum(r.total_us for r in results if r.channel == 'pw' and not r.error)
    cdp_ops = sum(r.n for r in results if r.channel == 'cdp' and not r.error)
    pw_ops = sum(r.n for r in results if r.channel == 'pw' and not r.error)

    print(f'  {"Channel":<10} {"Total time":>14} {"Iterations":>12}')
    print(f'  {"─" * 10} {"─" * 14} {"─" * 12}')
    print(f'  {"CDP":<10} {fmt(cdp_total):>14} {cdp_ops:>12}')
    print(f'  {"Playwright":<10} {fmt(pw_total):>14} {pw_ops:>12}')

    if cdp_total > 0:
        ratio = pw_total / cdp_total
        direction = 'slower' if ratio > 1 else 'faster'
        print(f'\n  {BOLD}Overall: Playwright is {ratio:.2f}x {direction} than raw CDP{RESET}')
        print(f'  {DIM}(Total across {len(TEST_PAGES)} sites × {len(OPERATIONS)} ops × {ITERATIONS} iterations){RESET}')

    # Per-category breakdown
    categories = {
        'JS eval': ['eval_title', 'eval_links', 'eval_text', 'eval_dom_stats', 'eval_interactive'],
        'DOM API': ['query_h1', 'query_all_links'],
        'Content': ['get_html'],
        'Binary': ['screenshot'],
        'Specialized': ['dom_snapshot', 'accessibility_tree'],
        'Pipeline': ['cleaning_pipeline'],
    }

    print(f'\n  {"Category":<14} {"CDP total":>12} {"PW total":>12} {"PW/CDP":>8}')
    print(f'  {"─" * 14} {"─" * 12} {"─" * 12} {"─" * 8}')

    for cat_name, op_names in categories.items():
        ct = sum(r.total_us for r in results if r.channel == 'cdp' and r.name in op_names and not r.error)
        pt = sum(r.total_us for r in results if r.channel == 'pw' and r.name in op_names and not r.error)
        if ct > 0:
            ratio = pt / ct
            print(f'  {cat_name:<14} {fmt(ct):>12} {fmt(pt):>12} {ratio:.2f}x{" ":>3}')


# ── Main benchmark ───────────────────────────────────────────


async def run_benchmark() -> None:
    banner('CDP vs Playwright Performance Benchmark')
    print(f'  {DIM}Both channels connected to the same Chrome instance.{RESET}')
    print(f'  {DIM}Iterations: {ITERATIONS} per operation (+ {WARMUP} warmup){RESET}')
    print(f'  {DIM}Pages: {", ".join(name for name, _ in TEST_PAGES)}{RESET}')
    print(f'  {DIM}Operations: {len(OPERATIONS)}{RESET}')

    chrome_proc = None
    cdp: CDPClient | None = None
    pw_instance = None

    try:
        # ── Setup: Launch Chrome ──
        phase('Setup: Launch Chrome & connect both channels')

        chrome_proc, ws_url = await launch_chrome(port=9222)
        ok(f'Chrome started (PID {chrome_proc.pid})')

        # Connect Playwright first (before CDP, to avoid HTTP endpoint conflicts)
        pw_instance = await async_playwright().start()
        browser = await pw_instance.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        pw_page = context.pages[0]
        ok('Playwright channel ready (connect_over_cdp via WS)')

        # Connect raw CDP to same browser
        cdp = CDPClient(ws_url)
        await cdp.connect()

        targets = await cdp.send('Target.getTargets')
        pages = [t for t in targets.get('targetInfos', []) if t['type'] == 'page']
        target_id = pages[0]['targetId']
        attach = await cdp.send(
            'Target.attachToTarget',
            {'targetId': target_id, 'flatten': True},
        )
        session_id = attach['sessionId']

        for domain in ['Page', 'DOM', 'Runtime', 'DOMSnapshot', 'Accessibility']:
            await cdp.send(f'{domain}.enable', session_id=session_id)
        ok('CDP channel ready (WebSocket + domains enabled)')

        # Create CDP session through Playwright for ops without native PW API
        pw_cdp_session = await context.new_cdp_session(pw_page)
        await pw_cdp_session.send('Accessibility.enable')
        await pw_cdp_session.send('DOMSnapshot.enable')

        # Create benchmark runners
        cdp_bench = CDPBench(cdp, session_id)
        pw_bench = PWBench(pw_page, cdp_session=pw_cdp_session)

        all_results: list[OpTiming] = []
        bench_start = time.perf_counter_ns()

        # ── Per-site benchmarks ──
        for site_name, url in TEST_PAGES:
            phase(f'Benchmarking: {site_name}')

            # Navigate via Playwright (both channels see the update)
            info(f'Navigating to {url}...')
            try:
                await pw_page.goto(url, wait_until='networkidle', timeout=30000)
            except Exception:
                await pw_page.goto(url, wait_until='load', timeout=30000)
            await asyncio.sleep(0.5)  # settle
            dom_info = await pw_page.evaluate(JS_DOM_STATS)
            ok(f'Page loaded — {dom_info["nodeCount"]} nodes, depth {dom_info["maxDepth"]}')

            for idx, (op_name, _op_desc, _op_notes) in enumerate(OPERATIONS):
                cdp_op = getattr(cdp_bench, op_name, None)
                pw_op = getattr(pw_bench, op_name, None)
                if not cdp_op or not pw_op:
                    warn(f'Skipping {op_name}: method not found')
                    continue

                # Alternate order to avoid systematic cache-warming bias
                if idx % 2 == 0:
                    cdp_timing = await bench_op(op_name, 'cdp', site_name, cdp_op)
                    pw_timing = await bench_op(op_name, 'pw', site_name, pw_op)
                else:
                    pw_timing = await bench_op(op_name, 'pw', site_name, pw_op)
                    cdp_timing = await bench_op(op_name, 'cdp', site_name, cdp_op)

                all_results.extend([cdp_timing, pw_timing])

                # Quick inline summary
                if cdp_timing.error:
                    warn(f'{op_name}: CDP error — {cdp_timing.error[:60]}')
                elif pw_timing.error:
                    warn(f'{op_name}: PW error — {pw_timing.error[:60]}  (CDP={fmt(cdp_timing.mean_us)})')
                else:
                    ratio = pw_timing.mean_us / cdp_timing.mean_us if cdp_timing.mean_us > 0 else 0
                    ok(
                        f'{op_name:<22} CDP={fmt(cdp_timing.mean_us):>8}'
                        f'  PW={fmt(pw_timing.mean_us):>8}  ratio={ratio:.2f}x'
                    )

        bench_elapsed = (time.perf_counter_ns() - bench_start) / 1_000_000_000.0

        # ── Reports ──

        for site_name, _ in TEST_PAGES:
            banner(f'Comparison: {site_name}')
            print_comparison(all_results, site_name)

        banner('Detailed Statistics (all sites)')
        print_detailed_stats(all_results)

        banner('Grand Total')
        print_grand_total(all_results)

        print(f'\n  {DIM}Benchmark completed in {bench_elapsed:.1f}s{RESET}\n')

    except Exception:
        logging.exception('Benchmark failed')
        raise
    finally:
        if pw_instance:
            await pw_instance.stop()
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

    asyncio.run(run_benchmark())


if __name__ == '__main__':
    main()
