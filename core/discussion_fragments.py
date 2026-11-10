"""Conservative utterance boundaries and non-semantic input detection."""
import re

from .message_features import analyze_text

_MEDIA = re.compile(r'\[(?:图片|表情包|动画表情|语音|视频|文件|image|audio|video|sticker)\]', re.I)


def low_information(text):
    text = _MEDIA.sub('', str(text or '')).strip()
    facts = analyze_text(text)
    return not any(char.isalnum() for char in text) or facts.is_filler or facts.is_ack


def fragment_groups(runtime, entries):
    """Only adjacent fragments from the same native debounced turn can merge."""
    positions = {n.msg_id: i for i, n in enumerate(runtime.dag.get_recent_nodes(0))}
    groups = []
    for entry in entries:
        node, revision = entry
        group = groups[-1] if groups else []
        previous = group[-1][0] if group else None
        routing = node.metadata.get('routing', {})
        prior = previous.metadata.get('routing', {}) if previous else {}
        compatible = bool(previous and len(group) < 4 and
            node.user_id == previous.user_id and revision == group[-1][1] and
            node.metadata.get('turn_id') and node.metadata.get('turn_id') == previous.metadata.get('turn_id') and
            positions.get(node.msg_id, -2) == positions.get(previous.msg_id, -4) + 1 and
            0 <= node.timestamp - group[0][0].timestamp <= 8 and
            not node.reply_to_id and not node.mentioned_users and not node.metadata.get('is_wake') and
            not analyze_text(node.text).is_topic_boundary and not low_information(node.text) and not low_information(previous.text) and
            routing.get('topic_status') != 'committed' and prior.get('topic_status') != 'committed' and
            not routing.get('discussion_rank_attempted') and not prior.get('discussion_rank_attempted') and
            tuple(routing.get('addressee_ids', ())) == tuple(prior.get('addressee_ids', ())) and
            routing.get('recipient_anchor_id', '') == prior.get('recipient_anchor_id', '') and
            sum(len(n.text) + 1 for n, _ in group) + len(node.text) <= 800)
        if compatible:
            group.append(entry)
        else:
            groups.append([entry])
    return groups
