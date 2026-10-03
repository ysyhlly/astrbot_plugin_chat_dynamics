"""Source-grounded dialogue excerpts captured with the turn's history watermark."""
from dataclasses import asdict, dataclass

from .active_dialogue import dialogue_for_node
from .context_retrieval import clip_text
from .topic_identity import node_topic_id


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

    def payload(self) -> dict:
        return asdict(self)


def capture_dialogue(runtime, node, eligible_ids, *, current_ids=()) -> DialogueSnapshot | None:
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
    ids = tuple(dict.fromkeys((*[b.msg_id for b in bots], *[n.msg_id for n in updates])))
    return DialogueSnapshot(dialogue.user_id, dialogue.topic_id, dialogue.last_bot_message_id,
                            dialogue.last_user_message_id, max(0.0, node.timestamp - dialogue.updated_at),
                            dialogue.interaction_state, dialogue.closed, pending,
                            clip_text("\n".join(b.text for b in bots), 800),
                            clip_text(dialogue.question_text, 240) if pending else "",
                            tuple((n.msg_id, clip_text(n.text, 600)) for n in updates), ids)
