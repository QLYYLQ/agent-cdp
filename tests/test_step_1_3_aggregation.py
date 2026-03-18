"""Tests for result aggregation free functions (C4 / Step 1.3).

TDD: These tests are written BEFORE the implementation.
Tests use record_result() to populate event_results directly — no scopes/connections needed.

Aggregation functions are free functions in agent_cdp.events.aggregation,
taking a BaseEvent as the first argument.
"""

from __future__ import annotations

import pytest

from agent_cdp.events import BaseEvent, HandlerError
from agent_cdp.events.aggregation import (
    event_result,
    event_results_by_handler_name,
    event_results_filtered,
    event_results_flat_dict,
    event_results_flat_list,
    event_results_list,
)
from agent_cdp.events.result import EventResult

# ── Inline concrete event subclasses ──


class AggSampleEvent(BaseEvent[str]):
    payload: str = 'default'


class AggDictEvent(BaseEvent[dict]):
    pass


class AggListEvent(BaseEvent[list]):
    pass


# ── Helpers ──


def _record_success(event: BaseEvent, conn_id: str, handler_name: str, result: object = None) -> None:  # type: ignore[type-arg]
    """Record a successful handler result."""
    event.record_result(connection_id=conn_id, handler_name=handler_name, result=result)


def _record_error(event: BaseEvent, conn_id: str, handler_name: str, error: Exception) -> None:  # type: ignore[type-arg]
    """Record a failed handler result."""
    event.record_result(connection_id=conn_id, handler_name=handler_name, error=error)


# ── Test groups ──


class TestEventResult:
    """event_result() — returns first non-None success result."""

    @pytest.mark.asyncio
    async def test_event_result_returns_first_non_none(self) -> None:
        """First non-None result from successful handlers, in insertion order."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result=None)
        _record_success(e, 'c2', 'h2', result='found')
        _record_success(e, 'c3', 'h3', result='also_found')

        result = await event_result(e)
        assert result == 'found'

    @pytest.mark.asyncio
    async def test_event_result_raise_if_any_error(self) -> None:
        """raise_if_any=True (default) raises HandlerError on first error."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result='ok')
        _record_error(e, 'c2', 'h2', ValueError('boom'))

        with pytest.raises(HandlerError) as exc_info:
            await event_result(e)

        assert exc_info.value.handler_name == 'h2'
        assert exc_info.value.connection_id == 'c2'
        assert isinstance(exc_info.value.original, ValueError)

    @pytest.mark.asyncio
    async def test_event_result_raise_if_none_all_none(self) -> None:
        """raise_if_none=True (default) raises when all results are None."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result=None)
        _record_success(e, 'c2', 'h2', result=None)

        with pytest.raises(ValueError, match='non-None'):
            await event_result(e)

    @pytest.mark.asyncio
    async def test_event_result_no_raise_if_none(self) -> None:
        """raise_if_none=False returns None when all results are None."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result=None)

        result = await event_result(e, raise_if_none=False)
        assert result is None


class TestEventResultsList:
    """event_results_list() — all non-None success results."""

    @pytest.mark.asyncio
    async def test_results_list_excludes_none(self) -> None:
        """Non-None success results only; None and errors excluded."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result='a')
        _record_success(e, 'c2', 'h2', result=None)
        _record_error(e, 'c3', 'h3', ValueError('err'))

        results = await event_results_list(e, raise_if_any=False)
        assert results == ['a']

    @pytest.mark.asyncio
    async def test_results_list_preserves_order(self) -> None:
        """Results preserve dict insertion order."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result='first')
        _record_success(e, 'c2', 'h2', result='second')
        _record_success(e, 'c3', 'h3', result='third')

        results = await event_results_list(e)
        assert results == ['first', 'second', 'third']


class TestEventResultsByHandlerName:
    """event_results_by_handler_name() — dict keyed by handler_name."""

    @pytest.mark.asyncio
    async def test_results_by_handler_name_mapping(self) -> None:
        """Maps handler_name → result value."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'dom_watchdog', result='dom_data')
        _record_success(e, 'c2', 'screenshot_watchdog', result='screenshot_data')

        mapping = await event_results_by_handler_name(e)
        assert mapping == {'dom_watchdog': 'dom_data', 'screenshot_watchdog': 'screenshot_data'}

    @pytest.mark.asyncio
    async def test_results_by_handler_name_duplicate_names(self) -> None:
        """Duplicate handler names: last-wins (natural dict behavior)."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'handler', result='first')
        _record_success(e, 'c2', 'handler', result='second')

        mapping = await event_results_by_handler_name(e)
        assert mapping == {'handler': 'second'}


class TestEventResultsFlatDict:
    """event_results_flat_dict() — merge all dict results."""

    @pytest.mark.asyncio
    async def test_flat_dict_merges_non_overlapping(self) -> None:
        """Non-overlapping dict results are merged."""
        e = AggDictEvent()
        _record_success(e, 'c1', 'h1', result={'a': 1})
        _record_success(e, 'c2', 'h2', result={'b': 2})

        merged = await event_results_flat_dict(e)
        assert merged == {'a': 1, 'b': 2}

    @pytest.mark.asyncio
    async def test_flat_dict_raises_on_conflict(self) -> None:
        """Overlapping keys raise KeyError when raise_if_conflicts=True (default)."""
        e = AggDictEvent()
        _record_success(e, 'c1', 'h1', result={'key': 'v1'})
        _record_success(e, 'c2', 'h2', result={'key': 'v2'})

        with pytest.raises(KeyError, match='key'):
            await event_results_flat_dict(e)

    @pytest.mark.asyncio
    async def test_flat_dict_allows_override_when_no_raise(self) -> None:
        """raise_if_conflicts=False: later handler overrides earlier."""
        e = AggDictEvent()
        _record_success(e, 'c1', 'h1', result={'key': 'v1'})
        _record_success(e, 'c2', 'h2', result={'key': 'v2'})

        merged = await event_results_flat_dict(e, raise_if_conflicts=False)
        assert merged == {'key': 'v2'}


class TestEventResultsFlatList:
    """event_results_flat_list() — concatenate all list/tuple results."""

    @pytest.mark.asyncio
    async def test_flat_list_concatenates(self) -> None:
        """list and tuple results are flattened into one list."""
        e = AggListEvent()
        _record_success(e, 'c1', 'h1', result=[1, 2])
        _record_success(e, 'c2', 'h2', result=(3, 4))

        flat = await event_results_flat_list(e)
        assert flat == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_flat_list_skips_non_iterable(self) -> None:
        """Non-list/tuple results (including strings) are skipped."""
        e = AggListEvent()
        _record_success(e, 'c1', 'h1', result=[1, 2])
        _record_success(e, 'c2', 'h2', result='not_a_list')
        _record_success(e, 'c3', 'h3', result=42)

        flat = await event_results_flat_list(e)
        assert flat == [1, 2]


class TestEventResultsFiltered:
    """event_results_filtered() — apply predicate to EventResult entries."""

    @pytest.mark.asyncio
    async def test_filtered_with_default_truthy(self) -> None:
        """Default filter (_is_truthy) keeps only successful results."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result='ok')
        _record_error(e, 'c2', 'h2', ValueError('err'))

        filtered = await event_results_filtered(e)
        assert 'c1' in filtered
        assert 'c2' not in filtered
        assert isinstance(filtered['c1'], EventResult)

    @pytest.mark.asyncio
    async def test_filtered_with_custom_predicate(self) -> None:
        """Custom predicate filters by arbitrary criteria."""
        e = AggSampleEvent()
        _record_success(e, 'c1', 'h1', result='short')
        _record_success(e, 'c2', 'h2', result='a_longer_result_value')

        # Custom filter: only results with length > 10
        filtered = await event_results_filtered(
            e,
            include=lambda er: er.is_success and isinstance(er.result, str) and len(er.result) > 10,
        )
        assert 'c1' not in filtered
        assert 'c2' in filtered

    @pytest.mark.asyncio
    async def test_filtered_empty_results(self) -> None:
        """Empty event_results returns empty dict."""
        e = AggSampleEvent()

        filtered = await event_results_filtered(e)
        assert filtered == {}


class TestAggregationTimeout:
    """Timeout behavior for aggregation methods."""

    @pytest.mark.asyncio
    async def test_aggregation_timeout_raises(self) -> None:
        """Explicit timeout raises TimeoutError when pending handlers don't complete."""
        e = AggSampleEvent()
        e._increment_pending()  # Simulate a queued handler that never completes

        with pytest.raises(TimeoutError):
            await event_result(e, timeout=0.05)

    @pytest.mark.asyncio
    async def test_aggregation_respects_event_timeout(self) -> None:
        """When no explicit timeout, falls back to event_timeout."""
        e = AggSampleEvent(event_timeout=0.05)
        e._increment_pending()  # Simulate a queued handler that never completes

        with pytest.raises(TimeoutError):
            await event_result(e)
