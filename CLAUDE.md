# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repo is building a new **Scoped Event System** for agent-era browser automation. The research phase (studying bubus/browser-use/Qt) is complete; this is now an active development workspace.

The core insight: bubus (the event bus used by browser-use) has fundamental limitations for multi-agent browser scenarios — no Direct dispatch, no event propagation control, no per-tab isolation, no concurrent dispatch across scopes. This project designs and implements a replacement that borrows from both bubus's event model and Qt's connection topology.

## Repository Layout

```
analysis/                    # Design documents (read these first for context)
├── bubus_design_gaps_in_agent_browser_scenario.md   # 8 architectural gaps identified
└── proposal_scoped_eventbus.md                      # Full API design for new system

qt_ref/qt/                   # 16 Qt/GObject reference docs for design rationale
bu_ref/                      # Reference copies of bubus/cdp-use/browser-use (gitignored)

pyproject.toml               # Workspace root (uv, Python >=3.11)
```

`bu_ref/` contains the original libraries for reference only — it is gitignored and not part of the project's source.

## Development Commands

```bash
uv sync                                        # install dependencies
uv run pytest -vxs tests/                       # all tests
uv run pytest -vxs tests/test_foo.py            # single test file
uv run pytest -vxs tests/test_foo.py::test_bar  # single test
uv run ruff check --fix && uv run ruff format   # lint + format
uv run pyright                                  # type check
```

## Architecture: Scoped Event System

The new system replaces bubus's single-bus model with a Qt-inspired connection topology. Full API design is in `analysis/proposal_scoped_eventbus.md`.

### Core Concepts

```
Source (EventScope) ──connect()──→ Handler    (Qt-style N:M connections)
  + per-Scope EventLoop for ordering           (bubus-style queued processing)
```

**Four key primitives:**

| Concept | Role |
|---------|------|
| `ConnectionType` | `DIRECT` (sync inline), `QUEUED` (async queue), `AUTO` (same-scope→Direct, cross-scope→Queued) |
| `Connection` | Explicit link between source scope + event type → handler. Has priority, optional filter, disconnect support |
| `EventScope` | Isolated event processing domain with its own event loop. Maps to a browser tab, a monitoring channel, etc. |
| `ScopeGroup` | Manages multiple scopes. Provides broadcast (global events to all scopes) and connect_all convenience |
| `CDPEventBridge` | Bridges CDP events into an EventScope via event factories. Session-ID filtering, auto-cleanup on close |
| `CDPCommandProtocol` | Structural type extending `CDPClientProtocol` with `async send()` for CDP command dispatch |
| `PausedTarget` | Async context manager for race-free pause → setup → resume coordination (Stagehand V3 pattern) |

### Key Design Decisions

1. **Connection is first-class** — not implicit bus subscription. Supports fan-out (one source → many handlers), fan-in (many sources → one handler), and full N:M.

2. **Direct mode enables zero-latency handlers** — security checks, popup dismissal, crash response run synchronously in the emit() call stack. This eliminates the "dual-track" problem where browser-use watchdogs bypass bubus via direct CDP callbacks.

3. **Per-scope event loops are independent asyncio Tasks** — different scopes dispatch concurrently without a global lock. No shared queue bottleneck.

4. **Event propagation control via `event.consume()`** — a handler can stop propagation to lower-priority handlers. Combined with priority ordering, this enables proper security gating (high-priority Direct handler blocks navigation before it happens).

5. **Auto-disconnect on scope close** — when a scope is closed, all connections (both outgoing and incoming) are automatically severed. This is the Qt `QObject` destruction guarantee that bubus lacks.

6. **BaseEvent remains generic over result type** — preserves bubus's result aggregation (flat dict, list, by handler ID) which is a genuine strength.

### Design Gaps Addressed (from bubus analysis)

| Gap | bubus Behavior | New System |
|-----|---------------|------------|
| Dispatch modes | Queued only | Direct / Queued / Auto |
| Propagation control | None (all handlers always run) | `event.consume()` stops propagation |
| Event filters | Ad-hoc (`_would_create_loop`, circuit breaker) | Connection-level `filter` callable |
| Auto-disconnect | None | Scope.close() severs all connections |
| Handler priority | FIFO registration order | Integer priority on Connection |
| Concurrency | Global single queue | Per-scope independent event loops |
| Connection topology | N:1:M via central bus | N:M direct connections |
| Pre-resume hook | None | `PausedTarget` context manager |

## Reference Materials

### Qt/GObject References (`qt_ref/qt/`)

16 documents mapping Qt concepts to bubus/browser-use equivalents. Key files:

- `01_signals_and_slots.md` — Core signal/slot mechanism → `EventBus.on() + dispatch()`
- `02_connection_type_enum.md` — 6 ConnectionType values → bubus has only Queued
- `04_event_system.md` — accept/ignore propagation → bubus has none
- `06_send_post_event.md` — sendEvent (sync) vs postEvent (queue) → bubus only has queue
- `10_event_filter.md` — installEventFilter chain → watchdog circuit breaker (ad-hoc)
- `16_gobject_signals.md` — 6-phase emission + accumulator → bubus `event_results` aggregation

### Original Libraries (`bu_ref/`, gitignored)

| Package | Version | Key files for reference |
|---------|---------|----------------------|
| bubus | 1.5.6 | `bubus/eventbus.py` (EventBus core), `bubus/event.py` (BaseEvent) |
| cdp-use | 1.4.5 | `cdp_use/client.py` (WebSocket CDP client) |
| browser-use | 0.12.2 | `browser_use/browser/session.py` (BrowserSession hub), `browser_use/browser/watchdog_base.py` (BaseWatchdog pattern), `browser_use/browser/events.py` (event definitions) |

browser-use has its own `CLAUDE.md` at `bu_ref/browser-use/CLAUDE.md` with detailed per-package conventions.

## Code Style

- Python >=3.11, managed with `uv`
- Single quotes, ruff for lint+format
- Pydantic models for data structures
- Type hints throughout (pyright strict)
- asyncio for async operations
