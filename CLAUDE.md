# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repo is an exploration workspace for browser event-driven design (browser-use edition). It contains three independent but interconnected libraries that together form a layered browser automation stack:

```
Agent (browser-use)  ←→  EventBus (bubus)  ←→  CDPClient (cdp-use)  ←→  Chrome WebSocket
```

The goal is to study how browser-use implements event-driven coordination across ~15 watchdog services, identify architectural strengths and gaps, and experiment with improvements.

## Repository Layout

| Directory | Package | Description |
|-----------|---------|-------------|
| `bubus/` | `bubus==1.5.6` | Pydantic-powered async event bus (pub/sub framework) |
| `cdp-use/` | `cdp-use==1.4.5` | Auto-generated type-safe Python bindings for Chrome DevTools Protocol |
| `browser-use/` | `browser-use==0.12.2` | AI browser automation agent that wires the above two together |

Each is a standalone `uv`-managed Python project with its own `.venv`, `pyproject.toml`, and test suite. For local development where browser-use depends on local bubus/cdp-use, uncomment `[tool.uv.sources]` in `browser-use/pyproject.toml`.

## Development Commands

All three use `uv` (Python >=3.11). Run commands from each package's directory.

### bubus

```bash
cd bubus
uv sync
uv run pytest -vxs tests/                    # all tests
uv run pytest -vxs tests/test_eventbus.py     # single test file
uv run ruff check --fix && uv run ruff format # lint + format
uv run pyright                                # type check (strict mode)
```

### cdp-use

```bash
cd cdp-use
uv sync
uv run python -m cdp_use.generator            # regenerate CDP types from protocol JSON
uv run ruff check cdp_use/ --statistics       # lint
uv run ruff format cdp_use/                   # format
# Or use: task generate / task lint / task format (Taskfile.yml)
```

### browser-use

```bash
cd browser-use
uv sync
uv run pytest -vxs tests/ci                   # CI test suite (default set)
uv run pytest -vxs tests/ci/test_foo.py       # single test
uv run ruff check --fix && uv run ruff format # lint + format
uv run pyright                                # type check (basic mode)
uv run pre-commit run --all-files             # all pre-commit hooks
```

## Architecture

### Layer 1: bubus — Event Bus

Core primitive. Provides `EventBus` and `BaseEvent[T_EventResultType]` (generic over return type).

Key capabilities: async dispatch with FIFO queue, `expect()` to await future events, event result aggregation (flat dict / list / by handler ID), handler timeouts, retry decorator with semaphore-based concurrency, event forwarding between bus instances (with loop prevention), Write-Ahead Logging for persistence, child/nested events with parent tracking.

### Layer 2: cdp-use — CDP Client

Thin typed wrapper over Chrome DevTools Protocol WebSocket. Two main pieces:

1. **Generator** (`cdp_use/generator/`) — downloads Chrome protocol JSON specs and generates Python TypedDict classes for all 50+ CDP domains (Page, Runtime, DOM, Network, Target, etc.)
2. **Client** (`cdp_use/client.py`) — WebSocket client exposing `cdp.send.Domain.method(params={...})` and `cdp.register.Domain.eventName(callback)` with full type safety

No event registration via `cdp.on(...)` — only `cdp.register.Domain.event(callback)`.

### Layer 3: browser-use — Agent + Watchdogs

The orchestration layer. Key architectural patterns:

**Service/Views pattern**: each component has `service.py` (logic) and `views.py` (Pydantic models).

**BrowserSession** (`browser_use/browser/session.py`) is the hub — manages CDP connections, tab lifecycle, and coordinates watchdogs through a central `EventBus`.

**Watchdog pattern** (`browser_use/browser/watchdog_base.py`): Each watchdog is a `BaseWatchdog` subclass (Pydantic model) that declares `LISTENS_TO` and `EMITS` class vars. Handlers auto-register by naming convention: `on_{EventClassName}(self, event)`.

15 watchdogs in `browser_use/browser/watchdogs/`:
- `dom_watchdog` — DOM snapshots, element highlighting, accessibility tree
- `screenshot_watchdog` — screenshot capture
- `downloads_watchdog` — file downloads, PDF auto-download
- `popups_watchdog` — JS dialogs
- `security_watchdog` — domain restrictions
- `aboutblank_watchdog` — empty page handling
- `local_browser_watchdog` — local Chrome process lifecycle
- `permissions_watchdog` — browser permissions
- `crash_watchdog` — target crash monitoring
- `storage_state_watchdog` — cookie/localStorage persistence
- `captcha_watchdog` — CAPTCHA solver integration
- `har_recording_watchdog` — network recording (HAR)
- `recording_watchdog` — browser automation recording
- `default_action_watchdog` — default action handlers

**Events** (`browser_use/browser/events.py`): All events inherit `BaseEvent[T]`. Two categories:
- Action events (Agent → Browser): `NavigateToUrlEvent`, `ClickElementEvent`, `TypeTextEvent`, `ScreenshotEvent`, `BrowserStartEvent`, etc.
- Notification events (Browser → Agent/Watchdogs): `BrowserConnectedEvent`, `TabCreatedEvent`, `NavigationCompleteEvent`, `DownloadStartedEvent`, `DialogOpenedEvent`, etc.

Event timeouts are configurable via environment variables (`TIMEOUT_NavigateToUrlEvent`, etc.).

## Code Style Differences

| | bubus | cdp-use | browser-use |
|---|---|---|---|
| Indent | spaces | (generated) | **tabs** |
| Quotes | single | — | single |
| pyright | strict | — | basic |
| Line length | 130 | — | 130 |

browser-use uses **tabs** for indentation. bubus uses **spaces**. Both use single quotes and ruff for linting/formatting.

## Testing Conventions (browser-use)

- Tests that pass go into `tests/ci/` — this is the CI-discovered default set
- **No mocking** except for LLM responses (use `conftest.py` fixtures for LLM mocking)
- Use `pytest-httpserver` for all test HTML/responses — never use real remote URLs
- Modern pytest-asyncio: no `@pytest.mark.asyncio` decorator needed, just write `async def test_*()`
- `asyncio_mode = "auto"` with session-scoped event loop (browser-use) or function-scoped (bubus)

## Key Design Decisions to Be Aware Of

1. **Shared state belongs on BrowserSession, not on individual watchdogs.** Watchdogs expose state/helpers via events if other watchdogs need access.
2. **CDP event forwarding**: BrowserSession forwards raw CDP events to watchdogs through the EventBus, not direct CDP callbacks.
3. **BaseEvent is generic over result type** (`BaseEvent[T_EventResultType]`) — dispatchers can aggregate results from multiple handlers via `event.event_result()`, `event.event_results_list()`, etc.
4. **Lazy imports** in `browser_use/__init__.py` for startup performance.
5. **browser-use has its own CLAUDE.md** (`browser-use/CLAUDE.md`) with detailed per-package conventions — read it when working inside browser-use specifically.

## Qt Reference Materials

`references/qt/` contains 16 Qt/GObject reference documents for systematic comparison with bubus/browser-use's event-driven architecture. The full index with source URLs, per-file rationale, and bubus architecture summary is in `references/qt/README.md`.

### Reference File Map

| # | File | Topic | Compared Against (bubus/browser-use) |
|---|------|-------|--------------------------------------|
| 01 | `01_signals_and_slots.md` | Qt Signal/Slot core mechanism | `EventBus.on()` + `dispatch()` |
| 02 | `02_connection_type_enum.md` | 6 ConnectionType values (Auto/Direct/Queued/BlockingQueued/Unique/SingleShot) | bubus has only Queued mode |
| 03 | `03_qobject_connect.md` | connect/disconnect overloads, lifecycle, auto-disconnect on destruction | bubus lacks auto-disconnect |
| 04 | `04_event_system.md` | Event propagation chain: accept/ignore, event filters | bubus has no accept/ignore |
| 05 | `05_qevent.md` | QEvent class, 100+ event types, accept()/ignore() | `BaseEvent[T]` |
| 06 | `06_send_post_event.md` | sendEvent (sync) vs postEvent (queue) + priority | bubus only has queue (postEvent) |
| 07 | `07_threads_and_qobjects.md` | Thread affinity, cross-thread auto-Queued | bubus is single-threaded asyncio |
| 08 | `08_qeventloop.md` | Nested event loops, processEvents | bubus `inside_handler_context` polling |
| 09 | `09_qthread.md` | QThread, moveToThread, Worker pattern | N/A (future multi-thread consideration) |
| 10 | `10_event_filter.md` | installEventFilter chain, return true to stop propagation | watchdog circuit breaker (ad-hoc) |
| 11 | `11_property_system.md` | Q_PROPERTY + NOTIFY auto-signal on change | watchdog state change is manual dispatch |
| 12 | `12_state_machine.md` | QStateMachine hierarchical FSM | BrowserSession lifecycle (flag-based) |
| 13 | `13_meta_object_system.md` | Meta-Object System overview | bubus `dir()` + naming convention reflection |
| 14 | `14_moc.md` | MOC compiler: codegen vs runtime reflection | bubus runtime `on_` method scanning |
| 15 | `15_woboq_signal_internals.md` | Qt internals: connection list, 64-bit bitmask fast-path, O(1) cleanup | bubus handler list + WeakSet |
| 16 | `16_gobject_signals.md` | GObject 6-phase emission, accumulator, detail filtering | bubus `event_results` aggregation |

### Key Design Gaps Identified (bubus vs Qt)

These are the primary areas where Qt's mature design reveals potential improvements for bubus:

1. **Connection types**: bubus only supports Queued dispatch (via `asyncio.Queue`). Qt offers Direct (synchronous inline), Queued, BlockingQueued, and Auto (auto-selects based on thread affinity). Adding at least a Direct mode would enable zero-overhead handler invocation for same-context calls.

2. **Event propagation control**: bubus has no accept/ignore mechanism. Once dispatched, an event reaches all registered handlers unconditionally. Qt allows handlers to accept (consume) or ignore (pass through) events, and event filters can intercept and stop propagation before the target sees it.

3. **Event filter chain**: bubus's event interception is ad-hoc — `_would_create_loop()` and the watchdog circuit breaker wrapper are hardcoded special cases. Qt provides a general `installEventFilter()` mechanism where any object can intercept events for any other object, with LIFO ordering and return-true-to-stop semantics.

4. **Auto-disconnect on destruction**: Qt automatically severs all signal/slot connections when either sender or receiver is destroyed. bubus has no equivalent — `BaseWatchdog` has no `detach_from_session()` method, and `__del__` only cancels asyncio tasks.

5. **Handler priority**: bubus handlers execute in registration order (FIFO) with no priority mechanism. Qt's `postEvent()` accepts integer priority for queue ordering. GObject goes further with 6 emission phases (RUN_FIRST → EMISSION_HOOK → HANDLER_RUN_FIRST → RUN_LAST → HANDLER_RUN_LAST → RUN_CLEANUP).

6. **State machine for lifecycle**: BrowserSession manages connection state via boolean flags (`is_cdp_connected`, `is_reconnecting`). Qt's `QStateMachine` provides formal hierarchical state machines with signal-driven transitions, error states, and property animation — a more robust pattern for complex lifecycle management.

7. **Property change notifications**: Watchdog state changes require manual `dispatch()` calls. Qt's `Q_PROPERTY(... NOTIFY signal)` automatically emits a signal when a property changes, eliminating boilerplate.

8. **Result accumulation**: bubus has flexible result aggregation (`event_result()`, `event_results_flat_dict()`, etc.). GObject's accumulator pattern is more formalized — an accumulator function receives each handler's return value and can short-circuit emission by returning FALSE. This is worth studying for bubus's `event_results_filtered()` design.
