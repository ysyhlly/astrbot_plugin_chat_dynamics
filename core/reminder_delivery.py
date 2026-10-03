"""Deliver configured one-shot group reminders and ack only successful sends."""
from __future__ import annotations

import asyncio
import logging

from .platform_bridge import send_plain

logger = logging.getLogger(__name__)


class ReminderDelivery:
    def __init__(self, plugin):
        self.plugin = plugin
        self.task = None
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.delivered = set()
        self.sending = {}

    def cancel(self, umo=None, item_id=None):
        for task, (owner, reminder_id) in tuple(self.sending.items()):
            if (umo is None or owner == umo) and (item_id is None or reminder_id == item_id) and not task.done():
                task.cancel()

    def start(self):
        if self.plugin._shutting_down:
            return
        if self.task is None or self.task.done():
            self.task = self.plugin._create_background_task(self.run())
        self.wake.set()

    async def run(self):
        while not self.plugin._shutting_down:
            self.wake.clear()
            await self.dispatch()
            try:
                await asyncio.wait_for(self.wake.wait(), 30)
            except asyncio.TimeoutError:
                pass

    async def dispatch(self):
        p = self.plugin
        async with self.lock:
            if p._shutting_down or p.shadow_mode or not p.group_memory.enabled:
                return
            for umo in p.group_memory.known_sessions():
                try:
                    parts = umo.rsplit(":", 2)
                    if len(parts) != 3 or parts[1].lower() != "groupmessage":
                        continue
                    group_id = parts[2]
                    if not p.is_group_takeover_enabled(group_id):
                        continue
                    for receipt in tuple(self.delivered):
                        if receipt[0] == umo:
                            p.group_memory.mark_reminder_sent(umo, receipt[1], now=p.time_service.wall_time())
                            self.delivered.discard(receipt)
                    key = p._resolve_session_key(umo) or umo
                    if p.arbiter.is_in_deep_cooling(key, current_time=p.time_service.time()):
                        continue
                    for row in p.group_memory.due_reminders(umo, now=p.time_service.wall_time()):
                        receipt = (umo, row["id"])
                        if p._shutting_down or p.shadow_mode or not p.is_group_takeover_enabled(group_id):
                            return
                        runtime = p._sessions.get(key)
                        if runtime is None:
                            if not p._ensure_runtime_capacity(key):
                                continue
                            runtime = p._get_or_create_runtime(key, group_id=group_id, umo=umo)
                        async with runtime.send_lock:
                            # Policies and notebook rows can change while waiting for a send.
                            current = p.group_memory.due_reminders(umo, now=p.time_service.wall_time())
                            if (p._shutting_down or p.shadow_mode or not p.is_group_takeover_enabled(group_id)
                                    or p.arbiter.is_in_deep_cooling(key, current_time=p.time_service.time())
                                    or not any(item["id"] == row["id"] for item in current)):
                                continue
                            text = "提醒：" + row["text"]
                            if not p.group_memory.begin_reminder_send(umo, row['id'], now=p.time_service.wall_time()):
                                continue
                            sending = p._create_background_task(send_plain(None, p.context, umo, text))
                            runtime.owned_turn_tasks[sending] = '__reminder__'
                            self.sending[sending] = (umo, row['id'])
                            try:
                                result = await asyncio.wait_for(sending, 30)
                            except asyncio.CancelledError:
                                self._uncertain(umo, row['id'])
                                if asyncio.current_task().cancelling():
                                    raise
                                continue
                            except Exception:
                                self._uncertain(umo, row['id'])
                                raise
                            finally:
                                runtime.owned_turn_tasks.pop(sending, None)
                                self.sending.pop(sending, None)
                            if not result.success:
                                p._metric("send_failed")
                                p.group_memory.finish_reminder_attempt(
                                    umo, row['id'], rejected=not result.delivery_uncertain)
                                continue
                            # Keep an in-process receipt if persistence needs a retry.
                            self.delivered.add(receipt)
                            if result.message_id:
                                runtime.remember_sent(result.message_id)
                            if p._sessions.get(key) is runtime:
                                node = runtime.dag.add_message(
                                    msg_id=result.message_id or p._next_outgoing_id(),
                                    user_id=runtime.bot_id or "bot", text=text,
                                    timestamp=p.time_service.time(),
                                    metadata={"reminder_id": row["id"], "platform_message_id": bool(result.message_id),
                                              'sender_is_bot': True, 'bot_identity_pending': not bool(runtime.bot_id)})
                                runtime.last_bot_node = node
                                p._last_bot_nodes[key] = node
                            p._metric("send_succeeded")
                            p.group_memory.mark_reminder_sent(umo, row["id"], now=p.time_service.wall_time())
                            self.delivered.discard(receipt)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("reminder delivery deferred type=%s", type(exc).__name__)

    def _uncertain(self, umo, item_id):
        try:
            self.plugin.group_memory.finish_reminder_attempt(umo, item_id)
        except Exception as exc:
            # The durable 'sending' state already prevents automatic redelivery.
            logger.warning('reminder receipt remains uncertain type=%s', type(exc).__name__)
