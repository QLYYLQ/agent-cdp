"""Immutable per-delivery execution facts and timeout diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from agent_cdp.events.contracts import Participation

if TYPE_CHECKING:
    from agent_cdp.events.base import BaseEvent

T = TypeVar('T')


class ResultStatus(StrEnum):
    """Execution state of one event-to-connection delivery."""

    PENDING = 'pending'  # Compatibility for manually constructed results.
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    TIMEOUT = 'timeout'
    CANCELLED = 'cancelled'
    REJECTED = 'rejected'


@dataclass(frozen=True)
class EventResult(Generic[T]):
    """Execution facts; a cancellation request is separate from actual exit.

    ``status`` describes execution. A completed handler may have a
    ``business_error`` from its explicit result contract. ``cancel_requested``
    never means that browser side effects were rolled back.
    """

    handler_name: str
    connection_id: str
    result: T | None = None
    error: BaseException | None = field(default=None, hash=False)
    status: ResultStatus = ResultStatus.PENDING
    event_children: tuple[BaseEvent[object], ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None
    event_id: str = ''
    event_type: str = ''
    source_scope_id: str = ''
    scope_id: str = ''
    participation: Participation = Participation.HANDLER
    receiver_key: str | None = None
    queued_at: datetime | None = None
    cancel_requested: bool = False
    business_error: BaseException | None = field(default=None, hash=False)

    @property
    def is_terminal(self) -> bool:
        return self.status not in (ResultStatus.PENDING, ResultStatus.QUEUED, ResultStatus.RUNNING)

    @property
    def queue_duration(self) -> float:
        """Seconds queued before start, settlement, or this snapshot query."""
        if self.queued_at is None:
            return 0.0
        end = self.started_at or self.completed_at or datetime.now(UTC)
        return max(0.0, (end - self.queued_at).total_seconds())

    @property
    def execution_duration(self) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, ((self.completed_at or datetime.now(UTC)) - self.started_at).total_seconds())

    @property
    def cancelled_before_start(self) -> bool:
        return self.status is ResultStatus.CANCELLED and self.started_at is None

    def _check_pending(self, target: ResultStatus) -> None:
        if self.is_terminal:
            raise ValueError(f'Cannot transition from {self.status} to {target}')

    def mark_completed(self, result: T | None = None) -> EventResult[T]:
        self._check_pending(ResultStatus.COMPLETED)
        return replace(self, status=ResultStatus.COMPLETED, result=result, completed_at=datetime.now(UTC))

    def mark_failed(self, error: BaseException) -> EventResult[T]:
        self._check_pending(ResultStatus.FAILED)
        return replace(self, status=ResultStatus.FAILED, error=error, completed_at=datetime.now(UTC))

    def mark_timeout(self, error: TimeoutError) -> EventResult[T]:
        self._check_pending(ResultStatus.TIMEOUT)
        return replace(self, status=ResultStatus.TIMEOUT, error=error, completed_at=datetime.now(UTC))

    @property
    def is_success(self) -> bool:
        return self.status is ResultStatus.COMPLETED and self.error is None and self.business_error is None


@dataclass(frozen=True)
class DeliveryDiagnostic:
    """Identity and elapsed durations for one pending or settled delivery."""

    event_id: str
    event_type: str
    connection_id: str
    handler_name: str
    source_scope_id: str
    scope_id: str
    status: ResultStatus
    queue_duration: float
    execution_duration: float
    cancel_requested: bool = False

    @classmethod
    def from_result(cls, result: EventResult[Any]) -> DeliveryDiagnostic:
        return cls(
            event_id=result.event_id,
            event_type=result.event_type,
            connection_id=result.connection_id,
            handler_name=result.handler_name,
            source_scope_id=result.source_scope_id,
            scope_id=result.scope_id,
            status=result.status,
            queue_duration=result.queue_duration,
            execution_duration=result.execution_duration,
            cancel_requested=result.cancel_requested,
        )
