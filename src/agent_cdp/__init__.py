"""agent-cdp: Scoped Event System for agent-era browser automation."""

from agent_cdp.advanced import EventLogWriter, expect
from agent_cdp.bridge import CDPEventBridge
from agent_cdp.connection import Connection, ConnectionType, connect
from agent_cdp.events import (
    BaseEvent,
    EmitPolicy,
    EventResult,
    EventTimeoutError,
    HandlerError,
    ResultStatus,
    event_result,
    event_results_by_handler_name,
    event_results_filtered,
    event_results_flat_dict,
    event_results_flat_list,
    event_results_list,
)
from agent_cdp.scope import EventScope, ScopeGroup

__all__ = [
    # Events
    'BaseEvent',
    'EmitPolicy',
    'EventResult',
    'EventTimeoutError',
    'ResultStatus',
    'HandlerError',
    # Connections
    'Connection',
    'ConnectionType',
    'connect',
    # Scopes
    'EventScope',
    'ScopeGroup',
    # Aggregation
    'event_result',
    'event_results_list',
    'event_results_by_handler_name',
    'event_results_flat_dict',
    'event_results_flat_list',
    'event_results_filtered',
    # Advanced
    'EventLogWriter',
    'expect',
    # CDP bridge
    'CDPEventBridge',
]
