"""Immutable views of successfully delivered, participant-scoped dialogues.

The existing runtime last_bot_node is the source of truth. This projection
adds no second mutable state to update on sends, cancellation, reset or prune.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .topic_identity import confirmed_topic_id
from .message_features import analyze_text
from .dialogue_continuity import evaluate


@dataclass(frozen=True)
class ActiveDialogue:
    user_id: str
    topic_id: str
    last_bot_message_id: str
    last_user_message_id: str
    last_bot_was_question: bool
    updated_at: float
    confidence: float
    bot_message_ids: tuple[str, ...] = ()
    question_text: str = ""
    interaction_state: str = "observing"
    closed: bool = False

    def accepts_answer(self, node: Any, recent_nodes: Sequence[Any]) -> bool:
        """A short answer may carry no semantic overlap with the question.

        Only the intended participant's first uninterrupted response qualifies.
        Explicit mentions/replies are handled before this rule by the resolver.

        The weights, the smooth time decay and the interruption factors live in
        dialogue_continuity so the learning layer can fit them; this method stays
        a boolean view of that score for callers that only need the decision.
        """
        return evaluate(self, node, recent_nodes).accepted


def active_dialogue(runtime: Any) -> ActiveDialogue | None:
    """Read the current delivery anchor and walk a bounded bot fragment chain."""
    return _project(runtime, getattr(runtime, "last_bot_node", None))


def _project(runtime: Any, bot: Any, eligible_ids=None) -> ActiveDialogue | None:
    dag = getattr(runtime, "dag", None)
    bot_id = str(getattr(runtime, "bot_id", "") or "")
    if dag is None or bot is None or not bot_id or str(bot.user_id) != bot_id:
        return None
    if dag.get_node(bot.msg_id) is not bot:
        return None
    cursor, seen, trigger = bot, set(), None
    fragments = [bot]
    for _ in range(80):
        mid = getattr(cursor, "reply_to_id", None)
        if not mid or mid in seen:
            break
        seen.add(mid)
        parent = dag.get_node(mid)
        if parent is None or (eligible_ids is not None and mid not in eligible_ids):
            break
        if str(parent.user_id) != bot_id:
            trigger = parent
            break
        cursor = parent
        fragments.append(parent)
    user_id = str(trigger.user_id) if trigger is not None else str(
        bot.metadata.get("trigger_user_id") or (
            getattr(runtime, "last_interlocutor", "") if bot is getattr(runtime, "last_bot_node", None) else "") or "")
    if not user_id or user_id == bot_id:
        return None
    revision = bot.metadata.get("dialogue_stop_revision")
    if revision is not None and revision != getattr(runtime, "topic_stop_revisions", {}).get(user_id, 0):
        return None
    question = next((n.text for n in fragments if analyze_text(n.text).question_ending), "")
    state = str(bot.metadata.get("dialogue_state") or "observing")
    closed = bot.metadata.get("dialogue_action") == "close" or state == "disengaging"
    return ActiveDialogue(user_id, confirmed_topic_id(bot), str(bot.msg_id),
                          str(trigger.msg_id) if trigger is not None else "",
                          bool(question) and not closed, float(bot.timestamp), 1.0 if trigger is not None else .76,
                          tuple(n.msg_id for n in reversed(fragments)), question, state, closed)


def dialogue_for_node(runtime: Any, node: Any, *, topic_id: str | None = None,
                      eligible_ids=None, window: float = 3600.0) -> ActiveDialogue | None:
    """Find the newest delivered exchange for this participant and known topic.

    Unknown topics use only the participant's newest exchange. This supplies
    context, never proof that the new message addresses the bot.
    """
    dag = getattr(runtime, "dag", None)
    last = getattr(runtime, "last_bot_node", None)
    if dag is None or last is None or node is None:
        return None
    current_topic = confirmed_topic_id(node) if topic_id is None else topic_id
    parent = str(getattr(node, "reply_to_id", None) or node.metadata.get("routing", {}).get("parent_message_id") or "")
    for bot in reversed(dag.get_recent_nodes(limit=0)):
        owner = bot.metadata.get("trigger_user_id")
        if owner and str(owner) != str(node.user_id):
            continue
        if (str(bot.user_id) != str(getattr(runtime, "bot_id", ""))
                or not 0 <= node.timestamp - bot.timestamp <= window
                or (eligible_ids is not None and bot.msg_id not in eligible_ids)
                or not (bot is last or bot.metadata.get("dialogue_delivered")
                        or bot.msg_id in getattr(runtime, "sent_id_set", set()))):
            continue
        dialogue = _project(runtime, bot, eligible_ids)
        if dialogue is None or dialogue.user_id != str(node.user_id):
            continue
        if current_topic and dialogue.topic_id != current_topic and parent not in dialogue.bot_message_ids:
            continue
        return dialogue
    return None
