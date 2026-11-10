"""One delayed media effect, owned by a successfully delivered primary turn."""
from __future__ import annotations

import asyncio
import logging

from .platform_bridge import SendResult

logger = logging.getLogger(__name__)


class DeferredReplyMedia:
    def __init__(self, engine, runtime, item):
        self.engine, self.runtime, self.item = engine, runtime, item
        self._ready = asyncio.Event()
        self._primary_delivered = False
        self._attempted = False
        self._lock = asyncio.Lock()
        self._primary_node = None
        self._ready_at = 0.0
        self._source_check = None
        self.reply_text = ""

    def confirm(self, source_check=None, *, text=""):
        self._primary_delivered = True
        self._primary_node = self.runtime.last_bot_node
        self._source_check = source_check
        self.reply_text = str(text)

    def finish(self):
        self._ready_at = asyncio.get_running_loop().time()
        self._ready.set()

    async def wait_ready(self):
        try:
            timeout = float(self.engine.plugin._runtime_config.tool_agent_timeout) + 30
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return await self.is_current()

    async def is_current(self):
        if (not self._ready.is_set() or not self._primary_delivered or self._attempted
                or asyncio.get_running_loop().time() - self._ready_at > 45
                or self.runtime.last_bot_node is not self._primary_node):
            return False
        if not await self.engine.valid_async(self.runtime, self.item):
            return False
        if callable(self._source_check) and not await self._source_check():
            return False
        return True

    async def send(self, chain, operation):
        """Keep platform-specific sticker sending inside CD's normal send lease."""
        if not await self.wait_ready():
            return False
        async with self._lock:
            async def deliver():
                async with self.runtime.send_lock:
                    if not await self.is_current():
                        return False
                    self._attempted = True  # no retry of an uncertain platform effect
                    event = self.item.events[-1]

                    async def effect(original_event):
                        try:
                            if callable(self._source_check) and not await self._source_check():
                                return SendResult(False, error="native_memory_source_invalidated")
                            if (not await self.engine.valid_async(self.runtime, self.item)
                                    or self.runtime.last_bot_node is not self._primary_node):
                                return SendResult(False, error="deferred_media_parent_changed")
                            sent = await operation(original_event)
                            return SendResult(bool(sent), error=None if sent else "deferred_media_not_sent")
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            return SendResult(False, error=type(exc).__name__, delivery_uncertain=True)

                    result = await self.engine.plugin._send_owned(
                        self.runtime, event, chain, send_effect=effect,
                        native_source_validator=self._source_check)
                    self.engine.plugin._metric("deferred_media_sent" if result.success else "deferred_media_failed")
                    logger.info("[ChatDynamics] deferred media session=%s success=%s uncertain=%s",
                                self.runtime.session_key, result.success, result.delivery_uncertain)
                    return result.success

            return await self.engine.run_owned(self.runtime, self.item.context.author, deliver, timeout=30)
