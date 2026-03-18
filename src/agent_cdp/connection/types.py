"""ConnectionType enum — Direct/Queued/Auto dispatch modes."""

from enum import StrEnum


class ConnectionType(StrEnum):
    """How events are dispatched through a connection."""

    DIRECT = 'direct'  # handler executes synchronously in emit() call stack
    QUEUED = 'queued'  # event enqueued to target scope's event loop
    AUTO = 'auto'  # same scope → Direct, cross scope → Queued
