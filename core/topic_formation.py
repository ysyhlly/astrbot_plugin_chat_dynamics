"""A quiet room has no topic: require a connected burst of human text first."""
from __future__ import annotations

import re
from typing import Any

from .semantics import lexical_tokens
from .topic_resolution import can_start_topic

# Fixed connectivity evidence for forming a burst, not a topic/parent acceptance
# threshold: it combines lexical overlap with either local semantic score.
_BURST_LINK_THRESHOLD = 0.58

_MEDIA = re.compile(r"\[(?:图片|表情(?:包)?|贴纸|动画表情|语音|视频|文件|转发|json|sticker|image|face|emoji|record|video|file|forward|戳一戳)(?:[^\]]*)\]", re.I)


def topic_text(node) -> str:
    """Use authored text, never repeated media placeholders or generated captions."""
    text = node.metadata.get("topic_source_text", node.text)
    return _MEDIA.sub("", str(text)).strip()


def discussion_burst(dag, node, bot_id: str, window: float = 60.0) -> list:
    """Four distinct substantive turns, two humans, one connected discussion."""
    text = topic_text(node)
    if node.user_id == bot_id or not can_start_topic(text):
        return []
    recent = [n for n in dag.get_recent_nodes(80)
              if 0 <= node.timestamp - n.timestamp <= window
              and n.user_id != bot_id and can_start_topic(topic_text(n))]
    # Repeated images, copypasta and duplicate delivery cannot inflate intensity.
    unique = {}
    for n in sorted(recent, key=lambda n: (n.timestamp, n.msg_id), reverse=True):
        key = re.sub(r"\s+", "", topic_text(n)).lower()
        unique.setdefault(key, n)
    remaining = [n for n in unique.values() if n.msg_id != node.msg_id]
    component = [node]
    # Texts and pair scores are memoised for this call only. The nested walk
    # recomputed the same (candidate, member) comparison on every attachment
    # round, which is cubic in the window size (~85k semantic calls at the
    # documented 80-node limit) and runs synchronously under the session lock.
    texts = {node.msg_id: text, **{n.msg_id: topic_text(n) for n in remaining}}
    token_cache = {msg_id: lexical_tokens(value) for msg_id, value in texts.items()}
    semantic_cache: dict = {}

    def _linked(left: Any, right: Any) -> bool:
        if left.reply_to_id == right.msg_id or right.reply_to_id == left.msg_id:
            return True
        shared = token_cache.get(left.msg_id, frozenset()) & token_cache.get(right.msg_id, frozenset())
        if len(shared) < 2:
            return False
        first, second = left.msg_id, right.msg_id
        key = (first, second) if first <= second else (second, first)
        score = semantic_cache.get(key)
        if score is None:
            match = dag.semantic_match_fn(texts.get(first, ""), texts.get(second, ""))
            score = max(float(getattr(match, "score", 0) or 0), float(getattr(match, "embedding_cosine", 0) or 0))
            semantic_cache[key] = score
        return score >= _BURST_LINK_THRESHOLD

    while remaining:
        attached = [
            candidate for candidate in remaining
            if any(_linked(candidate, member) for member in component)
        ]
        if not attached:
            break
        component.extend(attached)
        attached_ids = {n.msg_id for n in attached}
        remaining = [n for n in remaining if n.msg_id not in attached_ids]
    turns = {(n.user_id, n.metadata.get("turn_id") or n.msg_id) for n in component}
    if len(turns) < 4 or len({n.user_id for n in component}) < 2:
        return []
    return sorted(component, key=lambda n: (n.timestamp, n.msg_id))
