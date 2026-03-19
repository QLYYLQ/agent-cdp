"""conscribe-based event registry and Pydantic bridge.

Provides auto-registration for event subclasses with discriminated union support.
Pattern verified in demo_conscribe_pydantic.py.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from conscribe import create_registrar
from pydantic import BaseModel


@runtime_checkable
class EventProtocol(Protocol):
    """Protocol that all registered events must satisfy."""

    event_id: str
    consumed: bool


EventRegistrar = create_registrar(
    name='event',
    protocol=EventProtocol,
    discriminator_field='event_type',
    strip_suffixes=['Event'],
    key_separator='.',
)

EventBridge = EventRegistrar.bridge(BaseModel)  # type: ignore[reportUnknownVariableType,reportUnknownMemberType]
