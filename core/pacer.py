"""Pacing and Dynamic Fragmentation Engine for AstrBot Group Chat Dynamics.

Splits bulky LLM generations into realistic multi-turn fragmented bursts and calculates
human typing latencies (thinking pause + character counts) to eradicate instant robotic replies.
"""

from __future__ import annotations

import logging
import math
import re
from typing import List, Optional

from .reactions import ReactionPolicy
from .style_shaper import StyleShaper
from .vibe_analyzer import GroupChatMode

logger = logging.getLogger("astrbot_plugin_chat_dynamics.pacer")


_RHYTHM_SHORT_ACTS = frozenset({"wake_reply", "goodnight_reply", "morning_hi", "insomnia_line"})


def is_rhythm_short_act(action: str) -> bool:
    return str(action or "") in _RHYTHM_SHORT_ACTS


def scale_delay(delay: float, scale: float = 1.0) -> float:
    """Apply a gate pace factor. Values above 1 slow down; below 1 speed up."""
    try:
        factor = max(0.5, min(2.5, float(scale if scale is not None else 1.0)))
    except (TypeError, ValueError):
        factor = 1.0
    try:
        return max(0.0, float(delay) * factor)
    except (TypeError, ValueError):
        return 0.0


class PacingShaper:
    """Orchestrates message fragmentation, pacing delays, and stylistic adaptation."""

    @staticmethod
    def persona_fragments(text: str) -> List[str]:
        """Keep generated content intact; only split large prose at paragraph boundaries."""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= 1200 or any(token in text for token in ("`", "$", "http://", "https://")):
            return [text]
        if re.search(r"(?m)^\s*(?:[-*+] |\d+[.)] |\|)", text):
            return [text]
        paragraphs = text.split("\n\n")
        parts = []
        current = ""
        for paragraph in paragraphs:
            if current and len(current) + len(paragraph) > 1200 and len(parts) < 2:
                parts.append(current)
                current = paragraph
            else:
                current += ("\n\n" if current else "") + paragraph
        if current:
            parts.append(current)
        return parts

    def __init__(
        self,
        style_shaper: Optional[StyleShaper] = None,
        chars_per_second: float = 20.0,
        base_thinking_delay: float = 0.8,
        min_typing_delay: float = 0.4,
        max_typing_delay: float = 4.0,
        inter_burst_interval: float = 1.2,
    ):
        if not isinstance(chars_per_second, (int, float)) or chars_per_second <= 0:
            raise ValueError("chars_per_second must be greater than zero")
        if base_thinking_delay < 0 or min_typing_delay < 0 or max_typing_delay < min_typing_delay:
            raise ValueError("typing delay bounds are invalid")
        if inter_burst_interval < 0:
            raise ValueError("inter_burst_interval must be non-negative")
        self.style_shaper: StyleShaper = style_shaper or StyleShaper()
        self.reaction_policy: ReactionPolicy = ReactionPolicy()
        self.chars_per_second: float = chars_per_second
        self.base_thinking_delay: float = base_thinking_delay
        self.min_typing_delay: float = min_typing_delay
        self.max_typing_delay: float = max_typing_delay
        self.inter_burst_interval: float = inter_burst_interval

    def shape_and_fragment(
        self,
        text: str,
        mode: GroupChatMode,
        max_fragments: int = 3,
        min_fragment_chars: int = 15,
        max_fragment_chars: int = 120,
        trigger_text: str = "",
    ) -> List[str]:
        """Formats and splits output into 1-3 conversational bursts appropriate for mode.

        Args:
            text: Raw LLM output string.
            mode: Current group vibe mode.
            max_fragments: Maximum fragments to return (default 3).
            min_fragment_chars: Threshold below which fragments are not split further.

        Returns:
            List of message strings to be dispatched sequentially.
        """
        if max_fragments < 1:
            raise ValueError("max_fragments must be at least 1")
        if max_fragment_chars < min_fragment_chars:
            raise ValueError("max_fragment_chars must be at least min_fragment_chars")
        max_fragments = min(max_fragments, 3)

        # 1. Apply style shaping & strip robotic signoffs
        adapted_text = self.style_shaper.adapt_style(text, mode)
        if not adapted_text:
            return []

        def finalize(parts: List[str]) -> List[str]:
            fragments = [part.strip() for part in parts if part and part.strip()]
            if fragments:
                fragments[-1] = self.reaction_policy.apply(
                    fragments[-1],
                    mode=mode,
                    trigger_text=trigger_text,
                    enabled=self.style_shaper.casual_emoji_enabled,
                    fragment_count=len(fragments),
                )
            return fragments

        # Serious answers remain cohesive until they become visibly bulky.
        if mode == GroupChatMode.SERIOUS_INQUIRY and len(adapted_text) <= max_fragment_chars * 2:
            return finalize([adapted_text])

        # 3. If in FAST_BANTER (or CHILL_FADE), fragment long texts into 2-3 short bursts
        if len(adapted_text) < 35:
            return finalize([adapted_text])

        # The configured size is a soft per-message target.  For unusually
        # large replies it grows just enough to preserve all content in at
        # most three bursts instead of truncating the answer.
        target_chars = max(max_fragment_chars, math.ceil(len(adapted_text) / max_fragments))

        # Split on sentence boundaries, newlines, or transitional conjunctions
        # Patterns for splitting: \n+, [。？！?!]+, or comma-conjunction phrases
        def split_preserving_boundaries(pattern: str) -> List[str]:
            chunks = []
            start = 0
            for match in re.finditer(pattern, adapted_text):
                chunks.append(adapted_text[start : match.end()])
                start = match.end()
            if start < len(adapted_text):
                chunks.append(adapted_text[start:])
            return chunks

        # Keep separators until finalization so recombining chunks preserves
        # both Chinese punctuation and the original English word spacing.
        chunks = split_preserving_boundaries(r"[。？！?!]\s*|\n+")

        if len(chunks) <= 1:
            # Fallback: split on semicolon or comma if chunk is very long
            chunks = split_preserving_boundaries(r"[；;，,]\s*")

        expanded: List[str] = []
        for chunk in chunks:
            while len(chunk) > target_chars:
                window = chunk[: target_chars + 1]
                candidates = [window.rfind(token) for token in (" ", "，", ",", "；", ";", "：", ":")]
                split_at = max(candidates)
                if split_at < int(target_chars * 0.6):
                    split_at = target_chars
                else:
                    # The chosen delimiter belongs to the preceding chunk.
                    split_at += 1
                expanded.append(chunk[:split_at])
                chunk = chunk[split_at:]
            if chunk:
                expanded.append(chunk)
        chunks = expanded

        # Combine small adjacent chunks to satisfy min_fragment_chars
        fragments: List[str] = []
        current = ""

        for c in chunks:
            if not current:
                current = c
            elif (
                len(current) + len(c) < min_fragment_chars
                or len(fragments) >= (max_fragments - 1)
            ):
                current += c
            else:
                fragments.append(current)
                current = c

        if current:
            fragments.append(current)

        # Cap at max_fragments
        if len(fragments) > max_fragments:
            head = fragments[: max_fragments - 1]
            tail = "".join(fragments[max_fragments - 1 :])
            fragments = head + [tail]

        return finalize(fragments)

    def calculate_typing_delay(
        self,
        text_burst: str,
        mode: GroupChatMode,
        is_first_burst: bool = True,
        delay_scale: float = 1.0,
    ) -> float:
        """Calculates simulated typing latency in seconds for a specific burst.

        Args:
            text_burst: Text of the fragment about to be sent.
            mode: Current vibe mode.
            is_first_burst: Whether this is the opening burst (includes thinking pause).
            delay_scale: Gate pace factor; values above 1 slow the send.

        Returns:
            Float delay in seconds.
        """
        char_count = len(text_burst.strip())
        typing_time = char_count / self.chars_per_second

        # Include thinking delay on first burst
        thinking = self.base_thinking_delay if is_first_burst else 0.0

        if mode == GroupChatMode.FAST_BANTER:
            # Fast banter has slightly quicker responses
            typing_time *= 0.85
            thinking *= 0.70

        total_delay = thinking + typing_time
        bounded = max(self.min_typing_delay, min(self.max_typing_delay, round(total_delay, 3)))
        return scale_delay(bounded, delay_scale)

    def calculate_inter_burst_delay(
        self,
        mode: GroupChatMode,
        fragment_text: str = "",
        mpm: float = 0.0,
        delay_scale: float = 1.0,
    ) -> float:
        """Calculates delay between consecutive fragmented messages."""
        length_adjustment = min(0.6, len((fragment_text or "").strip()) / 400.0)
        if mpm <= 0.0:
            rate_adjustment = 0.0
        elif mpm >= 12.0:
            rate_adjustment = -0.25
        elif mpm <= 3.0:
            rate_adjustment = 0.20
        else:
            rate_adjustment = 0.0
        delay = self.inter_burst_interval + length_adjustment + rate_adjustment
        if mode == GroupChatMode.FAST_BANTER:
            delay *= 0.85
        bounded = round(max(0.6, min(2.0, delay)), 2)
        return scale_delay(bounded, delay_scale)
