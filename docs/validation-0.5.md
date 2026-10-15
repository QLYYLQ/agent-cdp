# agent-cdp 0.5.0 release validation

Validation date: 2026-10-15. Python 3.12, conscribe 2.0.0.
Final full suite with the real Chrome canary enabled: **375 passed, 1 skipped**
(the skipped external-site test passed separately). Ruff lint and format, strict
pyright, and lockfile consistency checks passed.
The implemented contract and its 24-item map are linked from [requirements](requirements-0.5.md).

## Library and artifacts

The full deterministic suite, ruff lint/format, strict pyright, build metadata,
wheel contents, and a clean environment installation are release gates. The
release was checked with the commands below; external browser tests are opt-in.

```sh
uv sync --all-extras
uv run pytest -q
uv run ruff check src tests demo scripts
uv run ruff format --check src tests demo scripts
uv run pyright
uv build
uv run twine check dist/agent_cdp-0.5.0*
```

The clean installation uses a separate virtual environment, the built wheel,
and only its declared runtime dependencies. It exercises import, event dispatch,
request contracts, flush, shutdown, and `EventLogWriter` (which imports anyio).
Wheel and source distribution exclude reference repositories, local research,
browser images, test artifacts, and credentials. `py.typed` ships in the wheel.

## Real Chrome and external websites

```sh
AGENT_CDP_CHROME_TEST=1 uv run pytest -q tests/test_paused_target_chrome.py
AGENT_CDP_LIVE_SITES=1 uv run pytest -q -s tests/test_live_sites.py
```

The Chrome harness launches a separate headless profile and closes its own process;
it does not attach to the user's normal browser profile. Chrome version in this
run: 155.0.8059.80.

The paused-target canary auto-attaches a worker with `waitingForDebugger`, verifies
that the old browser-level command with a session in params leaves its script
blocked, and proves that built-in `PausedTarget` sends the correct envelope route
and releases the script. This is an actual Chrome execution assertion.

The website check uses real CDP, shared Security/Popups/Screenshot watchdogs,
explicit action receiver contracts, immediately armed load waiters, session-bound
bridges, DOM evaluation, PNG screenshots, native flush, and teardown. It also
closes one scope and verifies the shared screenshot watchdog still serves another.
No login, purchase, or other account-changing operation is performed.

| Site | Observed external access | Framework verification |
|---|---|---|
| Douban | HTTP 200; title and document content present; stylesheet/image loading incomplete in this environment | Load event, envelope session, DOM, screenshot, flush, teardown |
| Xiaohongshu | Redirected to security restriction page, IP-risk code 300012 | The actual restriction page was observed and captured; same lifecycle checks |
| Amazon | Final run HTTP 200, actual shopping homepage, title “Amazon.com. Spend less. Smile more.”; 48/48 images loaded; one non-document Fetch failure | DOM/content, load event, envelope session, 815012-byte PNG, flush, teardown |

The first Amazon attempt returned a blank HTTP 202 page; the final normal test
rerun loaded the actual homepage without any bypass. These are **not three
unrestricted page-load successes**. Website access and
framework operation are separate fields in `tmp/acceptance/live-sites.json`.
The JSON also records final URLs, HTTP responses, routes, resource failures, and
cleanup facts; PNGs are in the same ignored directory. These are local evidence,
not release package contents. Screenshots were visually inspected.

## Actual AF consumers and composition

AF source baseline: `576d3527dc5bee4b1d1b5a6aa8f1d35d8b059c79`. AF's existing virtual
environment already has conscribe 2.0.0. No AF source or dependency lock was changed.

A reproducible combination probe imports the real AF classes rather than copying
reduced lookalikes into this repository:

```sh
/path/to/af/.venv/bin/python scripts/check_af_consumers.py --af-root /path/to/af
```

All six combined scenarios pass with the new upstream API:

1. Real ScreenshotWatchdog and BrowserMetaWatchdog produce their AF business values.
2. Real SecurityWatchdog veto prevents the navigation command.
3. Real ActionWatchdog performs allowed navigation.
4. Explicit result validation recognizes AF business failure and preserves its cause.
5. Observation handling awaits a separate JavaScriptExecutionWatchdog scope.
6. Closing tab A leaves the shared JavaScript watchdog's tab B binding usable.

This combination probe uses a deterministic CDP transport. Real transport is
covered separately by the browser tests above; the probe does not claim to be a
full AF run or to migrate BrowserCapability.

The existing AF consumer/composer subset was also run unmodified against this
checkout (set `PYTHONPATH` to agent-cdp's `src` in the AF working directory):

```sh
PYTHONPATH=/path/to/agent-cdp/src .venv/bin/python -m pytest \
  tests/test_core/test_capabilities/test_browser_watchdog_registrar.py \
  tests/test_core/test_capabilities/test_browser_watchdog_lifecycle.py \
  tests/test_core/test_capabilities/test_browser_js_scope.py \
  tests/test_core/test_capabilities/test_browser_security_watchdog.py \
  tests/test_core/test_capabilities/test_browser_screenshot_format.py \
  tests/test_core/test_capabilities/test_browser_meta_consistency.py \
  tests/test_core/test_capabilities/test_browser_observations.py \
  tests/test_core/test_capabilities/test_browser_observation_bounds.py \
  tests/test_core/test_composer tests/test_generated/test_composed_contract.py \
  -m 'not requires_chrome' -q --tb=short -p no:cacheprovider
```

Result: **304 passed, 25 failed, 1 deselected**. The failures expose the intended
migration boundaries: old implicit `target_id`/`session_id` metadata keywords,
AF's async CrashWatchdog wired as DIRECT, and test doubles lacking the new
`add_close_callback` resource interface. They are retained as integration findings,
not hidden or described as a green full-AF suite.

AF still needs its own dependency/lock upgrade, proper CrashWatchdog wiring,
explicit metadata, receiver contracts, public queue statistics/native flush,
and binding teardown integration, followed by its full recording/replay acceptance.
