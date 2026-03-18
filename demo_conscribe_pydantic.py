"""Demo: conscribe 0.4 + Pydantic BaseModel + Generic[T].

conscribe 0.4:
  - skip_pydantic_generic=True (default): skips classes with '[' in name
  - bridge() resolves metaclass conflict with Pydantic

Direct `metaclass=Meta` doesn't work (ModelMetaclass conflict).
bridge(BaseModel) creates a combined metaclass that handles both.
"""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar, runtime_checkable

from conscribe import create_registrar
from pydantic import BaseModel, Field


@runtime_checkable
class EventProtocol(Protocol):
    event_id: str


T_Result = TypeVar('T_Result')

# ── Create registrar ──

EventReg = create_registrar(
    'event',
    EventProtocol,
    discriminator_field='event_type',
    strip_suffixes=['Event'],
)

# ── Bridge resolves metaclass conflict ──

EventBridge = EventReg.bridge(BaseModel)


class BaseEvent(EventBridge, Generic[T_Result]):
    __abstract__ = True
    event_id: str = Field(default='test-id')
    consumed: bool = False

    def consume(self) -> None:
        self.consumed = True


# ── Concrete events (auto-registered) ──


class NavigateToUrlEvent(BaseEvent[str]):
    url: str


class CrashEvent(BaseEvent[None]):
    target_id: str


class ScreenshotEvent(BaseEvent[str]):
    full_page: bool = False


# ── Abstract intermediate (NOT registered) ──


class LifecycleEvent(BaseEvent[None]):
    __abstract__ = True


class SessionStartEvent(LifecycleEvent):
    session_id: str


class SessionEndEvent(LifecycleEvent):
    session_id: str
    reason: str = 'normal'


# ============================================================
# Verify
# ============================================================

print('=== Registry ===')
keys = EventReg.keys()
print(f'Keys: {keys}')

expected = {'navigate_to_url', 'crash', 'screenshot', 'session_start', 'session_end'}
actual = set(keys)
assert actual == expected, f'Expected {expected}, got {actual}'

bracket_keys = [k for k in keys if '[' in k]
assert len(bracket_keys) == 0, f'Unwanted: {bracket_keys}'
print('No bracket keys: OK')

for key, cls in [
    ('navigate_to_url', NavigateToUrlEvent),
    ('crash', CrashEvent),
    ('screenshot', ScreenshotEvent),
    ('session_start', SessionStartEvent),
    ('session_end', SessionEndEvent),
]:
    assert EventReg.get(key) is cls
print('All lookups: OK')

print('\n=== Pydantic ===')
nav = NavigateToUrlEvent(url='https://example.com')
print(f'model_dump: {nav.model_dump()}')
print(f'model_dump_json: {nav.model_dump_json()}')

nav2 = NavigateToUrlEvent.model_validate({'url': 'https://test.com', 'event_id': 'abc'})
assert nav2.url == 'https://test.com' and nav2.event_id == 'abc'
print('model_validate: OK')

nav3 = nav.model_copy(deep=True)
assert nav3 is not nav and nav3.url == nav.url
print('model_copy(deep=True): OK')

nav.consume()
assert nav.consumed
print('consume(): OK')

print('\n=== WAL deserialization ===')
for key, evt in [
    ('navigate_to_url', NavigateToUrlEvent(url='https://a.com')),
    ('crash', CrashEvent(target_id='tab-1')),
    ('session_start', SessionStartEvent(session_id='s-001')),
]:
    cls = EventReg.get(key)
    restored = cls.model_validate_json(evt.model_dump_json())
    print(f'  {key:>20} -> {type(restored).__name__}')

print('\n=== Metaclass chain ===')
print(f'EventBridge metaclass: {type(EventBridge).__name__}')
print(f'Meta MRO: {[c.__name__ for c in type(EventBridge).__mro__]}')

print('\nALL PASSED — bridge + skip_pydantic_generic works cleanly.')
