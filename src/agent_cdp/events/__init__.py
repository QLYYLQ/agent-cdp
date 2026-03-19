"""Events package — event types, result containers, emit policies, and aggregation."""

from agent_cdp.events.aggregation import (
    HandlerError,
    event_result,
    event_results_by_handler_name,
    event_results_filtered,
    event_results_flat_dict,
    event_results_flat_list,
    event_results_list,
)
from agent_cdp.events.base import AsyncHandlerError, BaseEvent, EmitPolicy, EventTimeoutError
from agent_cdp.events.result import EventResult, ResultStatus

__all__ = [
    'AsyncHandlerError',
    'BaseEvent',
    'EmitPolicy',
    'EventResult',
    'EventTimeoutError',
    'HandlerError',
    'ResultStatus',
    'event_result',
    'event_results_by_handler_name',
    'event_results_filtered',
    'event_results_flat_dict',
    'event_results_flat_list',
    'event_results_list',
]
