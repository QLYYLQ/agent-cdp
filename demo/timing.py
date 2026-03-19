"""Timing infrastructure for agent-cdp performance benchmarks.

Provides nanosecond-precision measurement of:
- Framework overhead (emit dispatch, connection resolution, event construction)
- Direct handler latency (synchronous in emit call stack)
- Queued handler latency (enqueue → dequeue → execute → complete)
- CDP command round-trips
- End-to-end operations (navigation, screenshot, popup handling)
"""

import time
from collections import defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class TimingRecord:
    label: str
    category: str  # framework | direct | queued | cdp | e2e
    duration_us: float  # microseconds
    site: str = ''


class TimingCollector:
    """Collects timing records and generates formatted reports."""

    def __init__(self) -> None:
        self.records: list[TimingRecord] = []

    @contextmanager
    def measure(self, label: str, category: str, site: str = '') -> Generator[None, None, None]:
        t0 = time.perf_counter_ns()
        yield
        elapsed_us = (time.perf_counter_ns() - t0) / 1000.0
        self.records.append(TimingRecord(label, category, elapsed_us, site))

    def add(self, label: str, category: str, duration_us: float, site: str = '') -> None:
        self.records.append(TimingRecord(label, category, duration_us, site))

    def format_duration(self, us: float) -> str:
        """Format microseconds into human-readable string."""
        if us < 1000:
            return f'{us:>8.1f} us'
        elif us < 1_000_000:
            return f'{us / 1000:>8.2f} ms'
        else:
            return f'{us / 1_000_000:>8.2f} s '

    def site_report(self, site: str) -> str:
        """Generate timing report for a single site."""
        records = [r for r in self.records if r.site == site]
        if not records:
            return f'  No records for {site}'

        lines: list[str] = []
        max_label = max(len(r.label) for r in records)
        col_w = max(max_label, 28)

        for r in records:
            dur = self.format_duration(r.duration_us)
            cat = f'[{r.category}]'
            lines.append(f'  {r.label:<{col_w}}  {dur}  {cat}')

        return '\n'.join(lines)

    def framework_report(self) -> str:
        """Generate aggregated report for framework overhead measurements."""
        records = [r for r in self.records if r.site == '__framework__']
        if not records:
            return '  No framework records'

        # Group by label and compute stats
        by_label: dict[str, list[float]] = defaultdict(list)
        for r in records:
            by_label[r.label].append(r.duration_us)

        lines: list[str] = []
        max_label = max(len(lbl) for lbl in by_label)
        col_w = max(max_label, 35)

        for label, values in by_label.items():
            avg = sum(values) / len(values)
            mn = min(values)
            mx = max(values)
            p50 = sorted(values)[len(values) // 2]
            lines.append(
                f'  {label:<{col_w}}  avg={self.format_duration(avg)}  '
                f'p50={self.format_duration(p50)}  min={self.format_duration(mn)}  '
                f'max={self.format_duration(mx)}'
            )

        return '\n'.join(lines)

    def summary_report(self) -> str:
        """Generate cross-site comparison summary."""
        by_category: dict[str, list[float]] = defaultdict(list)
        by_site: dict[str, float] = {}

        for r in self.records:
            if r.site == '__framework__':
                continue
            by_category[r.category].append(r.duration_us)
            if r.label == 'total (navigate+screenshot)':
                by_site[r.site] = r.duration_us

        lines: list[str] = []

        # Category averages
        for cat in ['framework', 'direct', 'queued', 'cdp', 'e2e']:
            vals = by_category.get(cat, [])
            if vals:
                avg = sum(vals) / len(vals)
                mn = min(vals)
                mx = max(vals)
                lines.append(
                    f'  {cat:<12}  avg={self.format_duration(avg)}  '
                    f'min={self.format_duration(mn)}  max={self.format_duration(mx)}  '
                    f'({len(vals)} samples)'
                )

        # Per-site totals
        if by_site:
            lines.append('')
            lines.append('  Per-site totals:')
            for site, total in by_site.items():
                lines.append(f'    {site:<25} {self.format_duration(total)}')

        return '\n'.join(lines)
