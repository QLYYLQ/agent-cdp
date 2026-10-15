# 0.5.0 requirement and acceptance map

The numbering follows AF's 24-item ledger. The confirmed
[event lifecycle specification](event-lifecycle-spec.md) overrides older proposals.
Public names and upgrade examples are in the [migration guide](migration-0.5.md).

| # | Requirement | Implementation / regression coverage |
|---|---|---|
| 1 | Public queue capacity, default 1024, 0 unbounded | `EventScope` / `ScopeGroup.create_scope`; `test_scope_runtime_contracts.py` |
| 2 | Visible rejection and synchronous non-droppable failure | `QueueFullError`, `ResultStatus.REJECTED`, `non_droppable`, rejection statistics; queue tests |
| 3 | Paused-target command routed to its session; success only after acknowledgement | `PausedTarget`; `test_paused_target.py`, `test_paused_target_chrome.py` |
| 4 | Zero history retains no events | History guard; scope runtime GC regressions |
| 5 | Factory can receive envelope route session explicitly | `bridge(with_session=True)`; `test_bridge_waiter_lifecycle.py` |
| 6 | Optional command timeout; independent bounded recovery | `CDPCommandProtocol.send(timeout=...)`, `resume_timeout`; paused-target contract tests |
| 7 | Fast idle close, bounded graceful/immediate close, parallel group close | `CloseResult`, `close(graceful, timeout)`, `close_all`; scope lifecycle tests |
| 8 | Zero hits DEBUG by default, per-subscription opt-in WARNING | `expect_hits`, `hit_counts`; bridge diagnostics tests |
| 9 | Deterministic same-scope self-wait rejected | `SelfWaitError`, queued wait / flush tests |
| 10 | Independent wait/execute budgets; explicit per-delivery cancellation | `event_timeout`, `execution_timeout`, `wait`, `cancel_execution`; event and scope contract tests |
| 11 | Uniform per-receiver timeout diagnostics and original causes | `DeliveryDiagnostic`, `EventTimeoutError`, `HandlerTimeoutError`; timeout diagnostics tests |
| 12 | Required receiver and business-result contracts; correct result error text | `Participation`, `RequestContract`, `ResultContract`, `check_success`; `test_event_contracts.py` |
| 13 | Handler cancellation does not kill the consumer; complete cleanup attempts | Cancellation-resistant/self-cancelling handlers, group cleanup regressions |
| 14 | Bridge follows scope lifetime and contains callback failures | Scope resource callbacks; bridge contract tests |
| 15 | Handler/mode validation at wiring, dynamic runtime fallback | `connect` / `Connection`; event contract and async-handler tests |
| 16 | Completed timers/context/execution structures release payloads | Scope runtime weak-reference / garbage-collection regressions |
| 17 | Read-only statistics count deliveries and timings | Frozen `ScopeStats`, read-only rejection maps; queue contract tests |
| 18 | Prefix `flush` without queue admission or extension by new input | `FlushResult`, compact `DeliverySummary` checkpoint records; scope flush tests |
| 19 | Configurable/disabled slow warnings retain independent self-wait detection | `slow_handler_threshold`, `slow_handler_check_interval`; monitor and scope tests |
| 20 | `expect` registers immediately, times from registration, releases listeners | `EventWaiter`; bridge/waiter contract tests |
| 21 | Per-binding watchdog detach and shared ownership | `WatchdogProtocol.detach`, `WatchdogBinding`, `track_binding`; `test_v05_watchdog_conscribe.py` |
| 22 | Explicit metadata; typo keywords rejected | Both scope constructors; queue/config regression tests |
| 23 | anyio runtime dependency and clean installation | `pyproject.toml`; isolated wheel install/import/event-log smoke check |
| 24 | Strict envelope routing, no params.sessionId fallback | Bound/unbound bridge tests, parent/child session separation regressions |

Additional release checks cover conscribe 2.x registration, hierarchical keys,
diamond inheritance, strict typing, lint, package metadata, and wheel contents.
Real Chrome acceptance proves the old params-only recovery left the target paused
and the corrected routed command actually starts its script.

## Reproducing validation

```sh
uv sync --all-extras
uv run pytest -q
uv run ruff check src tests demo scripts
uv run ruff format --check src tests demo scripts
uv run pyright
AGENT_CDP_CHROME_TEST=1 uv run pytest -q tests/test_paused_target_chrome.py
```

Live website checks and actual AF consumer checks are documented with their exact
commands and observed limitations in [release validation](validation-0.5.md).
They complement deterministic failure-path tests. A network or website access
restriction is recorded as such; it is not reported as successful site navigation.

## AF follow-up

The upstream release does not itself migrate AF's dependency lock, CrashWatchdog
wiring, metadata call sites, action success contracts, archive drain barrier, or
browser/run ownership. The real AF consumer probe identifies this boundary; see
the release validation report before claiming end-to-end AF integration is done.
