"""A quiet room has no topic: require a connected burst of human text first."""
from __future__ import annotations

import re

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
    remaining = list(unique.values())
    component = [node]
    remaining = [n for n in remaining if n.msg_id != node.msg_id]
    while remaining:
        attached = []
        for candidate in remaining:
            left = topic_text(candidate)
            for member in component:
                right = topic_text(member)
                explicit = candidate.reply_to_id == member.msg_id or member.reply_to_id == candidate.msg_id
                shared = lexical_tokens(left) & lexical_tokens(right)
                match = dag.semantic_match_fn(left, right)
                semantic = max(float(getattr(match, "score", 0) or 0), float(getattr(match, "embedding_cosine", 0) or 0))
                if explicit or (len(shared) >= 2 and semantic >= _BURST_LINK_THRESHOLD):
                    attached.append(candidate)
                    break
        if not attached:
            break
        component.extend(attached)
        attached_ids = {n.msg_id for n in attached}
        remaining = [n for n in remaining if n.msg_id not in attached_ids]
    turns = {(n.user_id, n.metadata.get("turn_id") or n.msg_id) for n in component}
    if len(turns) < 4 or len({n.user_id for n in component}) < 2:
        return []
    return sorted(component, key=lambda n: (n.timestamp, n.msg_id))
