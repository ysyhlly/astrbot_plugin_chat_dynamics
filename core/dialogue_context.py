"""Source-grounded dialogue excerpts captured with the turn's history watermark."""
from dataclasses import asdict, dataclass
import re

from .active_dialogue import dialogue_for_node
from .context_retrieval import clip_text
from .topic_identity import node_topic_id, confirmed_topic_id
from .message_features import analyze_text


@dataclass(frozen=True)
class DialogueSnapshot:
    user_id: str
    topic_id: str
    last_bot_message_id: str
    last_user_message_id: str
    age_seconds: float
    interaction_state: str
    closed: bool
    question_pending: bool
    last_bot_text: str
    pending_question: str
    # Raw attributed excerpts, not inferred facts or an invented task summary.
    user_updates: tuple[tuple[str, str], ...]
    source_message_ids: tuple[str, ...]
    continuation_kind: str = ""
    next_step: str = ""

    def payload(self) -> dict:
        return asdict(self)


def capture_dialogue(runtime, node, eligible_ids, *, current_ids=()) -> DialogueSnapshot | None:
    """Capture an owned exchange and bounded evidence for semantic continuation.

    Args:
        runtime: Session containing delivered message anchors.
        node: Current incoming message.
        eligible_ids: Messages visible at the original turn watermark.
        current_ids: Current fragments, excluded from prior user updates.

    Returns:
        Attributed prior dialogue, or None without a valid delivery anchor.
    """
    dialogue = dialogue_for_node(runtime, node, eligible_ids=eligible_ids)
    if dialogue is None:
        return None
    dag = runtime.dag
    bots = [dag.get_node(mid) for mid in dialogue.bot_message_ids]
    bots = [bot for bot in bots if bot is not None and bot.msg_id in eligible_ids]
    excluded = set(current_ids) | {node.msg_id}
    updates = [n for n in dag.get_recent_nodes(limit=0)
               if n.msg_id in eligible_ids and n.msg_id not in excluded
               and n.user_id == dialogue.user_id
               and 0 <= node.timestamp - n.timestamp <= 3600
               and (n.msg_id == dialogue.last_user_message_id
                    or bool(dialogue.topic_id and node_topic_id(n) == dialogue.topic_id))][-4:]
    answered = any(n.timestamp > dialogue.updated_at and (
        n.reply_to_id in dialogue.bot_message_ids
        or n.metadata.get("routing", {}).get("parent_message_id") in dialogue.bot_message_ids
    ) for n in updates)
    pending = dialogue.last_bot_was_question and not answered
    # This is a candidate for the existing semantic judge, not permission to
    # respond to every message by a familiar member. Keep actual attribution.
    routing = node.metadata.get("routing", {})
    bot_id = str(runtime.bot_id or "")
    mentions = set(node.metadata.get("actual_mentions", node.mentioned_users) or ())
    quoted = dag.get_node(node.reply_to_id) if node.reply_to_id else None
    recipients = set(routing.get("addressee_ids") or ())
    other_target = bool(
        mentions - {bot_id}
        or (quoted is not None and str(quoted.user_id) not in {bot_id, str(node.user_id)})
        or (routing.get("subject_is_bot") and not routing.get("bot_is_addressee"))
        or (
            recipients
            and bot_id not in recipients
            and routing.get("addressee_confidence", 0) >= 0.72
            and "inferred_reply" not in (routing.get("evidence") or ())
        )
    )
    features = analyze_text(node.text)
    age = max(0.0, node.timestamp - dialogue.updated_at)
    same_topic = bool(
        dialogue.topic_id
        and confirmed_topic_id(node) == dialogue.topic_id
        and routing.get("topic_confidence", 0) >= 0.8
    )
    parent = routing.get("parent_message_id", "")
    quoted_bot = bool(quoted is not None and quoted.msg_id in dialogue.bot_message_ids)
    linked_bot = parent in dialogue.bot_message_ids and routing.get("parent_confidence", 0) >= 0.72
    ending = bool(
        re.fullmatch(
            r"(?:先)?(?:不聊了|别回了|不用回了|不用回复了|到此为止|到这里吧|这样吧|晚安|再见)[。！!～~\s]*",
            node.text.strip(),
        )
    )
    window = 900 if dialogue.interaction_state == "focused" else 600
    candidate = ""
    if not other_target and not ending and 0 < age <= window:
        if quoted_bot:
            candidate = "quoted_reply"
        elif linked_bot and not features.answer_boundary:
            candidate = "linked_reply"
        elif same_topic and not features.answer_boundary:
            candidate = "same_topic"
        elif not confirmed_topic_id(node) and not features.answer_boundary:
            # Interleaved chatter from other members does not end this owned
            # exchange. The model must still disambiguate whom the answer is for.
            if pending:
                candidate = "pending_answer"
            elif features.dialogue_followup and not features.is_filler and not features.is_ack:
                candidate = "followup"
        if dialogue.closed and (features.is_filler or features.is_ack):
            candidate = ""
    next_step = (
        (
            "evaluate_reopening"
            if dialogue.closed
            else "respond_to_user_answer"
            if answered
            else "check_current_answer"
            if pending
            else "continue_topic"
        )
        if candidate
        else ""
    )
    ids = tuple(dict.fromkeys((*[b.msg_id for b in bots], *[n.msg_id for n in updates])))
    return DialogueSnapshot(
        dialogue.user_id,
        dialogue.topic_id,
        dialogue.last_bot_message_id,
        dialogue.last_user_message_id,
        max(0.0, node.timestamp - dialogue.updated_at),
        dialogue.interaction_state,
        dialogue.closed,
        pending,
        clip_text("\n".join(b.text for b in bots), 800),
        clip_text(dialogue.question_text, 240) if pending else "",
        tuple((n.msg_id, clip_text(n.text, 600)) for n in updates),
        ids,
        candidate,
        next_step,
    )
