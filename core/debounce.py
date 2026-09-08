"""Debounce Buffer Engine for AstrBot Group Chat Dynamics.

Implements sliding-window turn-taking debounce per (session_id, user_id),
dynamic incompleteness window stretching, hard cap cut-offs, concurrency locks,
turn consolidation, and graceful lifecycle shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .incompleteness import IncompletenessDetector
from .time_service import SystemClock, TimeService

logger = logging.getLogger("astrbot_plugin_chat_dynamics.debounce")


@dataclass
class DebounceItem:
    """Represents a single message ingested into the debounce buffer."""

    text: str
    event: Any
    timestamp: float


@dataclass
class DebounceResult:
    """Consolidated conversational turn resulting from a flushed debounce window."""

    session_id: str
    user_id: str
    consolidated_text: str
    messages: List[Any]  # list of DebounceItem
    raw_events: List[Any]  # list of original AstrMessageEvent objects
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def first_event(self) -> Optional[Any]:
        """Returns the earliest event in this conversational turn."""
        return self.raw_events[0] if self.raw_events else None

    @property
    def last_event(self) -> Optional[Any]:
        """Returns the latest event in this conversational turn (used for reply dispatch)."""
        return self.raw_events[-1] if self.raw_events else None

    @property
    def start_time(self) -> float:
        """Timestamp of the first message in the turn."""
        return self.metadata.get("start_time", 0.0)

    @property
    def end_time(self) -> float:
        """Timestamp of the final message in the turn."""
        return self.metadata.get("end_time", 0.0)

    @property
    def duration(self) -> float:
        """Total duration of the conversational turn in seconds."""
        return self.metadata.get("duration", 0.0)

    @property
    def message_count(self) -> int:
        """Total count of merged message fragments."""
        return len(self.raw_events)

    @property
    def was_extended(self) -> bool:
        """Whether this turn was extended by incompleteness heuristics."""
        return self.metadata.get("was_extended", False)


class _DebounceSlot:
    """Internal state machine and queue for a specific (session_id, user_id) tuple."""

    def __init__(self, session_id: str, user_id: str):
        self.session_id: str = session_id
        self.user_id: str = user_id
        self.items: List[DebounceItem] = []
        self.start_time: float = 0.0
        self.lock: asyncio.Lock = asyncio.Lock()
        self.timer_task: Optional[asyncio.Task] = None
        self.epoch: int = 0
        self.last_touch_time: float = 0.0
        self.session_generation: int = 0
        self.user_generation: int = 0
        self.was_extended: bool = False
        self.dropped_fragments: int = 0
        self.on_flush: Optional[Callable[[DebounceResult], Awaitable[None]]] = None

    @property
    def is_empty(self) -> bool:
        return len(self.items) == 0

    @property
    def has_active_timer(self) -> bool:
        return self.timer_task is not None and not self.timer_task.done()


class DefaultIncompletenessDetector:
    """Fallback zero-dependency detector used when IncompletenessDetector is unavailable."""

    import re

    _CONJUNCTIONS_RE = re.compile(
        r"(因为|所以|但是|但|而且|不过|如果|要是|虽然|然后|还有|以及|或者|另外|结果|话说|就是|比如|并且|"
        r"\b(and|or|but|because|so|if|though|although|also|then|like|which|when|while|since|cos|cuz))\s*$",
        re.IGNORECASE,
    )
    _PUNCT_RE = re.compile(r"(\.\.\.|…|---+|——+|~+|～+|[,，、\\:：;；])\s*$")
    _BRACKET_PAIRS = [("(", ")"), ("[", "]"), ("{", "}"), ("（", "）"), ("【", "】"), ("《", "》")]

    def is_incomplete(self, text: str) -> bool:
        if not text:
            return False
        clean = text.strip()
        if not clean:
            return False

        if self._CONJUNCTIONS_RE.search(clean):
            return True
        if self._PUNCT_RE.search(clean):
            return True
        if clean.count("```") % 2 != 0:
            return True
        for open_b, close_b in self._BRACKET_PAIRS:
            if clean.count(open_b) > clean.count(close_b):
                return True
        return False


class DebounceBuffer:
    """Thread-safe, sliding-window debounce buffer for group chat dynamics."""

    def __init__(
        self,
        time_service: Optional[TimeService] = None,
        base_cooldown: float = 3.5,
        extended_cooldown: float = 6.5,
        max_cap: float = 12.0,
        incompleteness_detector: Optional[Any] = None,
        max_fragments: int = 32,
        max_turn_chars: int = 8000,
    ):
        """Initializes the DebounceBuffer.

        Args:
            time_service: Monotonic time service implementation (SystemClock or VirtualClock).
                Defaults to SystemClock() if None.
            base_cooldown: Base sliding-window cooldown in seconds (default 3.5s).
            extended_cooldown: Extended cooldown when incompleteness detected (default 6.5s).
            max_cap: Hard maximum limit for any single turn in seconds (default 12.0s).
            incompleteness_detector: Optional custom detector instance; falls back to
                IncompletenessDetector() or DefaultIncompletenessDetector().
        """
        self.time_service: TimeService = time_service if time_service is not None else SystemClock()
        self.base_cooldown: float = float(base_cooldown)
        self.extended_cooldown: float = float(extended_cooldown)
        self.max_cap: float = float(max_cap)
        self.max_fragments: int = max(1, int(max_fragments))
        self.max_turn_chars: int = max(256, int(max_turn_chars))

        if incompleteness_detector is not None:
            self.detector = incompleteness_detector
        else:
            try:
                self.detector = IncompletenessDetector()
            except Exception:
                self.detector = DefaultIncompletenessDetector()

        self._slots: Dict[Tuple[str, str], _DebounceSlot] = {}
        self._master_lock: asyncio.Lock = asyncio.Lock()
        self._is_closed: bool = False
        self._background_tasks: set[asyncio.Task] = set()
        self._session_generations: Dict[str, int] = {}
        # Member stops invalidate only one (session, user) slot; reset still
        # uses the broader session generation above.
        self._user_generations: Dict[Tuple[str, str], int] = {}

    def _create_task(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _cancel_and_wait_background(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._background_tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def check_incompleteness(self, text: str) -> bool:
        """Evaluates whether the provided text contains conversational incompleteness cues."""
        if hasattr(self.detector, "is_incomplete"):
            return bool(self.detector.is_incomplete(text))
        if hasattr(self.detector, "check_incompleteness"):
            return bool(self.detector.check_incompleteness(text))
        if callable(self.detector):
            return bool(self.detector(text))
        return False

    async def ingest(
        self,
        session_id: str,
        user_id: str,
        text: str,
        event: Any,
        on_flush: Callable[[DebounceResult], Awaitable[None]],
        *,
        defer_callback: bool = False,
    ) -> None:
        """Ingests an incoming message into the user's debounce window.

        Args:
            session_id: Identifier for the conversation/group session.
            user_id: Identifier for the message author.
            text: Message text content.
            event: Raw platform event object (AstrMessageEvent).
            on_flush: Async callback invoked when the turn flushes.
        """
        # TC-09: Empty or whitespace-only messages are ignored
        if text is None or not text.strip():
            return

        now = self.time_service.time()
        key = (session_id, user_id)
        result: Optional[DebounceResult] = None

        # 1. Retrieve or create slot under master lock
        async with self._master_lock:
            if self._is_closed:
                raise RuntimeError("DebounceBuffer has been closed")
            if key not in self._slots:
                self._slots[key] = _DebounceSlot(session_id, user_id)
            slot = self._slots[key]
            slot.session_generation = self._session_generations.get(session_id, 0)
            slot.user_generation = self._user_generations.get(key, 0)

        # 2. Acquire per-slot lock for serialization
        async with slot.lock:
            if self._is_closed:
                raise RuntimeError("DebounceBuffer has been closed")
            slot.last_touch_time = now
            slot.on_flush = on_flush

            # If this message opens a new turn
            if not slot.items:
                slot.start_time = now
                slot.was_extended = False
                slot.dropped_fragments = 0

            # Bound the retained fragment itself as well as the consolidated
            # turn. This keeps a single oversized adapter payload from
            # occupying unbounded memory while preserving both its head and
            # tail for diagnostics and incompleteness checks.
            item_text = self._merge_texts([str(text)], max_chars=self.max_turn_chars)
            item = DebounceItem(text=item_text, event=event, timestamp=now)
            slot.items.append(item)
            if len(slot.items) > self.max_fragments:
                slot.dropped_fragments += len(slot.items) - self.max_fragments
                del slot.items[: len(slot.items) - self.max_fragments]

            # 3. Form candidate text to evaluate incompleteness
            candidate_text = self._merge_texts([it.text for it in slot.items], max_chars=self.max_turn_chars)
            is_incomplete = self.check_incompleteness(candidate_text)
            if is_incomplete:
                slot.was_extended = True

            # 4. Calculate elapsed time and hard cap boundaries
            elapsed = now - slot.start_time
            max_remaining = self.max_cap - elapsed

            # Boundary condition: Hard cap reached or exceeded -> immediate flush
            if max_remaining <= 0.0:
                result = self._extract_result_locked(slot)
                # Release slot.lock before awaiting callback
            else:
                # Select target cooldown: dynamic stretching
                if is_incomplete:
                    desired_cooldown = self.extended_cooldown
                else:
                    desired_cooldown = self.base_cooldown

                target_cooldown = min(desired_cooldown, max_remaining)

                # 5. Reschedule timer
                slot.epoch += 1
                current_epoch = slot.epoch

                if slot.timer_task and not slot.timer_task.done():
                    slot.timer_task.cancel()

                slot.timer_task = self._create_task(
                    self._timer_worker(slot, current_epoch, target_cooldown)
                )

        # Yield control outside slot.lock so timer_worker can start sleeping immediately
        await asyncio.sleep(0)

        # If hard cap cut-off was reached, invoke callback outside slot.lock.
        # The plugin orchestrator can defer this one extra scheduling hop while
        # holding its session state lock; otherwise the callback would try to
        # acquire the same lock recursively.
        if result is not None and on_flush is not None and self.is_result_current(result):
            if defer_callback:
                self._create_task(self._safe_invoke_callback(on_flush, result))
            else:
                try:
                    await on_flush(result)
                except Exception as e:
                    logger.error("Error executing on_flush callback on hard cap code=CD_DEBOUNCE_CALLBACK type=%s", type(e).__name__)

    async def _timer_worker(self, slot: _DebounceSlot, epoch: int, delay: float) -> None:
        """Asynchronous worker executing the cooldown delay."""
        try:
            await self.time_service.sleep(delay)
        except asyncio.CancelledError:
            # Expected when cancelled by a newer message or flush_all()
            return
        except Exception as e:
            logger.error("Error during debounce sleep code=CD_DEBOUNCE_TIMER type=%s", type(e).__name__)
            return

        result: Optional[DebounceResult] = None
        callback: Optional[Callable[[DebounceResult], Awaitable[None]]] = None

        # Acquire lock to safely extract items
        async with slot.lock:
            # Stale timer check: epoch mismatch means a newer message rescheduled
            if slot.epoch != epoch:
                return
            if not slot.items:
                return

            result = self._extract_result_locked(slot)
            callback = slot.on_flush

        # Await callback outside the slot lock to prevent deadlocks and race conditions
        if result is not None and callback is not None and self.is_result_current(result):
            try:
                await callback(result)
            except Exception as e:
                logger.error("Error executing debounce on_flush callback code=CD_DEBOUNCE_CALLBACK type=%s", type(e).__name__)

    def _extract_result_locked(self, slot: _DebounceSlot) -> Optional[DebounceResult]:
        """Extracts items and resets slot state. Must be called while holding slot.lock."""
        if not slot.items:
            return None

        items_to_flush = list(slot.items)
        slot.items.clear()
        start_time = slot.start_time
        was_extended = slot.was_extended
        slot.start_time = 0.0
        slot.was_extended = False

        if slot.timer_task and not slot.timer_task.done() and slot.timer_task is not asyncio.current_task():
            slot.timer_task.cancel()
        slot.timer_task = None
        slot.epoch += 1

        end_time = items_to_flush[-1].timestamp
        duration = max(0.0, end_time - start_time)
        consolidated_text = self._merge_texts([it.text for it in items_to_flush], max_chars=self.max_turn_chars)
        raw_events = [it.event for it in items_to_flush]

        metadata = {
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "message_count": len(items_to_flush),
            "was_extended": was_extended,
            "first_event": raw_events[0] if raw_events else None,
            "last_event": raw_events[-1] if raw_events else None,
            "buffer_generation": slot.session_generation,
            "buffer_user_generation": slot.user_generation,
            "truncated": bool(slot.dropped_fragments) or len(consolidated_text) < sum(len(it.text) for it in items_to_flush),
            "dropped_fragments": slot.dropped_fragments,
        }

        return DebounceResult(
            session_id=slot.session_id,
            user_id=slot.user_id,
            consolidated_text=consolidated_text,
            messages=items_to_flush,
            raw_events=raw_events,
            metadata=metadata,
        )

    async def flush(
        self, session_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> List[DebounceResult]:
        """Forces an immediate flush for specified slot or all matching slots.

        Args:
            session_id: Optional session filter.
            user_id: Optional user filter.

        Returns:
            List of flushed DebounceResult instances.
        """
        results: List[DebounceResult] = []
        callbacks_to_invoke: List[Tuple[Callable[[DebounceResult], Awaitable[None]], DebounceResult]] = []

        async with self._master_lock:
            target_keys = [
                key
                for key in self._slots.keys()
                if (session_id is None or key[0] == session_id)
                and (user_id is None or key[1] == user_id)
            ]
            slots = [self._slots[key] for key in target_keys]

        for slot in slots:
            async with slot.lock:
                res = self._extract_result_locked(slot)
                if res:
                    results.append(res)
                    if slot.on_flush:
                        callbacks_to_invoke.append((slot.on_flush, res))

        callback_tasks = [
            self._create_task(self._safe_invoke_callback(callback, res))
            for callback, res in callbacks_to_invoke
        ]
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        else:
            # Allow cancelled timer tasks to execute CancelledError cleanup.
            await asyncio.sleep(0)

        return results

    async def _safe_invoke_callback(
        self, callback: Callable[[DebounceResult], Awaitable[None]], res: DebounceResult
    ) -> None:
        try:
            if not self.is_result_current(res):
                return
            await callback(res)
        except Exception as e:
            logger.error("Error executing on_flush in manual flush code=CD_DEBOUNCE_CALLBACK type=%s", type(e).__name__)

    async def flush_all(self) -> List[DebounceResult]:
        """Flushes all pending slots immediately across all sessions and users."""
        return await self.flush(session_id=None, user_id=None)

    def current_generations(self, session_id: str, user_id: str) -> tuple[int, int]:
        """Return (session_generation, user_generation) for a synthetic flush."""
        return (
            int(self._session_generations.get(session_id, 0)),
            int(self._user_generations.get((session_id, user_id), 0)),
        )

    def is_result_current(self, result: DebounceResult) -> bool:
        """Return whether a flushed result predates the latest session reset."""
        expected = int(result.metadata.get("buffer_generation", 0))
        if expected != self._session_generations.get(result.session_id, 0):
            return False
        user_key = (result.session_id, result.user_id)
        expected_user = int(result.metadata.get("buffer_user_generation", 0))
        return expected_user == self._user_generations.get(user_key, 0)

    async def discard(self, session_id: str, user_id: Optional[str] = None) -> int:
        """Discard pending items for one session without invoking callbacks.

        The session generation advances before slot locks are acquired so a
        timer that has already extracted a result cannot re-inject stale state.
        """
        async with self._master_lock:
            if user_id is None:
                self._session_generations[session_id] = self._session_generations.get(session_id, 0) + 1
            else:
                user_key = (session_id, user_id)
                self._user_generations[user_key] = self._user_generations.get(user_key, 0) + 1
            target_keys = [
                key for key in self._slots
                if key[0] == session_id and (user_id is None or key[1] == user_id)
            ]
            slots = [(key, self._slots[key]) for key in target_keys]

        discarded = 0
        for _key, slot in slots:
            async with slot.lock:
                discarded += len(slot.items)
                if slot.timer_task and not slot.timer_task.done():
                    slot.timer_task.cancel()
                slot.items.clear()
                slot.timer_task = None
                slot.epoch += 1
                slot.start_time = 0.0
                slot.was_extended = False
                slot.on_flush = None
                slot.session_generation = self._session_generations.get(session_id, 0)
                slot.user_generation = self._user_generations.get((session_id, slot.user_id), 0)

        async with self._master_lock:
            for key, slot in slots:
                if self._slots.get(key) is slot and slot.is_empty and not slot.has_active_timer:
                    self._slots.pop(key, None)
        await asyncio.sleep(0)
        return discarded

    async def close(self, *, flush: bool = True) -> None:
        """Shuts down the buffer.

        Args:
            flush: When True (default), pending turns are flushed and on_flush runs.
                When False, timers are cancelled and items are discarded with no callbacks.
        """
        async with self._master_lock:
            self._is_closed = True

        if flush:
            await self.flush_all()
            await self._cancel_and_wait_background()
            return
        await self._discard_all()
        await self._cancel_and_wait_background()

    async def _discard_all(self) -> None:
        """Cancels timers and drops pending items without invoking on_flush."""
        async with self._master_lock:
            slots = list(self._slots.values())

        for slot in slots:
            async with slot.lock:
                if slot.timer_task and not slot.timer_task.done():
                    slot.timer_task.cancel()
                slot.items.clear()
                slot.timer_task = None
                slot.epoch += 1
                slot.start_time = 0.0
                slot.was_extended = False
                slot.on_flush = None

        async with self._master_lock:
            self._slots.clear()
            self._session_generations.clear()
            self._user_generations.clear()

        await asyncio.sleep(0)

    def get_pending_count(self, session_id: Optional[str] = None, user_id: Optional[str] = None) -> int:
        """Returns total count of pending messages in the buffer."""
        count = 0
        for (s_id, u_id), slot in self._slots.items():
            if (session_id is None or s_id == session_id) and (user_id is None or u_id == user_id):
                count += len(slot.items)
        return count

    def is_active(self, session_id: str, user_id: str) -> bool:
        """Checks if an active timer or pending buffer exists for the specified slot."""
        key = (session_id, user_id)
        slot = self._slots.get(key)
        if not slot:
            return False
        return not slot.is_empty or slot.has_active_timer

    def has_active_session(self, session_id: str) -> bool:
        return any(
            key[0] == session_id and (not slot.is_empty or slot.has_active_timer)
            for key, slot in self._slots.items()
        )

    def last_activity(self, session_id: str) -> float:
        return max(
            (slot.last_touch_time for key, slot in self._slots.items() if key[0] == session_id),
            default=0.0,
        )

    def prune_idle_slots(self, max_idle_seconds: float = 300.0) -> int:
        """Prunes empty, inactive slots older than max_idle_seconds to prevent memory growth.

        Returns:
            Number of pruned slots.
        """
        now = self.time_service.time()
        pruned = 0
        keys_to_remove = []

        for key, slot in list(self._slots.items()):
            if slot.is_empty and not slot.has_active_timer:
                if (now - slot.last_touch_time) > max_idle_seconds:
                    keys_to_remove.append(key)

        for key in keys_to_remove:
            self._slots.pop(key, None)
            pruned += 1

        return pruned

    def _merge_texts(self, texts: List[str], max_chars: Optional[int] = None) -> str:
        """Merges a list of message text fragments into a single consolidated string."""
        non_empty = [t.strip() for t in texts if t and t.strip()]
        if not non_empty:
            return ""
        merged = "\n".join(non_empty)
        if max_chars is None or len(merged) <= max_chars:
            return merged
        marker = "\n[内容已截断]\n"
        budget = max(0, max_chars - len(marker))
        head = budget // 2
        tail = budget - head
        return merged[:head] + marker + (merged[-tail:] if tail else "")
