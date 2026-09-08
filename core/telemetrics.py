"""Real-time Telemetrics Engine for AstrBot Group Chat Dynamics.

Calculates real-time group physical metrics:
- MPM (Messages Per Minute) over rolling time windows
- Token / character density
- Emoji & sticker ratio (Unicode emoji, CQ codes, [表情])
- Punctuation formality score
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from .semantics import classify_window


# Regexes are separate so the analyzer can distinguish reactions from media.
UNICODE_EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff"  # Supplemental symbols, emoticons, pictographs
    r"\u2600-\u27bf"           # Miscellaneous symbols, dingbats
    r"\u2300-\u23ff]",         # Miscellaneous technical
    flags=re.UNICODE | re.IGNORECASE,
)
MEDIA_PATTERN = re.compile(
    r"\[CQ:(?:face|image),[^\]]+\]|\[(?:表情|动画表情|图片|sticker|image|face|media|语音)\]",
    flags=re.UNICODE | re.IGNORECASE,
)
# Backwards-compatible combined marker matcher.
EMOJI_PATTERN = re.compile(
    UNICODE_EMOJI_PATTERN.pattern + "|" + MEDIA_PATTERN.pattern,
    flags=re.UNICODE | re.IGNORECASE,
)

FORMAL_PUNCTUATION = set("。？！；，：…—\"'（）、.!?;:\"'()")
CASUAL_PUNCTUATION = set("~～?？!！哈233")


@dataclass
class RoomTelemetrics:
    """Snapshot of group chat physical telemetrics."""

    mpm: float                    # Messages per minute in current window
    token_density: float          # Average character length per message
    emoji_ratio: float            # Proportion of messages containing emojis or emoji token count
    punctuation_formality: float   # 0.0 (very casual / no punctuation) to 1.0 (formal sentence structure)
    sample_size: int              # Number of messages sampled in window
    window_duration: float        # Sliding window duration in seconds
    unicode_emoji_ratio: float = 0.0
    media_ratio: float = 0.0
    scene_tags: Tuple[str, ...] = ()
    emotion_tags: Tuple[str, ...] = ()
    unique_speakers: int = 0

    @property
    def average_chars(self) -> float:
        """Average character length per message in the sliding window."""
        return self.token_density


@dataclass
class _MessageRecord:
    timestamp: float
    char_count: int
    emoji_count: int
    media_count: int
    has_formal_punc: bool
    text: str
    user_id: str = ""


class TelemetricsTracker:
    """Tracks rolling message statistics per group session."""

    def __init__(self, window_seconds: float = 60.0, max_history: int = 200, time_service: Optional[Any] = None):
        self.window_seconds: float = window_seconds
        self.max_history: int = max_history
        self.time_service = time_service
        self._records: Dict[str, Deque[_MessageRecord]] = {}

    def _now(self, timestamp: Optional[float] = None) -> float:
        if timestamp is not None:
            return timestamp
        if self.time_service is not None:
            return float(self.time_service.time())
        return time.time()

    def record_message(
        self,
        session_id: str,
        text: str,
        timestamp: Optional[float] = None,
        *,
        has_media: bool = False,
        user_id: str = "",
    ) -> None:
        """Records an incoming message event for telemetrics analysis."""
        ts = self._now(timestamp)
        if session_id not in self._records:
            self._records[session_id] = deque(maxlen=self.max_history)

        clean_text = text.strip() if text else ""
        char_count = len(clean_text)
        emojis_found = len(UNICODE_EMOJI_PATTERN.findall(clean_text))
        media_found = len(MEDIA_PATTERN.findall(clean_text)) + int(bool(has_media))

        # Check for formal punctuation ending or structure
        has_formal_punc = False
        if clean_text:
            if clean_text[-1] in "。？！.!?;":
                has_formal_punc = True
            elif any(p in clean_text for p in "，。；:："):
                has_formal_punc = True

        self._records[session_id].append(
            _MessageRecord(
                ts,
                char_count,
                emojis_found,
                media_found,
                has_formal_punc,
                clean_text[:1000],
                str(user_id or ""),
            )
        )

    def get_telemetrics(self, session_id: str, current_time: Optional[float] = None) -> RoomTelemetrics:
        """Calculates current telemetrics snapshot for the specified session."""
        now = self._now(current_time)
        cutoff = now - self.window_seconds

        records = self._records.get(session_id)
        if not records:
            return RoomTelemetrics(
                mpm=0.0,
                token_density=0.0,
                emoji_ratio=0.0,
                punctuation_formality=0.5,
                sample_size=0,
                window_duration=self.window_seconds,
            )

        # Filter to active sliding window
        window_items = [r for r in records if r.timestamp >= cutoff]
        count = len(window_items)

        if count == 0:
            return RoomTelemetrics(
                mpm=0.0,
                token_density=0.0,
                emoji_ratio=0.0,
                punctuation_formality=0.5,
                sample_size=0,
                window_duration=self.window_seconds,
            )

        # Calculate MPM (normalized to 60s)
        mpm = (count / self.window_seconds) * 60.0

        # Calculate Average Characters
        total_chars = sum(r.char_count for r in window_items)
        token_density = total_chars / count

        # Calculate Emoji Ratio (proportion of messages with at least 1 emoji or stickers)
        messages_with_emojis = sum(1 for r in window_items if r.emoji_count > 0)
        messages_with_media = sum(1 for r in window_items if r.media_count > 0)
        messages_with_markers = sum(1 for r in window_items if r.emoji_count > 0 or r.media_count > 0)
        unicode_emoji_ratio = messages_with_emojis / count
        media_ratio = messages_with_media / count
        emoji_ratio = messages_with_markers / count

        # Calculate Formality (ratio of messages with formal punctuation)
        formal_count = sum(1 for r in window_items if r.has_formal_punc)
        punctuation_formality = formal_count / count
        scene_tags, emotion_tags = classify_window(r.text for r in window_items)
        unique_speakers = len({r.user_id for r in window_items if r.user_id})

        return RoomTelemetrics(
            mpm=round(mpm, 2),
            token_density=round(token_density, 2),
            emoji_ratio=round(emoji_ratio, 3),
            punctuation_formality=round(punctuation_formality, 3),
            sample_size=count,
            window_duration=self.window_seconds,
            unicode_emoji_ratio=round(unicode_emoji_ratio, 3),
            media_ratio=round(media_ratio, 3),
            scene_tags=scene_tags,
            emotion_tags=emotion_tags,
            unique_speakers=unique_speakers,
        )

    @staticmethod
    def _classify_labels(texts: List[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        return classify_window(texts)

    def get_recent_messages(
        self,
        session_id: str,
        current_time: Optional[float] = None,
        *,
        limit: int = 12,
        max_chars: int = 1200,
    ) -> List[str]:
        now = self._now(current_time)
        cutoff = now - self.window_seconds
        records = self._records.get(session_id, ())
        messages = [record.text for record in records if record.timestamp >= cutoff and record.text]
        selected = messages[-max(1, limit):]
        while selected and sum(len(item) for item in selected) > max_chars:
            selected.pop(0)
        return selected

    def prune_stale_sessions(
        self,
        current_time: Optional[float] = None,
        max_idle: float = 600.0,
        *,
        exclude_sessions: Optional[set[str]] = None,
    ) -> int:
        """Removes sessions with no activity in max_idle seconds."""
        now = self._now(current_time)
        excluded = exclude_sessions or set()
        stale_keys = []
        for s_id, queue in self._records.items():
            if s_id in excluded:
                continue
            if not queue or (now - queue[-1].timestamp) > max_idle:
                stale_keys.append(s_id)

        for s_id in stale_keys:
            self._records.pop(s_id, None)

        return len(stale_keys)

    def reset_session(self, session_id: str) -> None:
        self._records.pop(session_id, None)

    def known_session_ids(self) -> List[str]:
        return list(self._records.keys())

    def last_timestamp(self, session_id: str) -> float:
        records = self._records.get(session_id)
        if not records:
            return 0.0
        return float(records[-1].timestamp)

    def get_rate_series(
        self,
        session_id: str,
        current_time: Optional[float] = None,
        buckets: int = 12,
    ) -> List[int]:
        """Bucketed message counts over the current sliding window."""
        now = self._now(current_time)
        buckets = max(1, int(buckets))
        series = [0] * buckets
        records = self._records.get(session_id)
        if not records:
            return series
        window = self.window_seconds
        bucket_size = window / buckets
        cutoff = now - window
        for record in records:
            ts = record.timestamp
            if ts < cutoff:
                continue
            idx = int((ts - cutoff) / bucket_size)
            if idx >= buckets:
                idx = buckets - 1
            if idx < 0:
                continue
            series[idx] += 1
        return series
