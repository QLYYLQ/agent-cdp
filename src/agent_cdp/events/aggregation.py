"""Free-function result aggregation for BaseEvent.

Decoupled from BaseEvent to keep the event class focused on data + completion mechanics.
Each function takes a BaseEvent as its first argument, waits for completion, then aggregates.

Usage:
    from agent_cdp.events.aggregation import event_result, event_results_flat_dict

    event = scope.emit(SomeEvent())
    result = await event_result(event)
    merged = await event_results_flat_dict(event)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from agent_cdp.events.result import EventResult

T = TypeVar('T')


class HandlerError(Exception):
    """Wraps a handler exception with identity info for diagnostics."""

    def __init__(self, original: Exception, handler_name: str, connection_id: str) -> None:
        self.original = original
        self.handler_name = handler_name
        self.connection_id = connection_id
        super().__init__(f'Handler {handler_name!r} (connection {connection_id!r}) raised: {original}')


def _is_truthy(er: EventResult[Any]) -> bool:
    """Default filter: keeps only successful results."""
    return er.is_success


async def _wait_for_completion(event: Any, timeout: float | None = None) -> None:
    """Wait for all pending handlers to complete.

    Fast-path when _pending_count == 0. Otherwise await with effective timeout
    (explicit timeout > event_timeout > indefinite).
    """
    if event._pending_count == 0:
        return
    effective_timeout = timeout if timeout is not None else event.event_timeout
    await asyncio.wait_for(event._completion.wait(), timeout=effective_timeout)


def _maybe_raise_errors(event: Any, raise_if_any: bool) -> None:
    """If raise_if_any, raise HandlerError wrapping the first error found."""
    if not raise_if_any:
        return
    for er in event.event_results.values():
        if isinstance(er, EventResult) and er.error is not None:
            raise HandlerError(
                original=er.error,
                handler_name=er.handler_name,
                connection_id=er.connection_id,
            )


async def event_result(
    event: Any,
    timeout: float | None = None,
    raise_if_any: bool = True,
    raise_if_none: bool = True,
) -> Any:
    """Return the first non-None result from successful handlers.

    - raise_if_any: raise HandlerError if any handler errored
    - raise_if_none: raise ValueError if all results are None
    """
    await _wait_for_completion(event, timeout)
    _maybe_raise_errors(event, raise_if_any)

    for er in event.event_results.values():
        if isinstance(er, EventResult) and er.is_success and er.result is not None:
            return er.result

    if raise_if_none:
        raise ValueError('All handlers returned non-None result: none found')
    return None


async def event_results_list(
    event: Any,
    timeout: float | None = None,
    raise_if_any: bool = True,
) -> list[Any]:
    """Return all non-None results from successful handlers, preserving insertion order."""
    await _wait_for_completion(event, timeout)
    _maybe_raise_errors(event, raise_if_any)

    return [
        er.result
        for er in event.event_results.values()
        if isinstance(er, EventResult) and er.is_success and er.result is not None
    ]


async def event_results_by_handler_name(
    event: Any,
    timeout: float | None = None,
    raise_if_any: bool = True,
) -> dict[str, Any]:
    """Return results keyed by handler_name. Duplicate names: last-wins."""
    await _wait_for_completion(event, timeout)
    _maybe_raise_errors(event, raise_if_any)

    return {
        er.handler_name: er.result
        for er in event.event_results.values()
        if isinstance(er, EventResult) and er.is_success
    }


async def event_results_flat_dict(
    event: Any,
    timeout: float | None = None,
    raise_if_any: bool = True,
    raise_if_conflicts: bool = True,
) -> dict[str, Any]:
    """Merge all dict results from successful handlers.

    raise_if_conflicts=True: raise KeyError on overlapping keys.
    raise_if_conflicts=False: later handler overrides earlier.
    """
    await _wait_for_completion(event, timeout)
    _maybe_raise_errors(event, raise_if_any)

    merged: dict[str, Any] = {}
    for er in event.event_results.values():
        if not isinstance(er, EventResult) or not er.is_success:
            continue
        r = er.result
        if not isinstance(r, dict):
            continue
        if raise_if_conflicts:
            conflicts = merged.keys() & r.keys()
            if conflicts:
                raise KeyError(f'Conflicting keys: {conflicts}')
        merged.update(r)
    return merged


async def event_results_flat_list(
    event: Any,
    timeout: float | None = None,
    raise_if_any: bool = True,
) -> list[Any]:
    """Concatenate all list/tuple results from successful handlers.

    Non-list/tuple results (including strings) are skipped.
    """
    await _wait_for_completion(event, timeout)
    _maybe_raise_errors(event, raise_if_any)

    flat: list[Any] = []
    for er in event.event_results.values():
        if not isinstance(er, EventResult) or not er.is_success:
            continue
        r = er.result
        if isinstance(r, (list, tuple)):
            flat.extend(r)
    return flat


async def event_results_filtered(
    event: Any,
    include: Callable[[EventResult[Any]], bool] = _is_truthy,
    timeout: float | None = None,
) -> dict[str, EventResult[Any]]:
    """Return event_results filtered by predicate. Default: successful results only."""
    await _wait_for_completion(event, timeout)

    return {key: er for key, er in event.event_results.items() if isinstance(er, EventResult) and include(er)}
