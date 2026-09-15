"""Own periodic draft generation and serialize manual/automatic work per UMO."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .annotation_review import AnnotationReview

logger = logging.getLogger(__name__)


class AnnotationDraftScheduler:
    def __init__(self, host: Any) -> None:
        self.host = host
        self.started = False
        self.closed = False
        self.loop_task: asyncio.Task | None = None
        self.jobs: dict[str, asyncio.Task] = {}
        self._tokens: dict[str, object] = {}
        self._retired: set[asyncio.Task] = set()
        self._signature: tuple = ()

    def _cancel(self, task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()
            self._retired.add(task)
            task.add_done_callback(self._retired.discard)

    def automatic_enabled(self) -> bool:
        return bool(not self.closed and not getattr(self.host, "_shutting_down", False)
                    and getattr(self.host, "enabled", False)
                    and getattr(self.host, "annotation_draft_enabled", False)
                    and getattr(self.host, "annotation_draft_auto_enabled", False))

    def start(self) -> None:
        self.started = True
        self.configure()

    def configure(self) -> None:
        signature = tuple(getattr(self.host, name, None) for name in (
            "enabled", "annotation_draft_enabled", "annotation_draft_auto_enabled",
            "annotation_draft_interval_minutes", "draft_provider_id", "provider_id",
            "annotation_draft_limit", "annotation_draft_timeout",
        )) + (getattr(self.host, "takeover_all", False),
              tuple(sorted(getattr(self.host, "takeover_groups", ()))),
              tuple(sorted(getattr(self.host, "exclude_groups", ()))))
        changed = signature != self._signature
        if changed:
            self._signature = signature
            self._tokens.clear()
            self._cancel(self.loop_task)
            self.loop_task = None
            for task in list(self.jobs.values()):
                self._cancel(task)
        if self.started and self.automatic_enabled() and (
                self.loop_task is None or self.loop_task.done()):
            self.loop_task = asyncio.create_task(self._run(), name="chat-dynamics-auto-drafts")

    def cancel_session(self, session_key: str) -> None:
        self._tokens.pop(session_key, None)
        self._cancel(self.jobs.get(session_key))

    def _eligible(self, session_key: str) -> bool:
        runtime = getattr(self.host, "_sessions", {}).get(session_key)
        return bool(runtime is not None and getattr(runtime, "bot_id", "")
                    and self.host.is_group_takeover_enabled(runtime.group_id)
                    and session_key in self.host.dags)

    async def generate(self, session_key: str, *, refresh: bool = False,
                       automatic: bool = False) -> dict[str, Any]:
        if self.closed or getattr(self.host, "_shutting_down", False):
            return {"state": "cancelled", "reason": "插件正在关闭", "session_key": session_key}
        running = self.jobs.get(session_key)
        if running is not None and not running.done():
            return {"state": "busy", "reason": "该会话正在生成 AI 草稿，请稍后查看", "session_key": session_key}
        if automatic and (not self.automatic_enabled() or not self._eligible(session_key)):
            return {"state": "disabled", "reason": "自动生成未开启或会话不在生效范围", "session_key": session_key}
        token = object()
        self._tokens[session_key] = token
        task = asyncio.create_task(AnnotationReview(self.host).annotation_draft_payload(
            session_key, refresh=refresh, automatic=automatic,
            is_valid=lambda: not self.closed and self._tokens.get(session_key) is token))
        self.jobs[session_key] = task
        try:
            # wait() distinguishes a cancelled child from a cancelled caller on
            # Python 3.10 too (Task.cancelling() is only available on newer hosts).
            await asyncio.wait({task})
            if task.cancelled():
                return {"state": "cancelled", "reason": "会话或设置已变更，本次生成已取消", "session_key": session_key}
            return task.result()
        except asyncio.CancelledError:
            self._cancel(task)
            raise
        finally:
            if self.jobs.get(session_key) is task:
                self.jobs.pop(session_key, None)
            if self._tokens.get(session_key) is token:
                self._tokens.pop(session_key, None)

    async def run_once(self) -> None:
        for session_key in list(self.host.dags):
            if not self.automatic_enabled():
                break
            if not self._eligible(session_key):
                continue
            try:
                await self.generate(session_key, automatic=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Automatic draft generation failed type=%s", type(exc).__name__)

    async def _run(self) -> None:
        while self.automatic_enabled():
            interval = float(self.host.annotation_draft_interval_minutes) * 60.0
            await self.host.time_service.sleep(interval)
            await self.run_once()

    async def close(self) -> None:
        self.closed = True
        self._tokens.clear()
        tasks = set(self.jobs.values()) | self._retired
        if self.loop_task is not None:
            tasks.add(self.loop_task)
        for task in tasks:
            self._cancel(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self._retired.clear()
        self.loop_task = None
