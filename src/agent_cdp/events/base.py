"""BaseEvent[T_Result] — generic event base class with pending/completion mechanics.

Responsibilities (intentionally limited):
- Data carrier (Pydantic fields + JSON serialization)
- conscribe auto-registration (via EventBridge)
- Pending state machine (_increment/_decrement_pending + _completion)
- Awaitable support (__await__)
- Propagation control (consume())
- Result recording (record_result)

Aggregation methods live in agent_cdp.events.aggregation as free functions.
"""

import asyncio
import copy
from enum import StrEnum
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import ConfigDict, Field, PrivateAttr

from agent_cdp._registry import EventBridge
from agent_cdp.events.result import EventResult, ResultStatus

T_Result = TypeVar('T_Result')


class EmitPolicy(StrEnum):
    """Controls how Direct handler exceptions are handled during emit."""

    FAIL_FAST = 'fail_fast'
    COLLECT_ERRORS = 'collect_errors'


class EventTimeoutError(TimeoutError):
    """Raised when ``await event`` exceeds *event_timeout*.

    Inherits :class:`TimeoutError` so existing ``except TimeoutError`` clauses
    catch it without changes.
    """

    def __init__(self, event_type: str, event_id: str, timeout: float) -> None:
        self.event_type = event_type
        self.event_id = event_id
        self.timeout = timeout
        super().__init__(f'{event_type}(id={event_id[:12]}...) timed out after {timeout:.1f}s')


class AsyncHandlerError(TypeError):
    """Raised when a Direct handler returns a coroutine or awaitable.

    Sentinel subclass so scope._dispatch_direct can catch *only* its own
    async-detection TypeErrors without swallowing user handler TypeErrors.
    """


def _make_set_event() -> asyncio.Event:
    """Born-set asyncio.Event. Zero-handler / pure-Direct scenarios: await event returns immediately."""
    e = asyncio.Event()
    e.set()
    return e


class BaseEvent(EventBridge, Generic[T_Result]):  # type: ignore[reportUntypedBaseClass]
    """Generic event base class. T_Result declares the result type handlers should return.

    Inherits from EventBridge (conscribe + Pydantic) and Generic[T_Result].
    Concrete subclasses auto-register via conscribe; set __abstract__ = True to skip.

    Result aggregation is handled by free functions in agent_cdp.events.aggregation.
    """

    __abstract__ = True

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Public fields ──
    event_id: str = Field(default_factory=lambda: _uuid7_str())
    event_timeout: float | None = 300.0
    consumed: bool = False
    event_parent_id: str | None = None
    event_results: dict[str, Any] = Field(default_factory=dict)  # dict[str, EventResult[T_Result]] at runtime

    # ── ClassVar (not serialized) ──
    emit_policy: ClassVar[EmitPolicy] = EmitPolicy.FAIL_FAST

    # ── PrivateAttr (not serialized) ──
    _completion: asyncio.Event = PrivateAttr(default_factory=_make_set_event)
    _pending_count: int = PrivateAttr(default=0)

    def consume(self) -> None:
        """Mark event as consumed, stopping propagation to lower-priority handlers.

        Semantics:
        - consume() affects only the current emit() dispatch chain on the current scope.
        - In broadcast scenarios, each scope receives a deep copy — consume() on one
          copy does not affect other scopes' copies.
        - Within a single scope's Direct handler chain, consume() is visible to all
          subsequent handlers because they share the same event reference. This is
          intentional: it enables security gating (high-priority handler blocks
          lower-priority handlers from executing).
        - consume() is in-place mutation by design — returning a new event is not
          feasible because the event reference is shared across the emit() loop and
          may be combined with `raise` (e.g., consume + raise for security checks).
        """
        self.consumed = True

    @property
    def has_pending(self) -> bool:
        """Whether there are queued handlers that have not yet completed."""
        return self._pending_count > 0

    def record_result(
        self,
        *,
        connection_id: str,
        handler_name: str,
        result: T_Result | None = None,
        error: Exception | None = None,
    ) -> None:
        """Record a handler's result. Called by the framework's _record helper.

        Directly constructs a terminal-state EventResult (COMPLETED, FAILED, or TIMEOUT),
        bypassing the PENDING → mark_* path.
        """
        if error is not None:
            status = ResultStatus.TIMEOUT if isinstance(error, TimeoutError) else ResultStatus.FAILED
        else:
            status = ResultStatus.COMPLETED

        self.event_results[connection_id] = EventResult(
            handler_name=handler_name,
            connection_id=connection_id,
            result=result,
            error=error,
            status=status,
        )

    def _increment_pending(self) -> None:
        """Called when a Queued handler is enqueued. Clears completion on first call."""
        if self._pending_count == 0:
            self._completion.clear()
        self._pending_count += 1

    def _decrement_pending(self) -> None:
        """Called when a Queued handler finishes. Sets completion when count reaches zero."""
        if self._pending_count > 0:
            self._pending_count -= 1
        if self._pending_count == 0:
            self._completion.set()

    # ── Awaitable ──

    def __await__(self) -> Any:
        """Allow ``await event`` to wait for all pending handlers to complete.

        Respects *event_timeout*: raises :class:`EventTimeoutError` if the
        pending count does not reach zero within the deadline.  Set
        ``event_timeout=None`` for an indefinite wait.
        """
        return self._await_impl().__await__()

    async def _await_impl(self) -> None:
        if self._pending_count == 0:
            return
        if self.event_timeout is not None:
            try:
                await asyncio.wait_for(self._completion.wait(), timeout=self.event_timeout)
            except TimeoutError:
                raise EventTimeoutError(
                    event_type=type(self).__name__,
                    event_id=self.event_id,
                    timeout=self.event_timeout,
                ) from None
        else:
            await self._completion.wait()

    def __deepcopy__(self, memo: dict[int, Any]) -> 'BaseEvent[T_Result]':
        """Deep copy with independent asyncio.Event and reset pending count.

        Ensures broadcast copies have isolated completion state.
        """
        # Let Pydantic handle the model fields copy
        cls = type(self)
        new = cls.model_construct(
            **{field_name: copy.deepcopy(getattr(self, field_name), memo) for field_name in cls.model_fields}
        )
        # Set fresh private attrs: independent completion (born-set) and zero pending
        new.__pydantic_private__ = {
            '_completion': _make_set_event(),
            '_pending_count': 0,
        }
        memo[id(self)] = new
        return new


def _uuid7_str() -> str:
    """Generate a UUID v7 string."""
    from uuid_utils import uuid7

    return str(uuid7())
