"""Incompleteness Heuristics Engine (IHE) for AstrBot Group Chat Dynamics.

Provides zero-dependency linguistic analysis for conversational turn-taking debounce,
detecting trailing conjunctions, hanging punctuation, unclosed syntax, and short fragments
across Chinese and English dialogue.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class IncompletenessResult:
    """Structured evaluation result for message incompleteness."""

    is_incomplete: bool
    score: float
    matched_rules: List[str] = field(default_factory=list)
    unclosed_items: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


class IncompletenessDetector:
    """High-performance, stateless conversational incompleteness detector

    supporting bilingual (Chinese & English) chat dynamics.
    """

    # ---------------------------------------------------------
    # 1. Trailing Conjunctions
    # ---------------------------------------------------------
    RE_ZH_CONJ = re.compile(
        r"(?:因为|所以|但是|但|而且|并且|如果|要是|假如|虽然|虽说|不过|然后|还有|以及|或者|或是|另外|话说|就是说|也就是|比如|譬如|哪怕|万一|既然|不仅|况且|除非|免得|以防|只要|只有)"
        r"[，,、:：…\.。 \t~～]*$",
        re.IGNORECASE,
    )
    RE_ZH_CONJ_EXEMPT = re.compile(
        r"(?:然后呢|所以呢|不过如此|知其所以然|理所当然)[？?。！!\s]*$",
        re.IGNORECASE,
    )

    RE_EN_CONJ = re.compile(
        r"\b(?:and|or|but|because|so|if|then|although|even\s+though|also|plus|yet|while|when|since|unless|until|otherwise|cuz|cos|bcuz)\b"
        r"[\s.,:;~…—\-_]*$",
        re.IGNORECASE,
    )
    RE_EN_CONJ_EXEMPT = re.compile(
        r"\b(?:think|hope|guess|suppose|believe|told\s+you|see\s+you|until|by|since|back|okay|ok|alright)\s+(?:so|then)\b[\s.!?]*$",
        re.IGNORECASE,
    )

    # ---------------------------------------------------------
    # 2. Hanging Punctuation
    # ---------------------------------------------------------
    RE_ELLIPSIS = re.compile(r"(?:\.{2,}|…+|。{2,}|·{2,})[\s]*$")
    RE_DASH = re.compile(r"(?:——+|—+|–+|--+|―+)[\s]*$")
    RE_COMMA = re.compile(r"[,，、\\][\s]*$")
    RE_COLON = re.compile(r"[:：][\s]*$")
    RE_SEMICOLON = re.compile(r"(?<![;:=-])[;；][\s]*$")
    RE_TILDE = re.compile(r"[~～]+[\s]*$")
    RE_MULTI_TILDE = re.compile(r"[~～]{2,}[\s]*$")

    # ---------------------------------------------------------
    # 3. Suspension Turn Holders & Numbered Lists
    # ---------------------------------------------------------
    RE_SUSPENSION = re.compile(
        r"^\s*(?:等一下|等等|稍等|稍等一下|稍等片刻|等我一下|等等我|等下|等会|等会儿|慢着|别急|先别|马上|马上来|"
        r"wait|hold\s+on|one\s+sec|sec|gimme\s+a\s+sec|brb|moment)[\s.,!~…—\-]*$",
        re.IGNORECASE,
    )

    RE_NUMBERED_ITEM = re.compile(
        r"^\s*(?:\d+[\.、\):：]|[-*•]\s+|[（(]\d+[）)]|[【\[]\d+[】\]]|第一[点个步、:：]|首先|其次)",
        re.IGNORECASE,
    )

    # ---------------------------------------------------------
    # 4. Terminal Punctuation & Modal Particles
    # ---------------------------------------------------------
    RE_TERMINAL_PUNCT = re.compile(r"[。！？!?\.][\s]*$")
    RE_MODAL_PARTICLES = re.compile(r"[了吧呢啊呀啦哦嘛哈呗么哇呐捏哒滴嗷耶][\s~～]*$")

    RE_ZH_HANGING_TAIL = re.compile(
        r"(?:看|听|说|想|觉得|查|做|写|发|找|用|改|问|知道|发现|包括|关于|对于|至于|根据|按照|的|地|得|个|次|遍|下|件|种|些|这|那|其|与|和|及|以|让|使|由|从|向|往|在|把|被|为)[~～\s]*$"
    )
    RE_ZH_HANGING_HEAD = re.compile(
        r"^\s*(?:我刚才|刚才|刚刚|关于|至于|对于|按照|根据|如果说|我说|我想说|实际上|其实|你看|还有)[^。！？!?]+$"
    )

    # ---------------------------------------------------------
    # 5. Autonomous Complete Vocabulary
    # ---------------------------------------------------------
    AUTONOMOUS_COMPLETE = {
        "好", "好的", "行", "不行", "可以", "不可", "收到", "明白", "对", "对的", "是的", "不是",
        "没问题", "知道", "知道了", "不用", "谢谢", "多谢", "感谢", "没事", "算了", "算了吧",
        "6", "666", "nb", "牛逼", "确实", "赞", "早", "晚安", "拜拜", "再见", "哈哈", "哈哈哈", "草",
        "ok", "okay", "okk", "k", "kk", "yes", "no", "yep", "nope", "sure", "fine", "cool",
        "thx", "thanks", "ty", "np", "gg", "gl", "lol", "lmao"
    }

    BRACKET_PAIRS = {
        "(": ")", "[": "]", "{": "}",
        "\uff08": "\uff09",  # （ ）
        "\u3010": "\u3011",  # 【 】
        "\uff5b": "\uff5d",  # ｛ ｝
        "\u300a": "\u300b",  # 《 》
        "\u300c": "\u300d",  # 「 」
        "\u300e": "\u300f"   # 『 』
    }

    def __init__(self, threshold: float = 0.50):
        self.threshold: float = float(threshold)

    def _mask_code_spans(self, text: str) -> Tuple[str, List[str]]:
        """Replace fenced/inline code with unique placeholders before quote checks."""
        unclosed: List[str] = []
        token = f"\ue000CB{uuid.uuid4().hex}\ue001"
        counter = 0

        if text.count("```") % 2 != 0:
            unclosed.append("code_block")

        def _fence(_match: re.Match) -> str:
            nonlocal counter
            counter += 1
            return f"{token}F{counter}X"

        masked = re.sub(r"```[\s\S]*?```", _fence, text)
        if "```" in masked and "code_block" not in unclosed:
            unclosed.append("code_block")

        def _inline(_match: re.Match) -> str:
            nonlocal counter
            counter += 1
            return f"{token}I{counter}X"

        masked = re.sub(r"`[^`]*`", _inline, masked)
        if masked.count("`") % 2 != 0:
            unclosed.append("inline_code")
        return masked, unclosed

    def _check_unclosed_syntax(self, text: str) -> List[str]:
        """Scans for unclosed brackets, quotes, and code fences."""
        masked, unclosed = self._mask_code_spans(text)

        # Paired brackets (code spans already replaced with quote-free tokens)
        closing_map = {v: k for k, v in self.BRACKET_PAIRS.items()}
        stack: List[str] = []
        for ch in masked:
            if ch in self.BRACKET_PAIRS:
                stack.append(ch)
            elif ch in closing_map:
                if stack and stack[-1] == closing_map[ch]:
                    stack.pop()
        if stack:
            unclosed.append(f"bracket_{stack[-1]}")

        # Paired Chinese quotes: “ ”, ‘ ’
        if masked.count("\u201c") > masked.count("\u201d"):
            unclosed.append("chinese_double_quote")
        if masked.count("\u2018") > masked.count("\u2019"):
            unclosed.append("chinese_single_quote")

        # ASCII Double Quote (ignore feet-inches like 5'10")
        masked_no_height = re.sub(r"\b\d+\x27\d+(?:\"|″)?", "", masked)
        if len(re.findall(r'(?<!\\)"', masked_no_height)) % 2 != 0:
            unclosed.append("ascii_double_quote")

        # ASCII Single Quote (ignoring contractions like it's, 90's, 5'10")
        text_no_contractions = re.sub(r"\b[a-zA-Z]+\x27[a-zA-Z]+\b", "", masked)
        text_no_contractions = re.sub(r"\b[a-zA-Z]+s[\x27\u2019](?=\s|[.,!?;:]|$)", "", text_no_contractions)
        text_no_contractions = re.sub(r"\b\d+[\x27\u2019]s\b", "", text_no_contractions)
        text_no_contractions = re.sub(r"\b\d+s[\x27\u2019]", "", text_no_contractions)
        text_no_contractions = re.sub(r"\b\d+\x27\d+(?:\"|″)?", "", text_no_contractions)
        if len(re.findall(r"(?<!\\)\x27", text_no_contractions)) % 2 != 0:
            unclosed.append("ascii_single_quote")

        return unclosed

    def evaluate(self, text: str) -> IncompletenessResult:
        """Evaluates conversational incompleteness for a given message text.

        Returns IncompletenessResult containing score, boolean flag, and matched rules.
        """
        if text is None:
            return IncompletenessResult(is_incomplete=False, score=0.0)

        cleaned = text.strip()
        if not cleaned:
            return IncompletenessResult(is_incomplete=False, score=0.0)

        tone_stripped = re.sub(r"[~～]+$", "", cleaned).strip()
        # Fast-Path 1: Autonomous complete expressions, including polite ~ / ~~ tails.
        if cleaned.lower() in self.AUTONOMOUS_COMPLETE or tone_stripped.lower() in self.AUTONOMOUS_COMPLETE:
            return IncompletenessResult(
                is_incomplete=False, score=0.0, details={"reason": "autonomous_complete"}
            )

        # Fast-Path 2: Explicit turn-holding suspension words
        if self.RE_SUSPENSION.match(cleaned):
            return IncompletenessResult(
                is_incomplete=True,
                score=0.85,
                matched_rules=["suspension_turn_holder"]
            )

        matched_rules: List[str] = []
        total_score = 0.0

        # Rule 1: Unclosed syntax (0.90)
        unclosed = self._check_unclosed_syntax(cleaned)
        if unclosed:
            matched_rules.append("unclosed_syntax")
            total_score += 0.90

        # Rule 2: Chinese trailing conjunctions (0.85)
        if self.RE_ZH_CONJ.search(cleaned) and not self.RE_ZH_CONJ_EXEMPT.search(cleaned):
            matched_rules.append("zh_trailing_conjunction")
            total_score += 0.85

        # Rule 3: English trailing conjunctions (0.85)
        if self.RE_EN_CONJ.search(cleaned) and not self.RE_EN_CONJ_EXEMPT.search(cleaned):
            matched_rules.append("en_trailing_conjunction")
            total_score += 0.85

        # Rule 4: Hanging Punctuation (0.75 - 0.80)
        if self.RE_ELLIPSIS.search(cleaned):
            matched_rules.append("trailing_ellipsis")
            total_score += 0.80
        if self.RE_DASH.search(cleaned):
            matched_rules.append("trailing_dash")
            total_score += 0.80
        if self.RE_COMMA.search(cleaned):
            matched_rules.append("trailing_comma")
            total_score += 0.80
        if self.RE_COLON.search(cleaned):
            matched_rules.append("trailing_colon")
            total_score += 0.80
        if self.RE_SEMICOLON.search(cleaned):
            matched_rules.append("trailing_semicolon")
            total_score += 0.75

        # Rule 5: Tildes (0.30 - 0.50)
        if self.RE_MULTI_TILDE.search(cleaned):
            matched_rules.append("trailing_multi_tilde")
            total_score += 0.50
        elif self.RE_TILDE.search(cleaned):
            matched_rules.append("trailing_tilde")
            total_score += 0.30

        # Rule 6: Numbered list item prefix (0.70)
        if self.RE_NUMBERED_ITEM.match(cleaned) and not self.RE_TERMINAL_PUNCT.search(cleaned):
            matched_rules.append("numbered_list_prefix")
            total_score += 0.70

        # Rule 7: Short fragment with hanging syntax (0.60)
        has_terminal = bool(self.RE_TERMINAL_PUNCT.search(cleaned))
        has_particle = bool(self.RE_MODAL_PARTICLES.search(cleaned))
        if 2 <= len(cleaned) <= 18 and not has_terminal and not has_particle and not matched_rules:
            # A terminal verb/measure word alone is not evidence of an unfinished utterance.
            # Explicit heads (because/if/etc.) still carry grammatical evidence.
            if self.RE_ZH_HANGING_HEAD.match(cleaned):
                matched_rules.append("short_fragment_hanging_syntax")
                total_score += 0.60

        final_score = min(1.0, total_score)
        is_inc = final_score >= self.threshold

        return IncompletenessResult(
            is_incomplete=is_inc,
            score=final_score,
            matched_rules=matched_rules,
            unclosed_items=unclosed,
            details={
                "cleaned_len": len(cleaned),
                "has_terminal": has_terminal,
                "has_particle": has_particle,
            },
        )

    def check_incompleteness(self, text: str) -> bool:
        """Lightweight boolean check directly consumed by DebounceBuffer."""
        return self.evaluate(text).is_incomplete

    def is_incomplete(self, text: str) -> bool:
        """Alias for check_incompleteness."""
        return self.evaluate(text).is_incomplete

    def __call__(self, text: str) -> bool:
        """Callable interface for check_incompleteness."""
        return self.evaluate(text).is_incomplete
