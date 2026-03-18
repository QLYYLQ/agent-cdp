"""EventResult[T] — immutable result container for handler execution outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from agent_cdp.events.base import BaseEvent  # type: ignore[import-not-found]  # C3 not yet implemented

T = TypeVar('T')


class ResultStatus(StrEnum):
    """Status of a handler execution result."""

    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    TIMEOUT = 'timeout'


@dataclass(frozen=True)
class EventResult(Generic[T]):
    """Immutable container for a single handler's execution result.

    Transitions via mark_* methods, which return new instances (frozen dataclass).
    """

    handler_name: str
    connection_id: str
    result: T | None = None
    error: Exception | None = field(default=None, hash=False)
    status: ResultStatus = ResultStatus.PENDING
    event_children: tuple[BaseEvent[object], ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def _check_pending(self, target: ResultStatus) -> None:
        """Guard: only PENDING results can transition."""
        if self.status is not ResultStatus.PENDING:
            msg = f'Cannot transition from {self.status} to {target}'
            raise ValueError(msg)

    def mark_completed(self, result: T | None = None) -> EventResult[T]:
        """Transition to COMPLETED with optional result value."""
        self._check_pending(ResultStatus.COMPLETED)
        return replace(
            self,
            status=ResultStatus.COMPLETED,
            result=result,
            completed_at=datetime.now(UTC),
        )

    def mark_failed(self, error: Exception) -> EventResult[T]:
        """Transition to FAILED with the causing exception."""
        self._check_pending(ResultStatus.FAILED)
        return replace(
            self,
            status=ResultStatus.FAILED,
            error=error,
            completed_at=datetime.now(UTC),
        )

    def mark_timeout(self, error: TimeoutError) -> EventResult[T]:
        """Transition to TIMEOUT with a TimeoutError."""
        self._check_pending(ResultStatus.TIMEOUT)
        return replace(
            self,
            status=ResultStatus.TIMEOUT,
            error=error,
            completed_at=datetime.now(UTC),
        )

    @property
    def is_success(self) -> bool:
        """True only when status is COMPLETED and no error is stored."""
        return self.status is ResultStatus.COMPLETED and self.error is None
