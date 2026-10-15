# Changelog

## 0.5.0 — 2026-10-15

This release implements the event scheduling and lifecycle specification agreed
with AF on 2026-10-15. It changes waiting, configuration, and binding contracts;
see [Migrating to 0.5](docs/migration-0.5.md) before upgrading.

- Separate per-wait timeouts from per-handler execution budgets. Preserve all
  delivery facts, cancellation causes, partial results, and timeout diagnostics.
- Add explicit receiver participation, required-receiver and business-result
  contracts. Reject missing required receivers before dispatch side effects.
- Add queue capacity, per-delivery rejection, non-droppable events, immutable
  statistics, prefix `flush()`, and configurable slow-handler warnings.
- Keep serial execution until a cancelled handler actually exits. Detect
  deterministic self-waits immediately. Support bounded graceful/immediate close
  and concurrent group cleanup without hiding unfinished handlers.
- Tie bridges, expect waiters, and tracked watchdog bindings to scope lifetime;
  preserve shared watchdog bindings on other scopes. Zero history retains no
  event objects, and completed handler timers release execution references.
- Route CDP messages only by envelope session; add explicit `with_session=True`
  factories. Make zero-hit warnings opt-in with `expect_hits=True`.
- Correct `PausedTarget` routing, wait for acknowledgement before marking resumed,
  bound both resume modes (default 5 seconds), and support explicit retry while
  preserving body exceptions and external cancellation.
- Arm `expect()` immediately at construction with registration-time deadlines and
  explicit/context-managed cleanup.
- Require `conscribe>=2.0.0,<3`, `pydantic>=2.7,<3`, and runtime `anyio>=4.0`.
- Keep local reference repositories, research notes, browser captures, scratch
  files, and local settings outside Git and release artifacts.

## 0.4.0

Scope-level slow-handler monitoring, MRO match caching, orjson event logging,
and browser-level CDP multi-tab examples.
