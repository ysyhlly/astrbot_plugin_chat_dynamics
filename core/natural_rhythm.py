"""Natural reply rhythm: away/busy catch-up, delayed afterthought, and online pacing.

Coordinates three humanlike conversational rhythm behaviors inside Chat Dynamics:
1. Away/busy deferral + chronological catch-up over a bounded lookback window
   (default 30 min public, 120 min direct @/quote/private) with Read Watermark,
   resolution-aware selection (typically 1-2 items), and pre-send re-verification.
2. Post-delivery delayed afterthought timed from the last successfully sent
   fragment (default 1-4 min, max once per turn, never nagging, silence-gated).
3. Demand-driven scheduler waking only sessions with pending work, plus
   lightweight persistence with reload-safe uncertain-delivery deduplication.
"""

from __future__ import annotations

import asyncio
import copy
import heapq
import random
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Literal, Optional, Sequence, Tuple

from astrbot.api import logger

from .deferred_media import preserve_event_media, release_event_media
from .platform_bridge import iter_message_components
from .time_service import TimeService


AwayKind = Literal["none", "micro_distract", "world_busy", "world_asleep"]
AttemptStatus = Literal["pending", "in_flight", "sent", "uncertain", "failed", "skipped"]


_URGENT_RE = re.compile(
    r"(紧急|十万火急|急事|很急|着急|快看|赶紧|立刻|马上|救命|在线等|"
    r"等你决定|就等你|快回|来不及|出大事|报错了|崩了|卡死了|"
    r"\b(?:urgent|asap|emergency|right\s+now)\b)",
    re.IGNORECASE,
)
_CLOSING_RE = re.compile(
    r"^(?:晚安|好梦|睡了|去睡|拜拜|再见|回头见|先忙|去忙|好的|好哒|好滴|知道了|明白|收到|嗯嗯|哦哦|行|ok|okay|gn|bye|good\s*night)[。！!~～.\s]*$",
    re.IGNORECASE,
)
_RESOLVED_RE = re.compile(
    r"(没事了|已经解决|解决了|搞定了|弄好了|不用了|算了不用|已经定好|定好了|就定|敲定了|买好了|找到了|"
    r"\b(?:never\s*mind|nvm|solved|fixed|all\s+set)\b)",
    re.IGNORECASE,
)
_PURE_FILLER_RE = re.compile(
    r"^(?:嗯+|哦+|啊+|哈+|草+|6+|ok|好的|行|收到|笑死|确实|是的|对+|好家伙|牛|赞|\+1|？+|\?+|!+|！+|\.+|…+|[~～\s]+)$",
    re.IGNORECASE,
)
_SILENT_MARKERS = frozenset({"", "NONE", "SKIP", "SILENT", "NO_AFTERTHOUGHT", "[SILENT]", "[无补充]", "无需补充"})

REASON_ZH: Dict[str, str] = {
    "rhythm_deferred_busy": "生活状态忙碌·稍后回看",
    "rhythm_deferred_asleep": "生活状态睡眠·醒来回看",
    "rhythm_deferred_distract": "短暂分心没看手机·稍后回看",
    "rhythm_urgent_breakthrough": "紧急/催促·立即破防回应",
    "rhythm_catchup_sent": "暂离回来·按序补回旧话",
    "rhythm_catchup_skipped_resolved": "回看跳过·旧话已解决或过时",
    "rhythm_catchup_skipped_expired": "回看跳过·超出回看时间窗",
    "rhythm_catchup_skipped_recalled": "回看跳过·原消息已撤回",
    "rhythm_catchup_skipped_uncertain": "回看跳过·重载前投递结果不确定",
    "rhythm_catchup_skipped_stopped": "回看跳过·成员已停止或边界拦截",
    "rhythm_catchup_skipped_persona": "回看跳过·角色或会话状态已变化",
    "rhythm_catchup_skipped_limit": "回看跳过·达到单次补回上限",
    "rhythm_catchup_skipped_superseded": "回看跳过·新进展已覆盖旧消息",
    "rhythm_catchup_skipped_unworthy": "回看跳过·决策模型判定无需补回",
    "rhythm_afterthought_armed": "已挂起延迟补话计时",
    "rhythm_afterthought_sent": "沉默后想起·补发一句",
    "rhythm_afterthought_cancelled_reply": "补话取消·对方或原话题已接话",
    "rhythm_afterthought_cancelled_recall": "补话取消·相关消息已撤回",
    "rhythm_afterthought_cancelled_stop": "补话取消·停止/边界/状态变化",
    "rhythm_afterthought_skipped_interrupt": "补话跳过·此刻易打断群聊氛围",
    "rhythm_afterthought_skipped_outreach": "补话跳过·近期已有主动外联",
    "rhythm_afterthought_skipped_empty": "补话跳过·暂无新意保持沉默",
    "rhythm_afterthought_skipped_failed": "未挂起补话·主回复未完整送达",
}


def is_urgent_text(text: str) -> bool:
    clean = (text or "").strip()
    return bool(clean and _URGENT_RE.search(clean))


def is_closing_text(text: str) -> bool:
    clean = (text or "").strip()
    return bool(clean and _CLOSING_RE.match(clean))


def is_resolution_text(text: str) -> bool:
    clean = (text or "").strip()
    return bool(clean and _RESOLVED_RE.search(clean))


def is_pure_filler(text: str) -> bool:
    clean = (text or "").strip()
    if not clean:
        return True
    return bool(_PURE_FILLER_RE.match(clean))


def _wall_ts_of_node(node: Any, fallback: float = 0.0) -> float:
    meta = getattr(node, "metadata", None)
    if isinstance(meta, dict):
        val = meta.get("wall_timestamp")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
    return float(fallback)


def _node_topic_id(node: Any) -> str:
    meta = getattr(node, "metadata", None)
    if not isinstance(meta, dict):
        return ""
    routing = meta.get("routing")
    if isinstance(routing, dict) and routing.get("topic_id"):
        return str(routing["topic_id"])
    return str(meta.get("topic_id") or "")


def _is_question_like(text: str) -> bool:
    clean = (text or "").strip()
    if not clean:
        return False
    return ("?" in clean or "？" in clean or clean.endswith(("吗", "呢", "嘛", "么", "几点", "哪儿", "哪里")))


def _answer_looks_resolving(question_text: str, later_text: str) -> bool:
    """Fast local check for an obvious later settlement (e.g. time decided)."""
    later = (later_text or "").strip()
    if not later:
        return False
    if is_resolution_text(later):
        return True
    q = (question_text or "").strip()
    if ("几点" in q or "什么时候" in q or "几号" in q) and re.search(
        r"(定(?:了|在|好)|就(?:定|选)?[一二三四五六七八九十两\d]+点|改(?:到|成)[一二三四五六七八九十两\d]+点|[一二三四五六七八九十两\d]+点(?:吧|见|走|集合|出发))",
        later,
    ):
        return True
    if ("去哪" in q or "在哪" in q or "哪里" in q) and re.search(
        r"(定在|就去|去.*吧|在.*集合|在.*见面)",
        later,
    ):
        return True
    return False


@dataclass
class CatchupCandidate:
    """Lightweight serializable record of a deferred message eligible for catch-up."""

    msg_id: str
    author_id: str
    topic_id: str = ""
    wall_ts: float = 0.0
    mono_ts: float = 0.0
    wake_kind: str = "none"
    directed: bool = False
    private_direct: bool = False
    persona_fingerprint: str = ""
    attempt_id: str = ""
    status: AttemptStatus = "pending"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id[:256],
            "author_id": self.author_id[:256],
            "topic_id": self.topic_id[:256],
            "wall_ts": float(self.wall_ts),
            "wake_kind": self.wake_kind[:32],
            "directed": bool(self.directed),
            "private_direct": bool(self.private_direct),
            "persona_fingerprint": self.persona_fingerprint[:128],
            "attempt_id": self.attempt_id[:128],
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, row: Dict[str, Any]) -> Optional["CatchupCandidate"]:
        if not isinstance(row, dict):
            return None
        msg_id = str(row.get("msg_id") or "").strip()
        author_id = str(row.get("author_id") or "").strip()
        if not msg_id or not author_id:
            return None
        status = str(row.get("status") or "pending")
        if status not in ("pending", "in_flight", "sent", "uncertain", "failed", "skipped"):
            status = "pending"
        try:
            wall_ts = float(row.get("wall_ts") or 0.0)
        except (TypeError, ValueError):
            wall_ts = 0.0
        return cls(
            msg_id=msg_id[:256],
            author_id=author_id[:256],
            topic_id=str(row.get("topic_id") or "")[:256],
            wall_ts=wall_ts,
            wake_kind=str(row.get("wake_kind") or "none")[:32],
            directed=bool(row.get("directed", False)),
            private_direct=bool(row.get("private_direct", False)),
            persona_fingerprint=str(row.get("persona_fingerprint") or "")[:128],
            attempt_id=str(row.get("attempt_id") or f"cu:{msg_id}")[:128],
            status=status,  # type: ignore[arg-type]
        )


@dataclass
class CatchupPlanItem:
    """Grouped chronological catch-up unit ready for generation and delivery."""

    message_ids: Tuple[str, ...]
    primary_msg_id: str
    author_id: str
    topic_id: str
    directed: bool
    private_direct: bool
    earliest_wall_ts: float
    latest_wall_ts: float
    should_quote: bool
    reason: str = "rhythm_catchup_sent"


@dataclass
class AfterthoughtSlot:
    """In-memory timer anchor for at most one post-delivery delayed supplement."""

    session_key: str
    anchor_bot_msg_id: str
    trigger_msg_id: str
    target_user_id: str
    anchor_topic_id: str
    delivered_text: str
    persona_fingerprint: str
    epoch: int
    private_direct: bool
    finished_at_mono: float
    finished_at_wall: float
    fire_at_mono: float
    tier: Literal["afterthought", "private_nudge"] = "afterthought"
    cancelled: bool = False
    cancel_reason: str = ""
    high_affection: bool = False


@dataclass
class IngressPacingDecision:
    """Result of evaluating whether an incoming turn should run now or defer."""

    defer: bool
    away_kind: AwayKind = "none"
    reason_code: str = ""
    reason_zh: str = ""
    wake_at_wall: float = 0.0
    urgent_breakthrough: bool = False


@dataclass
class SessionRhythmBook:
    """Per-session (scoped by persona fingerprint) natural rhythm state."""

    session_key: str
    persona_fingerprint: str = ""
    read_watermark_wall: float = 0.0
    away_kind: AwayKind = "none"
    away_since_wall: float = 0.0
    wake_at_wall: float = 0.0
    wake_at_mono: float = 0.0
    last_distract_end_wall: float = 0.0
    last_afterthought_wall: float = 0.0
    last_bot_finish_wall: float = 0.0
    last_bot_finish_mono: float = 0.0
    last_outreach_wall: float = 0.0
    pending_items: List[CatchupCandidate] = field(default_factory=list)
    # msg_id -> status for reload-safe idempotency (bounded)
    delivery_ledger: Dict[str, AttemptStatus] = field(default_factory=dict)
    # author_id -> deque of recent directed ping mono timestamps for rapid-ping breakthrough
    recent_pings: Dict[str, Deque[float]] = field(default_factory=dict)
    # in-memory only
    afterthought: Optional[AfterthoughtSlot] = None
    catching_up: bool = False
    replay_events: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=64))
    # Retained live events (in-memory only, never persisted) for direct reply execution.
    # Each msg_id maps to a tuple of independently media-preserved event clones.
    live_events: Dict[str, Tuple[Any, ...]] = field(default_factory=dict)


class NaturalRhythmController:
    """Orchestrates away/busy catch-up, delayed afterthought, and demand-driven scheduling."""

    MAX_LEDGER_ENTRIES = 128
    MAX_PENDING_ITEMS = 48
    DISTRACT_COOLDOWN_SECONDS = 600.0
    SAME_INTERLOCUTOR_STAY_WINDOW = 45.0
    RAPID_PING_WINDOW = 60.0
    RAPID_PING_COUNT = 2
    OUTREACH_COLLISION_WINDOW = 300.0

    def __init__(
        self,
        plugin: Any,
        *,
        rng: Optional[random.Random] = None,
        catchup_executor: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        afterthought_executor: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        catchup_reviewer: Optional[Callable[..., Awaitable[List[str]]]] = None,
    ) -> None:
        self.plugin = plugin
        self._rng = rng if rng is not None else random.Random()
        self._books: Dict[str, SessionRhythmBook] = {}
        self._heap: List[Tuple[float, int, str, str]] = []  # (wake_mono, seq, kind, session_key)
        self._seq: int = 0
        self._wake_event = asyncio.Event()
        self._scheduler_task: Optional[asyncio.Task] = None
        self._inflight_jobs: Dict[Tuple[str, str], asyncio.Task] = {}
        self._dispatch_semaphore: Optional[asyncio.Semaphore] = None
        self._running = False
        self._catchup_executor = catchup_executor
        self._afterthought_executor = afterthought_executor
        self._catchup_reviewer = catchup_reviewer

    # --- configuration & clock helpers ---------------------------------------

    @property
    def time_service(self) -> TimeService:
        return self.plugin.time_service

    def _cfg(self) -> Any:
        return getattr(self.plugin, "_runtime_config", None)

    def enabled(self) -> bool:
        cfg = self._cfg()
        return bool(getattr(cfg, "natural_reply_rhythm_enabled", False)) if cfg is not None else False

    def catchup_enabled(self) -> bool:
        cfg = self._cfg()
        return self.enabled() and bool(getattr(cfg, "rhythm_catchup_enabled", True))

    def afterthought_enabled(self) -> bool:
        cfg = self._cfg()
        return self.enabled() and bool(getattr(cfg, "rhythm_afterthought_enabled", True))

    def micro_pacing_enabled(self) -> bool:
        cfg = self._cfg()
        return self.enabled() and bool(getattr(cfg, "rhythm_micro_pacing_enabled", True))

    def _lookback_seconds(self, *, directed: bool = False) -> float:
        cfg = self._cfg()
        public_min = int(getattr(cfg, "rhythm_lookback_minutes", 30) or 30)
        if directed:
            direct_min = int(getattr(cfg, "rhythm_direct_lookback_minutes", 120) or 120)
            return float(max(public_min, direct_min)) * 60.0
        return float(max(5, public_min)) * 60.0

    def _max_catchup_items(self, *, has_multiple_direct: bool = False) -> int:
        cfg = self._cfg()
        base = int(getattr(cfg, "rhythm_catchup_max_items", 2) or 2)
        base = max(1, min(5, base))
        if has_multiple_direct:
            return min(5, max(base, 3))
        return base

    def _distract_range(self) -> Tuple[float, float]:
        cfg = self._cfg()
        low = float(getattr(cfg, "rhythm_distract_min_seconds", 60.0) or 60.0)
        high = float(getattr(cfg, "rhythm_distract_max_seconds", 300.0) or 300.0)
        return max(5.0, low), max(low, high)

    def _distract_probability(self) -> float:
        cfg = self._cfg()
        return max(0.0, min(1.0, float(getattr(cfg, "rhythm_distract_probability", 0.30))))

    def _afterthought_range(self, *, tier: str = "afterthought") -> Tuple[float, float]:
        cfg = self._cfg()
        if tier == "private_nudge":
            return 180.0, 600.0
        low = float(getattr(cfg, "rhythm_afterthought_min_seconds", 60.0) or 60.0)
        high = float(getattr(cfg, "rhythm_afterthought_max_seconds", 240.0) or 240.0)
        return max(5.0, low), max(low, high)

    def _afterthought_cooldown(self) -> float:
        cfg = self._cfg()
        return max(0.0, float(getattr(cfg, "rhythm_afterthought_cooldown_seconds", 1500.0)))

    def _afterthought_probability(self) -> float:
        cfg = self._cfg()
        return max(0.0, min(1.0, float(getattr(cfg, "rhythm_afterthought_probability", 0.25))))

    def _book(self, session_key: str) -> SessionRhythmBook:
        key = str(session_key or "")
        book = self._books.get(key)
        if book is None:
            book = SessionRhythmBook(session_key=key)
            self._books[key] = book
        return book

    def _metric(self, name: str) -> None:
        fn = getattr(self.plugin, "_metric", None)
        if callable(fn):
            try:
                fn(name)
            except Exception:
                pass

    def _record_replay(
        self,
        book: SessionRhythmBook,
        *,
        action: str,
        reason_code: str,
        node: Any = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        wall_now = self.time_service.wall_time()
        zh = REASON_ZH.get(reason_code, reason_code)
        entry = {
            "ts": wall_now,
            "action": action,
            "reason_code": reason_code,
            "reason_zh": zh,
            "session_id": book.session_key,
            **({"detail": dict(detail)} if detail else {}),
        }
        book.replay_events.append(entry)
        if node is not None and isinstance(getattr(node, "metadata", None), dict):
            node.metadata["rhythm_trace"] = {
                "action": action,
                "reason_code": reason_code,
                "reason_zh": zh,
                "ts": wall_now,
                **({"detail": dict(detail)} if detail else {}),
            }

    def _record_ledger(self, book: SessionRhythmBook, msg_id: str, status: AttemptStatus) -> None:
        if not msg_id:
            return
        book.delivery_ledger[msg_id] = status
        while len(book.delivery_ledger) > self.MAX_LEDGER_ENTRIES:
            oldest = next(iter(book.delivery_ledger))
            book.delivery_ledger.pop(oldest, None)

    def _sync_runtime_state(self, runtime: Any, book: SessionRhythmBook) -> None:
        if runtime is None:
            return
        runtime.natural_rhythm_state = self.export_session_state(book.session_key)
        marker = getattr(self.plugin, "_mark_panel_runtime_dirty", None)
        if callable(marker):
            try:
                marker()
            except Exception:
                pass

    # --- lifecycle & demand-driven scheduler ---------------------------------

    def start(self) -> None:
        if self._running and self._scheduler_task is not None and not self._scheduler_task.done():
            return
        self._running = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._scheduler_task = loop.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._running = False
        self._wake_event.set()
        task = self._scheduler_task
        self._scheduler_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        inflight = list(self._inflight_jobs.values())
        self._inflight_jobs.clear()
        for job in inflight:
            if not job.done():
                job.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        for book in self._books.values():
            self._release_live_events(book)

    def _schedule_wake(self, session_key: str, kind: str, wake_mono: float) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (float(wake_mono), self._seq, kind, str(session_key)))
        self._wake_event.set()
        if self._scheduler_task is None or self._scheduler_task.done():
            self.start()

    def _spawn_dispatch(self, session_key: str, kind: str) -> None:
        key = (session_key, kind)
        existing = self._inflight_jobs.get(key)
        if existing is not None and not existing.done():
            return
        if self._dispatch_semaphore is None:
            self._dispatch_semaphore = asyncio.Semaphore(4)
        sem = self._dispatch_semaphore

        async def _runner() -> None:
            try:
                async with sem:
                    await self._dispatch_due(session_key, kind)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[ChatDynamics] natural rhythm job error session=%s kind=%s type=%s",
                    session_key,
                    kind,
                    type(exc).__name__,
                )
            finally:
                if self._inflight_jobs.get(key) is asyncio.current_task():
                    self._inflight_jobs.pop(key, None)

        task = asyncio.create_task(_runner())
        self._inflight_jobs[key] = task

    async def _scheduler_loop(self) -> None:
        """Sleep until the earliest pending catch-up or afterthought deadline."""
        try:
            while self._running and not getattr(self.plugin, "_shutting_down", False):
                try:
                    if not self._heap:
                        self._wake_event.clear()
                        await self._wake_event.wait()
                        continue
                    wake_mono, _seq, kind, session_key = self._heap[0]
                    now_mono = self.time_service.time()
                    delay = wake_mono - now_mono
                    if delay > 0:
                        self._wake_event.clear()
                        sleep_task = asyncio.create_task(self.time_service.sleep(delay))
                        wake_task = asyncio.create_task(self._wake_event.wait())
                        done, pending = await asyncio.wait(
                            {sleep_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        # Check if a closer deadline arrived while waiting.
                        if wake_task in done and self._heap and self._heap[0][0] > self.time_service.time():
                            continue
                        continue
                    heapq.heappop(self._heap)
                    self._spawn_dispatch(session_key, kind)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "[ChatDynamics] natural rhythm scheduler tick error type=%s",
                        type(exc).__name__,
                    )
                    await self.time_service.sleep(0.5)
        except asyncio.CancelledError:
            return

    async def _dispatch_due(self, session_key: str, kind: str) -> None:
        runtime = getattr(self.plugin, "_sessions", {}).get(session_key)
        book = self._books.get(session_key)
        if runtime is None or book is None:
            return
        if kind == "catchup":
            if book.wake_at_mono <= self.time_service.time() + 1e-6 and (
                book.pending_items or book.away_kind != "none"
            ):
                await self.run_catchup(runtime)
        elif kind == "afterthought":
            slot = book.afterthought
            if slot is not None and not slot.cancelled and slot.fire_at_mono <= self.time_service.time() + 1e-6:
                await self.run_afterthought(runtime)

    async def process_due_once(self) -> int:
        """Deterministic helper (also used by tests) to run all currently due jobs."""
        ran = 0
        while self._heap and self._heap[0][0] <= self.time_service.time() + 1e-6:
            _wake, _seq, kind, session_key = heapq.heappop(self._heap)
            await self._dispatch_due(session_key, kind)
            ran += 1
        return ran

    def _clone_event_for_catchup(self, event: Any, session: str) -> Any:
        """Clone an event and independently preserve local media before the original turn releases it."""
        if event is None:
            return None
        try:
            cloned = copy.copy(event)
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj is not None:
                cloned.message_obj = copy.copy(msg_obj)
                parts = list(iter_message_components(event))
                if parts:
                    cloned.message_obj.message = [copy.deepcopy(part) for part in parts]
            if hasattr(event, "_extras"):
                cloned._extras = dict(getattr(event, "_extras") or {})
            if hasattr(event, "_temporary_local_files"):
                cloned._temporary_local_files = []
            # Detach inherited finalizers so releasing the original turn never deletes our copy.
            for attr in (
                "_chat_dynamics_media_cleanup",
                "_chat_dynamics_media_directory",
                "_chat_dynamics_media_archive",
            ):
                if hasattr(cloned, attr):
                    try:
                        delattr(cloned, attr)
                    except Exception:
                        pass
            if hasattr(event, "send"):
                cloned.send = event.send
            archive = getattr(self.plugin, "media_archive", None)
            return preserve_event_media(cloned, archive=archive, session=session)
        except Exception:
            return event

    @staticmethod
    def _release_live_events(book: SessionRhythmBook, msg_id: Optional[str] = None) -> None:
        if msg_id is None:
            keys = list(book.live_events.keys())
        else:
            keys = [msg_id] if msg_id in book.live_events else []
        for key in keys:
            events = book.live_events.pop(key, ())
            for ev in events if isinstance(events, (tuple, list)) else (events,):
                try:
                    release_event_media(ev)
                except Exception:
                    pass

    # --- session resets, stops, recalls, and persona changes -----------------

    def reset_session(self, session_key: str) -> None:
        book = self._books.pop(str(session_key), None)
        if book is not None:
            self._release_live_events(book)
            if book.afterthought is not None:
                book.afterthought.cancelled = True
                book.afterthought.cancel_reason = "rhythm_afterthought_cancelled_stop"

    def on_member_stopped(self, session_key: str, user_id: str) -> None:
        book = self._books.get(str(session_key))
        if book is None:
            return
        uid = str(user_id or "")
        for cand in book.pending_items:
            if cand.author_id == uid and cand.status == "pending":
                cand.status = "skipped"
                self._record_ledger(book, cand.msg_id, "skipped")
                self._release_live_events(book, cand.msg_id)
        if book.afterthought is not None and book.afterthought.target_user_id == uid:
            book.afterthought.cancelled = True
            book.afterthought.cancel_reason = "rhythm_afterthought_cancelled_stop"
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_stop")
            book.afterthought = None

    def on_message_recalled(self, session_key: str, msg_id: str, runtime: Any = None) -> bool:
        """Mark message recalled and cancel any pending catch-up or afterthought anchored on it."""
        mid = str(msg_id or "").strip()
        if not mid:
            return False
        book = self._books.get(str(session_key))
        changed = False
        if runtime is not None:
            marker = getattr(runtime, "mark_recalled", None)
            if callable(marker):
                marker(mid)
            dag = getattr(runtime, "dag", None)
            node = dag.get_node(mid) if dag is not None else None
            if node is not None and isinstance(getattr(node, "metadata", None), dict):
                node.metadata["recalled"] = True
                changed = True
        if book is None:
            return changed
        for cand in book.pending_items:
            if cand.msg_id == mid and cand.status == "pending":
                cand.status = "skipped"
                self._record_ledger(book, mid, "skipped")
                self._release_live_events(book, mid)
                node = runtime.dag.get_node(mid) if runtime is not None and getattr(runtime, "dag", None) else None
                self._record_replay(book, action="silent", reason_code="rhythm_catchup_skipped_recalled", node=node)
                self._metric("rhythm_recall_cancelled")
                changed = True
        slot = book.afterthought
        if slot is not None and not slot.cancelled and mid in {slot.anchor_bot_msg_id, slot.trigger_msg_id}:
            slot.cancelled = True
            slot.cancel_reason = "rhythm_afterthought_cancelled_recall"
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_recall")
            self._metric("rhythm_recall_cancelled")
            book.afterthought = None
            changed = True
        if runtime is not None and changed:
            self._sync_runtime_state(runtime, book)
        return changed

    def note_outreach_sent(self, session_key: str) -> None:
        book = self._book(session_key)
        book.last_outreach_wall = self.time_service.wall_time()
        if book.afterthought is not None and not book.afterthought.cancelled:
            book.afterthought.cancelled = True
            book.afterthought.cancel_reason = "rhythm_afterthought_skipped_outreach"
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_skipped_outreach")
            book.afterthought = None

    def on_availability_changed(self, runtime: Any, availability: str) -> None:
        """Wake a waiting session promptly when world availability returns to available."""
        if runtime is None or not self.catchup_enabled():
            return
        book = self._books.get(runtime.session_key)
        if book is None:
            return
        avail = str(availability or "").lower()
        if avail == "available" and book.away_kind in {"world_busy", "world_asleep"}:
            book.away_kind = "none"
            now_mono = self.time_service.time()
            # Short human "just picked up phone" beat (2-6s) before catch-up review.
            beat = self._rng.uniform(2.0, 6.0)
            book.wake_at_mono = now_mono + beat
            book.wake_at_wall = self.time_service.wall_time() + beat
            if any(item.status == "pending" for item in book.pending_items):
                self._schedule_wake(runtime.session_key, "catchup", book.wake_at_mono)
            self._sync_runtime_state(runtime, book)

    # --- ingress observation & deferral gate ---------------------------------

    def on_user_message_observed(
        self,
        runtime: Any,
        *,
        msg_id: str,
        user_id: str,
        text: str,
        topic_id: str = "",
        reply_to_id: str = "",
        directed: bool = False,
        private_direct: bool = False,
    ) -> None:
        """Called on every inbound user message to manage afterthought cancellation & ping tracking."""
        if runtime is None:
            return
        book = self._book(runtime.session_key)
        now_mono = self.time_service.time()
        uid = str(user_id or "")
        if directed or private_direct:
            pings = book.recent_pings.setdefault(uid, deque(maxlen=6))
            pings.append(now_mono)
            while pings and now_mono - pings[0] > self.RAPID_PING_WINDOW:
                pings.popleft()

        slot = book.afterthought
        if slot is None or slot.cancelled:
            return
        # Private chat: any incoming message from the user cancels afterthought immediately.
        should_cancel = False
        if slot.private_direct or private_direct:
            should_cancel = True
        else:
            # Group chat: cancel if someone replied to our anchor, continued our topic,
            # or directly addressed us. Unrelated group topics leave the slot alive.
            if reply_to_id and reply_to_id == slot.anchor_bot_msg_id:
                should_cancel = True
            elif directed:
                should_cancel = True
            elif topic_id and slot.anchor_topic_id and topic_id == slot.anchor_topic_id:
                should_cancel = True
            elif uid == slot.target_user_id and not topic_id and (now_mono - slot.finished_at_mono) <= 90.0:
                should_cancel = True
        if should_cancel:
            slot.cancelled = True
            slot.cancel_reason = "rhythm_afterthought_cancelled_reply"
            self._record_replay(
                book,
                action="silent",
                reason_code="rhythm_afterthought_cancelled_reply",
                detail={"by_msg_id": msg_id, "by_user_id": uid},
            )
            book.afterthought = None

    def _is_rapid_ping(self, book: SessionRhythmBook, user_id: str, now_mono: float) -> bool:
        pings = book.recent_pings.get(str(user_id or ""))
        if not pings:
            return False
        recent = [ts for ts in pings if 0.0 <= now_mono - ts <= self.RAPID_PING_WINDOW]
        return len(recent) >= self.RAPID_PING_COUNT

    def evaluate_ingress_pacing(
        self,
        runtime: Any,
        *,
        msg_id: str,
        user_id: str,
        text: str,
        topic_id: str = "",
        wake_kind: str = "none",
        directed: bool = False,
        private_direct: bool = False,
        availability: str = "available",
        persona_fingerprint: str = "",
        node: Any = None,
        live_event: Any = None,
        live_events: Sequence[Any] = (),
    ) -> IngressPacingDecision:
        """Decide whether an incoming turn runs immediately or defers to catch-up."""
        if not self.catchup_enabled() or runtime is None:
            return IngressPacingDecision(defer=False)
        if getattr(self.plugin, "shadow_mode", False):
            return IngressPacingDecision(defer=False)

        book = self._book(runtime.session_key)
        now_mono = self.time_service.time()
        now_wall = self.time_service.wall_time()

        if persona_fingerprint and book.persona_fingerprint and book.persona_fingerprint != persona_fingerprint:
            # Character switched: never carry over previous persona's catch-up debt.
            book.pending_items.clear()
            self._release_live_events(book)
            book.away_kind = "none"
            if book.afterthought is not None:
                book.afterthought.cancelled = True
                book.afterthought = None
        if persona_fingerprint:
            book.persona_fingerprint = persona_fingerprint

        avail = str(availability or "available").lower()
        # Expire finished micro-distraction if its timer already passed.
        if book.away_kind == "micro_distract" and book.wake_at_mono and now_mono >= book.wake_at_mono:
            book.away_kind = "none"
            book.last_distract_end_wall = now_wall

        # Determine effective away kind.
        away_kind: AwayKind = "none"
        if avail == "asleep":
            away_kind = "world_asleep"
        elif avail == "busy":
            away_kind = "world_busy"
        elif book.away_kind == "micro_distract":
            away_kind = "micro_distract"
        else:
            # Available: check whether she briefly put down her phone after a natural pause.
            idle_since_bot = (
                now_mono - book.last_bot_finish_mono
                if book.last_bot_finish_mono > 0
                else (now_mono - float(getattr(runtime, "last_model_send", 0.0) or 0.0)
                      if getattr(runtime, "last_model_send", 0.0) else 9999.0)
            )
            same_interlocutor = (
                bool(user_id)
                and str(getattr(runtime, "last_interlocutor", "") or "") == str(user_id)
                and idle_since_bot < self.SAME_INTERLOCUTOR_STAY_WINDOW
            )
            cooldown_ok = (
                book.last_distract_end_wall <= 0
                or (now_wall - book.last_distract_end_wall) >= self.DISTRACT_COOLDOWN_SECONDS
            )
            # Stay present during active back-and-forth with the same person.
            if not same_interlocutor and cooldown_ok and not book.catching_up:
                prob = self._distract_probability()
                if idle_since_bot < self.SAME_INTERLOCUTOR_STAY_WINDOW:
                    prob *= 0.15  # Very low chance of brief mid-chat walk-away
                if self._rng.random() < prob:
                    low, high = self._distract_range()
                    duration = self._rng.uniform(low, high)
                    away_kind = "micro_distract"
                    book.away_kind = "micro_distract"
                    book.away_since_wall = now_wall
                    book.wake_at_wall = now_wall + duration
                    book.wake_at_mono = now_mono + duration

        if away_kind == "none":
            book.away_kind = "none"
            return IngressPacingDecision(defer=False)

        # Update away window for world_busy / world_asleep.
        if away_kind in {"world_busy", "world_asleep"}:
            if book.away_kind != away_kind:
                book.away_kind = away_kind
                book.away_since_wall = now_wall
            # Schedule a periodic check / catch-up review in case availability stays busy
            # or flips back without an explicit callback.
            if book.wake_at_mono <= now_mono:
                check_after = 180.0 if away_kind == "world_busy" else 600.0
                book.wake_at_mono = now_mono + check_after
                book.wake_at_wall = now_wall + check_after

        # Urgent / decision-waiting / rapid-ping breakthrough check.
        urgent = is_urgent_text(text)
        rapid_ping = (directed or private_direct) and self._is_rapid_ping(book, user_id, now_mono)
        # During asleep, only explicit urgent text directed at the bot breaks through.
        breakthrough = (urgent and (directed or private_direct)) if away_kind == "world_asleep" else (
            urgent or rapid_ping
        )
        if breakthrough:
            if away_kind == "micro_distract":
                book.away_kind = "none"
                book.last_distract_end_wall = now_wall
            # Mark handled by the immediate live path so later catch-up never duplicates it.
            self._record_ledger(book, msg_id, "sent")
            self._record_replay(
                book,
                action="speak",
                reason_code="rhythm_urgent_breakthrough",
                node=node,
                detail={"away_kind": away_kind, "urgent": urgent, "rapid_ping": rapid_ping},
            )
            self._sync_runtime_state(runtime, book)
            return IngressPacingDecision(
                defer=False,
                away_kind=away_kind,
                reason_code="rhythm_urgent_breakthrough",
                reason_zh=REASON_ZH["rhythm_urgent_breakthrough"],
                urgent_breakthrough=True,
            )

        # Defer this message into the session's catch-up bookmark.
        reason_code = (
            "rhythm_deferred_asleep"
            if away_kind == "world_asleep"
            else "rhythm_deferred_busy"
            if away_kind == "world_busy"
            else "rhythm_deferred_distract"
        )
        attempt_id = f"cu:{msg_id}"
        existing = next((c for c in book.pending_items if c.msg_id == msg_id), None)
        if existing is None and book.delivery_ledger.get(msg_id) not in {"sent", "uncertain"}:
            cand = CatchupCandidate(
                msg_id=msg_id,
                author_id=str(user_id or ""),
                topic_id=str(topic_id or ""),
                wall_ts=now_wall,
                mono_ts=now_mono,
                wake_kind=str(wake_kind or "none"),
                directed=bool(directed or private_direct),
                private_direct=bool(private_direct),
                persona_fingerprint=book.persona_fingerprint,
                attempt_id=attempt_id,
                status="pending",
            )
            book.pending_items.append(cand)
            if len(book.pending_items) > self.MAX_PENDING_ITEMS:
                evicted = book.pending_items[:-self.MAX_PENDING_ITEMS]
                del book.pending_items[:-self.MAX_PENDING_ITEMS]
                for evicted_cand in evicted:
                    self._release_live_events(book, evicted_cand.msg_id)
            self._record_ledger(book, msg_id, "pending")
            raw_events = tuple(ev for ev in live_events if ev is not None)
            if not raw_events and live_event is not None:
                raw_events = (live_event,)
            if raw_events:
                session_label = str(getattr(runtime, "umo", "") or runtime.session_key)
                preserved = tuple(
                    cloned
                    for ev in raw_events
                    if (cloned := self._clone_event_for_catchup(ev, session_label)) is not None
                )
                if preserved:
                    self._release_live_events(book, msg_id)
                    book.live_events[msg_id] = preserved
                    while len(book.live_events) > self.MAX_PENDING_ITEMS:
                        oldest_key = next(iter(book.live_events))
                        self._release_live_events(book, oldest_key)

        self._record_replay(
            book,
            action="silent",
            reason_code=reason_code,
            node=node,
            detail={"wake_at_wall": book.wake_at_wall, "msg_id": msg_id},
        )
        self._metric("rhythm_deferred")
        self._schedule_wake(runtime.session_key, "catchup", book.wake_at_mono)
        self._sync_runtime_state(runtime, book)
        return IngressPacingDecision(
            defer=True,
            away_kind=away_kind,
            reason_code=reason_code,
            reason_zh=REASON_ZH[reason_code],
            wake_at_wall=book.wake_at_wall,
        )

    # --- catch-up selection, verification, and chronological execution --------

    async def select_catchup_items(
        self,
        runtime: Any,
        *,
        now_wall: Optional[float] = None,
        persona_fingerprint: str = "",
    ) -> List[CatchupPlanItem]:
        """Review the recent lookback window and pick 1-2 (up to cap) still-worthy items in order."""
        book = self._book(runtime.session_key)
        stamp_wall = self.time_service.wall_time() if now_wall is None else float(now_wall)
        dag = getattr(runtime, "dag", None)
        if dag is None:
            return []
        bot_id = str(getattr(runtime, "bot_id", "") or "")
        recent_nodes = sorted(dag.get_recent_nodes(limit=0), key=lambda n: n.timestamp)
        if not recent_nodes:
            return []

        public_window = self._lookback_seconds(directed=False)
        direct_window = self._lookback_seconds(directed=True)
        pending_by_id = {c.msg_id: c for c in book.pending_items}
        recalled_set = set(getattr(runtime, "recalled_id_set", ()) or ())

        # Step 1: Collect candidate nodes since read_watermark_wall and audit expirations/dedups.
        eligible_nodes: List[Tuple[Any, Optional[CatchupCandidate], bool, float]] = []
        for node in recent_nodes:
            mid = str(getattr(node, "msg_id", "") or "")
            uid = str(getattr(node, "user_id", "") or "")
            if not mid or not uid or (bot_id and uid == bot_id):
                continue
            cand = pending_by_id.get(mid)
            node_wall = cand.wall_ts if cand is not None and cand.wall_ts > 0 else _wall_ts_of_node(node, stamp_wall)
            # Only consider messages newer than read_watermark_wall (or explicitly pending).
            if cand is None and book.read_watermark_wall > 0 and node_wall <= book.read_watermark_wall:
                continue
            # Persona isolation.
            if persona_fingerprint and cand is not None and cand.persona_fingerprint and cand.persona_fingerprint != persona_fingerprint:
                cand.status = "skipped"
                self._record_replay(book, action="silent", reason_code="rhythm_catchup_skipped_persona", node=node)
                continue
            # Reload-safe uncertain/sent dedupe.
            ledger_status = book.delivery_ledger.get(mid)
            if ledger_status in {"sent", "uncertain"} or (cand is not None and cand.status in {"sent", "uncertain"}):
                if ledger_status == "uncertain" or (cand is not None and cand.status == "uncertain"):
                    self._record_replay(book, action="silent", reason_code="rhythm_catchup_skipped_uncertain", node=node)
                    self._metric("rhythm_reload_dedup_skipped")
                continue
            if cand is not None and cand.status == "skipped":
                continue
            # Recall check.
            if mid in recalled_set or getattr(node, "metadata", {}).get("recalled") is True:
                if cand is not None:
                    cand.status = "skipped"
                self._record_ledger(book, mid, "skipped")
                self._record_replay(book, action="silent", reason_code="rhythm_catchup_skipped_recalled", node=node)
                continue

            is_private = getattr(runtime, "scope_kind", "group") == "private"
            meta = getattr(node, "metadata", None) or {}
            directed = bool(
                is_private
                or (cand is not None and cand.directed)
                or meta.get("is_wake")
                or (bot_id and bot_id in (getattr(node, "mentioned_users", None) or ()))
            )
            max_age = direct_window if directed else public_window
            age = stamp_wall - node_wall
            if age > max_age:
                if cand is not None:
                    cand.status = "skipped"
                self._record_ledger(book, mid, "skipped")
                self._record_replay(
                    book,
                    action="silent",
                    reason_code="rhythm_catchup_skipped_expired",
                    node=node,
                    detail={"age_seconds": round(age, 1), "window_seconds": max_age},
                )
                self._metric("rhythm_catchup_skipped")
                continue
            if is_pure_filler(getattr(node, "text", "")):
                if cand is not None:
                    cand.status = "skipped"
                continue
            if not directed and is_resolution_text(str(getattr(node, "text", "") or "")) and not _is_question_like(str(getattr(node, "text", "") or "")):
                if cand is not None:
                    cand.status = "skipped"
                self._record_ledger(book, mid, "skipped")
                self._record_replay(book, action="silent", reason_code="rhythm_catchup_skipped_resolved", node=node)
                continue
            eligible_nodes.append((node, cand, directed, node_wall))

        if not eligible_nodes:
            return []

        # Step 2: Check whether later messages in the window already resolved/settled an older item.
        unresolved: List[Tuple[Any, Optional[CatchupCandidate], bool, float]] = []
        node_index = {str(n.msg_id): idx for idx, n in enumerate(recent_nodes)}
        for node, cand, directed, node_wall in eligible_nodes:
            idx = node_index.get(str(node.msg_id), -1)
            later_nodes = recent_nodes[idx + 1 :] if idx >= 0 else []
            resolved_by = None
            node_topic = _node_topic_id(node)
            node_text = str(getattr(node, "text", "") or "")
            for later in later_nodes:
                if str(getattr(later, "msg_id", "")) in recalled_set:
                    continue
                later_text = str(getattr(later, "text", "") or "")
                later_uid = str(getattr(later, "user_id", "") or "")
                later_topic = _node_topic_id(later)
                same_thread = (
                    getattr(later, "reply_to_id", None) == node.msg_id
                    or (node_topic and later_topic and node_topic == later_topic)
                    or (not node_topic and not later_topic)
                )
                if bot_id and later_uid == bot_id and same_thread:
                    # Bot already spoke on this thread later.
                    resolved_by = later.msg_id
                    break
                if same_thread and _is_question_like(node_text) and _answer_looks_resolving(node_text, later_text):
                    # A later message settled this question (e.g. "已经定了八点").
                    # Do not let a new directed question to the bot resolve an older unrelated one.
                    if not directed or later_uid == str(node.user_id):
                        resolved_by = later.msg_id
                        break
                if later_uid == str(node.user_id) and is_resolution_text(later_text):
                    resolved_by = later.msg_id
                    break
            if resolved_by is not None:
                if cand is not None:
                    cand.status = "skipped"
                self._record_ledger(book, str(node.msg_id), "skipped")
                self._record_replay(
                    book,
                    action="silent",
                    reason_code="rhythm_catchup_skipped_resolved",
                    node=node,
                    detail={"resolved_by": str(resolved_by)},
                )
                self._metric("rhythm_catchup_skipped")
                continue
            unresolved.append((node, cand, directed, node_wall))

        if not unresolved:
            return []

        # Step 3: Merge consecutive same-author messages (private: all consecutive;
        # group: consecutive same-author + same-topic within 120s).
        grouped: List[CatchupPlanItem] = []
        is_private = getattr(runtime, "scope_kind", "group") == "private"
        for node, cand, directed, node_wall in unresolved:
            mid = str(node.msg_id)
            uid = str(node.user_id)
            topic = (cand.topic_id if cand and cand.topic_id else _node_topic_id(node))
            if grouped:
                prev = grouped[-1]
                can_merge = (
                    prev.author_id == uid
                    and (is_private or (prev.topic_id == topic and (node_wall - prev.latest_wall_ts) <= 120.0))
                )
                if can_merge:
                    grouped[-1] = CatchupPlanItem(
                        message_ids=(*prev.message_ids, mid),
                        primary_msg_id=mid,
                        author_id=uid,
                        topic_id=prev.topic_id or topic,
                        directed=prev.directed or directed,
                        private_direct=prev.private_direct or is_private,
                        earliest_wall_ts=prev.earliest_wall_ts,
                        latest_wall_ts=node_wall,
                        should_quote=prev.should_quote,
                    )
                    continue
            # Quote in group chat when there are intervening/newer messages or multiple threads.
            has_newer = node_index.get(mid, len(recent_nodes) - 1) < len(recent_nodes) - 1
            grouped.append(
                CatchupPlanItem(
                    message_ids=(mid,),
                    primary_msg_id=mid,
                    author_id=uid,
                    topic_id=topic,
                    directed=directed,
                    private_direct=is_private,
                    earliest_wall_ts=node_wall,
                    latest_wall_ts=node_wall,
                    should_quote=(not is_private) and (has_newer or len(grouped) > 0),
                )
            )

        # Step 4: Optional model/custom reviewer to prune resolved/unworthy items with full 30m context.
        if callable(self._catchup_reviewer) and grouped:
            try:
                kept_ids = set(await self._catchup_reviewer(runtime, grouped, recent_nodes))
                filtered: List[CatchupPlanItem] = []
                for item in grouped:
                    if item.primary_msg_id in kept_ids or any(m in kept_ids for m in item.message_ids):
                        filtered.append(item)
                    else:
                        for m in item.message_ids:
                            c = pending_by_id.get(m)
                            if c is not None:
                                c.status = "skipped"
                            self._record_ledger(book, m, "skipped")
                            self._record_replay(
                                book,
                                action="silent",
                                reason_code="rhythm_catchup_skipped_unworthy",
                                node=dag.get_node(m),
                            )
                        self._metric("rhythm_catchup_skipped")
                grouped = filtered
            except Exception as exc:
                logger.warning("[ChatDynamics] catchup reviewer failed type=%s", type(exc).__name__)

        if not grouped:
            return []

        # Step 5: Prioritize direct items first, then public topics, enforce cap,
        # and return in original chronological order (old -> new).
        direct_items = [it for it in grouped if it.directed]
        ambient_items = [it for it in grouped if not it.directed]
        cap = self._max_catchup_items(has_multiple_direct=len(direct_items) >= 2)
        chosen: List[CatchupPlanItem] = []
        # Take the most relevant direct items (latest if > cap, preserving oldest-to-newest among chosen).
        if len(direct_items) <= cap:
            chosen.extend(direct_items)
            remaining = cap - len(chosen)
            if remaining > 0 and ambient_items:
                chosen.extend(ambient_items[-remaining:])
        else:
            chosen.extend(direct_items[-cap:])
        chosen_ids = {it.primary_msg_id for it in chosen}
        for dropped in grouped:
            if dropped.primary_msg_id not in chosen_ids:
                for m in dropped.message_ids:
                    c = pending_by_id.get(m)
                    if c is not None:
                        c.status = "skipped"
                    self._record_ledger(book, m, "skipped")
                    self._record_replay(
                        book,
                        action="silent",
                        reason_code="rhythm_catchup_skipped_limit",
                        node=dag.get_node(m),
                    )
                self._metric("rhythm_catchup_skipped")

        chosen.sort(key=lambda it: it.earliest_wall_ts)
        return chosen

    async def _pre_send_verify_catchup(
        self,
        runtime: Any,
        book: SessionRhythmBook,
        item: CatchupPlanItem,
        *,
        expected_epoch: int,
        expected_persona: str,
    ) -> Tuple[bool, str]:
        """Re-check latest progress, recall, stop, boundary, and persona before sending."""
        if getattr(self.plugin, "_shutting_down", False) or getattr(self.plugin, "shadow_mode", False):
            return False, "rhythm_catchup_skipped_stopped"
        if runtime.epoch != expected_epoch:
            return False, "rhythm_catchup_skipped_persona"
        if expected_persona and book.persona_fingerprint and book.persona_fingerprint != expected_persona:
            return False, "rhythm_catchup_skipped_persona"
        recalled_set = set(getattr(runtime, "recalled_id_set", ()) or ())
        dag = getattr(runtime, "dag", None)
        for mid in item.message_ids:
            if mid in recalled_set:
                return False, "rhythm_catchup_skipped_recalled"
            if book.delivery_ledger.get(mid) in {"sent", "uncertain"}:
                return False, "rhythm_catchup_skipped_uncertain"
            node = dag.get_node(mid) if dag is not None else None
            if node is None or getattr(node, "metadata", {}).get("recalled") is True:
                return False, "rhythm_catchup_skipped_recalled"
        # Check whether a brand-new message arrived mid-catchup that resolved this item.
        if dag is not None:
            newest_nodes = sorted(dag.get_recent_nodes(limit=0), key=lambda n: n.timestamp)
            primary = dag.get_node(item.primary_msg_id)
            if primary is not None:
                for later in newest_nodes:
                    if later.timestamp <= primary.timestamp or later.msg_id in item.message_ids:
                        continue
                    if str(later.msg_id) in recalled_set:
                        continue
                    if str(later.user_id) == item.author_id and is_resolution_text(str(later.text or "")):
                        return False, "rhythm_catchup_skipped_superseded"
        return True, ""

    async def run_catchup(self, runtime: Any) -> int:
        """Execute chronological catch-up for a session whose away/busy window ended."""
        if runtime is None or not self.catchup_enabled():
            return 0
        book = self._book(runtime.session_key)
        if book.catching_up:
            return 0
        # If world availability is still busy/asleep (or not yet confirmed available),
        # keep waiting instead of waking early. When live state is unreachable/disabled
        # (returns "unmanaged"), release the away window so pending catch-up never starves.
        current_avail = await self._probe_world_availability(runtime)
        if current_avail in {"busy", "asleep"} or (
            book.away_kind in {"world_busy", "world_asleep"}
            and current_avail not in {"available", "unmanaged"}
        ):
            now_mono = self.time_service.time()
            effective_away = (
                "world_busy"
                if current_avail == "busy"
                else "world_asleep"
                if current_avail == "asleep"
                else book.away_kind
            )
            recheck = 180.0 if effective_away == "world_busy" else 600.0
            book.away_kind = effective_away  # type: ignore[assignment]
            book.wake_at_mono = now_mono + recheck
            book.wake_at_wall = self.time_service.wall_time() + recheck
            if any(c.status == "pending" for c in book.pending_items):
                self._schedule_wake(runtime.session_key, "catchup", book.wake_at_mono)
            return 0

        book.catching_up = True
        book.away_kind = "none"
        book.last_distract_end_wall = self.time_service.wall_time()
        expected_epoch = runtime.epoch
        expected_persona = book.persona_fingerprint
        sent_count = 0
        try:
            plan = await self.select_catchup_items(
                runtime,
                now_wall=self.time_service.wall_time(),
                persona_fingerprint=expected_persona,
            )
            pending_by_id = {c.msg_id: c for c in book.pending_items}
            for idx, item in enumerate(plan):
                if idx > 0:
                    # Realistic pause as if scrolling down and typing the next reply.
                    gap = self._rng.uniform(3.5, 9.5)
                    await self.time_service.sleep(gap)
                ok, skip_reason = await self._pre_send_verify_catchup(
                    runtime, book, item, expected_epoch=expected_epoch, expected_persona=expected_persona
                )
                if not ok:
                    for mid in item.message_ids:
                        if mid in pending_by_id:
                            pending_by_id[mid].status = "skipped"
                        self._record_ledger(book, mid, "skipped")
                        node = runtime.dag.get_node(mid) if getattr(runtime, "dag", None) else None
                        self._record_replay(book, action="silent", reason_code=skip_reason, node=node)
                    self._metric("rhythm_catchup_skipped")
                    continue

                for mid in item.message_ids:
                    if mid in pending_by_id:
                        pending_by_id[mid].status = "in_flight"
                    self._record_ledger(book, mid, "in_flight")

                outcome = await self._execute_catchup_item(runtime, book, item)
                status = str(outcome.get("status") or "failed")
                if status == "sent":
                    sent_count += 1
                    for mid in item.message_ids:
                        if mid in pending_by_id:
                            pending_by_id[mid].status = "sent"
                        self._record_ledger(book, mid, "sent")
                        node = runtime.dag.get_node(mid) if getattr(runtime, "dag", None) else None
                        self._record_replay(book, action="speak", reason_code="rhythm_catchup_sent", node=node)
                    self._metric("rhythm_catchup_sent")
                    book.read_watermark_wall = max(book.read_watermark_wall, item.latest_wall_ts)
                elif status == "uncertain":
                    for mid in item.message_ids:
                        if mid in pending_by_id:
                            pending_by_id[mid].status = "uncertain"
                        self._record_ledger(book, mid, "uncertain")
                        node = runtime.dag.get_node(mid) if getattr(runtime, "dag", None) else None
                        self._record_replay(
                            book, action="silent", reason_code="rhythm_catchup_skipped_uncertain", node=node
                        )
                elif status == "skipped":
                    reason = str(outcome.get("reason") or "rhythm_catchup_skipped_unworthy")
                    for mid in item.message_ids:
                        if mid in pending_by_id:
                            pending_by_id[mid].status = "skipped"
                        self._record_ledger(book, mid, "skipped")
                        node = runtime.dag.get_node(mid) if getattr(runtime, "dag", None) else None
                        self._record_replay(book, action="silent", reason_code=reason, node=node)
                    self._metric("rhythm_catchup_skipped")
                else:
                    for mid in item.message_ids:
                        if mid in pending_by_id:
                            pending_by_id[mid].status = "failed"
                        self._record_ledger(book, mid, "failed")

            # Advance read watermark past the reviewed window and prune completed items.
            book.read_watermark_wall = max(book.read_watermark_wall, self.time_service.wall_time())
            remaining_pending = [c for c in book.pending_items if c.status == "pending"]
            remaining_ids = {c.msg_id for c in remaining_pending}
            for stale_mid in [mid for mid in list(book.live_events) if mid not in remaining_ids]:
                self._release_live_events(book, stale_mid)
            book.pending_items = remaining_pending
            self._sync_runtime_state(runtime, book)
        finally:
            book.catching_up = False
        return sent_count

    async def _probe_world_availability(self, runtime: Any) -> str:
        """Query live availability from dynamic_life_state before falling back to cached bundles."""
        narrative = getattr(self.plugin, "narrative", None)
        if narrative is None:
            return ""
        umo = str(getattr(runtime, "umo", "") or getattr(runtime, "session_key", "") or "")
        enabled_fn = getattr(narrative, "enabled", None)
        if callable(enabled_fn):
            try:
                if not enabled_fn(umo):
                    return "unmanaged"
            except Exception:
                pass
        bundles = getattr(narrative, "_bundles", None)
        bundle = (
            bundles.get(runtime.umo) or bundles.get(runtime.session_key)
            if isinstance(bundles, dict)
            else None
        )
        # Prefer live dynamic_life_state.get_snapshot so idle groups never starve on a stale bundle cache.
        call_fn = getattr(narrative, "call", None)
        if callable(call_fn):
            scope = None
            if bundle is not None:
                raw_scope = getattr(bundle, "scope", None)
                if isinstance(raw_scope, dict) and raw_scope.get("umo"):
                    scope = dict(raw_scope)
                else:
                    try:
                        snap = getattr(bundle, "snapshot", None)
                        if isinstance(snap, dict) and isinstance(snap.get("scope"), dict):
                            scope = dict(snap["scope"])
                    except Exception:
                        pass
            if scope is None and umo:
                platform = umo.split(":", 1)[0] if ":" in umo else ""
                scope = {"platform": platform, "umo": umo, "persona_id": "", "instance_id": ""}
            if scope is not None:
                try:
                    live_snap = await call_fn("dynamic_life_state", "get_snapshot", scope, timeout=3.0)
                except TypeError:
                    try:
                        live_snap = await call_fn("dynamic_life_state", "get_snapshot", scope)
                    except Exception:
                        live_snap = None
                except Exception:
                    live_snap = None
                if isinstance(live_snap, dict):
                    if live_snap.get("managed") is False:
                        return "unmanaged"
                    if not live_snap.get("stale"):
                        live_avail = str(live_snap.get("availability") or "").strip().lower()
                        if live_avail:
                            return live_avail
                elif live_snap is None:
                    # Check whether dynamic_life_state provider is completely absent/unloaded.
                    integrations = getattr(self.plugin, "integrations", None)
                    provider_fn = getattr(integrations, "narrative_provider", None) if integrations else None
                    if callable(provider_fn) and provider_fn("dynamic_life_state") is None:
                        return "unmanaged"
        if bundle is not None:
            try:
                snap = bundle.snapshot
                if isinstance(snap, dict) and not snap.get("stale"):
                    return str(snap.get("availability") or "").strip().lower()
            except Exception:
                pass
        return ""

    async def _execute_catchup_item(
        self, runtime: Any, book: SessionRhythmBook, item: CatchupPlanItem
    ) -> Dict[str, Any]:
        if callable(self._catchup_executor):
            return await self._catchup_executor(runtime, item)
        engine = getattr(self.plugin, "persona_engine", None)
        runner = getattr(engine, "execute_catchup_item", None)
        if callable(runner):
            collected: List[Any] = []
            events_by_id: Dict[str, Tuple[Any, ...]] = {}
            for mid in item.message_ids:
                evs = book.live_events.get(mid)
                if not evs:
                    continue
                ev_tuple = tuple(evs) if isinstance(evs, (tuple, list)) else (evs,)
                events_by_id[mid] = ev_tuple
                collected.extend(ev_tuple)
            try:
                return await runner(
                    runtime,
                    item,
                    live_event=collected[-1] if collected else None,
                    live_events=tuple(collected),
                    events_by_id=events_by_id,
                )
            finally:
                for mid in item.message_ids:
                    self._release_live_events(book, mid)
        return {"status": "skipped", "reason": "rhythm_catchup_skipped_unworthy"}

    # --- post-delivery delayed afterthought ----------------------------------

    def on_turn_delivered(
        self,
        runtime: Any,
        *,
        anchor_bot_msg_id: str,
        trigger_msg_id: str,
        target_user_id: str,
        anchor_topic_id: str,
        delivered_text: str,
        persona_fingerprint: str,
        private_direct: bool,
        all_fragments_delivered: bool,
        delivery_uncertain: bool = False,
        decision_action: str = "reply",
        rhythm_action: str = "",
        is_afterthought: bool = False,
        high_affection: bool = False,
    ) -> bool:
        """Advance read watermark and optionally arm a single delayed afterthought timer."""
        if runtime is None:
            return False
        book = self._book(runtime.session_key)
        now_mono = self.time_service.time()
        now_wall = self.time_service.wall_time()
        book.last_bot_finish_mono = now_mono
        book.last_bot_finish_wall = now_wall
        book.read_watermark_wall = max(book.read_watermark_wall, now_wall)
        if trigger_msg_id and not delivery_uncertain and all_fragments_delivered:
            self._record_ledger(book, str(trigger_msg_id), "sent")
        elif trigger_msg_id and delivery_uncertain:
            self._record_ledger(book, str(trigger_msg_id), "uncertain")
        if persona_fingerprint:
            book.persona_fingerprint = persona_fingerprint
        # Never chain a second afterthought from an afterthought.
        if is_afterthought or not self.afterthought_enabled() or getattr(self.plugin, "shadow_mode", False):
            return False
        # Partial or uncertain delivery must NEVER arm an afterthought.
        if not all_fragments_delivered or delivery_uncertain or not (delivered_text or "").strip():
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_skipped_failed")
            return False
        # Never follow up after a closing exchange or sleep/wake rhythm act.
        if decision_action == "close" or rhythm_action in {
            "wake_reply",
            "goodnight_reply",
            "morning_hi",
            "insomnia_line",
        }:
            return False
        if is_closing_text(delivered_text):
            return False
        # Respect session cooldown.
        if book.last_afterthought_wall > 0 and (now_wall - book.last_afterthought_wall) < self._afterthought_cooldown():
            return False

        # Choose tier and apply probabilistic gate.
        prob = self._afterthought_probability()
        tier: Literal["afterthought", "private_nudge"] = "afterthought"
        if private_direct and high_affection and self._rng.random() < (prob * 0.35):
            tier = "private_nudge"
        elif self._rng.random() >= prob:
            return False

        low, high = self._afterthought_range(tier=tier)
        delay = self._rng.uniform(low, high)
        slot = AfterthoughtSlot(
            session_key=runtime.session_key,
            anchor_bot_msg_id=str(anchor_bot_msg_id),
            trigger_msg_id=str(trigger_msg_id),
            target_user_id=str(target_user_id or ""),
            anchor_topic_id=str(anchor_topic_id or ""),
            delivered_text=str(delivered_text),
            persona_fingerprint=str(persona_fingerprint or ""),
            epoch=runtime.epoch,
            private_direct=bool(private_direct),
            finished_at_mono=now_mono,
            finished_at_wall=now_wall,
            fire_at_mono=now_mono + delay,
            tier=tier,
            high_affection=bool(high_affection),
        )
        book.afterthought = slot
        self._record_replay(
            book,
            action="silent",
            reason_code="rhythm_afterthought_armed",
            detail={"delay_seconds": round(delay, 1), "tier": tier, "anchor_bot_msg_id": anchor_bot_msg_id},
        )
        self._schedule_wake(runtime.session_key, "afterthought", slot.fire_at_mono)
        return True

    async def run_afterthought(self, runtime: Any) -> bool:
        """Verify silence/atmosphere/safety and emit at most one delayed supplement."""
        if runtime is None or not self.afterthought_enabled():
            return False
        book = self._book(runtime.session_key)
        slot = book.afterthought
        if slot is None or slot.cancelled:
            return False
        # Consume slot up front so this turn can NEVER trigger more than once.
        book.afterthought = None
        now_wall = self.time_service.wall_time()
        now_mono = self.time_service.time()

        # 1. Lifecycle / persona / recall / world availability checks.
        if getattr(self.plugin, "_shutting_down", False) or getattr(self.plugin, "shadow_mode", False):
            return False
        if runtime.epoch != slot.epoch or (
            slot.persona_fingerprint and book.persona_fingerprint and book.persona_fingerprint != slot.persona_fingerprint
        ):
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_stop")
            return False
        recalled_set = set(getattr(runtime, "recalled_id_set", ()) or ())
        if slot.anchor_bot_msg_id in recalled_set or slot.trigger_msg_id in recalled_set:
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_recall")
            return False
        avail = await self._probe_world_availability(runtime)
        if avail in {"busy", "asleep"}:
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_stop")
            return False
        if book.last_outreach_wall > 0 and (now_wall - book.last_outreach_wall) < self.OUTREACH_COLLISION_WINDOW:
            self._record_replay(book, action="silent", reason_code="rhythm_afterthought_skipped_outreach")
            self._metric("rhythm_afterthought_skipped")
            return False

        # 2. Verify latest DAG progress since finished_at_mono.
        dag = getattr(runtime, "dag", None)
        bot_id = str(getattr(runtime, "bot_id", "") or "")
        later_nodes = []
        if dag is not None:
            for node in sorted(dag.get_recent_nodes(limit=0), key=lambda n: n.timestamp):
                if node.timestamp > slot.finished_at_mono and str(node.msg_id) != slot.anchor_bot_msg_id:
                    later_nodes.append(node)
        has_unrelated_group_chatter = False
        for node in later_nodes:
            uid = str(getattr(node, "user_id", "") or "")
            if bot_id and uid == bot_id:
                # Bot already spoke again in the meantime.
                self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_reply")
                return False
            if slot.private_direct:
                self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_reply")
                return False
            # Group chat: check if anyone replied to our anchor or same topic.
            ntopic = _node_topic_id(node)
            if (
                getattr(node, "reply_to_id", None) == slot.anchor_bot_msg_id
                or (ntopic and slot.anchor_topic_id and ntopic == slot.anchor_topic_id)
                or (bot_id and bot_id in (getattr(node, "mentioned_users", None) or ()))
            ):
                self._record_replay(book, action="silent", reason_code="rhythm_afterthought_cancelled_reply")
                return False
            has_unrelated_group_chatter = True

        # 3. Group anti-interruption check: do not barge into conflict/deciding/private_field
        # or a high-velocity burst when others moved to a serious new exchange.
        if not slot.private_direct and has_unrelated_group_chatter:
            last_occasion = getattr(runtime, "last_occasion", None) or {}
            kind = str(last_occasion.get("kind") or "neutral").lower()
            if kind in {"conflict", "deciding", "private_field"}:
                self._record_replay(
                    book,
                    action="silent",
                    reason_code="rhythm_afterthought_skipped_interrupt",
                    detail={"occasion": kind},
                )
                self._metric("rhythm_afterthought_skipped")
                return False
            vibe = getattr(self.plugin, "vibe_analyzer", None)
            if vibe is not None:
                try:
                    tele = vibe.get_telemetrics(runtime.session_key, current_time=now_mono)
                    mpm = float(getattr(tele, "mpm", 0.0) or 0.0)
                    fast_mpm = float(getattr(self._cfg(), "fast_banter_enter_mpm", 12.0) or 12.0)
                    if mpm >= fast_mpm and len(later_nodes) >= 4:
                        self._record_replay(
                            book,
                            action="silent",
                            reason_code="rhythm_afterthought_skipped_interrupt",
                            detail={"mpm": round(mpm, 1)},
                        )
                        self._metric("rhythm_afterthought_skipped")
                        return False
                except Exception:
                    pass

        # 4. Execute lightweight afterthought generation (may decline silently).
        quote_self = (not slot.private_direct) and has_unrelated_group_chatter
        outcome = await self._execute_afterthought(runtime, slot, quote_self=quote_self)
        status = str(outcome.get("status") or "skipped")
        if status == "sent":
            book.last_afterthought_wall = self.time_service.wall_time()
            book.last_bot_finish_mono = self.time_service.time()
            book.last_bot_finish_wall = book.last_afterthought_wall
            bot_node = dag.get_node(str(outcome.get("message_id") or "")) if dag is not None else None
            self._record_replay(
                book,
                action="speak",
                reason_code="rhythm_afterthought_sent",
                node=bot_node,
                detail={"tier": slot.tier, "quote_self": quote_self},
            )
            self._metric("rhythm_afterthought_sent")
            self._sync_runtime_state(runtime, book)
            return True

        reason = str(outcome.get("reason") or "rhythm_afterthought_skipped_empty")
        self._record_replay(book, action="silent", reason_code=reason)
        self._metric("rhythm_afterthought_skipped")
        return False

    async def _execute_afterthought(
        self, runtime: Any, slot: AfterthoughtSlot, *, quote_self: bool
    ) -> Dict[str, Any]:
        if callable(self._afterthought_executor):
            return await self._afterthought_executor(runtime, slot, quote_self=quote_self)
        engine = getattr(self.plugin, "persona_engine", None)
        runner = getattr(engine, "execute_afterthought", None)
        if callable(runner):
            return await runner(runtime, slot, quote_self=quote_self)
        return {"status": "skipped", "reason": "rhythm_afterthought_skipped_empty"}

    # --- reload-safe persistence ---------------------------------------------

    def export_session_state(self, session_key: str) -> Dict[str, Any]:
        """Serialize bounded, wall-clock catch-up state; never saves afterthought timers."""
        book = self._books.get(str(session_key))
        if book is None:
            return {}
        now_wall = self.time_service.wall_time()
        max_window = self._lookback_seconds(directed=True)
        pending = [
            c.to_dict()
            for c in book.pending_items[-self.MAX_PENDING_ITEMS :]
            if c.status in {"pending", "in_flight"} and (now_wall - c.wall_ts) <= max_window
        ]
        # Convert any in-flight item persisted during an abrupt reload into uncertain
        # so the next process NEVER risks duplicate delivery.
        ledger: Dict[str, str] = {}
        for mid, st in list(book.delivery_ledger.items())[-self.MAX_LEDGER_ENTRIES :]:
            ledger[mid] = "uncertain" if st == "in_flight" else st
        for row in pending:
            if row["status"] == "in_flight":
                row["status"] = "uncertain"
                ledger[row["msg_id"]] = "uncertain"
        return {
            "persona_fingerprint": book.persona_fingerprint[:128],
            "read_watermark_wall": float(book.read_watermark_wall),
            "away_kind": book.away_kind,
            "away_since_wall": float(book.away_since_wall),
            "wake_at_wall": float(book.wake_at_wall),
            "last_distract_end_wall": float(book.last_distract_end_wall),
            "last_afterthought_wall": float(book.last_afterthought_wall),
            "pending_items": pending,
            "delivery_ledger": ledger,
        }

    def restore_session_state(self, runtime: Any, data: Any) -> int:
        """Restore still-valid catch-up items after reload, skipping uncertain deliveries."""
        if runtime is None or not isinstance(data, dict) or not data:
            return 0
        book = self._book(runtime.session_key)
        now_wall = self.time_service.wall_time()
        now_mono = self.time_service.time()
        book.persona_fingerprint = str(data.get("persona_fingerprint") or "")[:128]
        try:
            book.read_watermark_wall = float(data.get("read_watermark_wall") or 0.0)
            book.away_since_wall = float(data.get("away_since_wall") or 0.0)
            book.wake_at_wall = float(data.get("wake_at_wall") or 0.0)
            book.last_distract_end_wall = float(data.get("last_distract_end_wall") or 0.0)
            book.last_afterthought_wall = float(data.get("last_afterthought_wall") or 0.0)
        except (TypeError, ValueError):
            pass
        away = str(data.get("away_kind") or "none")
        book.away_kind = away if away in ("none", "micro_distract", "world_busy", "world_asleep") else "none"

        raw_ledger = data.get("delivery_ledger")
        if isinstance(raw_ledger, dict):
            for mid, st in list(raw_ledger.items())[-self.MAX_LEDGER_ENTRIES :]:
                s = str(st or "")
                if s == "in_flight":
                    s = "uncertain"
                if isinstance(mid, str) and mid and s in ("pending", "sent", "uncertain", "failed", "skipped"):
                    book.delivery_ledger[mid[:256]] = s  # type: ignore[assignment]

        dag = getattr(runtime, "dag", None)
        restored = 0
        raw_items = data.get("pending_items")
        if isinstance(raw_items, list):
            for row in raw_items[-self.MAX_PENDING_ITEMS :]:
                cand = CatchupCandidate.from_dict(row)
                if cand is None:
                    continue
                if cand.status == "in_flight":
                    cand.status = "uncertain"
                if cand.status == "uncertain" or book.delivery_ledger.get(cand.msg_id) in {"sent", "uncertain"}:
                    book.delivery_ledger[cand.msg_id] = "uncertain" if cand.status == "uncertain" else book.delivery_ledger.get(cand.msg_id, "uncertain")
                    node = dag.get_node(cand.msg_id) if dag is not None else None
                    self._record_replay(book, action="silent", reason_code="rhythm_catchup_skipped_uncertain", node=node)
                    self._metric("rhythm_reload_dedup_skipped")
                    continue
                # Source must still be confirmable in DAG and within lookback window.
                if dag is None or dag.get_node(cand.msg_id) is None:
                    continue
                window = self._lookback_seconds(directed=cand.directed)
                if (now_wall - cand.wall_ts) > window:
                    node = dag.get_node(cand.msg_id)
                    self._record_replay(book, action="silent", reason_code="rhythm_catchup_skipped_expired", node=node)
                    continue
                book.pending_items.append(cand)
                restored += 1

        if restored > 0 and self.catchup_enabled():
            remaining = max(0.0, book.wake_at_wall - now_wall) if book.wake_at_wall > 0 else 2.0
            book.wake_at_mono = now_mono + remaining
            self._schedule_wake(runtime.session_key, "catchup", book.wake_at_mono)
        return restored

    # --- observability / replay / status ------------------------------------

    def replay_rows(self, session_key: str = "", *, limit: int = 32) -> List[Dict[str, Any]]:
        if session_key:
            book = self._books.get(str(session_key))
            return list(book.replay_events)[-limit:] if book is not None else []
        rows: List[Dict[str, Any]] = []
        for book in self._books.values():
            rows.extend(book.replay_events)
        rows.sort(key=lambda r: float(r.get("ts") or 0.0))
        return rows[-limit:]

    def status(self, session_key: str = "") -> Dict[str, Any]:
        if session_key:
            book = self._books.get(str(session_key))
            if book is None:
                return {
                    "enabled": self.enabled(),
                    "away_kind": "none",
                    "pending_catchup": 0,
                    "afterthought_armed": False,
                    "wake_at_wall": 0.0,
                }
            return {
                "enabled": self.enabled(),
                "away_kind": book.away_kind,
                "pending_catchup": sum(1 for c in book.pending_items if c.status == "pending"),
                "afterthought_armed": bool(book.afterthought is not None and not book.afterthought.cancelled),
                "wake_at_wall": book.wake_at_wall,
                "read_watermark_wall": book.read_watermark_wall,
                "catching_up": book.catching_up,
            }
        return {
            "enabled": self.enabled(),
            "catchup_enabled": self.catchup_enabled(),
            "afterthought_enabled": self.afterthought_enabled(),
            "micro_pacing_enabled": self.micro_pacing_enabled(),
            "active_sessions": sum(
                1
                for b in self._books.values()
                if b.away_kind != "none"
                or any(c.status == "pending" for c in b.pending_items)
                or (b.afterthought is not None and not b.afterthought.cancelled)
            ),
            "pending_catchup_total": sum(
                sum(1 for c in b.pending_items if c.status == "pending") for b in self._books.values()
            ),
            "afterthought_armed_total": sum(
                1 for b in self._books.values() if b.afterthought is not None and not b.afterthought.cancelled
            ),
        }

    @staticmethod
    def is_silent_afterthought(text: str) -> bool:
        clean = (text or "").strip()
        if not clean:
            return True
        return clean.upper() in _SILENT_MARKERS or clean in _SILENT_MARKERS


def build_afterthought_prompt(
    *,
    delivered_text: str,
    recent_context: Sequence[Dict[str, Any]],
    private_direct: bool,
    tier: str = "afterthought",
    quote_self: bool = False,
) -> str:
    """Build a compact, cache-friendly prompt for optional post-delivery supplement."""
    import json as _json

    guidance = (
        "你刚才发出的消息已经完整送达，之后原话题安静了一会儿。"
        "结合你刚才真正发出的原文和此刻最新上下文，判断是否有自然、简短、真正有新意的一句话想补充"
        "（例如：刚才漏说的细节、轻微改口/补充限定、突然想到的相关联想或小笑点）。"
        "如果没有真正想补的，直接输出 NO_AFTERTHOUGHT，保持沉默非常自然。"
        "严禁催回复（绝不发“在吗/怎么不回/人呢”），不重复刚才已经说过的话，只发一句简短自然的话，发完自然收尾。"
    )
    if private_direct and tier == "private_nudge":
        guidance += "（私聊亲近关系且话未说完时，也可极轻地顺着原话题补半句，绝不粘人催促。）"
    if quote_self:
        guidance += "（群里其他人正在聊别的话题，你的补充会自动引用你刚才发的那条，保持简短不打断别人。）"
    payload = {
        "rhythm_mode": "afterthought",
        "tier": tier,
        "quote_self": quote_self,
        "previous_bot_reply": delivered_text[:1200],
        "recent_context": list(recent_context)[-10:],
        "guidance": guidance,
    }
    return _json.dumps(payload, ensure_ascii=False)
