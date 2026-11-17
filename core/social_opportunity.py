"""Scoped interaction evidence for optional, grounded social invitations.

This only admits an observation for the life planner; it never authorizes a
reply, changes its recipient, or supplies a fictional member request.
"""
from __future__ import annotations

import re
from . import member_boundary
from .private_original_input import private_message_recipient

_WITHDRAWAL = re.compile(
    r"别(?:再)?(?:主动)?(?:烦|打扰|找我|联系|回复)|不要(?:再)?(?:主动)?(?:打扰|找我|联系|回复)|"
    r"不想(?:聊|说)|不用(?:回|回复)|停止(?:回复|联系)|先别说|让我静静|我要睡|我去睡|在忙|晚安|别回|"
    r"别(?:再)?(?:聊|说)(?:这|了)|不要(?:再)?(?:聊|说)|不聊了|"
    r"\b(?:leave me alone|do not contact|don't contact|stop replying)\b", re.I
)
_PRIVATE_OR_CONFLICT = re.compile(
    r"私(?:下(?:再)?(?:说|聊)|事|密|聊)|保密|这是秘密|别告诉别人|只跟你说|不方便在群|"
    r"吵架|别吵|闭嘴|\b(?:in private|keep this secret|confidential)\b", re.I
)


def public_social_candidate(turn, message) -> bool:
    """Recognize an open, self-authored group observation without a new LLM."""
    parts = str(turn.session_key).rsplit(":", 2)
    if len(parts) != 3 or parts[1].lower() != "groupmessage":
        return False
    return _social_candidate(turn, message)


def private_social_candidate(turn, message) -> bool:
    """Keep single-recipient observations separate from public interaction."""
    return (private_message_recipient(turn.session_key) == str(message.author or "")
            and _social_candidate(turn, message))


def _social_candidate(turn, message) -> bool:
    uid, bot = str(message.author or ""), str(turn.bot_id or "")
    if not uid or uid in {"bot", "system", bot} or uid != str(turn.author):
        return False
    if turn.peer_continuation is not None:
        return False
    semantics = message.semantics
    if semantics is None or semantics.sender_is_bot:
        return False
    # Require the member's own words, without quote or mention ambiguity.
    if (message.reply_to or message.mentioned_users or semantics.mentioned_user_ids
            or semantics.quoted_message_id or semantics.quoted_author_id
            or semantics.recipient_ids):
        return False
    if set(semantics.scenes) & {"private_topic", "support"} or "tense" in semantics.emotions:
        return False
    text = (message.source_text if message.source_text is not None else message.text).strip()
    if not 4 <= len(text) <= 600 or _WITHDRAWAL.search(text) or _PRIVATE_OR_CONFLICT.search(text):
        return False
    if text.startswith(("/", "!", "！", "[引用消息")):
        return False
    # Media-only and acknowledgement-only messages are not a reason to invite
    # a member into a new topic. Ordinary jokes still qualify as observations.
    if not re.sub(r"\[[^\]]*\]|[\W_]", "", text):
        return False
    features = getattr(semantics, "features", None)
    return not (features and (features.is_filler or features.is_ack or features.is_elliptical))


def decision_allows_social_observation(evidence) -> bool:
    """Reuse the already completed decision's privacy/recipient assessment."""
    if not isinstance(evidence, dict) or evidence.get('social_context_current') is not True:
        return False
    reason = evidence.get("reason", {})
    recipient = evidence.get("recipient", {})
    if (not isinstance(reason, dict) or reason.get('type') != 'choice'
            or reason.get('choice') not in {
                'addressed_request', 'addressed_question', 'ongoing_thread', 'open_group_topic',
                'social_signal', 'other_recipient', 'boundary_or_sensitive', 'low_value_chatter'
            }
            or not isinstance(recipient, dict) or recipient.get('type') != 'choice'
            or recipient.get('choice') not in {'bot', 'group', 'other', 'unclear'}):
        return False
    if reason.get("choice") in {
        "boundary_or_sensitive", "other_recipient", "unfinished_message", "wait_for_completion"
    }:
        return False
    if recipient.get("choice") == "other":
        return False
    return True


def social_boundary_allows(plugin, event, scope, uid) -> bool:
    policy, _, state = member_boundary.peek(plugin, event, uid, scope=scope)
    if not state.available and (member_boundary.fail_closed(state) or state.service is not None):
        return False
    return not member_boundary.blocked(policy, purpose="send")
