"""Style Shaper and Format Adaptation Engine for AstrBot Group Chat Dynamics.

Degrades overly formal AI assistant formatting (Markdown, bullet lists, robotic sign-offs)
into colloquial human chat styles appropriate for active group chat vibes.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import List

from .vibe_analyzer import GroupChatMode

logger = logging.getLogger("astrbot_plugin_chat_dynamics.style_shaper")

# Cliché assistant endings to strictly strip from outputs
ROBOTIC_SIGNOFFS = [
    r"如果您?还有(?:其他|其它)?(?:任何)?问题[，,]?(?:请)?随时(?:问我|告诉我|找我|联系我)[。!！]?",
    r"希望(?:这个|这些)?(?:回答|解释|建议)?(?:能够|能)?(?:对|帮到)?(?:您|你)?(?:有所帮助|有所启发)?[。!！]?",
    r"如果还有疑问[，,]?(?:欢迎|可以)继续提问[。!！]?",
    r"祝(?:您|你)生活愉快[！!。]?",
    r"作为(?:一个)?(?:AI|人工智能)(?:助手)?[，,]?",
    r"随时乐意(?:为(?:您|你))?效劳[。!！]?",
    r"很高兴能为(?:您|你)解答[。!！]?",
]


class StyleShaper:
    """Adapts LLM text formatting and tone to match current group chat vibe."""

    def __init__(self, strip_markdown_in_banter: bool = True, casual_emoji_enabled: bool = False):
        self.strip_markdown_in_banter: bool = strip_markdown_in_banter
        self.casual_emoji_enabled: bool = casual_emoji_enabled

    def clean_robotic_signoffs(self, text: str) -> str:
        """Removes corporate/customer-service clichés and robotic boilerplate."""
        # Never rewrite quoted/code/math content; this is a presentation operation.
        parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|https?://\S+)", text)
        for index in range(0, len(parts), 2):
            for pattern in ROBOTIC_SIGNOFFS:
                parts[index] = re.sub(pattern, "", parts[index], flags=re.IGNORECASE)
        cleaned = "".join(parts)
        # Clean multiple trailing whitespace or punctuation
        cleaned = re.sub(r"[\s\n]+$", "", cleaned)
        return cleaned.strip()

    def strip_markdown(self, text: str, preserve_code_blocks: bool = True) -> str:
        """Strips bold, italics, headers, list bullets, and blockquotes."""
        # If code blocks present and preservation requested, isolate them
        code_blocks: List[str] = []
        marker = f"\ue000CD{uuid.uuid4().hex}X{{}}\ue001"
        if preserve_code_blocks:
            def save_code(match):
                code_blocks.append(match.group(0))
                return marker.format(len(code_blocks) - 1)
            text = re.sub(r"```[\s\S]*?```|`[^`\n]+`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|https?://\S+", save_code, text)

        # Strip headers (# Title)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        # Strip bold/italic (**bold**, *italic*, __bold__)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]*?\S)\*(?![\w*])", r"\1", text)
        text = re.sub(r"__([^_]+)__", r"\1", text)
        # Require non-identifier boundaries so snake_case is not eaten as italic.
        text = re.sub(r"(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])", r"\1", text)
        # Strip strikethrough (~~del~~)
        text = re.sub(r"~~([^~]+)~~", r"\1", text)
        # Strip blockquotes (> Quote)
        text = re.sub(r"^>\s+", "", text, flags=re.MULTILINE)
        # Strip bullet points (* item, - item)
        text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
        # Strip numbered lists (1. item)
        text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)

        # Restore code blocks
        if preserve_code_blocks and code_blocks:
            for i, block in enumerate(code_blocks):
                text = text.replace(marker.format(i), block, 1)

        return text.strip()

    def adapt_style(self, text: str, mode: GroupChatMode) -> str:
        """Adapts formatting based on the room's current GroupChatMode."""
        # 1. Clean robotic boilerplate regardless of mode
        text = self.clean_robotic_signoffs(text)

        # 2. In FAST_BANTER mode: complete format degradation to casual tone
        if mode == GroupChatMode.FAST_BANTER and self.strip_markdown_in_banter:
            text = self.strip_markdown(text, preserve_code_blocks=True)

        elif mode == GroupChatMode.SERIOUS_INQUIRY:
            # In serious inquiry, preserve Markdown and technical structure (e.g. code snippets)
            pass

        elif mode == GroupChatMode.CHILL_FADE:
            # In chill fade, keep response succinct and clean
            text = self.strip_markdown(text, preserve_code_blocks=True)

        return text.strip()
