"""Shared bot identity interpretation for routing and ingress."""
from dataclasses import dataclass
from typing import Sequence
import re


@dataclass(frozen=True)
class BotReference:
    mention: bool = False
    vocative: bool = False
    subject: bool = False


class BotIdentityMatcher:
    @staticmethod
    def vocative_spans(name: str, text: str) -> tuple[tuple[int, int], ...]:
        name = name.strip()
        if not name or not text:
            return ()
        spans = []
        for match in re.finditer(r"@(" + re.escape(name) + r")(?=$|[\s,，。！？!?:：])", text, re.I):
            spans.append(match.span(1))
        boundary = r"(?![A-Za-z0-9_])" if name.isascii() else ""
        pattern = (r"(?:^\s*(?:@[^\s]+\s+)*|[，,。！？!?;；\n])\s*"
                   r"(?:(?:喂|嗨|hi|hello)[，,\s]*)?(" + re.escape(name) + r")" + boundary)
        for match in re.finditer(pattern, text, re.I):
            tail = text[match.end():]
            if not tail.strip(" \t,，。!！?？:："):
                spans.append(match.span(1))
                continue
            tail = tail.lstrip(" \t,，:：!！")
            if re.match(
                r"(?:第[一二三四五六七八九十0-9]+[问个]|帮|请|能不能|能否|可以|在吗|在不在|出来|回答|查|算|你|怎么看|怎么做|为什么|继续|说说|讲讲|看一下|看图|看看|看下|早上好|你好|晚上好|"
                r"please\b|can\s+you\b|could\s+you\b|help\b|what\s+do\s+you\b)",
                tail, re.I):
                spans.append(match.span(1))
        return tuple(sorted(set(spans)))

    @classmethod
    def is_vocative(cls, name: str, text: str) -> bool:
        return bool(cls.vocative_spans(name, text))

    @staticmethod
    def is_subject(name: str, text: str) -> bool:
        name = name.strip()
        if not name or not text:
            return False
        # Mentioning a Chinese substring is not enough: require a separate name
        # or a predicate about the named participant.
        left = r"(?<![A-Za-z0-9_])" if name.isascii() else r"(?:^|[\s，,。！？!?：:]|这个|那个|刚才|刚刚)"
        right = r"(?![A-Za-z0-9_])" if name.isascii() else r"(?=$|[\s，,。！？!?：:]|刚才|刚刚|今天|昨天|现在|最近|说|的|怎么|是不是|一直|又|总是)"
        if not re.search(left + re.escape(name) + right, text, re.I):
            return False
        if BotIdentityMatcher.is_vocative(name, text):
            return bool(re.search(re.escape(name) + r"(?:刚才|刚刚|之前|说|的)|你(?:刚才|刚刚|之前|为什么这么说|怎么老)", text))
        return True

    @classmethod
    def match(cls, text: str, names: Sequence[str] = (), *,
              mentions: Sequence[str] = (), bot_id: str = "") -> BotReference:
        aliases = tuple(str(n).strip() for n in names if str(n).strip())
        keys = {n.casefold() for n in aliases}
        if bot_id:
            keys.add(str(bot_id).casefold())
        return BotReference(
            any(str(uid).casefold() in keys for uid in mentions),
            any(cls.is_vocative(n, text) for n in aliases),
            any(cls.is_subject(n, text) for n in aliases),
        )

    @classmethod
    def strip_vocative(cls, text: str, names: Sequence[str] = ()) -> str:
        spans = sorted({span for name in names for span in cls.vocative_spans(name, text)}, reverse=True)
        for start, end in spans:
            text = text[:start] + text[end:]
        return text

    @classmethod
    def strip_leading_name(cls, text: str, names: Sequence[str] = ()) -> str:
        """Drop one opening name occurrence, vocative or not.

        Callers measuring how much content follows an address must not treat a
        platform wake as proof that the opening name is a vocative; the same
        prefix can read as a subject. Longest names win, and ASCII names keep a
        token boundary so "bot" never consumes "both".
        """
        for name in sorted((str(n).strip() for n in names if str(n).strip()), key=len, reverse=True):
            indent = text[:len(text) - len(text.lstrip())]
            rest = text[len(indent):]
            if not rest.startswith(name):
                continue
            tail = rest[len(name):]
            if name.isascii() and tail[:1] and (tail[0].isalnum() or tail[0] == "_"):
                continue
            return indent + tail
        return text
