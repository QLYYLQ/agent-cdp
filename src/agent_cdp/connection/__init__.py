"""Connection package — connection types and connection management."""

from agent_cdp.connection.connection import Connection, connect
from agent_cdp.connection.types import ConnectionType

__all__ = ['Connection', 'ConnectionType', 'connect']
