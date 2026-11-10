"""Vibe & Energy Analyzer with Schmitt-trigger Hysteresis for AstrBot Group Chat Dynamics.

Analyzes physical telemetrics, classifies conversational mood, and provides
environment variables for adaptive generation and pacing strategies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
import re
from typing import Awaitable, Callable, Dict, Optional, Tuple

from .telemetrics import RoomTelemetrics, TelemetricsTracker

logger = logging.getLogger("astrbot_plugin_chat_dynamics.vibe")


class GroupChatMode(str, Enum):
    """Dominant energy and tone modes of the group chat."""

    FAST_BANTER = "fast_banter"        # Rapid short bursts, memeing, high MPM, playful
    SERIOUS_INQUIRY = "serious_inquiry"  # Long-form questions, code/technical inquiry, high formality
    CHILL_FADE = "chill_fade"          # Sparse messages, low energy, decaying thread


_MODE_LABELS = {
    "fast_banter": GroupChatMode.FAST_BANTER,
    "serious_inquiry": GroupChatMode.SERIOUS_INQUIRY,
    "chill_fade": GroupChatMode.CHILL_FADE,
}


@dataclass(frozen=True)
class AtmosphereSnapshot:
    """Independent physical energy, scene, and emotion readout."""

    energy: RoomTelemetrics
    scene_tags: Tuple[str, ...]
    emotion_tags: Tuple[str, ...]
    mode: GroupChatMode

    @property
    def unique_speakers(self) -> int:
        return int(getattr(self.energy, "unique_speakers", 0) or 0)


def parse_mode_label(raw: str) -> Optional[GroupChatMode]:
    """Extract a GroupChatMode from free-form classifier output."""
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    compact = re.sub(r"[\s\-]+", "_", text.lower())
    for key, mode in _MODE_LABELS.items():
        if re.search(rf"\b{key}\b", compact) or key in compact:
            return mode
    if any(token in text for token in ("碎梗", "闲聊", "玩梗")):
        return GroupChatMode.FAST_BANTER
    if any(token in text for token in ("严肃", "认真", "探讨")):
        return GroupChatMode.SERIOUS_INQUIRY
    if any(token in text for token in ("冷场", "冷清", "衰退")):
        return GroupChatMode.CHILL_FADE
    return None


class VibeAnalyzer:
    """Classifies real-time group vibe and applies hysteresis to prevent mode flapping."""

    def __init__(
        self,
        telemetrics_tracker: Optional[TelemetricsTracker] = None,
        fast_banter_enter_mpm: float = 12.0,
        fast_banter_exit_mpm: float = 7.0,
        chill_fade_enter_mpm: float = 3.0,
        chill_fade_exit_mpm: float = 5.0,
        serious_min_density: float = 28.0,
    ):
        self.tracker: TelemetricsTracker = telemetrics_tracker or TelemetricsTracker()
        self.fast_banter_enter_mpm: float = fast_banter_enter_mpm
        self.fast_banter_exit_mpm: float = fast_banter_exit_mpm
        self.chill_fade_enter_mpm: float = chill_fade_enter_mpm
        self.chill_fade_exit_mpm: float = chill_fade_exit_mpm
        self.serious_min_density: float = serious_min_density

        # Per-session current mode. Unknown rooms start in CHILL_FADE so we
        # do not treat silence as fast banter.
        self._current_modes: Dict[str, GroupChatMode] = {}
        self._mode_sources: Dict[str, str] = {}
        # Optional external async LLM analyzer callback for hybrid mode
        self.llm_intent_analyzer: Optional[Callable[[str, str], Awaitable[Optional[GroupChatMode]]]] = None
        self._last_llm_snapshot_time: Dict[str, float] = {}
        self._llm_snapshot_counts: Dict[str, int] = {}

    def record_message(
        self,
        session_id: str,
        text: str,
        timestamp: Optional[float] = None,
        *,
        has_media: bool = False,
        user_id: str = "",
    ) -> None:
        """Feeds an incoming message into the vibe analyzer and underlying tracker."""
        self.tracker.record_message(
            session_id, text, timestamp=timestamp, has_media=has_media, user_id=user_id
        )

    def get_telemetrics(self, session_id: str, current_time: Optional[float] = None) -> RoomTelemetrics:
        """Retrieves real-time telemetrics."""
        return self.tracker.get_telemetrics(session_id, current_time=current_time)

    def get_atmosphere(self, session_id: str, current_time: Optional[float] = None) -> AtmosphereSnapshot:
        """Return physical energy, scene, and emotion as independent fields."""
        energy = self.get_telemetrics(session_id, current_time=current_time)
        return AtmosphereSnapshot(
            energy=energy,
            scene_tags=tuple(energy.scene_tags),
            emotion_tags=tuple(energy.emotion_tags),
            mode=self.peek_mode(session_id, current_time=current_time),
        )

    def peek_mode(self, session_id: str, current_time: Optional[float] = None) -> GroupChatMode:
        """Read-only mode evaluation. Does not commit hysteresis state."""
        return self._evaluate_mode(session_id, current_time=current_time)

    def get_mode(self, session_id: str, current_time: Optional[float] = None) -> GroupChatMode:
        """Evaluates mode, commits hysteresis, and returns the active GroupChatMode."""
        new_mode = self._evaluate_mode(session_id, current_time=current_time)
        self._current_modes[session_id] = new_mode
        self._mode_sources[session_id] = "telemetrics"
        return new_mode

    def _evaluate_mode(self, session_id: str, current_time: Optional[float] = None) -> GroupChatMode:
        """Schmitt-trigger hysteresis without writing session state."""
        telemetrics = self.get_telemetrics(session_id, current_time=current_time)
        current_mode = self._current_modes.get(session_id, GroupChatMode.CHILL_FADE)
        new_mode = current_mode

        mpm = telemetrics.mpm
        density = telemetrics.token_density
        formality = telemetrics.punctuation_formality

        # Not enough evidence: stay chill. Do NOT globally force chill on mpm < 3
        # or FAST_BANTER hysteresis (exit at 7.0) is never observed.
        if telemetrics.sample_size < 2:
            new_mode = current_mode

        elif current_mode == GroupChatMode.CHILL_FADE:
            if mpm >= self.chill_fade_exit_mpm:
                if density >= self.serious_min_density and formality >= 0.40:
                    new_mode = GroupChatMode.SERIOUS_INQUIRY
                elif mpm >= self.fast_banter_enter_mpm or telemetrics.emoji_ratio >= 0.25:
                    new_mode = GroupChatMode.FAST_BANTER
                elif density >= 20.0:
                    new_mode = GroupChatMode.SERIOUS_INQUIRY
                # Stay CHILL_FADE in the [exit, enter) band so hysteresis
                # does not flap into FAST_BANTER below fast_banter_exit_mpm.

        elif current_mode == GroupChatMode.FAST_BANTER:
            if mpm < self.chill_fade_enter_mpm:
                new_mode = GroupChatMode.CHILL_FADE
            elif mpm < self.fast_banter_exit_mpm:
                if density >= self.serious_min_density and formality >= 0.50:
                    new_mode = GroupChatMode.SERIOUS_INQUIRY
                elif density >= 20.0:
                    new_mode = GroupChatMode.SERIOUS_INQUIRY
                else:
                    new_mode = GroupChatMode.CHILL_FADE
            elif density >= self.serious_min_density and formality >= 0.50:
                new_mode = GroupChatMode.SERIOUS_INQUIRY

        elif current_mode == GroupChatMode.SERIOUS_INQUIRY:
            if mpm < self.chill_fade_enter_mpm:
                new_mode = GroupChatMode.CHILL_FADE
            elif mpm >= self.fast_banter_enter_mpm and density < 20.0:
                new_mode = GroupChatMode.FAST_BANTER

        return new_mode

    def set_mode(self, session_id: str, mode: GroupChatMode, source: str = "manual") -> None:
        """Manually overrides or sets mode (e.g. from LLM analyzer snapshot)."""
        self._current_modes[session_id] = mode
        self._mode_sources[session_id] = str(source or "manual")

    def mode_source(self, session_id: str) -> str:
        return self._mode_sources.get(session_id, "telemetrics")

    def last_llm_snapshot_time(self, session_id: str) -> float:
        return float(self._last_llm_snapshot_time.get(session_id, 0.0))

    def has_llm_snapshot(self, session_id: str) -> bool:
        return session_id in self._last_llm_snapshot_time

    def mark_llm_snapshot(self, session_id: str, timestamp: float) -> None:
        self._last_llm_snapshot_time[session_id] = float(timestamp)
        self._llm_snapshot_counts[session_id] = self._llm_snapshot_counts.get(session_id, 0) + 1

    def llm_snapshot_count(self, session_id: str) -> int:
        return self._llm_snapshot_counts.get(session_id, 0)

    def reset_session(self, session_id: str) -> None:
        self._current_modes.pop(session_id, None)
        self._mode_sources.pop(session_id, None)
        self._last_llm_snapshot_time.pop(session_id, None)
        self._llm_snapshot_counts.pop(session_id, None)
        if hasattr(self.tracker, "reset_session"):
            self.tracker.reset_session(session_id)
