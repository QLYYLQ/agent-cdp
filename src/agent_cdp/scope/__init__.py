"""Scope package — event scopes and scope groups."""

from agent_cdp.scope.group import ScopeGroup
from agent_cdp.scope.scope import EventScope

__all__ = ['EventScope', 'ScopeGroup', 'CloseResult', 'FlushResult', 'ScopeStats', 'DeliverySummary']

from agent_cdp.scope.event_loop import CloseResult, DeliverySummary, FlushResult, ScopeStats
