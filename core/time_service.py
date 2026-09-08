"""Time service abstractions for AstrBot Group Chat Dynamics.

Provides TimeService ABC, SystemClock for production monotonic time,
and VirtualClock for deterministic, zero-latency async testing.
"""

from __future__ import annotations

import abc
import asyncio
import heapq
import time
from typing import List, Tuple


class TimeService(abc.ABC):
    """Abstract interface for time querying and async delay execution."""

    @abc.abstractmethod
    def time(self) -> float:
        """Returns the current monotonic timestamp in seconds."""
        pass

    @abc.abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Asynchronously suspends execution for the specified number of seconds."""
        pass

    def now(self) -> float:
        """Convenience alias for time()."""
        return self.time()

    def wall_time(self) -> float:
        """Civil clock for hour-of-day logic. Defaults to time()."""
        return self.time()


class SystemClock(TimeService):
    """Production time service using monotonic system clock and asyncio sleep."""

    def time(self) -> float:
        """Returns monotonic system time in seconds.

        Guaranteed never to decrease, unaffected by NTP updates or system time adjustments.
        """
        return time.monotonic()

    def wall_time(self) -> float:
        """Local civil time for daily rhythm and hour buckets."""
        return time.time()

    async def sleep(self, seconds: float) -> None:
        """Yields to the asyncio event loop for the requested real-world duration."""
        if seconds > 0:
            await asyncio.sleep(seconds)
        else:
            # Yield control for zero or negative duration without blocking
            await asyncio.sleep(0)


class VirtualClock(TimeService):
    """Deterministic virtual time service for sub-millisecond async testing.

    Maintains a simulated monotonic clock and uses a priority min-heap to
    order async timer callbacks deterministically without real-world wall-clock delay.
    """

    def __init__(self, initial_time: float = 1000.0):
        self._current_time: float = float(initial_time)
        self._timer_heap: List[Tuple[float, int, asyncio.Future]] = []
        self._counter: int = 0
        self.sleep_history: List[float] = []
        self._cancelled_since_compaction: int = 0

    def time(self) -> float:
        """Returns current virtual monotonic time in seconds."""
        return self._current_time

    async def sleep(self, seconds: float) -> None:
        """Schedules execution resumption at current_time + seconds."""
        if seconds < 0:
            seconds = 0.0

        self.sleep_history.append(seconds)

        if seconds == 0:
            await asyncio.sleep(0)
            return

        wake_time = self._current_time + seconds
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._counter += 1
        heapq.heappush(self._timer_heap, (wake_time, self._counter, future))

        try:
            await future
        except asyncio.CancelledError:
            # Task was cancelled while sleeping; clean up future if still open
            if not future.done():
                future.cancel()
            self._cancelled_since_compaction += 1
            self._compact_cancelled_timers()
            raise

    def _compact_cancelled_timers(self, force: bool = False) -> None:
        """Remove cancelled futures when lazy heap deletion becomes costly."""
        if not force and self._cancelled_since_compaction < 64:
            return
        self._timer_heap = [item for item in self._timer_heap if not item[2].done()]
        heapq.heapify(self._timer_heap)
        self._cancelled_since_compaction = 0

    async def advance(self, seconds: float) -> float:
        """Advances virtual time by `seconds`, triggering scheduled timers in chronological order.

        Args:
            seconds: Non-negative seconds to advance.

        Returns:
            New virtual time.

        Raises:
            ValueError: If seconds is negative.
        """
        if seconds < 0:
            raise ValueError(f"Cannot advance virtual clock backwards: {seconds}s")

        # Yield first to let any newly scheduled tasks register their sleep timers
        await asyncio.sleep(0)

        target_time = self._current_time + seconds

        while self._timer_heap and self._timer_heap[0][0] <= target_time:
            next_wake_time = self._timer_heap[0][0]
            self._current_time = next_wake_time

            # Collect and resolve all timers ready at this exact virtual timestamp
            ready_futures: List[asyncio.Future] = []
            while self._timer_heap and self._timer_heap[0][0] <= next_wake_time:
                _, _, fut = heapq.heappop(self._timer_heap)
                if not fut.done():
                    ready_futures.append(fut)

            for fut in ready_futures:
                fut.set_result(None)

            # Yield control to the event loop so awakened tasks can execute and schedule subsequent steps
            await asyncio.sleep(0)

        self._current_time = target_time
        await asyncio.sleep(0)
        return self._current_time

    def advance_sync(self, seconds: float) -> float:
        """Synchronous stepping variant for non-async callbacks or immediate state updates."""
        if seconds < 0:
            raise ValueError(f"Cannot advance virtual clock backwards: {seconds}s")
        target_time = self._current_time + seconds
        while self._timer_heap and self._timer_heap[0][0] <= target_time:
            wake_time, _, fut = heapq.heappop(self._timer_heap)
            if not fut.done():
                self._current_time = wake_time
                fut.set_result(None)
        self._current_time = target_time
        return self._current_time

    @property
    def pending_timers_count(self) -> int:
        """Returns the number of active, uncompleted timers."""
        return sum(1 for _, _, fut in self._timer_heap if not fut.done())

    def reset(self, initial_time: float = 1000.0) -> None:
        """Cancels all pending timers and resets the virtual clock."""
        while self._timer_heap:
            _, _, fut = heapq.heappop(self._timer_heap)
            if not fut.done():
                fut.cancel()
        self._current_time = float(initial_time)
        self._counter = 0
        self._cancelled_since_compaction = 0
        self.sleep_history.clear()
