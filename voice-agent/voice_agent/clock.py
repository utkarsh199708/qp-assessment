"""Real and simulated clocks.

The pipeline paces outbound audio and runs idle timers against a ``Clock`` so a
whole call can be simulated in milliseconds of wall time with realistic
virtual timings (tests, cost simulations, load models).
"""

from __future__ import annotations

import asyncio
import heapq
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class SimClock:
    """Discrete-event virtual clock.

    ``sleep`` parks the caller until the driver advances time to its wake-up
    point. The driver yields to the event loop a number of times first so every
    runnable task makes progress before virtual time moves; only when all tasks
    are blocked does the earliest sleeper wake. Start it with ``start()`` inside
    a running loop and ``stop()`` when done (or use ``async with``).
    """

    def __init__(self, yields_per_step: int = 20):
        self._t = 0.0
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0
        self._driver: asyncio.Task[None] | None = None
        self.yields_per_step = yields_per_step

    def now(self) -> float:
        return self._t

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self._t + seconds, self._seq, fut))
        self._seq += 1
        await fut

    def start(self) -> None:
        if self._driver is None:
            self._driver = asyncio.create_task(self._drive(), name="simclock")

    async def stop(self) -> None:
        if self._driver is not None:
            self._driver.cancel()
            try:
                await self._driver
            except asyncio.CancelledError:
                pass
            self._driver = None

    async def __aenter__(self) -> SimClock:
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _drive(self) -> None:
        while True:
            for _ in range(self.yields_per_step):
                await asyncio.sleep(0)
            if not self._heap:
                continue
            wake_t, _, fut = heapq.heappop(self._heap)
            self._t = max(self._t, wake_t)
            if not fut.done():
                fut.set_result(None)
            # wake everything else due at the same instant
            while self._heap and self._heap[0][0] <= self._t:
                _, _, other = heapq.heappop(self._heap)
                if not other.done():
                    other.set_result(None)
