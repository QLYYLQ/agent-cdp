"""Explicit participation, receiver requirements, and business result rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class Participation(StrEnum):
    """Whether a connection executes the request or merely observes it."""

    HANDLER = 'handler'
    OBSERVER = 'observer'


class ResultContractError(ValueError):
    """An explicit validator rejected a returned business result."""

    def __init__(self, result: Any, description: str) -> None:
        self.result = result
        self.description = description
        super().__init__(description)


@dataclass(frozen=True)
class ResultContract:
    """Validate a returned value without guessing its business meaning.

    The validator must return ``True`` for success or ``False`` for rejection,
    or raise an exception. Raised exceptions are preserved as the original
    business error. ``None`` and other returned values have no implicit meaning.
    """

    validator: Callable[[Any], bool]
    description: str = 'Business result did not satisfy its result contract'

    def validate(self, result: Any) -> None:
        accepted: object = self.validator(result)
        if type(accepted) is not bool:
            raise TypeError('ResultContract.validator must return bool or raise an exception')
        if not accepted:
            raise ResultContractError(result, self.description)


@dataclass(frozen=True)
class RequestContract:
    """Declare actual receivers and completion requirements before dispatch.

    ``required_receivers`` contains connection IDs or explicit ``receiver_key``
    names. Every named receiver must have at least one applicable HANDLER
    connection; observers and filtered connections never satisfy requirements.
    ``min_receivers`` counts applicable HANDLER connections. ``wait_for`` selects
    all deliveries, or all required HANDLER deliveries (all HANDLER connections
    when no names are supplied). A request-level result rule applies to each
    HANDLER result in addition to its connection-level rule.
    """

    min_receivers: int = 0
    required_receivers: tuple[str, ...] = ()
    wait_for: Literal['all', 'required'] = 'all'
    result_contract: ResultContract | None = None

    def __post_init__(self) -> None:
        if self.min_receivers < 0:
            raise ValueError('min_receivers must be nonnegative')
        if self.wait_for not in ('all', 'required'):
            raise ValueError("wait_for must be 'all' or 'required'")


class ReceiverContractError(ValueError):
    """Applicable connections do not satisfy a request, before any execution."""

    def __init__(
        self,
        event_id: str,
        event_type: str,
        *,
        missing_receivers: tuple[str, ...] = (),
        required_count: int = 0,
        actual_count: int = 0,
    ) -> None:
        self.event_id = event_id
        self.event_type = event_type
        self.missing_receivers = missing_receivers
        self.required_count = required_count
        self.actual_count = actual_count
        super().__init__(
            f'{event_type}(id={event_id}) receiver contract failed: '
            f'{actual_count} applicable handler(s), need {required_count}; missing {missing_receivers}'
        )
