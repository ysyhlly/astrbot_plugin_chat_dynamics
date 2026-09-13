"""Immutable view of the last successfully delivered bot dialogue.

The existing runtime last_bot_node is the source of truth. This projection
adds no second mutable state to update on sends, cancellation, reset or prune.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .topic_identity import node_topic_id
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
    dag = getattr(runtime, "dag", None)
    bot = getattr(runtime, "last_bot_node", None)
    bot_id = str(getattr(runtime, "bot_id", "") or "")
    if dag is None or bot is None or not bot_id or str(bot.user_id) != bot_id:
        return None
    if dag.get_node(bot.msg_id) is not bot:
        return None
    cursor, seen, trigger = bot, set(), None
    for _ in range(80):
        mid = getattr(cursor, "reply_to_id", None)
        if not mid or mid in seen:
            break
        seen.add(mid)
        parent = dag.get_node(mid)
        if parent is None:
            break
        if str(parent.user_id) != bot_id:
            trigger = parent
            break
        cursor = parent
    user_id = str(trigger.user_id) if trigger is not None else str(
        bot.metadata.get("trigger_user_id") or getattr(runtime, "last_interlocutor", "") or "")
    if not user_id or user_id == bot_id:
        return None
    question = analyze_text(bot.text).question_ending
    return ActiveDialogue(user_id, node_topic_id(bot), str(bot.msg_id),
                          str(trigger.msg_id) if trigger is not None else "",
                          question, float(bot.timestamp), 1.0 if trigger is not None else .76)
