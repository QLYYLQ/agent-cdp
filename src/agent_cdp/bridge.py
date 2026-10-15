"""CDP event routing and pause/setup/resume coordination."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Protocol, Self, cast, overload, runtime_checkable

from agent_cdp.events.base import BaseEvent
from agent_cdp.scope.scope import EventScope

logger = logging.getLogger(__name__)


@runtime_checkable
class CDPClientProtocol(Protocol):
    """CDP callbacks receive ``(params, route_session_id)`` from the envelope.

    Clients that only deliver params can be used with an unfiltered bridge.
    Payload fields such as ``params.sessionId`` are never routing information.
    """

    def on_event(self, method: str, callback: Callable[..., Any]) -> None: ...
    def off_event(self, method: str, callback: Callable[..., Any]) -> None: ...


@runtime_checkable
class CDPCommandProtocol(CDPClientProtocol, Protocol):
    """CDP client supporting commands; omitted timeout uses the client's default."""

    @property
    def is_connected(self) -> bool: ...

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any: ...


class ResumeTimeoutError(TimeoutError):
    """The resume budget expired; the CDP command may already have taken effect."""

    def __init__(self, timeout: float, session_id: str | None, task: asyncio.Task[None]) -> None:
        self.timeout = timeout
        self.session_id = session_id
        self.task = task
        super().__init__(
            f'PausedTarget resume not confirmed within {timeout}s (session={session_id!r}); '
            'cancellation requested, but the attempt may still be running'
        )


@dataclass
class _ResumeAttempt:
    task: asyncio.Task[None]
    deadline: float | None
    failure: BaseException | None = None

    def cancel(self, error: BaseException) -> None:
        # A cancellation-resistant coroutine must not be repeatedly interrupted.
        if self.failure is None:
            self.failure = error
            if not self.task.done():
                self.task.cancel()


def _retrieve_task_error(task: asyncio.Task[None]) -> None:
    """Observe late failures while retaining the task for caller inspection."""
    if not task.cancelled():
        task.exception()


class PausedTarget:
    """Register bridges inside the context, then resume the paused target.

    ``resume_timeout`` bounds each attempt, including custom resume callables.
    It does not override the CDP client's command timeout. Success requires an
    acknowledgement; failure allows an explicit retry once the attempt exits.
    Concurrent callers share the attempt. A coroutine that ignores cancellation
    remains available through ``resume_task`` and blocks a fresh attempt.
    """

    def __init__(
        self,
        *,
        cdp: CDPCommandProtocol | None = None,
        resume: Callable[[], Coroutine[Any, Any, None]] | None = None,
        session_id: str | None = None,
        resume_timeout: float | None = 5.0,
    ) -> None:
        if (cdp is None) == (resume is None):
            raise ValueError('Provide exactly one of cdp or resume, not both/neither')
        if resume_timeout is not None and resume_timeout < 0:
            raise ValueError('resume_timeout must be non-negative or None')
        self._cdp = cdp
        self._session_id = session_id
        self._custom_resume = resume
        self._resume_timeout = resume_timeout
        self._resumed = False
        self._entered = False
        self._attempt: _ResumeAttempt | None = None

    @property
    def resumed(self) -> bool:
        """Whether an attempt completed successfully within its budget."""
        return self._resumed

    @property
    def resume_task(self) -> asyncio.Task[None] | None:
        """Latest attempt, retained even if it ignores cancellation."""
        return self._attempt.task if self._attempt else None

    @property
    def resume_error(self) -> BaseException | None:
        """Original error from the latest unsuccessful attempt, if any."""
        return self._attempt.failure if self._attempt else None

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError('PausedTarget already entered; not reentrant')
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            await self.resume()
        except asyncio.CancelledError:
            # New external cancellation during cleanup must supersede an older
            # ordinary body exception; ordinary cleanup errors must not do so.
            raise
        except Exception as error:
            if exc_val is None:
                raise
            exc_val.add_note(f'PausedTarget cleanup also failed: {type(error).__name__}: {error}')
            raise exc_val.with_traceback(exc_tb) from error

    async def _perform_resume(self) -> None:
        if self._custom_resume is not None:
            await self._custom_resume()
        elif self._cdp is not None:
            await self._cdp.send('Runtime.runIfWaitingForDebugger', session_id=self._session_id)

    async def resume(self) -> None:
        """Await one shared attempt; only acknowledged success is idempotent.

        A timeout requests cancellation and returns promptly without claiming
        the operation stopped or undoing an already sent command.
        """
        if self._resumed:
            return
        loop = asyncio.get_running_loop()
        attempt = self._attempt
        if attempt is None or (attempt.task.done() and attempt.failure is not None):
            task = loop.create_task(self._perform_resume(), name=f'cdp-resume:{self._session_id}')
            task.add_done_callback(_retrieve_task_error)
            deadline = None if self._resume_timeout is None else loop.time() + self._resume_timeout
            attempt = self._attempt = _ResumeAttempt(task, deadline)
        if attempt.failure is not None:
            raise attempt.failure
        try:
            remaining = None if attempt.deadline is None else max(0.0, attempt.deadline - loop.time())
            done, _ = await asyncio.wait((attempt.task,), timeout=remaining)
            if not done:
                assert self._resume_timeout is not None
                error = ResumeTimeoutError(self._resume_timeout, self._session_id, attempt.task)
                attempt.cancel(error)
                raise error
            attempt.task.result()
            if attempt.failure is not None:
                raise attempt.failure
            self._resumed = True
        except asyncio.CancelledError as error:
            attempt.cancel(error)
            raise
        except Exception as error:
            if attempt.failure is None:
                attempt.failure = error
            raise


@dataclass
class _BridgeRegistration:
    method: str
    callback: Callable[..., Any]
    expect_hits: bool
    hits: int = 0


class CDPEventBridge:
    """Bridge CDP params into a scope, optionally filtering envelope sessions.

    No session binding means no session filter. ``with_session=True`` explicitly
    passes both params and the route session to the factory. Scope closure also
    closes the bridge. Factory and delivery errors are logged without escaping
    into the client's receive loop; delivery results retain rejection details.
    """

    def __init__(self, cdp: CDPClientProtocol, scope: EventScope, *, session_id: str | None = None) -> None:
        self._cdp = cdp
        self._scope = scope
        self._session_id = session_id
        self._registrations: list[_BridgeRegistration] = []
        self._hit_counts: dict[str, int] = {}
        self._closed = False
        self._scope.add_close_callback(self.close)

    @overload
    def bridge(
        self,
        cdp_method: str,
        event_factory: Callable[[dict[str, Any]], BaseEvent[Any]],
        *,
        with_session: Literal[False] = False,
        expect_hits: bool = False,
    ) -> None: ...

    @overload
    def bridge(
        self,
        cdp_method: str,
        event_factory: Callable[[dict[str, Any], str | None], BaseEvent[Any]],
        *,
        with_session: Literal[True],
        expect_hits: bool = False,
    ) -> None: ...

    def bridge(
        self,
        cdp_method: str,
        event_factory: Callable[[dict[str, Any]], BaseEvent[Any]]
        | Callable[[dict[str, Any], str | None], BaseEvent[Any]],
        *,
        with_session: bool = False,
        expect_hits: bool = False,
    ) -> None:
        """Register a factory; zero hits warn only for ``expect_hits=True``."""
        if self._closed:
            raise RuntimeError('Cannot bridge on a closed CDPEventBridge')
        self._hit_counts.setdefault(cdp_method, 0)

        def _callback(params: dict[str, Any], session_id: str | None = None) -> None:
            if self._closed or self._scope.closed:
                return
            if self._session_id is not None and session_id != self._session_id:
                return
            registration.hits += 1
            self._hit_counts[cdp_method] += 1
            try:
                if with_session:
                    factory = cast(Callable[[dict[str, Any], str | None], BaseEvent[Any]], event_factory)
                    event = factory(params, session_id)
                else:
                    factory_without_session = cast(Callable[[dict[str, Any]], BaseEvent[Any]], event_factory)
                    event = factory_without_session(params)
            except (Exception, asyncio.CancelledError):
                logger.exception('CDPEventBridge(%s): event_factory for %r raised', self._scope.scope_id, cdp_method)
                return
            try:
                self._scope.emit(event)
            except (Exception, asyncio.CancelledError):
                logger.exception(
                    'CDPEventBridge(%s): delivery for %r failed (route_session=%r, event=%s)',
                    self._scope.scope_id,
                    cdp_method,
                    session_id,
                    event.event_id,
                )

        registration = _BridgeRegistration(cdp_method, _callback, expect_hits)
        self._cdp.on_event(cdp_method, _callback)
        self._registrations.append(registration)

    @property
    def hit_counts(self) -> dict[str, int]:
        """Per-method matching callback counts (a copy)."""
        return dict(self._hit_counts)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Unregister callbacks, attempting all removals even if one fails.

        Failed unregisters remain tracked so that explicit retry or scope
        closure can finish cleanup. Late callbacks stop delivering immediately.
        """
        if self._closed and not self._registrations:
            return
        first_close = not self._closed
        self._closed = True
        errors: list[Exception] = []
        remaining: list[_BridgeRegistration] = []
        for registration in self._registrations:
            if first_close and registration.hits == 0:
                logger.log(
                    logging.WARNING if registration.expect_hits else logging.DEBUG,
                    'CDPEventBridge(%s): zero-hit method %r (never fired)',
                    self._scope.scope_id,
                    registration.method,
                )
            try:
                self._cdp.off_event(registration.method, registration.callback)
            except Exception as error:
                errors.append(error)
                remaining.append(registration)
            # Successful registrations no longer hold their factories/scopes.
        self._registrations = remaining
        if not remaining:
            self._scope.remove_close_callback(self.close)
        if errors:
            raise ExceptionGroup('CDPEventBridge callback cleanup failed', errors)

    @staticmethod
    def paused(
        *,
        cdp: CDPCommandProtocol | None = None,
        resume: Callable[[], Coroutine[Any, Any, None]] | None = None,
        session_id: str | None = None,
        resume_timeout: float | None = 5.0,
    ) -> PausedTarget:
        """Convenience factory for :class:`PausedTarget`."""
        return PausedTarget(cdp=cdp, resume=resume, session_id=session_id, resume_timeout=resume_timeout)
