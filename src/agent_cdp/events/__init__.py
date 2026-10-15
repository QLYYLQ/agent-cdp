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
from agent_cdp.events.base import (
    AsyncHandlerError,
    BaseEvent,
    EmitPolicy,
    EventExecutionError,
    EventTimeoutError,
    HandlerTimeoutError,
    QueueFullError,
    SelfWaitError,
    wait_for_results,
)
from agent_cdp.events.contracts import (
    Participation,
    ReceiverContractError,
    RequestContract,
    ResultContract,
    ResultContractError,
)
from agent_cdp.events.result import DeliveryDiagnostic, EventResult, ResultStatus

__all__ = [
    'AsyncHandlerError',
    'BaseEvent',
    'DeliveryDiagnostic',
    'EmitPolicy',
    'EventExecutionError',
    'EventResult',
    'EventTimeoutError',
    'HandlerError',
    'HandlerTimeoutError',
    'Participation',
    'QueueFullError',
    'ReceiverContractError',
    'RequestContract',
    'ResultContract',
    'ResultContractError',
    'ResultStatus',
    'SelfWaitError',
    'event_result',
    'event_results_by_handler_name',
    'event_results_filtered',
    'event_results_flat_dict',
    'event_results_flat_list',
    'event_results_list',
    'wait_for_results',
]
