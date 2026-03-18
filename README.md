# agent-cdp

**Scoped Event System for agent-era browser automation.**

A Qt-inspired, connection-based event framework designed for AI agent browser automation pipelines. Replaces the single-bus model (bubus/browser-use) with scoped, concurrent, priority-aware event dispatch — enabling multi-tab isolation, zero-latency security gating, and N:M connection topologies.

## Who is this for?

- **Browser automation framework authors** building AI agent pipelines (like browser-use, Skyvern, Agent-TARS) who need per-tab event isolation and concurrent dispatch
- **Multi-agent system developers** coordinating multiple AI agents operating on the same browser instance across different tabs
- **Watchdog/plugin authors** who need zero-latency Direct dispatch for security checks, popup dismissal, and crash recovery — without bypassing the event system
- **Anyone outgrowing bubus** or ad-hoc Playwright event handling in agent-driven browser scenarios

## Why agent-cdp?

### The problem with bubus in agent browser scenarios

[bubus](https://github.com/nicegui-dev/bubus) is the event bus behind [browser-use](https://github.com/browser-use/browser-use). It works well for simple single-agent flows, but has fundamental limitations when scaling to multi-agent, multi-tab scenarios:

| Limitation | Impact |
|-----------|--------|
| **Queued-only dispatch** | All events go through an asyncio queue. CDP events that need instant response (popups, crashes, downloads) are forced to **bypass bubus entirely** via direct CDP callbacks — creating a dual-track event system |
| **No propagation control** | Every handler always runs. A security watchdog can't `consume()` a navigation event to prevent subsequent handlers from executing — it can only `raise ValueError` as a hack |
| **No handler priority** | Handlers run in FIFO registration order. Security checks may execute *after* navigation has already started |
| **No per-tab isolation** | Single global event queue. All tabs share one dispatch path — no concurrent processing across tabs |
| **No auto-disconnect** | When a tab closes, its handlers remain registered unless manually cleaned up |
| **No event filters** | Circuit-breaker logic is duplicated in every single handler wrapper (15 watchdogs x N handlers) |

### The problem with raw Playwright events

Playwright provides low-level page events (`page.on('dialog')`, `page.on('response')`) but no structured event system for agent coordination:

- No event result aggregation (multiple handlers contributing partial state)
- No priority-based handler ordering
- No cross-tab event routing or fan-in/fan-out topologies
- No awaitable events with timeout and deadlock detection
- No event history or audit logging
- Building agent watchdog coordination on top of Playwright events means reinventing most of what agent-cdp provides

### What agent-cdp provides

agent-cdp combines the best of Qt's connection topology with bubus's domain-specific event model:

```
Source (EventScope) ──connect()──→ Handler    (Qt-style N:M connections)
  + per-Scope EventLoop for ordering           (bubus-style queued processing)
```

## Features

### Direct + Queued + Auto dispatch

```python
from agent_cdp.connection import connect, ConnectionType

# Direct: zero-latency, runs in emit() call stack (sync)
connect(tab, NavigateToUrlEvent, security_check, mode=ConnectionType.DIRECT, priority=100)

# Queued: async, runs in scope's event loop
connect(tab, NavigateToUrlEvent, dom_rebuild, mode=ConnectionType.QUEUED, priority=0)

# Auto: same-scope → Direct, cross-scope → Queued
connect(tab, CrashEvent, crash_handler, mode=ConnectionType.AUTO)
```

No more bypassing the event system for time-critical handlers. Popup dismissal, crash recovery, and security checks all go through the same connection mechanism.

### Event propagation control

```python
def security_check(event: NavigateToUrlEvent) -> None:
    if not is_allowed(event.url):
        event.consume()  # stop propagation — navigation handler never runs
        raise NavigationBlocked(event.url)

connect(tab, NavigateToUrlEvent, security_check, mode=ConnectionType.DIRECT, priority=100)
connect(tab, NavigateToUrlEvent, do_navigate, mode=ConnectionType.DIRECT, priority=50)
```

High-priority Direct handler blocks the event *before* navigation starts — not after (as in bubus's "navigate then redirect to about:blank" pattern).

### Per-scope isolation with concurrent dispatch

```python
from agent_cdp.scope import EventScope, ScopeGroup

group = ScopeGroup('browser')
tab1 = await group.create_scope('tab-1', target_id='...')
tab2 = await group.create_scope('tab-2', target_id='...')

# Each scope has its own event loop — true concurrent processing
tab1.emit(NavigateToUrlEvent(url='https://site-a.com'))
tab2.emit(NavigateToUrlEvent(url='https://site-b.com'))
# Both process independently, no global queue bottleneck
```

### N:M connection topology

```python
# Fan-out: one source → many handlers
connect(tab1, NavEvent, security.check, mode=DIRECT, priority=100)
connect(tab1, NavEvent, dom.rebuild,    mode=QUEUED, priority=0)
connect(tab1, NavEvent, har.record,     mode=QUEUED, priority=-10)

# Fan-in: many sources → one handler
connect(tab1, NavEvent, monitor.on_nav, mode=QUEUED)
connect(tab2, NavEvent, monitor.on_nav, mode=QUEUED)
connect(tab3, NavEvent, monitor.on_nav, mode=QUEUED)

# Broadcast to all scopes
group.broadcast(CrashEvent(message='Chrome crashed'))
```

### Generic typed events with result aggregation

```python
from agent_cdp.events import BaseEvent, event_result, event_results_flat_dict

class ScreenshotEvent(BaseEvent[str]):  # result type = str (base64)
    full_page: bool = False

# Multiple handlers contribute partial state
event = tab.emit(BrowserStateRequestEvent())
await event  # wait for all handlers (Direct + Queued)
state = await event_results_flat_dict(event)
# {'dom_tree': ..., 'screenshot': ..., 'downloads': [...]}
```

Six aggregation modes: `event_result`, `event_results_flat_dict`, `event_results_flat_list`, `event_results_by_handler_name`, `event_results_list`, `event_results_filtered`.

### Auto-disconnect on scope close

```python
await group.close_scope('tab-1')
# → Event loop stopped
# → All outgoing connections severed (other scopes stop receiving)
# → All incoming connections severed (this scope stops receiving)
# → Handler references released for GC
```

No manual cleanup. No leaked handlers accumulating over browser reconnects.

### Connection-level event filters

```python
# Circuit breaker — one definition, applies to all handlers
tab.connect_all(circuit_breaker,
    mode=ConnectionType.DIRECT, priority=1000,
    filter=lambda e: type(e).__name__ not in LIFECYCLE_EVENTS)
```

Replace bubus's per-handler duplicated circuit-breaker wrappers with a single connection-level filter.

### Awaitable events + expect()

```python
from agent_cdp.advanced import expect

# Events are awaitables — emit returns immediately, await for completion
event = tab.emit(NavigateToUrlEvent(url='https://example.com'))
await event  # waits for all Queued handlers

# Declarative future event waiting
complete = await expect(
    tab, NavigationCompleteEvent,
    include=lambda e: e.url == 'https://example.com',
    timeout=30.0,
)
```

### Event logging with conscribe deserialization

```python
from agent_cdp.advanced import EventLogWriter

writer = EventLogWriter(path='events.jsonl')
# Append completed events as JSONL with full type preservation
# Deserialize back using conscribe discriminated unions
```

## Comparison

| Capability | bubus | Playwright | agent-cdp |
|-----------|-------|-----------|-----------|
| Dispatch modes | Queued only | N/A | Direct / Queued / Auto |
| Propagation control | None | None | `event.consume()` |
| Handler priority | FIFO order | N/A | Integer priority |
| Per-tab isolation | Shared queue | Per-page events | Per-scope event loops |
| Concurrent dispatch | Global lock | N/A | Independent per-scope |
| Connection topology | N:1:M (central bus) | 1:N (page events) | N:M (direct connections) |
| Auto-disconnect | None | Page close removes listeners | `scope.close()` severs all |
| Event filters | Ad-hoc circuit breakers | None | Connection-level `filter` |
| Result aggregation | 6 modes | None | 6 modes (preserved) |
| Typed events | `BaseEvent[T]` | Untyped | `BaseEvent[T]` (preserved) |
| Event awaiting | `await event` | Callbacks | `await event` + `expect()` |
| Handler timeout | Per-handler | None | Per-handler + deadlock detection |
| Event logging | JSONL WAL | None | JSONL EventLog + conscribe |
| Broadcast | Event forwarding (shared ref) | N/A | Deep-copy broadcast |
| Backpressure | Unbounded queue | N/A | Bounded queue (default 1024) |

## Installation

```bash
pip install agent-cdp
```

Requires Python >= 3.11.

## Quick start

```python
import asyncio
from agent_cdp.events import BaseEvent
from agent_cdp.connection import connect, ConnectionType
from agent_cdp.scope import EventScope, ScopeGroup

# Define events
class NavigateEvent(BaseEvent[str]):
    url: str

class PageLoadedEvent(BaseEvent[None]):
    url: str

# Create scopes
group = ScopeGroup('browser')

async def main():
    tab = await group.create_scope('tab-1')

    # Direct handler: security check runs synchronously in emit()
    def security_check(event: NavigateEvent) -> None:
        if 'evil.com' in event.url:
            event.consume()
            raise ValueError(f'Blocked: {event.url}')

    # Queued handler: async navigation
    async def do_navigate(event: NavigateEvent) -> str:
        # ... perform CDP navigation ...
        return f'navigated to {event.url}'

    connect(tab, NavigateEvent, security_check, mode=ConnectionType.DIRECT, priority=100)
    connect(tab, NavigateEvent, do_navigate, mode=ConnectionType.QUEUED, priority=0)

    # Blocked — security_check consumes the event
    try:
        tab.emit(NavigateEvent(url='https://evil.com'))
    except ValueError as e:
        print(f'Blocked: {e}')

    # Allowed — flows through security check, then queued navigation
    event = tab.emit(NavigateEvent(url='https://example.com'))
    await event  # wait for queued handler

    await group.close_all()

asyncio.run(main())
```

## Architecture

```
From Qt:
├── ConnectionType (Direct / Queued / Auto)
├── N:M connection topology (connect / disconnect)
├── Event propagation control (consume)
├── Handler priority (integer ordering)
├── Auto-disconnect (scope.close)
└── EmitPolicy (FAIL_FAST / COLLECT_ERRORS)

From bubus:
├── BaseEvent[T] generic typed events
├── 6 result aggregation modes
├── Awaitable events (await event)
├── expect() declarative future event waiting
├── Parent-child event tracing (event_parent_id)
├── Per-handler timeout + deadlock detection
└── EventLog persistence (JSONL)

New in agent-cdp:
├── EventScope (isolated event processing domain)
├── ScopeGroup (lifecycle management + broadcast)
├── Per-scope event loops (no global lock)
├── Deep-copy broadcast
├── MRO-based event matching
├── connect_all() catch-all
├── Backpressure control (bounded queue, drop-newest)
└── Direct handler timing monitor (>100ms warning)
```

## Development

```bash
git clone https://github.com/QLYYLQ/agent-cdp.git
cd agent-cdp
uv sync
uv run pytest -vxs tests/          # run all tests (185 tests)
uv run ruff check --fix && uv run ruff format   # lint + format
uv run pyright                       # type check (strict mode)
```

## License

MIT
