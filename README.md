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

### Race-free CDP setup with PausedTarget

```python
from agent_cdp.bridge import PausedTarget, CDPEventBridge

# Guarantee: all bridges + handlers are wired BEFORE the target resumes
async with PausedTarget(resume=my_resume_fn):
    bridge = CDPEventBridge(cdp, scope, session_id='sess-A')
    bridge.bridge('Page.loadEventFired', lambda p: PageLoadEvent(**p))
    bridge.bridge('Page.javascriptDialogOpening', lambda p: DialogEvent(**p))
    scope.connect(PageLoadEvent, on_load, mode=ConnectionType.DIRECT)
    scope.connect(DialogEvent, on_dialog, mode=ConnectionType.DIRECT, priority=100)
# resume called automatically — even if setup raises an exception

# Or use the convenience factory:
async with CDPEventBridge.paused(resume=my_resume_fn):
    ...
```

No more race conditions between CDP event registration and page loading. `PausedTarget` coordinates the Stagehand V3 `waitForDebuggerOnStart` → pause → setup → resume pattern. Resume is idempotent and exception-safe.

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
| Pre-resume hook | None | None | `PausedTarget` context manager |
| Event logging | JSONL WAL | None | JSONL EventLog + conscribe |
| Broadcast | Event forwarding (shared ref) | N/A | Deep-copy broadcast |
| Backpressure | Unbounded queue | N/A | Bounded queue (default 1024) |

## Performance

Benchmarked on real websites (Google, Xiaohongshu, Bilibili, reCAPTCHA Demo) with 100 iterations per operation, same Chrome instance, GC disabled during measurement.

### Framework overhead: negligible

| Operation | Avg latency | Notes |
|-----------|-------------|-------|
| `BaseEvent` construction | 6.7 us (p50) | Pydantic model + UUID7 |
| `emit()` zero handlers | 3.6 us (p50) | Connection resolution + empty loop |
| `emit()` 1 Direct no-op | 6.5 us (p50) | Includes handler call + result recording |
| `emit()` 5 Direct handlers | 16.7 us (p50) | Priority sorting + 5 handler invocations |
| `emit()` Direct + `consume()` | 5.9 us (p50) | Early exit on propagation stop |
| `emit()` 1 Queued (enqueue only) | 9.4 us (p50) | `queue.put_nowait()` + pending tracking |
| SecurityWatchdog (allowed URL) | 16.5 us (p50) | Real handler: URL parse + domain check |
| SecurityWatchdog (blocked + raise) | 15.2 us (p50) | `consume()` + exception propagation |

**Key insight:** agent-cdp framework overhead averages **43 us** per emit — **0.0017%** of end-to-end operation time (avg 2.54s). The bottleneck is always network I/O, never the event system.

### Raw CDP vs agent-cdp vs Playwright

Three channels connected to the same Chrome instance. Identical JavaScript executed through all channels to isolate pure automation-layer overhead.

- **Raw CDP** — direct WebSocket commands via minimal CDPClient
- **agent-cdp** — `emit(Event)` → Queued handler → CDP WebSocket → `await event_result()`
- **Playwright** — `page.evaluate()` / `page.screenshot()` / high-level API

| Operation | Raw CDP (p50) | agent-cdp (p50) | Playwright (p50) | acdp/CDP | PW/acdp |
|-----------|--------------|-----------------|------------------|----------|---------|
| JS evaluate (title) | 1.24 ms | 1.51 ms | 3.79 ms | 1.22x | **2.51x** |
| JS evaluate (links) | 1.41 ms | 1.38 ms | 3.43 ms | 0.98x | **2.49x** |
| DOM querySelector h1 | 3.78 ms | 4.71 ms | 13.44 ms | 1.25x | **2.85x** |
| DOM querySelectorAll a | 2.57 ms | 3.69 ms | 8.24 ms | 1.44x | **2.23x** |
| querySelectorAll (436-node page) | 5.26 ms | 5.16 ms | 88.60 ms | 0.98x | **17.17x** |
| Screenshot (PNG) | 48.21 ms | 47.01 ms | 66.31 ms | 0.98x | **1.41x** |
| DOMSnapshot + styles | 1.47 ms | 1.47 ms | 3.64 ms | 1.00x | **2.48x** |
| Accessibility tree | 2.05 ms | 2.10 ms | 4.27 ms | 1.02x | **2.03x** |
| Full cleaning pipeline (5 evals) | 7.17 ms | 8.02 ms | 18.47 ms | 1.12x | **2.30x** |

**acdp/CDP ≈ 1.0x** — agent-cdp's ~40us event dispatch overhead is negligible on millisecond-scale CDP operations.

**PW/acdp = 2–17x** — Playwright's protocol overhead (IPC, ElementHandle wrapping) compounds on every call.

| Category | Raw CDP | agent-cdp | Playwright | acdp/CDP | PW/acdp |
|----------|---------|-----------|------------|----------|---------|
| JS eval (5 ops) | 1.64s | 1.73s | 3.89s | 1.05x | 2.25x |
| DOM API (querySelector) | 1.64s | 1.90s | 12.26s | 1.16x | **6.45x** |
| Content (HTML) | 434 ms | 464 ms | 813 ms | 1.07x | 1.75x |
| Binary (screenshot) | 11.19s | 10.96s | 15.13s | 0.98x | 1.38x |
| Specialized (snapshot + a11y) | 1.75s | 1.89s | 3.90s | 1.08x | 2.06x |
| Pipeline (5-step cleaning) | 1.76s | 1.56s | 3.68s | 0.89x | 2.36x |

DOM API operations show the largest gap because Playwright wraps each element in an `ElementHandle` with IPC overhead, while agent-cdp (like raw CDP) operates on raw `nodeId` integers.

Reproduce: `uv run python -m demo.bench_cdp_vs_pw` (Raw CDP vs PW) and `uv run python -m demo.bench_agentcdp_vs_pw` (agent-cdp vs PW)

### Real-website benchmark: agent-cdp vs Playwright

Tested on production websites with complex DOM structures. 50 iterations per operation, same Chrome instance.

| Site | DOM nodes | Depth | agent-cdp total | PW total | PW/acdp |
|------|-----------|-------|-----------------|----------|---------|
| **Amazon** | 5014 | 27 | 33.73s | 75.67s | 2.24x |
| **Xiaohongshu** | 2356 | 28 | 27.93s | 34.37s | 1.23x |
| **Bilibili** | 2650 | 21 | 21.64s | 37.27s | 1.72x |
| **Google** | 594 | 19 | 4.02s | 16.67s | 4.15x |

Per-operation breakdown (p50 values):

| Operation | Amazon acdp | Amazon PW | XHS acdp | XHS PW | Bilibili acdp | Bilibili PW | Google acdp | Google PW |
|-----------|-------------|-----------|----------|--------|---------------|-------------|-------------|-----------|
| get_html | 104 ms | 126 ms | 66 ms | 81 ms | 33 ms | 40 ms | 26 ms | 34 ms |
| eval_title | 1.8 ms | 3.9 ms | 2.0 ms | 4.0 ms | 2.0 ms | 4.4 ms | 1.8 ms | 4.0 ms |
| eval_links | 6.3 ms | 16.2 ms | 2.1 ms | 7.7 ms | 3.8 ms | 9.7 ms | 2.2 ms | 4.4 ms |
| eval_dom_stats | 3.2 ms | 5.7 ms | 2.7 ms | 5.8 ms | 3.2 ms | 5.9 ms | 1.8 ms | 3.5 ms |
| eval_interactive | 7.4 ms | 24.1 ms | 3.2 ms | 8.9 ms | 4.5 ms | 11.1 ms | 2.4 ms | 6.0 ms |
| query_h1 | 4.0 ms | 4.5 ms | 3.1 ms | 4.4 ms | 3.3 ms | 4.7 ms | 3.2 ms | 4.4 ms |
| querySelectorAll a | 30.6 ms | **505 ms** | 11.4 ms | **171 ms** | 11.3 ms | **226 ms** | 5.8 ms | **31.8 ms** |
| screenshot | 159 ms | 218 ms | 294 ms | 311 ms | 296 ms | 346 ms | 48 ms | 70 ms |
| dom_snapshot | 72 ms | 127 ms | 45 ms | 67 ms | 24 ms | 45 ms | 16 ms | 25 ms |
| accessibility_tree | 66 ms | 154 ms | 18 ms | 38 ms | 21 ms | 45 ms | 5.0 ms | 10 ms |
| cleaning_pipeline | 121 ms | 173 ms | 69 ms | 96 ms | 44 ms | 73 ms | 34 ms | 50 ms |

Category totals across all 4 real websites:

| Category | agent-cdp | Playwright | PW/acdp |
|----------|-----------|------------|---------|
| JS eval (5 ops) | 3.22s | 7.35s | **2.28x** |
| DOM API (querySelector) | 3.91s | 49.05s | **12.56x** |
| Content (HTML) | 11.73s | 14.33s | 1.22x |
| Binary (screenshot) | 40.37s | 47.80s | 1.18x |
| Specialized (snapshot + a11y) | 13.89s | 25.77s | **1.85x** |
| Pipeline (5-step cleaning) | 14.20s | 19.73s | 1.39x |

**Overall: Playwright is 1.88x slower than agent-cdp** (87.33s vs 164.03s total across 4 sites × 12 ops × 50 iterations).

The DOM API gap scales with page complexity — Amazon's 5014-node DOM produces a **16.6x** gap on `querySelectorAll`, while Google's 594-node DOM shows **4.8x**. This is because Playwright creates an `ElementHandle` object per element with round-trip IPC, while agent-cdp returns raw `nodeId` arrays in a single CDP response.

Reproduce: `uv run python -m demo.bench_agentcdp_vs_pw`

### Real-site watchdog latency

Tested on Google, Xiaohongshu, Bilibili, and reCAPTCHA Demo:

| Operation | Latency | Mode |
|-----------|---------|------|
| Security check (Direct handler) | 72–164 us | DIRECT, priority=100 |
| Popup auto-dismiss | 5.5–12.5 ms | CDP event → DIRECT handler |
| Screenshot (Queued handler) | 47–321 ms | QUEUED, depends on page complexity |
| CDP `Page.navigate` round-trip | 232–910 ms | Raw CDP command |
| Full page load (navigate + render) | 0.83–1.67 s | End-to-end |

Reproduce: `uv run python -m demo.bench`

### Multi-tab parallel benchmark (v0.4.0)

Browser-level CDP (single WebSocket, the same URL cloud providers give you) → 4 tabs × 5 watchdogs each (Security, Popups, Screenshot, Captcha, Crash). Real-world heavy-DOM sites.

#### Sequential vs Parallel

| Mode | Wall-clock | Details |
|------|-----------|---------|
| **Sequential** (one tab at a time) | 15.69s | Each tab waits for the previous |
| **Parallel** (all 4 tabs via `asyncio.gather`) | **6.78s** | All tabs navigate concurrently |
| **Parallelism speedup** | **2.52x** | Limited by slowest tab (Xiaohongshu) |

#### Per-site breakdown (5 watchdogs, browser-level CDP)

| Site | Security check | CDP navigate | Page load | Screenshot | Captcha scan | Total |
|------|---------------|-------------|-----------|------------|-------------|-------|
| Xiaohongshu | 97 us | 520 ms | 1.51s | 281 ms | 3.2 ms | 3.88s |
| Amazon | 87 us | 638 ms | 1.34s | 1.02s | 2.4 ms | 4.54s |
| Bilibili | 77 us | 207 ms | 943 ms | 240 ms | 3.3 ms | 2.94s |
| reCAPTCHA | 76 us | 632 ms | 2.03s | 68 ms | 2.8 ms | 4.33s |

Framework overhead: **0.0007%** of end-to-end time. The bottleneck is always network I/O, never the event system.

#### Tier 1 optimizations (v0.4.0)

| Optimization | Metric | Speedup |
|---|---|---|
| **Scope-level deadlock monitor** (replaces per-handler `asyncio.create_task`) | per-handler overhead | **23.5x** (7.6µs → 0.3µs) |
| **orjson** (replaces stdlib `json`) dumps screenshot 496KB | CDP serialization | **30.5x** (1.14ms → 37µs) |
| **orjson** dumps DOM 76KB | CDP serialization | **4.3x** (302µs → 71µs) |
| **orjson** loads DOM 76KB | CDP deserialization | **2.0x** (248µs → 122µs) |
| **MRO match cache** (ABC-inspired positive + negative + version counter) | emit storm 100x | 10-12µs/event (cached) |

Reproduce: `uv run python -m demo.bench_optimizations`

## Action dispatch: using agent-cdp as an agent action executor

agent-cdp is not only for browser→handler event flow. It works equally well for the **reverse direction**: agent→browser action dispatch with anti-detection, security gating, and result collection.

### Architecture

```
Agent LLM decides: "click #submit-btn"
  ↓
scope.emit(ClickAction(selector='#submit-btn'))
  ↓ handlers execute by priority
  ├─ [DIRECT p=100] security_check     → allowed? consume() + raise if not
  ├─ [QUEUED p=50]  stealth_executor   → bezier mouse trajectory + CDP Input
  └─ [QUEUED p=0]   audit_logger       → async log, doesn't block agent
  ↓
result = (await event_results_by_handler_name(event))['stealth_executor']
# → ClickResult(coords=(450, 320), trajectory_points=25)
```

### Defining action events

```python
from pydantic import BaseModel
from agent_cdp.events import BaseEvent, EmitPolicy

class ClickResult(BaseModel):
    coords: tuple[float, float]
    trajectory_points: int

class ClickAction(BaseEvent[ClickResult]):
    """BaseEvent[ClickResult] declares what handlers should return."""
    selector: str = ''
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST  # security failure stops chain
```

### Registering handlers

```python
from agent_cdp.connection import ConnectionType

# Security gate — DIRECT = runs synchronously inside emit()
scope.connect(ClickAction, security_check, mode=ConnectionType.DIRECT, priority=100)

# Anti-detection executor — QUEUED = async, can await CDP calls
scope.connect(ClickAction, stealth_click,  mode=ConnectionType.QUEUED, priority=50)

# Audit log — QUEUED, lowest priority, doesn't block agent
scope.connect(ClickAction, audit_logger,   mode=ConnectionType.QUEUED, priority=0)
```

### Emitting actions and collecting results

```python
event = scope.emit(ClickAction(selector='#submit-btn'))
# DIRECT handlers already executed (security check passed)

await event  # wait for QUEUED handlers to complete

# 4 ways to get results:
from agent_cdp.events import event_result, event_results_list, event_results_by_handler_name

# 1. First successful result
r = await event_result(event)

# 2. All results as list
all_r = await event_results_list(event)

# 3. By handler function name (most useful for action dispatch)
by_name = await event_results_by_handler_name(event)
click_result = by_name['stealth_click']  # → ClickResult(...)

# 4. By connection ID (most precise)
er = event.event_results[conn.id]
er.result       # ClickResult(...)
er.status       # ResultStatus.COMPLETED
er.handler_name # 'stealth_click'
er.error        # None
```

### Security gating with consume()

```python
def security_check(event: ClickAction) -> ClickResult:
    if event.selector in BLOCKED_SELECTORS:
        event.consume()  # prevents stealth_click and audit_logger from running
        raise SecurityViolation(f'Blocked: {event.selector}')
    return ClickResult(coords=(0, 0), trajectory_points=0)
```

When `consume()` is called, `emit()` breaks out of the handler loop. No subsequent handlers execute — the stealth executor never sends CDP commands, the audit logger never records. The exception propagates to the caller.

### Anti-detection mouse trajectory (real CDP)

```python
async def stealth_click(event: ClickAction) -> ClickResult:
    # 1. Get element coordinates via CDP
    rect = await cdp.evaluate(f'document.querySelector("{event.selector}").getBoundingClientRect()')

    # 2. Generate bezier curve trajectory
    trajectory = bezier_trajectory(current_pos, (rect.x, rect.y), steps=25)

    # 3. Send real mouse events via CDP Input domain
    for x, y in trajectory:
        await cdp.send('Input.dispatchMouseEvent', {
            'type': 'mouseMoved', 'x': x, 'y': y
        })
        await asyncio.sleep(random.uniform(0.005, 0.02))

    # 4. Click with human-like press/release timing
    await cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', ...})
    await asyncio.sleep(random.uniform(0.04, 0.10))
    await cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', ...})

    # 5. Return value is automatically recorded as the action result
    return ClickResult(coords=(rect.x, rect.y), trajectory_points=len(trajectory))
```

The handler's `return` value is automatically captured by agent-cdp into `event.event_results`. The agent retrieves it via `await event` + aggregation functions. No manual `record_result()` calls needed.

### MRO matching for base action types

```python
class BrowserAction(BaseEvent[ActionResult]):
    __abstract__ = True

class ClickAction(BrowserAction): ...
class TypeAction(BrowserAction): ...
class ScrollAction(BrowserAction): ...

# Register on base class — automatically matches all subclass events
scope.connect(BrowserAction, security_check, mode=ConnectionType.DIRECT, priority=100)
scope.connect(BrowserAction, audit_logger,   mode=ConnectionType.QUEUED, priority=0)

# Register specific executors per action type
scope.connect(ClickAction,  stealth_click,  mode=ConnectionType.QUEUED, priority=50)
scope.connect(TypeAction,   stealth_type,   mode=ConnectionType.QUEUED, priority=50)
scope.connect(ScrollAction, stealth_scroll, mode=ConnectionType.QUEUED, priority=50)
```

Demos: `demo_nano.py` (minimal 70 lines), `demo_feedback.py` (result collection), `demo_real_xhs.py` (real Chrome + xiaohongshu.com with stealth mouse trajectory)

## Scope architecture: real-world validation

Three demos validate agent-cdp's scope advantages with real Chrome and CDP. All results below are from actual runs, not simulated.

### Per-scope isolation (demo.main, demo.multi_tab)

```
Phase 7: Per-Tab Popup Isolation
  ✓ [Google] popup dismissed: "popup-isolation-test"
  ✓ Other tabs saw 0 popup events — per-scope isolation confirmed
```

A `Page.javascriptDialogOpening` CDP event on the Google tab triggers the popup handler **only on that tab's scope**. Bilibili, Xiaohongshu, and reCAPTCHA scopes see nothing — no event leaks across scopes.

### Concurrent dispatch: 15x speedup over sequential (demo.advanced)

```
Phase 6: Concurrent Dispatch — 5 scopes in parallel
  ✓ Sequential: 12192ms
  ✓ Concurrent: 813ms
  ✓ Speedup: 15.0x (bubus global queue → always sequential)
```

5 tabs navigate simultaneously. Each scope has its own event loop — no global queue serialization. bubus's single `asyncio.Queue` forces all tabs to wait in line.

### Auto-disconnect on scope close (demo.multi_tab, demo.advanced)

```
Phase 10: Tab Close → Auto-Disconnect
  ✓ tab-bilibili closed. Remaining: ['tab-google', 'tab-xiaohongshu', 'tab-recaptcha']
  ✓ Closed scope rejects emit: Cannot emit on closed scope 'tab-bilibili'
  ✓ [Google] still works after Bilibili closed (screenshot OK)
```

`scope.close()` stops the event loop, severs all outgoing and incoming connections, and releases handler references for GC. Remaining scopes continue operating normally.

### Fan-in / Fan-out / Cross-scope routing (demo.advanced)

| Pattern | What happens | bubus equivalent |
|---------|-------------|-----------------|
| **Fan-in (N:1)** | `connect_all_scopes`: 1 call → 6 connections | Not possible — single bus |
| **Fan-out (1:N)** | 1 event → 4 handlers (Direct + Queued + cross-scope) | Partial — all handlers always run |
| **Cross-scope** | Handler runs in monitor's event loop, not emitter's | Not possible |
| **Filters** | Handler skipped entirely when `filter=False` | Ad-hoc circuit breakers |
| **Broadcast** | Deep-copy to all scopes (315 us for 4 scopes) | Shared ref (mutation leaks) |

### CaptchaWatchdog: DOM-based detection (demo.multi_tab)

```
Phase 6: CaptchaWatchdog Detection
  ✓ reCAPTCHA DETECTED on demo page (2.03ms)
    vendor: recaptcha
    sitekey: 6Le-wvkSAAAAAPBMRTvw...
    elements: 3, challenge visible: True
  ✓ [Google] no captcha (correct)
  ✓ [Bilibili] no captcha (correct)
```

Tab-specific QUEUED watchdog inspects DOM via CDP `Runtime.evaluate` — fires only on the reCAPTCHA tab's scope, not on others.

### Parallel screenshots across tabs

```
Phase 9: Parallel Screenshots (all tabs)
  ✓ [Google] 33965B, [Bilibili] 1019642B, [Xiaohongshu] 914908B, [reCAPTCHA] 23714B
  ✓ All 4 screenshots captured in 658.99ms (parallel)
```

Reproduce all demos:

```bash
uv run python -m demo.main            # single-tab scope advantages
uv run python -m demo.multi_tab       # multi-tab isolation + captcha detection
uv run python -m demo.advanced        # 8 architectural advantages with 5 tabs
uv run python -m demo.paused_target   # PausedTarget race-free setup (Amazon + Xiaohongshu)
```

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
├── Direct handler timing monitor (>100ms warning)
├── CDPEventBridge (CDP → EventScope bridging)
├── CDPCommandProtocol (structural type for CDP clients)
└── PausedTarget (race-free pause → setup → resume coordination)
```

## Development

```bash
git clone https://github.com/QLYYLQ/agent-cdp.git
cd agent-cdp
uv sync
uv run pytest -vxs tests/          # run all tests (302 tests)
uv run ruff check --fix && uv run ruff format   # lint + format
uv run pyright                       # type check (strict mode)
```

## License

MIT
