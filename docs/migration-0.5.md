# Migrating to agent-cdp 0.5

0.5.0 implements the [2026-10-15 scheduling and lifecycle contract](event-lifecycle-spec.md).
Python remains >=3.11. Runtime dependencies now include `conscribe>=2.0.0,<3`,
`pydantic>=2.7,<3`, and `anyio>=4.0`. The event registrar bridge, hierarchical
keys, and diamond inheritance continue to work with conscribe 2.x. Invalid
conscribe declarations now fail earlier; see its [2.0 migration guide](https://github.com/QLYYLQ/conscribe/blob/master/conscribe/docs/migrating-to-2.0.md).

## Waiting, execution, and cancellation

`event_timeout` is now only the default wait budget. `execution_timeout` is the
per-handler execution budget (default 300 seconds); it starts when a queued
handler actually starts. An explicit `event.wait(timeout=...)`, aggregation
function timeout, or `emit_and_wait(..., timeout=...)` applies to that one wait
without modifying the event. `None` means no deadline when explicitly passed to
`event.wait()`. The default wait includes time spent queued after waiting begins.

```python
event = scope.emit(Action(execution_timeout=30))
try:
    await event.wait(timeout=5)
except TimeoutError:
    # Explicit owner decision: remove pending work and request running cancellation.
    event.cancel_execution()
    raise
```

Each event-to-connection delivery is recorded once. Re-emitting the same event
instance to an already-used connection raises `ValueError` before new dispatch
side effects; create a fresh event for a new request. The same event can still be
delivered to different connections/scopes.

Ordinary timeout or cancellation of a waiter only stops that wait. To withdraw
only work that has not begun, use `event.cancel_execution(include_running=False)`.
A running handler's cancellation is a cooperative request: its delivery remains
running until it actually exits. Cancellation does not undo Chrome side effects.
A handler that ignores cancellation prevents the next item in its scope from
starting. The scope reports its unfinished work rather than pretending it stopped.

`EventTimeoutError` uses a common diagnostic shape for waiting and execution,
with delivery identity, handler, scope, queued/running state, and elapsed times.
The error's `phase` distinguishes the two budgets. `event.results` provides a
snapshot of partial delivery results without waiting; original causes remain on
the corresponding result/error objects. A handler raising `CancelledError` is
recorded as cancelled and does not kill the queue consumer.

Waiting for queued work requiring the currently executing scope, or flushing a
prefix that includes the current handler, raises `SelfWaitError` immediately.
Use a separate `target_scope` or restructure the call; queues do not re-enter.

## Declare request and result contracts

Connections default to `Participation.HANDLER`; mark logging and other passive
listeners `Participation.OBSERVER`. `expect()` already does this. A notification
may have no receivers. An action can require real receivers before any handler
side effects occur:

```python
from agent_cdp import ConnectionType, Participation, RequestContract, ResultContract

scope.connect(Action, perform_action, mode=ConnectionType.QUEUED,
              receiver_key='executor',
              result_contract=ResultContract(lambda result: result.succeeded))
scope.connect(Action, log_request, mode=ConnectionType.DIRECT,
              participation=Participation.OBSERVER)

event = Action(request_contract=RequestContract(required_receivers=('executor',)))
scope.emit(event)
await event.wait(timeout=10)
event.raise_for_status()
```

`required_receivers` accepts connection IDs or explicitly assigned `receiver_key`
names. `min_receivers` counts applicable non-observer connections. Filtering runs
before these checks, so a filtered executor cannot satisfy the contract. If an
actual handler is missing, `emit()` raises `ReceiverContractError` before dispatch.
`wait_for='all'` waits for all deliveries; `'required'` waits for required handlers
(or all handlers if no names were supplied).

A `ResultContract` validator must return a boolean or raise the original business
exception. Returning `None`, a false value, or a dictionary with an `error` field
has no special meaning without an explicit validator. Request-level result rules
apply in addition to connection-level rules. Waiting means settlement, while
`check_success()` / `raise_for_status()` check the result contract. Partial results
remain readable when any required delivery fails, times out, is cancelled, or is
rejected. `event_result()` still selects a non-None value; it is not a substitute
for declaring which receiver performs an action.

Known async handlers connected as `DIRECT`, or known synchronous handlers as
`QUEUED`, now fail during connection. Dynamic callables still receive execution-
time validation; the library does not silently change their execution mode.

## Scope configuration, queues, and flushing

Both scope constructors expose the same keyword-only configuration:

```python
scope = await group.create_scope(
    '__archive__', queue_maxsize=65536, max_history_size=0,
    slow_handler_threshold=None, slow_handler_check_interval=5,
    metadata={'purpose': 'archive'},
)
```

Replace `create_scope('tab', target_id=target)` with
`create_scope('tab', metadata={'target_id': target})`. Unknown keywords now raise
`TypeError` instead of becoming metadata. Queue capacity defaults to 1024; 0 is
unbounded. History defaults to 1000 event objects; 0 retains none. A count limit
is not a byte limit: an event may retain large DOM trees, screenshots, and results.
Slow warnings default to a 15-second threshold checked every 5 seconds. Set the
threshold to `None` to disable warnings; deterministic self-wait detection remains.

`emit()` remains synchronous and nonblocking. Every rejected delivery has
`QueueFullError` and a rejected result. With `non_droppable=True` on an event,
queue saturation also raises synchronously from `emit()`. Deliveries to different
receivers are not atomically rolled back; inspect partial results and `scope.stats`.
Statistics count deliveries, not unique events, and expose queued/peak counts,
accepted/completed/failed/cancelled/rejected totals, durations, and rejection
breakdowns without retaining all historical event payloads.

```python
prefix = await scope.flush(timeout=10)
print(prefix)
print(scope.stats)
```

`flush()` fixes the accepted prefix at invocation, uses no queue slot, and ignores
new arrivals when deciding completion. Failed/cancelled deliveries are settled,
not lost. `FlushResult.deliveries` includes compact outcomes since the previous successful
flush checkpoint, including failures or rejections that settled before this call.
Each summary carries identities, status, timing, and bounded error descriptions;
it does not retain event/result payloads or exception tracebacks. `results` holds
full results for deliveries outstanding at invocation, and caller-owned events
retain their original causes. `stats` is cumulative. A successful flush clears
its acknowledged summary prefix; concurrent arrivals remain for the next flush.
Successful draining does not imply successful business processing. A timeout uses the same
per-delivery diagnostic structure as event waiting.

## Closing and binding ownership

```python
result = await scope.close(graceful=True, timeout=10)
if not result.complete:
    # A cooperative handler/cleanup has not exited: retain and supervise scope.
    print(result)
```

`graceful=False` withdraws queued work and requests running cancellation immediately.
Both modes have a finite caller-selected budget (default 10 seconds). Closure stops
new input and closes registered bridges/waiters before draining or cancelling work,
then releases connections. Unfinished tasks remain tracked. Idle closure does not
wait for a polling tick. `ScopeGroup.close_all()` attempts all scopes concurrently;
actual cleanup failures are aggregated after all attempts. Existing business
handler errors are not re-raised as cleanup failures. Incomplete scopes remain
reachable in the group.

`WatchdogProtocol` now requires `detach(scope, session_id=None)`, symmetric with
`attach`. `WatchdogBase.detach()` defaults to releasing matching tracked bindings
(and is a no-op when none were tracked). Call `track_binding()` inside `attach`:

```python
conn = scope.connect(Action, handler, mode=ConnectionType.QUEUED)
self.track_binding(scope, session_id, connections=[conn],
                   close_callbacks=[bridge.close])
```

`WatchdogBinding.close()` and `detach()` are idempotent. Closing one scope releases
only that attachment. Capture per-session handler data in closures or binding
objects; overwriting one shared `self.session_id` breaks multi-scope watchdogs.
`add_close_callback()` and `remove_close_callback()` support explicit ownership of
other scope resources. Scope isolation is scheduling isolation: AF owns browser
contexts, targets, sessions, and permissions across runs.

## CDP routing and paused targets

A session-bound `CDPEventBridge` matches only the envelope route session. Missing
routes never match; `params['sessionId']` is payload, not a fallback route. An
unbound bridge applies no session filter. Opt in to a two-argument factory:

```python
bridge.bridge('Target.attachedToTarget',
              lambda params, parent_session: Attached(
                  child_session=params['sessionId'], parent_session=parent_session),
              with_session=True)
```

Factories receive only `params` by default. Factory and dispatch errors are logged
without escaping into the CDP receive loop. Closing the scope unbinds the bridge.
Zero hits are DEBUG by default; `bridge(..., expect_hits=True)` makes zero hits a
WARNING for that subscription. Public hit counts remain available.

`CDPCommandProtocol.send()` gains optional `timeout`. Omitting it preserves the
client default. `PausedTarget(cdp=..., session_id=..., resume_timeout=5)` sends
`Runtime.runIfWaitingForDebugger` with `session_id` as a routing argument, waits
for acknowledgement, and only then marks `resumed=True`. The old bug sent the
command at browser level and left the target paused; it did not resume another
target. Both built-in and custom resume functions obey `resume_timeout`.

Concurrent resume callers share one tracked `resume_task`. Failure allows an
explicit retry after the previous attempt actually exits; success is idempotent.
On timeout, `ResumeTimeoutError` reports that resume is unconfirmed and cancellation
is requested. An uncooperative custom coroutine may still be running, remains
tracked, and prevents a new attempt. A body failure is preserved with cleanup
failure attached; external cancellation remains cancellation.

## Immediate event expectations

```python
with expect(scope, PageLoaded, timeout=10) as waiter:
    trigger_navigation()  # listener already registered
    loaded = await waiter
```

`expect()` returns an `EventWaiter` immediately, not a coroutine. Its deadline
starts at registration, including time before the first await. Replace
`asyncio.create_task(expect(...))` with the waiter itself (or `ensure_future()` if
a task wrapper is needed). Sync and async context managers, `close()`, completion,
timeout, cancellation, and scope closure all release the listener. Cancelling a
waiter does not cancel execution of the event it observed.

## AF integration boundary

AF must update its dependency/lock, declare action/observer contracts, use a 65536
archive queue and native flush, adopt public statistics and per-binding teardown,
and select appropriate tab history limits. Its CrashWatchdog wiring, target/run
ownership, recorder supervision, and trace/WAL behavior are application changes.
This release supplies the upstream APIs; AF's integration and live recording,
observation, action, and replay acceptance remain a separate delivery.
