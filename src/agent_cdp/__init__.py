"""agent-cdp: Scoped Event System for agent-era browser automation."""

from agent_cdp._watchdog_registry import WatchdogBase, WatchdogBinding, WatchdogProtocol, WatchdogRegistrar
from agent_cdp.advanced import EventLogWriter, EventWaiter, expect
from agent_cdp.bridge import CDPCommandProtocol, CDPEventBridge, PausedTarget, ResumeTimeoutError
from agent_cdp.connection import Connection, ConnectionType, connect
from agent_cdp.events import (
    AsyncHandlerError,
    BaseEvent,
    DeliveryDiagnostic,
    EmitPolicy,
    EventExecutionError,
    EventResult,
    EventTimeoutError,
    HandlerError,
    HandlerTimeoutError,
    Participation,
    QueueFullError,
    ReceiverContractError,
    RequestContract,
    ResultContract,
    ResultContractError,
    ResultStatus,
    SelfWaitError,
    event_result,
    event_results_by_handler_name,
    event_results_filtered,
    event_results_flat_dict,
    event_results_flat_list,
    event_results_list,
    wait_for_results,
)
from agent_cdp.scope import CloseResult, DeliverySummary, EventScope, FlushResult, ScopeGroup, ScopeStats

__all__ = [
    'DeliveryDiagnostic',
    'EventExecutionError',
    'HandlerTimeoutError',
    'Participation',
    'QueueFullError',
    'ReceiverContractError',
    'RequestContract',
    'ResultContract',
    'ResultContractError',
    'SelfWaitError',
    'wait_for_results',
    'CloseResult',
    'DeliverySummary',
    'FlushResult',
    'ScopeStats',
    'EventWaiter',
    'ResumeTimeoutError',
    'WatchdogBinding',
    # Events
    'AsyncHandlerError',
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
    'CDPCommandProtocol',
    'CDPEventBridge',
    'PausedTarget',
    # Watchdog registry
    'WatchdogBase',
    'WatchdogProtocol',
    'WatchdogRegistrar',
]
