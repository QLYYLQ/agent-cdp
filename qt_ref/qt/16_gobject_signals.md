# GObject Signal System: Technical Overview

> Source: https://docs.gtk.org/gobject/concepts.html#signals

## Signal Fundamentals

Signals are GObject's mechanism for connecting application-specific events with listeners. "GObject's signals have nothing to do with standard UNIX signals: they connect arbitrary application-specific events with any number of listeners."

Each signal is registered on a specific type and can be emitted on instances of that type. Users connect closures to signals, and when a signal is emitted, all connected closures are invoked.

## Signal Registration

Signal registration uses three primary functions: `g_signal_newv()`, `g_signal_new_valist()`, or `g_signal_new()`. The `g_signal_newv()` function accepts these parameters:

- **signal_name**: Uniquely identifies the signal
- **itype**: Instance type on which the signal can be emitted
- **signal_flags**: Determines the invocation order of connected closures
- **class_closure**: Default closure invoked during emission (if non-NULL)
- **accumulator**: Function computing the signal's return value from closure returns; returning FALSE stops emission
- **c_marshaller**: Default C marshaller for connected closures
- **return_type**: Signal return value type
- **n_params / param_types**: Parameter count and types

## Emission Phases

Signal emission occurs in six sequential phases:

1. **RUN_FIRST**: Class closure invoked if `G_SIGNAL_RUN_FIRST` flag was set
2. **EMISSION_HOOK**: Emission hooks invoked (if registered)
3. **HANDLER_RUN_FIRST**: Closures connected via `g_signal_connect()` (if unblocked)
4. **RUN_LAST**: Class closure invoked if `G_SIGNAL_RUN_LAST` flag was set
5. **HANDLER_RUN_LAST**: Closures from `g_signal_connect_after()` (if unblocked)
6. **RUN_CLEANUP**: Class closure invoked if `G_SIGNAL_RUN_CLEANUP` flag was set

The accumulator function executes after each closure invocation (except during `RUN_EMISSION_HOOK` and `RUN_CLEANUP`). If it returns FALSE at any point, emission jumps to the cleanup phase.

## Signal Connection

Three connection mechanisms exist:

- **Class closure**: Registered at signal creation; system-wide operation
- **g_signal_override_class_closure()**: Overrides class closure on derived types
- **g_signal_connect() family**: Instance-specific operations; closure invoked only on specified instances

Emission hooks provide an additional mechanism, invoked "whenever a given signal is emitted whatever the instance on which it is emitted," using `g_signal_add_emission_hook()`.

## Detail Argument

The detail parameter enables optional filtering. Connection functions accept detailed signal names in the format `signal_name::detail_name`. During emission, if the emitted detail matches a closure's registered detail, the closure executes. Setting detail to zero disables this filtering.

This mechanism optimizes frequently-emitted signals. The `notify` signal exemplifies this: when properties change, "GObject associates as a detail to this signal emission the name of the property modified," allowing clients to filter before closure execution.

## Signal Emission Functions

Primary emission functions include:

- **g_signal_emit()**: Emits using GValue array
- **g_signal_emitv()**: Emits with explicit detail parameter
- **g_signal_emit_by_name()**: Emits using signal name string
- **g_signal_emit_valist()**: Variadic emission function

## Signal-Closure Relationship

Signals and closures form a tight integration. A closure represents "a generic representation of a callback" containing a function pointer, user_data, and destructor function pointer. Signal emission invokes closures through the marshalling system, which "transform[s] the array of GValues which represent the parameters to the target function into a C-style function parameter list."

## Key Differences from Qt Signals

- **Accumulator pattern**: GObject can compute aggregate return values from multiple handlers via accumulator functions — similar to bubus's `event_results` aggregation but more formalized
- **Emission phases**: 6 distinct phases with RUN_FIRST/RUN_LAST/RUN_CLEANUP ordering — provides finer-grained control than Qt's simple connection-order invocation
- **Detail strings**: Built-in filtering mechanism for high-frequency signals — neither Qt nor bubus have this
- **Emission hooks**: Global interception points independent of specific instances — similar to Qt's application-level event filters
