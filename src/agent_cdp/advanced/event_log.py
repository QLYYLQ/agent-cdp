"""EventLogWriter — per-scope JSONL event log with conscribe-based deserialization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio

from agent_cdp._registry import EventRegistrar
from agent_cdp.events.base import BaseEvent


class EventLogWriter:
    """Write-behind JSONL event log for per-scope event persistence.

    Events are written after completion (not write-ahead).
    Deserialization uses EventRegistrar.get() to look up event classes by name.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def write(self, event: BaseEvent[Any]) -> None:
        """Append a completed event as a JSON line to the log file.

        Injects the 'event_type' discriminator field (conscribe registry key)
        into the serialized JSON so read_all() can look up the correct class.
        """
        data = json.loads(event.model_dump_json())  # type: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        data['event_type'] = type(event).__registry_key__  # type: ignore[reportUnknownMemberType]
        line = json.dumps(data)
        async with await anyio.open_file(self._path, 'a') as f:
            await f.write(line + '\n')

    async def read_all(self) -> list[BaseEvent[Any]]:
        """Read and deserialize all events from the log file.

        Uses EventRegistrar.get(event_type_name) to look up the correct
        event class for deserialization — this is conscribe's core value point.

        Returns an empty list if the file does not exist or is empty.
        """
        if not self._path.exists():
            return []

        events: list[BaseEvent[Any]] = []
        async with await anyio.open_file(self._path, 'r') as f:
            content = await f.read()

        for line in content.strip().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            event_type_name = data['event_type']
            event_cls = EventRegistrar.get(event_type_name)  # type: ignore[reportUnknownVariableType,reportUnknownMemberType]
            events.append(event_cls.model_validate_json(line))  # type: ignore[reportUnknownMemberType,reportUnknownArgumentType]

        return events
