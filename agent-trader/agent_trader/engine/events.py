"""In-process event bus with a cursor-addressable ring buffer.

Agents consume events by long-polling ``since(cursor)`` or through the SSE endpoint; both
are built on this buffer so no threads or async queues are needed inside the engine.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Event:
    id: int
    ts: datetime
    type: str
    agent_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts.isoformat(),
            "type": self.type,
            "agent_id": self.agent_id,
            **self.payload,
        }


class EventBus:
    def __init__(self, maxlen: int = 20_000) -> None:
        self._buf: deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._next = 1
        self._cv = threading.Condition(self._lock)

    def publish(self, ts: datetime, type_: str, agent_id: str | None, payload: dict[str, Any]) -> Event:
        with self._cv:
            ev = Event(self._next, ts, type_, agent_id, payload)
            self._next += 1
            self._buf.append(ev)
            self._cv.notify_all()
            return ev

    @property
    def cursor(self) -> int:
        """Id of the most recent event (0 if none)."""
        with self._lock:
            return self._next - 1

    def since(
        self, cursor: int, *, agent_id: str | None = None, types: set[str] | None = None, limit: int = 500
    ) -> list[Event]:
        with self._lock:
            out = []
            for ev in self._buf:
                if ev.id <= cursor:
                    continue
                if agent_id is not None and ev.agent_id not in (agent_id, None):
                    continue
                if types and ev.type not in types:
                    continue
                out.append(ev)
                if len(out) >= limit:
                    break
            return out

    def wait(self, cursor: int, timeout: float) -> bool:
        """Block until an event newer than ``cursor`` exists or ``timeout`` elapses."""
        with self._cv:
            if self._next - 1 > cursor:
                return True
            return self._cv.wait_for(lambda: self._next - 1 > cursor, timeout=timeout)
