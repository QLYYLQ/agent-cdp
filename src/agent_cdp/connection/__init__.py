"""Connection package — connection types and connection management."""

from agent_cdp.connection.connection import Connection, connect
from agent_cdp.connection.types import ConnectionType
from agent_cdp.events.contracts import Participation, ResultContract

__all__ = ['Connection', 'ConnectionType', 'Participation', 'ResultContract', 'connect']
