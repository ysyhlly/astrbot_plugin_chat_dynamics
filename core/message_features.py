"""Immutable input facts shared by routing owners, independent of routing results.

Compatibility views deliberately retain different question and continuation rules.
Caches are bounded and keyed by source text, never by mutable node identity.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
import re
from .bot_identity import BotIdentityMatcher, BotReference
from .semantics import lexical_tokens, concept_scores

_BOUNDARY = re.compile(r"^(?:对了|话说|顺便问一下|另外|换个话题|说到这个|by the way)", re.I)
_ELLIPTICAL = re.compile(r"^(?:那|这个|这样|然后|继续|怎么办|为什么|怎么设|默认|我默认(?:的)?|好的|行|对|确实|好使|不行|成|那这个|这个呢)[呢？?。!！\s]*$")
_FILLER = re.compile(r"^(?:哈+|嗯+|哦+|好的|好|ok|233+|[？?!！]+)$", re.I)

def _can_start_topic(text: str) -> bool:
    """Require content beyond a reaction, acknowledgement or dangling fragment."""
    text = text.strip()
    compact = re.sub(r"[\W_]+", "", text)
    if len(compact) < 5 or bool(_ELLIPTICAL.fullmatch(text)) or _FILLER.fullmatch(text):
        return False
    return not bool(re.fullmatch(
        r"(?:哈哈|呵呵|嘿嘿|嗯|哦|啊|额|呃)+|"
        r"(?:那)?(?:确实|对的|是的|没错|好吧|好呀|好的|收到|知道了|明白了|原来如此|"
        r"真的假的|笑死我了|不知道|我也是|还真是|这么说|然后呢|所以呢|算了吧)[啊呀呢吧了的]*",
        compact,
    ))



@dataclass(frozen=True)
class MessageFeatures:
    is_short: bool
    is_elliptical: bool
    is_ack: bool
    is_question: bool
    is_answer_like: bool
    is_topic_boundary: bool
    is_vocative: bool
    has_explicit_mention: bool
    has_explicit_reply: bool
    information_density: float
    lexical_keywords: tuple[str, ...]
    can_start_topic: bool
    is_filler: bool
    quoted_subject_followup: bool
    short_followup: bool
    dialogue_followup: bool
    topicless_followup: bool
    question_ending: bool
    answer_boundary: bool
    parent_question: bool
    parent_filler: bool
    platform_wake: bool = False
    bot_reference: BotReference = BotReference()


def analyze_text(text: str) -> MessageFeatures:
    text = str(text or "")
    return (_analyze_text if len(text) <= 2048 else _analyze_text.__wrapped__)(text)


@lru_cache(maxsize=512)
def _analyze_text(text: str) -> MessageFeatures:
    stripped = text.strip()
    compact = re.sub(r"[\W_]+", "", stripped)
    elliptical = bool(_ELLIPTICAL.fullmatch(stripped))
    short_followup = bool(re.fullmatch(r"(?:然后呢|为什么|那这个呢|真的吗|怎么弄)[？?。!！\s]*", stripped))
    answer_boundary = bool(re.match(r"(?:对了|话说|另外|顺便|换个话题|by the way\b)", stripped, re.I))
    return MessageFeatures(
        is_short=len(stripped) <= 12,
        is_elliptical=elliptical,
        is_ack=bool(re.fullmatch(r"(?:好的|好|行|对|确实|收到|明白了|知道了|ok)[。！!\s]*", stripped, re.I)),
        is_question=bool(concept_scores(text).get("question")),
        is_answer_like=bool(stripped) and len(stripped) <= 32 and not bool(re.search(r"[\n。！？!?]", stripped.rstrip("。！？!?"))) and not answer_boundary,
        is_topic_boundary=bool(_BOUNDARY.search(stripped)),
        is_vocative=False, has_explicit_mention=False, has_explicit_reply=False,
        information_density=len(compact) / max(1, len(stripped)),
        lexical_keywords=tuple(sorted(lexical_tokens(text))),
        can_start_topic=_can_start_topic(text),
        is_filler=bool(_FILLER.fullmatch(stripped)),
        quoted_subject_followup=bool(re.fullmatch(r"\s*(?:那|这个|这样|这个呢|那这个呢|那怎么办|这个怎么办)[呢？?。!！]*\s*", text)),
        short_followup=short_followup,
        dialogue_followup=short_followup or bool(re.search(r"然后|继续|那|这个|这样|怎么办|呢[？?]?$", text)),
        topicless_followup=short_followup or elliptical or bool(re.fullmatch(r"\s*那(?:这个|怎么办|怎么做)[呢？?。!！\s]*", text)),
        question_ending=bool(re.search(r"[？?][\s）)\]】]*$|(?:吗|呢)[。！!\s]*$", stripped)),
        answer_boundary=answer_boundary,
        parent_question=bool(re.search(r"[?？]|怎么|如何|多少|几点|吗|能不能|可以吗|为啥|为什么", text)),
        parent_filler=bool(re.fullmatch(r"(?:哈+|[?？!！]+|嗯+|哦+|好的|好|ok|[hH]+|233+)", stripped, re.I)),
    )


def message_features(node, bot_names=None, bot_id="", *, text=None, mentions=None) -> MessageFeatures:
    """Read current source inputs; aliases and mentions are never stale cached state.

    Pass topic_text(node) explicitly for authored-topic views. Default is node.text.
    Platform metadata participates only in explicit facts, never inferred routing.
    """
    source = node.text if text is None else text
    bot_names = tuple(bot_names or ())
    mentions = tuple(node.metadata.get("actual_mentions", node.mentioned_users) if mentions is None else mentions)
    reference = BotIdentityMatcher.match(source, bot_names, mentions=mentions, bot_id=bot_id)
    return replace(analyze_text(source), is_vocative=reference.vocative,
                   has_explicit_mention=bool(mentions), has_explicit_reply=bool(node.reply_to_id),
                   platform_wake=bool(node.metadata.get("is_wake")), bot_reference=reference)
