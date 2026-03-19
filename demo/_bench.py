"""Shared benchmark utilities for demo bench scripts."""

from __future__ import annotations

import asyncio
import gc
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass
class OpTiming:
    """Per-iteration timings for one (operation, channel, site) combo."""

    name: str
    channel: str
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


async def bench_op(
    name: str,
    channel: str,
    site: str,
    op: Callable[[], Awaitable[Any]],
    warmup: int = 3,
    iters: int = 50,
) -> OpTiming:
    """Run op (warmup + iters) times, return timing data."""
    timing = OpTiming(name=name, channel=channel, site=site)

    for i in range(warmup):
        try:
            await asyncio.wait_for(op(), timeout=10.0)
        except Exception as e:
            timing.error = f'warmup iter {i}: {str(e)[:100]}'
            return timing

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


# ── Shared JavaScript snippets ───────────────────────────────

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


class PWBench:
    """Playwright operations via high-level API."""

    def __init__(self, page: Any, cdp_session: Any = None) -> None:
        self.page = page
        self.cdp_session = cdp_session

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

    async def query_h1(self) -> str:
        el = await self.page.query_selector('h1')
        if not el:
            return ''
        return await el.inner_html()

    async def query_all_links(self) -> int:
        els = await self.page.query_selector_all('a')
        return len(els)

    async def screenshot(self) -> int:
        data = await self.page.screenshot(type='png')
        return len(data)

    async def dom_snapshot(self) -> int:
        if not self.cdp_session:
            raise RuntimeError('No CDP session')
        r = await self.cdp_session.send(
            'DOMSnapshot.captureSnapshot',
            {'computedStyles': ['display', 'visibility', 'opacity']},
        )
        return len(r.get('documents', []))

    async def accessibility_tree(self) -> int:
        if not self.cdp_session:
            raise RuntimeError('No CDP session')
        r = await self.cdp_session.send('Accessibility.getFullAXTree')
        return len(r.get('nodes', []))

    async def cleaning_pipeline(self) -> dict[str, int]:
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
