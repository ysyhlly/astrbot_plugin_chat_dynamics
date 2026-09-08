"""Deterministic acknowledgment / emoji reaction policy.

Picks at most one Unicode reaction from the trigger's scene and emotion.
Serious, private, technical, long, or already-emoji replies stay plain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .semantics import classify_message
from .vibe_analyzer import GroupChatMode

_EMOJI_RE = re.compile(r"[\U00010000-\U0010ffff\u2600-\u27bf]")
_LAUGH_CUES = ("哈哈", "hhh", "233", "笑", "草", "梗", "lol")
_THUMBS_CUES = ("赞", "好的", "可以", "收到", "谢谢", "感谢", "牛")
_THINK_CUES = ("？", "?", "怎么", "为什么", "为啥", "呢")


@dataclass(frozen=True)
class ReactionDecision:
    emoji: str = ""
    reason: str = ""

    @property
    def append_to_text(self) -> bool:
        return bool(self.emoji)


class ReactionPolicy:
    """Chooses a single acknowledgment emoji for informal bot replies."""

    def decide(
        self,
        *,
        reply_text: str,
        mode: GroupChatMode,
        trigger_text: str = "",
        enabled: bool = False,
        fragment_count: int = 1,
    ) -> ReactionDecision:
        if not enabled:
            return ReactionDecision(reason="disabled")
        if mode == GroupChatMode.SERIOUS_INQUIRY:
            return ReactionDecision(reason="serious-mode")
        text = (reply_text or "").strip()
        if not text or "```" in text:
            return ReactionDecision(reason="code-or-empty")
        if _EMOJI_RE.search(text):
            return ReactionDecision(reason="already-has-emoji")
        if len(text) > 72:
            return ReactionDecision(reason="long-reply")
        if fragment_count > 1 and len(text) > 40:
            return ReactionDecision(reason="multi-fragment")

        scenes, emotions = classify_message(trigger_text)
        if "private_topic" in scenes:
            return ReactionDecision(reason="private-topic")
        if "technical_help" in scenes and "banter" not in scenes:
            return ReactionDecision(reason="technical")

        emoji = self._pick_emoji(trigger_text, scenes, emotions)
        if not emoji:
            return ReactionDecision(reason="no-signal")
        return ReactionDecision(emoji=emoji, reason="ack")

    def apply(
        self,
        reply_text: str,
        *,
        mode: GroupChatMode,
        trigger_text: str = "",
        enabled: bool = False,
        fragment_count: int = 1,
    ) -> str:
        decision = self.decide(
            reply_text=reply_text,
            mode=mode,
            trigger_text=trigger_text,
            enabled=enabled,
            fragment_count=fragment_count,
        )
        if not decision.append_to_text:
            return reply_text
        return f"{reply_text.rstrip()} {decision.emoji}"

    @staticmethod
    def _pick_emoji(
        trigger_text: str,
        scenes: Sequence[str],
        emotions: Sequence[str],
    ) -> str:
        lowered = (trigger_text or "").lower()
        # Negation and contrast make keyword polarity unreliable; do not
        # invent a deterministic face for "不开心 / 不烦 / 开心但很累".
        if re.search(r"不|没|并非|不是|别|但|但是|不过|可是", trigger_text or ""):
            if not any(cue in lowered for cue in _LAUGH_CUES) and "banter" not in scenes:
                return ""
        if "positive" in emotions and "negative" in emotions:
            return ""
        if any(cue in lowered for cue in _LAUGH_CUES) or "banter" in scenes:
            return "😂"
        if "positive" in emotions:
            return "👍"
        if "support" in scenes or "negative" in emotions:
            return "🙏"
        if "tense" in emotions:
            return "👀"
        if any(cue in lowered for cue in _THUMBS_CUES):
            return "👍"
        if any(cue in trigger_text for cue in _THINK_CUES):
            return "👀"
        return ""
