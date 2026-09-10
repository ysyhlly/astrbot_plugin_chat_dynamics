"""Intervention and Back-off Arbiter for AstrBot Group Chat Dynamics.

Decides when the bot should intervene, when it must stay silent, and when it
should gracefully back off to avoid spamming or interrupting group flow.
"""

from __future__ import annotations

import logging
import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Tuple

from .addressivity import AddressivityLevel, AddressivityScore
from .semantics import classify_message
from .telemetrics import RoomTelemetrics
from .vibe_analyzer import GroupChatMode

logger = logging.getLogger("astrbot_plugin_chat_dynamics.arbiter")


def _session_label(session_id: Any) -> str:
    return hashlib.sha256(str(session_id or "").encode("utf-8", "ignore")).hexdigest()[:12]


# Acknowledgments are short but they mean "continue", not "I'm done talking".
ACK_REPLIES = {
    "好", "行", "好的", "收到", "知道了", "可以", "谢谢", "多谢", "感谢",
    "ok", "k", "yes", "y", "got it", "thx", "thanks", "ty",
}

# Minimal filler responses indicating energy asymmetry / conversation dying.
LOW_EFFORT_REPLIES = {
    "哦", "噢", "恩", "嗯",
    "6", "666", "牛", "啊", "哈", "这样啊", "害", "好吧", "行吧",
    "fine", "cool",
}


@dataclass
class ArbitrationResult:
    """Outcome of arbitration evaluation."""

    should_speak: bool
    willingness_score: float
    threshold: float
    reason: str
    in_deep_cooling: bool = False
    is_energy_asymmetric: bool = False
    professionalism: float = 0.0
    topic_relevance: float = 0.0
    fatigue_penalty: float = 0.0
    question_value: float = 0.0
    participation: float = 0.0
    private_topic: bool = False


class InterventionArbiter:
    """Arbitrates bot speech decisions based on addressivity, vibe, energy asymmetry, and cooling."""

    def __init__(
        self,
        base_threshold: float = 0.60,
        deep_cooling_duration: float = 900.0,  # 15 minutes default
        asymmetry_streak_limit: int = 2,
        time_service: Optional[Any] = None,
        recent_bot_window: float = 120.0,
        topic_weight: float = 0.12,
        professionalism_weight: float = 0.08,
        question_weight: float = 0.08,
        participation_weight: float = 0.06,
        fatigue_weight: float = 1.0,
    ):
        self.base_threshold: float = base_threshold
        self.deep_cooling_duration: float = deep_cooling_duration
        self.asymmetry_streak_limit: int = asymmetry_streak_limit
        self.time_service = time_service
        self.recent_bot_window: float = recent_bot_window
        self.topic_weight: float = topic_weight
        self.professionalism_weight: float = professionalism_weight
        self.question_weight: float = question_weight
        self.participation_weight: float = participation_weight
        self.fatigue_weight: float = fatigue_weight

        # Track deep cooling timestamps per session: session_id -> cooling_expiration_timestamp
        self._cooling_until: Dict[str, float] = {}

        # Track consecutive low-effort responses per (session_id, user_id)
        self._low_effort_streaks: Dict[Tuple[str, str], int] = {}

        # Track bot's last intervention timestamp per session
        self._last_bot_speak_time: Dict[str, float] = {}

        # User the bot last successfully spoke to, used for same-thread energy checks
        self._last_interlocutor: Dict[str, str] = {}
        self._last_bot_topic: Dict[str, str] = {}
        self._last_bot_msg_id: Dict[str, str] = {}
        self._bot_speak_history: Dict[str, Deque[float]] = {}
        self._last_decisions: Dict[str, ArbitrationResult] = {}
        self.on_cooling_changed: Optional[Any] = None

    def _now(self, current_time: Optional[float] = None) -> float:
        if current_time is not None:
            return current_time
        if self.time_service is not None:
            return float(self.time_service.time())
        return time.time()

    def is_in_deep_cooling(self, session_id: str, current_time: Optional[float] = None) -> bool:
        """Checks whether the session is currently in deep cooling."""
        return self.cooling_remaining(session_id, current_time=current_time) > 0.0

    def cooling_remaining(self, session_id: str, current_time: Optional[float] = None) -> float:
        now = self._now(current_time)
        expiry = self._cooling_until.get(session_id, 0.0)
        return max(0.0, expiry - now)

    def cooling_map(self, current_time: Optional[float] = None) -> Dict[str, float]:
        now = self._now(current_time)
        return {
            session_id: max(0.0, expiry - now)
            for session_id, expiry in self._cooling_until.items()
            if expiry > now
        }

    def trigger_cooling(self, session_id: str, duration_seconds: Optional[float] = None, current_time: Optional[float] = None) -> None:
        """Activates a deep cooling period for the session."""
        now = self._now(current_time)
        duration = duration_seconds if duration_seconds is not None else self.deep_cooling_duration
        self._cooling_until[session_id] = now + duration
        logger.info("Triggered deep cooling for session %s for %.1fs", _session_label(session_id), duration)
        self._emit_cooling_changed()

    def cooling_export(self) -> Dict[str, float]:
        return dict(self._cooling_until)

    def cooling_export_wall(
        self,
        *,
        wall_now: float,
        current_time: Optional[float] = None,
    ) -> Dict[str, float]:
        """Serialize live cooling as civil-wall expiry timestamps.

        In-memory expiry stays monotonic; KV must not store that absolute
        stamp because a new process starts monotonic near zero.
        """
        now = self._now(current_time)
        payload: Dict[str, float] = {}
        for session_id, expiry in self._cooling_until.items():
            remaining = float(expiry) - now
            if remaining > 0.0:
                payload[str(session_id)] = float(wall_now) + remaining
        return payload

    def cooling_restore(self, mapping: Dict[str, float], current_time: Optional[float] = None) -> None:
        now = self._now(current_time)
        restored: Dict[str, float] = {}
        for session_id, expiry in (mapping or {}).items():
            try:
                until = float(expiry)
            except (TypeError, ValueError):
                continue
            if until > now:
                restored[str(session_id)] = until
        self._cooling_until = restored

    def cooling_restore_wall(
        self,
        mapping: Dict[str, float],
        *,
        wall_now: float,
        current_time: Optional[float] = None,
    ) -> None:
        """Restore KV wall-epoch expiry into monotonic in-memory stamps."""
        now = self._now(current_time)
        restored: Dict[str, float] = {}
        for session_id, expiry in (mapping or {}).items():
            try:
                remaining = float(expiry) - float(wall_now)
            except (TypeError, ValueError):
                continue
            if remaining > 0.0:
                restored[str(session_id)] = now + remaining
        self._cooling_until = restored

    def _emit_cooling_changed(self) -> None:
        callback = self.on_cooling_changed
        if callable(callback):
            try:
                callback()
            except Exception as exc:
                logger.debug("cooling change callback failed code=CD_COOLING_CALLBACK type=%s", type(exc).__name__)

    def record_bot_spoke(
        self,
        session_id: str,
        timestamp: Optional[float] = None,
        user_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        msg_id: Optional[str] = None,
    ) -> None:
        """Records that the bot spoke and clears low-effort streaks for the session."""
        now = self._now(timestamp)
        self._last_bot_speak_time[session_id] = now
        history = self._bot_speak_history.setdefault(session_id, deque(maxlen=32))
        history.append(now)
        if user_id:
            self._last_interlocutor[session_id] = str(user_id)
        if topic_id:
            self._last_bot_topic[session_id] = str(topic_id)
        if msg_id:
            self._last_bot_msg_id[session_id] = str(msg_id)
        stale_keys = [key for key in self._low_effort_streaks if key[0] == session_id]
        for key in stale_keys:
            self._low_effort_streaks.pop(key, None)

    MEDIA_MARKERS = {
        "[sticker]", "[image]", "[face]", "[media]",
        "[表情]", "[图片]", "[动画表情]", "[语音]",
    }

    def is_low_effort_text(self, text: str) -> bool:
        clean = (text or "").strip().lower()
        clean = re.sub(r"[。？！.?!~～\s]+$", "", clean)
        if not clean:
            return True
        if clean in ACK_REPLIES:
            return False
        if clean in self.MEDIA_MARKERS or clean in {m.lower() for m in self.MEDIA_MARKERS}:
            return True
        if re.fullmatch(r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff]+", clean):
            return True
        if clean in LOW_EFFORT_REPLIES:
            return True
        # ASCII crumbs like "6" / "n" are low-effort; CJK acknowledgements are not.
        return clean.isascii() and len(clean) <= 2 and clean.isalnum()

    def bot_spoke_recently(self, session_id: str, current_time: Optional[float] = None) -> bool:
        now = self._now(current_time)
        last = self._last_bot_speak_time.get(session_id)
        if last is None:
            return False
        elapsed = now - last
        return 0.0 <= elapsed <= self.recent_bot_window

    def check_energy_asymmetry(self, session_id: str, user_id: str, text: str) -> bool:
        """Checks if a user's response is low-effort, incrementing and returning True if limit reached."""
        is_low_effort = self.is_low_effort_text(text)
        key = (session_id, user_id)

        if is_low_effort:
            current_streak = self._low_effort_streaks.get(key, 0) + 1
            self._low_effort_streaks[key] = current_streak
            if current_streak >= self.asymmetry_streak_limit:
                return True
        else:
            self._low_effort_streaks[key] = 0

        return False

    def reset_asymmetry(self, session_id: str, user_id: str) -> None:
        """Resets the low effort streak for a user."""
        self._low_effort_streaks.pop((session_id, user_id), None)

    def reset_session(self, session_id: str) -> None:
        """Clears cooling, streaks, and last-speak state for a session."""
        self._cooling_until.pop(session_id, None)
        self._last_bot_speak_time.pop(session_id, None)
        self._last_interlocutor.pop(session_id, None)
        self._last_bot_topic.pop(session_id, None)
        self._last_bot_msg_id.pop(session_id, None)
        self._bot_speak_history.pop(session_id, None)
        self._last_decisions.pop(session_id, None)
        stale_keys = [key for key in self._low_effort_streaks if key[0] == session_id]
        for key in stale_keys:
            self._low_effort_streaks.pop(key, None)
        self._emit_cooling_changed()

    def _is_same_user_continuation(
        self,
        session_id: str,
        user_id: str,
        current_time: float,
        topic_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        runtime: Any = None,
    ) -> bool:
        if not user_id:
            return False
        if self._last_interlocutor.get(session_id) != user_id:
            return False
        if not self.bot_spoke_recently(session_id, current_time=current_time):
            return False

        bot_topic = self._last_bot_topic.get(session_id)
        bot_msg = self._last_bot_msg_id.get(session_id)
        if runtime is not None:
            if not bot_topic:
                bot_topic = getattr(getattr(runtime, "routing_state", None), "last_bot_topic_id", None)
            if not bot_msg:
                bot_msg = getattr(getattr(runtime, "last_bot_node", None), "msg_id", None)

        if bot_topic or bot_msg:
            same_topic = bool(topic_id and bot_topic and topic_id == bot_topic)
            parent_continuity = bool(parent_id and bot_msg and parent_id == bot_msg)
            if not (same_topic or parent_continuity):
                return False

        return True

    def has_bot_spoken(self, session_id: str) -> bool:
        return session_id in self._last_bot_speak_time

    def last_spoke_time(self, session_id: str) -> float:
        return float(self._last_bot_speak_time.get(session_id, 0.0))

    def last_decision(self, session_id: str) -> Optional[ArbitrationResult]:
        return self._last_decisions.get(session_id)

    def remember_decision(self, session_id: str, result: ArbitrationResult) -> ArbitrationResult:
        """Record a final decision after orchestration applies gate policy."""
        return self._remember(session_id, result)

    def _remember(self, session_id: str, result: ArbitrationResult) -> ArbitrationResult:
        self._last_decisions[session_id] = result
        return result

    def _fatigue_penalty(self, session_id: str, now: float) -> float:
        history = self._bot_speak_history.get(session_id)
        if not history:
            return 0.0
        recent = sum(1 for timestamp in history if 0.0 <= now - timestamp <= 300.0)
        return min(0.25, max(0, recent - 2) * 0.05)

    @staticmethod
    def _question_value(text: str) -> float:
        clean = (text or "").strip()
        if not clean:
            return 0.0
        if re.search(r"[？?]|为什么|怎么|如何|哪[里儿]|吗|呢", clean):
            return 1.0 if len(clean) >= 8 else 0.55
        if clean.endswith(("吗", "呢", "？", "?")):
            return 0.7
        return 0.0

    @staticmethod
    def _participation(telemetrics: RoomTelemetrics) -> float:
        speakers = int(getattr(telemetrics, "unique_speakers", 0) or 0)
        return round(min(1.0, max(0, speakers - 1) / 4.0), 3)

    @staticmethod
    def _professionalism(text: str, telemetrics: RoomTelemetrics) -> float:
        clean = (text or "").strip()
        length_signal = min(1.0, len(clean) / 80.0)
        technical = 1.0 if re.search(
            r"代码|报错|接口|架构|算法|部署|配置|数据库|日志|怎么实现|为什么失败|\b(?:api|python|java|sql)\b",
            clean,
            flags=re.IGNORECASE,
        ) else 0.0
        return round(min(1.0, length_signal * 0.45 + telemetrics.punctuation_formality * 0.25 + technical * 0.30), 3)

    def maybe_auto_cool(
        self,
        session_id: str,
        vibe_mode: GroupChatMode,
        telemetrics: RoomTelemetrics,
        current_time: Optional[float] = None,
        min_samples: int = 3,
    ) -> bool:
        """Enters deep cooling when a previously active room fades to chill."""
        if vibe_mode != GroupChatMode.CHILL_FADE:
            return False
        if not self.has_bot_spoken(session_id):
            return False
        if telemetrics.sample_size < min_samples:
            return False
        now = self._now(current_time)
        if self.is_in_deep_cooling(session_id, current_time=now):
            return False
        self.trigger_cooling(session_id, current_time=now)
        return True

    def evaluate(
        self,
        session_id: str,
        addressivity: AddressivityScore,
        telemetrics: RoomTelemetrics,
        vibe_mode: GroupChatMode,
        user_id: str,
        text: str,
        current_time: Optional[float] = None,
        allow_ambient: bool = True,
        topic_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        runtime: Any = None,
    ) -> ArbitrationResult:
        """Evaluates whether the bot should intervene and speak.

        Args:
            session_id: Group session identifier.
            addressivity: Evaluated AddressivityScore from AddressivityRouter.
            telemetrics: Real-time room telemetrics.
            vibe_mode: Current classified GroupChatMode.
            user_id: Sender user ID.
            text: Consolidated text from flushed turn.
            current_time: Optional virtual or system timestamp.

        Returns:
            ArbitrationResult containing decision and diagnostics.
        """
        now = self._now(current_time)
        in_cooling = self.is_in_deep_cooling(session_id, current_time=now)
        is_low_effort = self.is_low_effort_text(text)
        professionalism = self._professionalism(text, telemetrics)
        topic_relevance = getattr(addressivity, "topic_relevance", 0.0)
        fatigue_penalty = self._fatigue_penalty(session_id, now)
        question_value = self._question_value(text)
        participation = self._participation(telemetrics)
        current_private_topic = "private_topic" in classify_message(text)[0]
        private_topic = "private_topic" in getattr(telemetrics, "scene_tags", ())
        # Count low-effort only for an explicit bot address or the same user
        # the bot just spoke to.  Other members' "哦/6" after a bot turn must
        # not pollute the interlocutor's streak, even if temporal proximity
        # lands them in SAFE_HOVER.
        explicit_bot_address = addressivity.level == AddressivityLevel.STRONG
        same_user_continuation = self._is_same_user_continuation(
            session_id, user_id, now, topic_id=topic_id, parent_id=parent_id, runtime=runtime
        )
        track_energy = explicit_bot_address or same_user_continuation

        is_asymmetric = False
        if track_energy:
            is_asymmetric = self.check_energy_asymmetry(session_id, user_id, text)

        # 1. Strong + substantive content overrides cooling and a prior low-effort streak.
        if addressivity.level == AddressivityLevel.STRONG and not is_low_effort:
            self.reset_asymmetry(session_id, user_id)
            return self._remember(session_id, ArbitrationResult(
                should_speak=True,
                willingness_score=1.0,
                threshold=self.base_threshold,
                reason="Strong direct addressivity (@bot or quote)",
                in_deep_cooling=in_cooling,
                professionalism=professionalism,
                topic_relevance=topic_relevance,
                fatigue_penalty=fatigue_penalty,
                question_value=question_value,
                participation=participation,
                private_topic=private_topic,
            ))

        # 2. Consecutive low-effort replies in a bot conversation cut off speech.
        if is_asymmetric:
            return self._remember(session_id, ArbitrationResult(
                should_speak=False,
                willingness_score=0.0,
                threshold=self.base_threshold,
                reason=f"Energy asymmetry triggered after {self._low_effort_streaks.get((session_id, user_id))} low-effort replies",
                in_deep_cooling=in_cooling,
                is_energy_asymmetric=True,
                professionalism=professionalism,
                topic_relevance=topic_relevance,
                fatigue_penalty=fatigue_penalty,
                question_value=question_value,
                participation=participation,
                private_topic=private_topic,
            ))

        # 3. Strong address (including first low-effort ping) overrides deep cooling.
        if addressivity.level == AddressivityLevel.STRONG:
            return self._remember(session_id, ArbitrationResult(
                should_speak=True,
                willingness_score=1.0,
                threshold=self.base_threshold,
                reason="Strong direct addressivity (@bot or quote)",
                in_deep_cooling=in_cooling,
                professionalism=professionalism,
                topic_relevance=topic_relevance,
                fatigue_penalty=fatigue_penalty,
                question_value=question_value,
                participation=participation,
                private_topic=private_topic,
            ))

        if current_private_topic:
            return self._remember(session_id, ArbitrationResult(
                should_speak=False,
                willingness_score=0.0,
                threshold=self.base_threshold,
                reason="Private-topic boundary: stay silent unless explicitly addressed",
                in_deep_cooling=in_cooling,
                professionalism=professionalism,
                topic_relevance=topic_relevance,
                fatigue_penalty=fatigue_penalty,
                question_value=question_value,
                participation=participation,
                private_topic=True,
            ))

        # 3. If in Deep Cooling and not strong address -> stay silent
        if in_cooling:
            remaining = self._cooling_until[session_id] - now
            return self._remember(session_id, ArbitrationResult(
                should_speak=False,
                willingness_score=0.1,
                threshold=self.base_threshold,
                reason=f"Suppressed: session in deep cooling ({remaining:.1f}s remaining)",
                in_deep_cooling=True,
                professionalism=professionalism,
                topic_relevance=topic_relevance,
                fatigue_penalty=fatigue_penalty,
                question_value=question_value,
                participation=participation,
                private_topic=private_topic,
            ))

        # 4. Safe Hover never speaks. Evidence may later promote a follow-up to STRONG.
        if addressivity.level == AddressivityLevel.SAFE_HOVER:
            return self._remember(session_id, ArbitrationResult(
                should_speak=False,
                willingness_score=addressivity.score,
                threshold=self.base_threshold,
                reason="Safe hover: silently buffered, never speaks",
                in_deep_cooling=False,
                professionalism=professionalism,
                topic_relevance=topic_relevance,
                fatigue_penalty=fatigue_penalty,
                question_value=question_value,
                participation=participation,
                private_topic=private_topic,
            ))

        # Filter mode: weak / ambient turns never open a native LLM call.
        if not allow_ambient:
            return self._remember(session_id, ArbitrationResult(
                should_speak=False,
                willingness_score=addressivity.score,
                threshold=self.base_threshold,
                reason="Filter gate: no ambient intervention unless explicitly addressed",
                in_deep_cooling=False,
                professionalism=professionalism,
                topic_relevance=topic_relevance,
                fatigue_penalty=fatigue_penalty,
                question_value=question_value,
                participation=participation,
                private_topic=private_topic,
            ))

        # 5. Calculate Dynamic Willingness-to-Speak Score (WTS)
        # Base from addressivity
        wts = addressivity.score

        # Vibe adjustment
        threshold = self.base_threshold
        if vibe_mode == GroupChatMode.FAST_BANTER:
            # In fast banter, lower threshold slightly to join banter if relevant
            threshold = 0.55
            wts += 0.05
        elif vibe_mode == GroupChatMode.SERIOUS_INQUIRY:
            # In serious inquiry, higher threshold to avoid frivolous interruptions
            threshold = 0.65
        elif vibe_mode == GroupChatMode.CHILL_FADE:
            # In chill fade, avoid unprompted intrusion into dying room
            threshold = 0.75
            wts -= 0.10

        # Content value and topical continuity can justify a restrained
        # intervention in active rooms; repeated bot participation pushes the
        # score back down.
        wts += topic_relevance * self.topic_weight
        wts += professionalism * self.professionalism_weight
        wts += question_value * self.question_weight
        wts += participation * self.participation_weight
        wts -= fatigue_penalty * self.fatigue_weight

        # Frequency / cooldown penalty: if bot spoke within last 15s, penalize WTS
        last_spoke = self._last_bot_speak_time.get(session_id, 0.0)
        time_since_bot = now - last_spoke
        if 0.0 <= time_since_bot < 15.0:
            wts -= 0.20

        wts = max(0.0, min(1.0, round(wts, 3)))
        should_speak = wts >= threshold

        reason = (
            f"WTS {wts:.2f} >= threshold {threshold:.2f}"
            if should_speak
            else f"WTS {wts:.2f} < threshold {threshold:.2f} (mode: {vibe_mode.value})"
        )

        return self._remember(session_id, ArbitrationResult(
            should_speak=should_speak,
            willingness_score=wts,
            threshold=threshold,
            reason=reason,
            in_deep_cooling=False,
            professionalism=professionalism,
            topic_relevance=topic_relevance,
            fatigue_penalty=fatigue_penalty,
            question_value=question_value,
            participation=participation,
            private_topic=private_topic,
        ))
